"""Recruitment workflow bridge from legacy JSON flow to SQLite.

This module deliberately keeps BOSS page automation untouched. It orchestrates
existing application services, reads their JSON artifacts, and persists the
business records into SQLite with audit logs.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from boss_hr.config_manager import ConfigManager, get_config
from shared.analysis_artifact_lock import RunArtifactLock
from shared.analysis_result import (analysis_record, analyze_with_retries, error_detail,
                                    is_completed_analysis, is_passed_analysis)
from database.connection import Database
from database.repositories import UnitOfWork
from logger.log_manager import (
    get_audit_logger,
    get_automation_logger,
    get_system_logger,
    log_exception,
)
from logger.risk_control import RiskControl, RiskStop


def _now_str() -> str:
    """阶段计时专用时间字符串（与 run_orchestrator._now_str 格式一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class WorkflowResult:
    """Structured workflow result for callers and tests."""

    ok: bool
    stage: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class RecruitWorkflow:
    """Coordinate existing BOSS automation and SQLite persistence."""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        database: Database | None = None,
        analyzer: Any | None = None,
    ) -> None:
        self.config = config or get_config()
        self.database = database or Database(self.config)
        self.database.init()
        # 允许测试或私有部署注入兼容的分析器，生产环境默认使用外置 AI 配置。
        if analyzer is None:
            from ai.resume_analyzer import ResumeAnalyzer
            analyzer = ResumeAnalyzer(config=self.config)
        self.analyzer = analyzer
        self.system_logger = get_system_logger(self.config)
        self.automation_logger = get_automation_logger(self.config)
        self.audit_logger = get_audit_logger(self.config)
        self.risk_control = RiskControl(config=self.config, database=self.database)

    def _record_stage_timing(
        self,
        *,
        job_name: str,
        encrypt_job_id: str,
        run_id: str,
        stage: str,
        started_at: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """把阶段耗时写到 run.json 的 stage_timings 字段。

        写失败不影响主流程，只记 audit log。
        """
        try:
            from shared.run_orchestrator import RunOrchestrator
            orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
            orch.record_stage_timing(
                run_id=run_id,
                stage=stage,
                started_at=started_at,
                finished_at=_now_str(),
                extra=extra,
            )
        except Exception as exc:
            self._audit(
                "workflow.stage_timing.failed",
                {"stage": stage, "run_id": run_id, "error": str(exc)[:200]},
                "warning",
            )

    def run_end_to_end(
        self,
        *,
        job_name: str,
        encrypt_job_id: str,
        run_id: str,
        count: int,
        max_greetings: int | None = None,
        dry_run: bool = False,
        sync_feishu: bool = True,
        skip_fetch: bool = False,
        job_requirement: dict[str, Any] | str | None = None,
        feishu_client: Any | None = None,
        refresh_page: bool = False,
    ) -> WorkflowResult:
        """执行抓取、AI评估、招呼、SQLite 落库和飞书同步的完整链路。

        ``run_id`` 必须先由 ``start`` 创建并由用户执行 ``confirm`` 确认。
        这项限制保留人工筛选条件确认门，避免脚本未经确认直接操作 BOSS。
        """
        if max_greetings is None:
            max_greetings = int(self.config.get("recruitment.daily_send_limit", 10) or 10)

        try:
            allowed_count = self.risk_control.ensure_candidate_limit(count)
            # 新流程在简历弹层仍打开时就完成招呼，因此必须在 fetch 子进程启动前
            # 计算本轮真实可发送额度，不能等批量分析完成后再做风控。
            transactional_greet_limit = max_greetings
            if not skip_fetch and not dry_run:
                transactional_greet_limit = self.risk_control.ensure_can_send(max_greetings)
            if skip_fetch:
                fetched = self.load_existing_fetch_artifacts(
                    job_name=job_name,
                    encrypt_job_id=encrypt_job_id,
                    run_id=run_id,
                    count=allowed_count,
                )
            else:
                fetched = self.fetch_and_save_candidates(
                    job_name=job_name,
                    encrypt_job_id=encrypt_job_id,
                    run_id=run_id,
                    count=allowed_count,
                    transactional=True,
                    max_greetings=transactional_greet_limit,
                    dry_run=dry_run,
                    job_requirement=job_requirement,
                )
            if not fetched.ok:
                return fetched

            fetch_data = (fetched.data.get("fetch") or {}).get("data") or {}
            if fetch_data.get("transactional_flow"):
                screening_file = fetch_data.get("screening_results_file")
                screening = self._read_json_object(screening_file)
                summary = screening.get("summary") or {}
                saved_analysis = self.persist_analysis_from_screening_results(
                    job_name=job_name,
                    screening_results_file=screening_file,
                    model=self.config.get("ai.model", ""),
                    run_id=run_id,
                )
                analyzed = WorkflowResult(
                    True, "analysis", "analysis_complete",
                    {
                        "screening_results_file": screening_file,
                        "analyzed": int(summary.get("total", 0) or 0),
                        "saved_analysis": saved_analysis,
                        "failed": screening.get("failures") or [],
                        "transactional_flow": True,
                    },
                )
            else:
                analyzed = self.analyze_fetched_candidates(
                    job_name=job_name,
                    run_id=run_id,
                    fetch_data=fetch_data,
                    job_requirement=job_requirement,
                    encrypt_job_id=encrypt_job_id,
                )
            if not analyzed.ok:
                if analyzed.status == "no_resumes":
                    feishu_stage_started_at = _now_str()
                    sync_stats = self._sync_current_run_to_feishu(
                        job_name=job_name,
                        run_id=run_id,
                        sync_feishu=sync_feishu,
                        dry_run=dry_run,
                        feishu_client=feishu_client,
                    )
                    self._record_stage_timing(
                        job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                        stage="feishu", started_at=feishu_stage_started_at,
                        extra={
                            "created": sync_stats.get("created", 0),
                            "updated": sync_stats.get("updated", 0),
                            "failed": sync_stats.get("failed", 0),
                            "total": sync_stats.get("total", 0),
                            "ok": sync_stats.get("failed", 0) == 0,
                        },
                    )
                    if sync_stats.get("failed", 0):
                        data = {"fetch": fetched.data, "analysis": analyzed.data, "feishu": sync_stats}
                        self._audit("workflow.end_to_end.feishu_partial_failed", data, "warning")
                        # 飞书同步部分失败不阻断流程
                    payload = {
                        "fetch": fetched.data,
                        "analysis": analyzed.data,
                        "greet": {"skipped": True, "reason": analyzed.error},
                        "feishu": sync_stats,
                    }
                    self._audit(
                        "workflow.end_to_end.no_resumes", payload,
                        "warning" if sync_stats.get("failed", 0) else "success",
                    )
                    return WorkflowResult(
                        ok=True,
                        stage="complete",
                        status="no_resumes_synced" if not dry_run else "dry_run_no_resumes",
                        data=payload,
                    )
                return analyzed

            if fetch_data.get("transactional_flow"):
                greet_log_file = fetch_data.get("greet_log_file")
                greet_log = self._read_json_object(greet_log_file)
                greet_summary = greet_log.get("summary") or {}
                saved_greetings = self.persist_greeting_records_from_log(
                    job_name=job_name,
                    greet_log_file=greet_log_file,
                    run_id=run_id,
                )
                failed_greetings = int(greet_summary.get("failed", 0) or 0)
                greeted = WorkflowResult(
                    True,
                    "greet",
                    "greet_complete" if failed_greetings == 0 else "greet_partial_failed",
                    {
                        "greet": {
                            "status": greet_log.get("status", "complete"),
                            "data": {
                                **greet_summary,
                                "greet_log_file": greet_log_file,
                                "transactional_flow": True,
                            },
                        },
                        "saved_greeting_records": saved_greetings,
                    },
                    None if failed_greetings == 0 else f"{failed_greetings} 位候选人固定话术发送失败",
                )
            else:
                greeted = self.send_greetings_and_save_records(
                    job_name=job_name,
                    encrypt_job_id=encrypt_job_id,
                    run_id=run_id,
                    max_count=max_greetings,
                    dry_run=dry_run,
                    refresh_page=refresh_page,
                )
            if not greeted.ok:
                return greeted

            sync_stats: dict[str, int] = {
                "created": 0, "updated": 0, "failed": 0, "total": 0,
            }
            feishu_stage_started_at = _now_str()
            sync_stats = self._sync_current_run_to_feishu(
                job_name=job_name,
                run_id=run_id,
                sync_feishu=sync_feishu,
                dry_run=dry_run,
                feishu_client=feishu_client,
            )
            self._record_stage_timing(
                job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                stage="feishu", started_at=feishu_stage_started_at,
                extra={
                    "created": sync_stats.get("created", 0),
                    "updated": sync_stats.get("updated", 0),
                    "failed": sync_stats.get("failed", 0),
                    "total": sync_stats.get("total", 0),
                },
            )
            if sync_stats.get("failed", 0):
                data = {"feishu": sync_stats}
                self._audit("workflow.end_to_end.feishu_partial_failed", data, "warning")
                # 飞书同步部分失败不阻断流程：招呼已成功，飞书失败只是部分记录未同步
                # 下一轮 RE-RUN 时会重新同步这些候选人

            payload = {
                "fetch": fetched.data,
                "analysis": analyzed.data,
                "greet": greeted.data,
                "feishu": sync_stats,
            }
            incomplete = bool(fetch_data.get("transaction_incomplete"))
            self._audit(
                "workflow.end_to_end.finish", payload,
                "warning" if incomplete or sync_stats.get("failed", 0) else "success",
            )
            return WorkflowResult(
                ok=True,
                stage="complete",
                status=(
                    "automation_partial_incomplete" if incomplete
                    else "automation_complete" if not dry_run
                    else "dry_run_complete"
                ),
                data=payload,
            )
        except RiskStop as exc:
            data = {"code": exc.code, "detail": exc.detail}
            self._audit("workflow.end_to_end.risk_stop", data, "stopped")
            return WorkflowResult(False, "risk_control", exc.code, data, str(exc))
        except Exception as exc:
            log_exception(
                "workflow.end_to_end", exc, self.config,
                detail={"job_name": job_name, "run_id": run_id},
            )
            self._audit(
                "workflow.end_to_end.exception",
                {"job_name": job_name, "run_id": run_id, "error": str(exc)[:500]},
                "failed",
            )
            return WorkflowResult(False, "internal", "error", {}, str(exc))

    def _sync_current_run_to_feishu(
        self,
        *,
        job_name: str,
        run_id: str,
        sync_feishu: bool,
        dry_run: bool,
        feishu_client: Any | None = None,
    ) -> dict[str, int]:
        """Sync every SQLite candidate attached to the current run."""
        sync_stats: dict[str, int] = {
            "created": 0, "updated": 0, "failed": 0, "total": 0,
        }
        # dry-run 没有真实发送记录，不把演练数据同步到业务表。
        if not sync_feishu or dry_run:
            return sync_stats
        from feishu.sync_service import FeishuSyncService

        sync_service = FeishuSyncService(
            config=self.config,
            database=self.database,
            client=feishu_client,
        )
        try:
            return sync_service.sync_run_candidates(job=job_name, run_id=run_id)
        except Exception as exc:
            # BOSS actions and SQLite writes have already completed. Preserve
            # their outcome so the daemon does not retry the entire job merely
            # because the optional Feishu mirror is unavailable.
            log_exception(
                "workflow.feishu_sync", exc, self.config,
                detail={"job_name": job_name, "run_id": run_id},
            )
            return {**sync_stats, "failed": 1, "error": str(exc)[:500]}

    # ------------------------------------------------------------------
    # Full/step orchestration
    # ------------------------------------------------------------------
    def fetch_and_save_candidates(
        self,
        *,
        job_name: str,
        encrypt_job_id: str,
        run_id: str,
        count: int,
        transactional: bool = False,
        max_greetings: int = 10,
        dry_run: bool = False,
        job_requirement: dict[str, Any] | str | None = None,
    ) -> WorkflowResult:
        """Run existing BOSS fetch flow and persist candidate records."""
        from boss_hr.application.fetch_service import fetch_candidates

        stage_started_at = _now_str()
        self._audit(
            "workflow.fetch_candidates.start",
            {"job_name": job_name, "encrypt_job_id": encrypt_job_id, "run_id": run_id, "count": count},
            "started",
        )
        self.automation_logger.info(
            "fetch_candidates_start | job=%s | run_id=%s | count=%s",
            job_name,
            run_id,
            count,
        )
        result = fetch_candidates(
            job_name=job_name,
            encrypt_job_id=encrypt_job_id,
            run_id=run_id,
            count=count,
            transactional=transactional,
            max_greetings=max_greetings,
            dry_run=dry_run,
            job_requirement=job_requirement,
        )
        if not result.ok:
            self._audit("workflow.fetch_candidates.finish", result.to_dict("fetch"), "failed")
            self.automation_logger.error("fetch_candidates_failed | result=%s", result.to_dict("fetch"))
            self._record_stage_timing(
                job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                stage="fetch", started_at=stage_started_at,
                extra={"ok": False, "status": result.status or "error"},
            )
            return WorkflowResult(
                ok=False,
                stage="fetch",
                status=result.status or "error",
                data=result.to_dict("fetch"),
                error=result.error.message if result.error else "fetch failed",
            )

        saved = self.persist_candidates_from_fetch_result(
            job_name=job_name,
            run_id=run_id,
            fetch_data=result.data,
        )
        payload = {"fetch": result.to_dict("fetch"), "saved_candidates": saved}
        self._audit("workflow.fetch_candidates.finish", payload, "success")
        self.automation_logger.info(
            "fetch_candidates_finish | scanned=%s | downloaded=%s | failed=%s | saved=%s",
            result.data.get("listed_count", 0),
            result.data.get("downloaded_count", 0),
            result.data.get("failed_count", 0),
            saved,
        )
        self._record_stage_timing(
            job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
            stage="fetch", started_at=stage_started_at,
            extra={
                "listed": result.data.get("listed_count", 0),
                "downloaded": result.data.get("downloaded_count", 0),
                "failed": result.data.get("failed_count", 0),
                "saved_candidates": saved,
            },
        )
        return WorkflowResult(ok=True, stage="fetch", status=result.status, data=payload)

    def load_existing_fetch_artifacts(
        self,
        *,
        job_name: str,
        encrypt_job_id: str,
        run_id: str,
        count: int,
    ) -> WorkflowResult:
        """复用当前 run 已有抓取产物，避免排障时重复滚动 BOSS 卡片。"""
        from output_manager import JobOutputManager

        out = JobOutputManager(job_name, encrypt_job_id=encrypt_job_id, run_id=run_id, lazy=True)
        process_dir = Path(out.process_dir)
        data = {
            "requested_count": count,
            "candidate_list_file": str(process_dir / "recommend_geek_ids.json"),
            "card_prescreen_rejected_file": str(process_dir / "card_prescreen_rejected.json"),
            "card_prescreen_summary_file": str(process_dir / "card_prescreen_summary.json"),
            "new_resumes_file": str(process_dir / "new_resumes.json"),
            "failed_resumes_file": str(process_dir / "failed_resumes.json"),
        }
        candidates = self._read_json_array(data["candidate_list_file"])
        resumes = self._read_json_array(data["new_resumes_file"])
        failed = self._read_json_array(data["failed_resumes_file"])
        data.update({
            "listed_count": len(candidates),
            "downloaded_count": sum(1 for item in resumes if item.get("ok", True)),
            "failed_count": len(failed),
        })
        saved = self.persist_candidates_from_fetch_result(
            job_name=job_name,
            run_id=run_id,
            fetch_data=data,
        )
        data["saved_candidates"] = saved
        self._audit("workflow.fetch_candidates.reuse", data, "success")
        return WorkflowResult(ok=True, stage="fetch", status="fetch_reused", data=data)

    def save_analysis_from_screening(
        self,
        *,
        job_name: str,
        screening_results_file: str | Path,
        model: str | None = None,
        run_id: str = "",
    ) -> WorkflowResult:
        """Persist AI analysis records from screening_results.json."""
        count = self.persist_analysis_from_screening_results(
            job_name=job_name,
            screening_results_file=screening_results_file,
            model=model or self.config.get("ai.model", ""),
            run_id=run_id,
        )
        data = {"screening_results_file": str(screening_results_file), "saved_analysis": count}
        self._audit("workflow.save_analysis.finish", data, "success")
        self.automation_logger.info(
            "save_analysis_finish | job=%s | saved_analysis=%s | file=%s",
            job_name,
            count,
            screening_results_file,
        )
        return WorkflowResult(ok=True, stage="analysis", status="analysis_saved", data=data)

    def analyze_fetched_candidates(
        self,
        *,
        job_name: str,
        run_id: str,
        fetch_data: dict[str, Any],
        job_requirement: dict[str, Any] | str | None = None,
        encrypt_job_id: str = "",
    ) -> WorkflowResult:
        """Serialize all read/modify/write work for a single run."""
        resumes_file = fetch_data.get("new_resumes_file")
        if not resumes_file:
            return self._analyze_fetched_candidates_unlocked(
                job_name=job_name, run_id=run_id, fetch_data=fetch_data,
                job_requirement=job_requirement, encrypt_job_id=encrypt_job_id)
        with RunArtifactLock(Path(str(resumes_file)).resolve().parent):
            return self._analyze_fetched_candidates_unlocked(
                job_name=job_name, run_id=run_id, fetch_data=fetch_data,
                job_requirement=job_requirement, encrypt_job_id=encrypt_job_id)

    def _analyze_fetched_candidates_unlocked(
        self,
        *,
        job_name: str,
        run_id: str,
        fetch_data: dict[str, Any],
        job_requirement: dict[str, Any] | str | None = None,
        encrypt_job_id: str = "",
    ) -> WorkflowResult:
        """调用 AI 对照岗位硬性要求逐条判断简历，并生成招呼脚本兼容的筛选文件。"""
        stage_started_at = _now_str()
        resumes_file = fetch_data.get("new_resumes_file")
        resumes = self._read_json_array(resumes_file)
        if not resumes:
            # 没有完整简历时，把上游产物路径带回去，方便判断是卡片预筛拦截还是下载失败。
            debug_data = {
                "candidate_list_file": fetch_data.get("candidate_list_file"),
                "card_prescreen_rejected_file": fetch_data.get("card_prescreen_rejected_file"),
                "card_prescreen_summary_file": fetch_data.get("card_prescreen_summary_file"),
                "new_resumes_file": fetch_data.get("new_resumes_file"),
                "failed_resumes_file": fetch_data.get("failed_resumes_file"),
                "listed_count": fetch_data.get("listed_count"),
                "downloaded_count": fetch_data.get("downloaded_count"),
                "failed_count": fetch_data.get("failed_count"),
            }
            return WorkflowResult(
                False, "analysis", "no_resumes", debug_data, "本次抓取没有可供 AI 分析的简历",
            )

        if job_requirement is None:
            job_requirement = self._configured_job_requirement(job_name)

        output_path = Path(str(resumes_file)).parent / "screening_results.json"
        existing = self._read_json_object(output_path)
        if existing.get("job") and existing["job"] != job_name:
            raise ValueError("筛选结果岗位与本轮岗位不一致，禁止覆盖")
        if existing.get("run_id") and existing["run_id"] != run_id:
            raise ValueError("筛选结果 run_id 与本轮不一致，禁止覆盖")
        candidates = [item for item in existing.get("candidates", []) if is_completed_analysis(item)]
        completed_ids = {str(item.get("encrypt_geek_id") or item.get("geek_id") or "")
                         for item in candidates}
        failures: list[dict[str, Any]] = [
            item for item in existing.get("failures", [])
            if str(item.get("encrypt_geek_id") or item.get("geek_id") or "") not in completed_ids
        ]

        # 竞品公司直接打招呼：如果岗位配置了 competitor_companies，且简历的
        # work_experience 中有公司命中竞品名单，则直接 passed=True 跳过 AI 分析。
        # 适用于运营、高级剪拍等"看公司不看其他条件"的岗位。
        competitor_list: list[str] = []
        jr_dict = job_requirement if isinstance(job_requirement, dict) else {}
        competitor_list = (jr_dict.get("card_prescreen") or {}).get("competitor_companies") or []
        # 这两个岗位要求核验同一段经历的岗位、时长及其他完整简历条件，
        # 公司命中不能替代 AI 对全部硬性条件的分析。编导同理：竞品名单只用于
        # 豁免数据成果，不能凭公司命中直接放行。
        if job_name in ("运营", "高级剪拍", "编导") or jr_dict.get("job_name") in ("运营", "高级剪拍", "编导"):
            competitor_list = []

        # 预处理：过滤有效简历，按原始顺序保留索引
        pending: list[tuple[int, str, str, dict[str, Any]]] = []
        for idx, resume in enumerate(resumes):
            if not isinstance(resume, dict) or not resume.get("ok", True):
                continue
            meta = resume.get("_meta") or {}
            geek_id = str(meta.get("encrypt_geek_id") or resume.get("encrypt_geek_id") or "")
            name = str(resume.get("name") or "")
            if geek_id and geek_id not in completed_ids:
                pending.append((idx, geek_id, name, resume))
                completed_ids.add(geek_id)
        resume_by_idx = {item[0]: item[3] for item in pending}

        # 竞品命中直接通过的结果（不进 AI 分析队列，节省 API 调用）
        competitor_results: dict[int, tuple[str, str, dict[str, Any] | None, Exception | None]] = {}
        ai_pending: list[tuple[int, str, str, dict[str, Any]]] = []
        if competitor_list:
            for item in pending:
                idx, gid, nm, res = item
                companies = self._extract_resume_companies(res)
                matched = self._match_competitor(companies, competitor_list)
                if matched:
                    competitor_results[idx] = (gid, nm, {
                        "passed": True,
                        "reason": f"竞品公司命中（{matched}），直接打招呼",
                    }, None)
                else:
                    ai_pending.append(item)
        else:
            ai_pending = list(pending)

        def _analyze_one(item: tuple[int, str, str, dict[str, Any]]) -> tuple[int, str, str, dict[str, Any] | None, Exception | None]:
            idx, gid, nm, res = item
            try:
                result, error, _attempts = analyze_with_retries(
                    self.analyzer, job_requirement, res,
                    attempts=self.config.get("performance.ai.total_attempts", 3),
                    retry_delay=self.config.get("performance.ai.retry_delay_seconds", 2.0),
                    retry_max_delay=self.config.get("performance.ai.retry_max_delay_seconds", 8.0),
                )
                return idx, gid, nm, result, error
            except Exception as exc:
                return idx, gid, nm, None, exc

        # 并发分析：DeepSeek API 是 IO 密集型，用线程池大幅缩短总耗时
        results_map: dict[int, tuple[str, str, dict[str, Any] | None, Exception | None]] = {}
        max_workers = min(8, len(ai_pending)) if len(ai_pending) > 1 else 1
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_analyze_one, item) for item in ai_pending]
                for fut in as_completed(futures):
                    idx, gid, nm, result, exc = fut.result()
                    results_map[idx] = (gid, nm, result, exc)
        else:
            for item in ai_pending:
                idx, gid, nm, result, exc = _analyze_one(item)
                results_map[idx] = (gid, nm, result, exc)
        # 合并竞品直接通过的结果
        results_map.update(competitor_results)

        # 按原始顺序组装结果
        for idx, gid, nm, _ in pending:
            gid2, nm2, analysis, exc = results_map.get(idx, (gid, nm, None, None))
            failures[:] = [f for f in failures if str(f.get("geek_id") or f.get("encrypt_geek_id") or "") != gid2]
            if exc is not None:
                failures.append({"geek_id": gid2, "name": nm2, **error_detail(exc),
                                 "stage": "deepseek_analysis", "terminal_status": "analysis_unavailable",
                                 "retry_mode": "saved_screenshot_only"})
                self._audit(
                    "candidate_analysis.failed",
                    {"geek_id": gid2, "name": nm2, "error": str(exc)[:300]},
                    "failed",
                )
                continue
            if analysis is None:
                continue
            resume = resume_by_idx.get(idx, {})
            record = analysis_record(analysis, resume, gid2, nm2)
            record["age"] = self._coerce_age(resume.get("age") or resume.get("age_desc"))
            record["degree"] = self._extract_degree(resume)
            candidates.append(record)

        if not candidates:
            self._write_json_atomic(output_path, {
                **existing, "job": job_name, "run_id": run_id, "candidates": [],
                "failures": failures,
                "summary": {"total": 0, "passed": 0, "rejected": 0, "failed": len(failures)},
            })
            self._record_stage_timing(
                job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                stage="analysis", started_at=stage_started_at,
                extra={"ok": False, "analyzed": 0, "failed_count": len(failures)},
            )
            return WorkflowResult(
                False, "analysis", "analysis_failed",
                {"failures": failures}, "没有任何简历成功完成 AI 评估",
            )

        # 通过硬性条件的候选人排前面；同组内保持抓取顺序。
        candidates.sort(key=lambda item: not item["passed"])
        for rank, item in enumerate(candidates, 1):
            item["rank"] = rank
        payload = {
            **existing,
            "job": job_name,
            "run_id": run_id,
            "failures": failures,
            "summary": {
                "total": len(candidates),
                "passed": sum(1 for c in candidates if c["passed"]),
                "rejected": sum(1 for c in candidates if not c["passed"]),
                "failed": len(failures),
            },
            "candidates": candidates,
        }
        output_path = Path(str(resumes_file)).parent / "screening_results.json"
        self._write_json_atomic(output_path, payload)
        saved = self.persist_analysis_from_screening_results(
            job_name=job_name,
            screening_results_file=output_path,
            model=self.config.get("ai.model", ""),
            run_id=run_id,
        )
        data = {
            "screening_results_file": str(output_path.resolve()),
            "analyzed": len(candidates),
            "saved_analysis": saved,
            "failed": failures,
        }
        self._audit("workflow.ai_analysis.finish", data, "success")
        self._record_stage_timing(
            job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
            stage="analysis", started_at=stage_started_at,
            extra={
                "analyzed": len(candidates),
                "passed": sum(1 for c in candidates if c["passed"]),
                "rejected": sum(1 for c in candidates if not c["passed"]),
                "failed_count": len(failures),
            },
        )
        return WorkflowResult(True, "analysis", "analysis_complete", data)

    def send_greetings_and_save_records(
        self,
        *,
        job_name: str,
        encrypt_job_id: str,
        run_id: str,
        max_count: int | None = None,
        only_names: str | None = None,
        dry_run: bool = False,
        refresh_page: bool = False,
    ) -> WorkflowResult:
        """Run existing greet flow and persist greeting records."""
        from boss_hr.application.greet_service import greet_candidates

        stage_started_at = _now_str()
        if max_count is None:
            max_count = int(self.config.get("recruitment.daily_send_limit", 10) or 10)

        if not dry_run:
            try:
                max_count = self.risk_control.ensure_can_send(max_count)
            except RiskStop as exc:
                data = {"code": exc.code, "detail": exc.detail}
                self._audit("workflow.greet.risk_stop", data, "stopped")
                self._record_stage_timing(
                    job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                    stage="greet", started_at=stage_started_at,
                    extra={"ok": False, "status": exc.code},
                )
                return WorkflowResult(
                    ok=False,
                    stage="greet",
                    status=exc.code,
                    data=data,
                    error=str(exc),
                )

        self._audit(
            "workflow.greet.start",
            {
                "job_name": job_name,
                "encrypt_job_id": encrypt_job_id,
                "run_id": run_id,
                "max_count": max_count,
                "dry_run": dry_run,
            },
            "started",
        )
        self.automation_logger.info(
            "greet_start | job=%s | run_id=%s | max_count=%s | dry_run=%s",
            job_name,
            run_id,
            max_count,
            dry_run,
        )
        try:
            if not dry_run:
                self.risk_control.wait_before_action("greet")
            result = greet_candidates(
                job_name=job_name,
                encrypt_job_id=encrypt_job_id,
                run_id=run_id,
                only_names=only_names,
                max_count=max_count,
                dry_run=dry_run,
                refresh_page=refresh_page,
            )
        except Exception as exc:
            log_exception("workflow.greet", exc, self.config, detail={"job_name": job_name, "run_id": run_id})
            self._audit(
                "workflow.greet.exception",
                {"job_name": job_name, "run_id": run_id, "error": str(exc)[:500]},
                "failed",
            )
            raise
        saved = 0
        if result.ok:
            greet_log_file = (result.data or {}).get("greet_log_file")
            if greet_log_file:
                saved = self.persist_greeting_records_from_log(
                    job_name=job_name,
                    greet_log_file=greet_log_file,
                    run_id=run_id,
                )
        payload = {"greet": result.to_dict("greet"), "saved_greeting_records": saved}
        self._audit(
            "workflow.greet.finish",
            payload,
            "success" if result.ok else "failed",
        )
        self.automation_logger.info(
            "greet_finish | sent=%s | failed=%s | saved_records=%s | status=%s",
            result.data.get("greeted", 0) if result.data else 0,
            result.data.get("not_found", 0) if result.data else 0,
            saved,
            result.status,
        )
        if not result.ok:
            self._record_stage_timing(
                job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
                stage="greet", started_at=stage_started_at,
                extra={
                    "ok": False,
                    "greeted": result.data.get("greeted", 0) if result.data else 0,
                    "not_found": result.data.get("not_found", 0) if result.data else 0,
                },
            )
            return WorkflowResult(
                ok=False,
                stage="greet",
                status=result.status or "error",
                data=payload,
                error=result.error.message if result.error else "greet failed",
            )
        self._record_stage_timing(
            job_name=job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
            stage="greet", started_at=stage_started_at,
            extra={
                "greeted": result.data.get("greeted", 0) if result.data else 0,
                "not_found": result.data.get("not_found", 0) if result.data else 0,
                "saved_greeting_records": saved,
                "dry_run": dry_run,
            },
        )
        return WorkflowResult(ok=True, stage="greet", status=result.status, data=payload)

    # ------------------------------------------------------------------
    # JSON artifact ingestion
    # ------------------------------------------------------------------
    def persist_candidates_from_fetch_result(
        self,
        *,
        job_name: str,
        run_id: str = "",
        fetch_data: dict[str, Any],
    ) -> int:
        """Persist candidates from fetch service output files."""
        candidate_list = self._read_json_array(fetch_data.get("candidate_list_file"))
        rejected_list = self._read_json_array(fetch_data.get("card_prescreen_rejected_file"))
        resumes = self._read_json_array(fetch_data.get("new_resumes_file"))
        saved = 0

        with UnitOfWork(self.database.session_factory) as uow:
            by_boss_id: dict[str, dict[str, Any]] = {}
            for item in candidate_list:
                record = self._candidate_from_recommend_item(item, job_name, run_id=run_id)
                if not record.get("boss_id"):
                    continue
                by_boss_id[record["boss_id"]] = record

            for item in rejected_list:
                record = self._candidate_from_rejected_item(item, job_name, run_id=run_id)
                if not record.get("boss_id"):
                    continue
                base = by_boss_id.get(record["boss_id"], {})
                by_boss_id[record["boss_id"]] = {**base, **record}

            for resume in resumes:
                record = self._candidate_from_resume(resume, job_name, run_id=run_id)
                if not record.get("boss_id"):
                    continue
                base = by_boss_id.get(record["boss_id"], {})
                merged = {**base, **record}
                by_boss_id[record["boss_id"]] = merged

            for record in by_boss_id.values():
                # 保护已招呼候选人：如果候选人已有 greeting_record（greeted/sent/success/dry_run），
                # 不覆盖 card_prescreen 字段，避免新一轮 fetch 因 bug 或条件变化错误覆盖之前通过的筛选状态
                existing = uow.candidates.get_by_boss_id(record.get("boss_id", ""), job_name)
                if existing is not None:
                    existing_greeting = uow.greetings.latest_for_candidate(existing.id)
                    if existing_greeting is not None and existing_greeting.status in (
                        "greeted", "sent", "success", "dry_run",
                    ):
                        for k in ("card_prescreen_status", "card_prescreen_passed",
                                  "card_prescreen_reason", "card_prescreen_detail"):
                            record.pop(k, None)
                candidate = uow.candidates.upsert(**record)
                uow.session.flush()
                saved += 1
                uow.audit_logs.add(
                    action="candidate.save",
                    detail={"candidate_id": candidate.id, "boss_id": candidate.boss_id, "job": candidate.job},
                    result="success",
                )
        return saved

    def persist_analysis_from_screening_results(
        self,
        *,
        job_name: str,
        screening_results_file: str | Path,
        model: str = "",
        run_id: str = "",
    ) -> int:
        """Persist candidate_analysis rows from screening_results.json."""
        data = self._read_json_object(screening_results_file)
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not isinstance(candidates, list):
            return 0

        saved = 0
        with UnitOfWork(self.database.session_factory) as uow:
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                if "passed" in item and not is_completed_analysis(item):
                    continue
                if "passed" not in item and "recommended" in item and type(item["recommended"]) is not bool:
                    continue
                if item.get("analysis_status") not in (None, "", "completed", "passed", "rejected"):
                    continue
                candidate = self._find_candidate_for_analysis(uow, item, job_name)
                if candidate is None:
                    continue
                passed = item.get("passed")
                if passed is None:
                    passed = item.get("recommended")
                # 如果候选人已被招呼过，不覆盖原 passed=True 的分析结果
                # 避免"显示未通过但已打招呼"的飞书数据矛盾
                if passed is False:
                    existing_greeting = uow.greetings.latest_for_candidate(candidate.id)
                    if existing_greeting is not None and existing_greeting.status in ("greeted", "sent", "success"):
                        # 已招呼的候选人重新筛选未通过，跳过覆盖
                        uow.audit_logs.add(
                            action="candidate_analysis.skip_overwrite",
                            detail={
                                "candidate_id": candidate.id,
                                "name": str(item.get("name") or ""),
                                "reason": "候选人已被招呼，跳过 passed=False 覆盖",
                            },
                            result="skipped",
                        )
                        continue
                if type(passed) is bool:
                    # Canonical boolean takes precedence over stale tier/decision aliases.
                    decision = "recommend" if passed else "reject"
                else:
                    decision = str(item.get("decision") or "")
                    if not decision:
                        decision = self._decision_from_tier(str(item.get("tier") or ""))
                    if decision:
                        passed = decision == "recommend"
                reason = str(
                    item.get("reason") or item.get("greeting_score_reason") or ""
                )
                uow.analyses.add(
                    candidate_id=candidate.id,
                    passed=None if passed is None else bool(passed),
                    reason=reason,
                    decision=decision,
                    model=model,
                    run_id=run_id,
                )
                saved += 1
                uow.audit_logs.add(
                    action="candidate_analysis.save",
                    detail={
                        "candidate_id": candidate.id,
                        "passed": bool(passed) if passed is not None else None,
                        "decision": decision,
                        "model": model,
                        "analysis_audit": item.get("analysis_audit"),
                    },
                    result="success",
                )
        return saved

    def persist_greeting_records_from_log(
        self,
        *,
        job_name: str,
        greet_log_file: str | Path,
        run_id: str = "",
    ) -> int:
        """Persist greeting_record rows from greet_log.json."""
        data = self._read_json_object(greet_log_file)
        results = data.get("results", []) if isinstance(data, dict) else []
        if not isinstance(results, list):
            return 0

        saved = 0
        with UnitOfWork(self.database.session_factory) as uow:
            for item in results:
                if not isinstance(item, dict):
                    continue
                candidate = self._find_candidate_for_greeting(uow, item, job_name)
                if candidate is None:
                    continue
                status = str(item.get("status") or "")
                error = str(item.get("reason") or item.get("error") or "")
                message = str(item.get("message") or self._default_message_template())
                uow.greetings.add(
                    candidate_id=candidate.id,
                    message=message,
                    status=status,
                    error=error,
                    run_id=run_id,
                )
                saved += 1
                uow.audit_logs.add(
                    action="greeting_record.save",
                    detail={"candidate_id": candidate.id, "status": status, "error": error},
                    result="success" if status in ("greeted", "sent", "dry_run") else "warning",
                )
        return saved

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _read_json_array(path: str | Path | None) -> list[Any]:
        if not path:
            return []
        p = Path(path)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _read_json_object(path: str | Path | None) -> dict[str, Any]:
        if not path:
            return {}
        p = Path(path)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _candidate_from_recommend_item(
        item: dict[str, Any],
        job_name: str,
        *,
        run_id: str = "",
    ) -> dict[str, Any]:
        card = item.get("geekCard") or {}
        prescreen = item.get("_card_prescreen") or {}
        if not isinstance(prescreen, dict):
            prescreen = {}
        prescreen_detail = dict(prescreen)
        raw_employment_status = item.get("_raw_employment_status")
        if raw_employment_status:
            # 复用现有 JSON 审计列保存 BOSS 原始状态字段，避免在确认枚举映射前
            # 把数字码误解释成具体求职状态。
            prescreen_detail["raw_employment_status"] = raw_employment_status
        prescreen_reasons = prescreen.get("reasons") or []
        salary = ""
        low_salary = card.get("lowSalary")
        high_salary = card.get("highSalary")
        # BOSS 返回 0 表示"面议"，不是有效薪资数据
        if low_salary and high_salary and low_salary not in (0, None, "") and high_salary not in (0, None, ""):
            salary = f"{low_salary}-{high_salary}K"
        elif low_salary or high_salary:
            # 只有一端有值也算有效
            if low_salary and low_salary not in (0, None, ""):
                salary = f"{low_salary}K以上"
            elif high_salary and high_salary not in (0, None, ""):
                salary = f"{high_salary}K以下"
        # 都为0/None时 salary 保持 ""，后续 expected_salary 会 fallback
        return {
            "boss_id": item.get("encryptGeekId") or item.get("uid") or card.get("encryptGeekId") or "",
            "job": job_name,
            "run_id": run_id,
            "name": card.get("geekName") or item.get("name") or "",
            "resume": None,
            "company": card.get("company") or card.get("brandName") or "",
            "work_years": card.get("workYearDesc") or card.get("experience") or "",
            "experience": card.get("workYearDesc") or card.get("experience") or "",
            "skills": card.get("skills") or card.get("tagList") or item.get("skills"),
            "age": RecruitWorkflow._coerce_age(card.get("ageDesc") or item.get("age")),
            "degree": str(card.get("degreeName") or card.get("degree") or card.get("geekDegree") or ""),
            "city": str(card.get("cityName") or card.get("city") or card.get("expectLocationName") or ""),
            "expected_salary": str(
                card.get("expectSalary") or item.get("expected_salary") or salary or "面议"
            ),
            "current_title": str(
                card.get("expectPosition") or card.get("expectPositionName")
                or card.get("positionName") or ""
            ),
            "card_prescreen_reason": "；".join(str(x) for x in prescreen_reasons),
            "card_prescreen_status": str(prescreen.get("status") or ""),
            "card_prescreen_passed": prescreen.get("passed"),
            "card_prescreen_detail": prescreen_detail or None,
        }

    @staticmethod
    def _candidate_from_rejected_item(
        item: dict[str, Any],
        job_name: str,
        *,
        run_id: str = "",
    ) -> dict[str, Any]:
        # recommend_list.py 的真实产物把精简卡片放在 "card"（含 geekCard/_card_prescreen）；
        # 旧审计文件用 "raw"；都没有时退回 item 本身。
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else None
        if raw is None:
            raw = item.get("card") if isinstance(item.get("card"), dict) else None
        if raw is None:
            raw = item
        raw = dict(raw)
        if not raw.get("_card_prescreen"):
            raw["_card_prescreen"] = {
                "passed": False,
                "status": item.get("status") or "rejected",
                "reasons": item.get("reasons") or [],
                "missing_fields": item.get("missing_fields") or [],
                "extracted": item.get("extracted") or {},
            }
        record = RecruitWorkflow._candidate_from_recommend_item(raw, job_name, run_id=run_id)
        if not record.get("name"):
            record["name"] = str(item.get("name") or "")
        return record

    @staticmethod
    def _candidate_from_resume(
        resume: dict[str, Any],
        job_name: str,
        *,
        run_id: str = "",
    ) -> dict[str, Any]:
        meta = resume.get("_meta") or {}
        work_experience = resume.get("work_experience") or []
        first_work = work_experience[0] if work_experience and isinstance(work_experience[0], dict) else {}
        return {
            "boss_id": meta.get("encrypt_geek_id") or resume.get("encrypt_geek_id") or "",
            "job": job_name,
            "run_id": run_id,
            "name": resume.get("name") or "",
            "resume": resume,
            "company": first_work.get("company") or "",
            "work_years": resume.get("work_years") or "",
            "experience": resume.get("work_years") or "",
            "skills": resume.get("skills") or resume.get("certifications") or [],
            "age": RecruitWorkflow._coerce_age(resume.get("age") or resume.get("age_desc")),
            "degree": RecruitWorkflow._extract_degree(resume),
            "city": str(resume.get("city") or resume.get("expect_city") or ""),
            "expected_salary": str(resume.get("expected_salary") or resume.get("expect_salary") or ""),
            "current_title": str(
                resume.get("current_role") or resume.get("expect_position") or first_work.get("position") or ""
            ),
        }

    @staticmethod
    def _decision_from_tier(tier: str) -> str:
        """把旧产物的中文分层统一转成机器可筛选的决策。"""
        if tier == "推荐":
            return "recommend"
        if tier == "待定":
            return "pending"
        return "reject"

    def _configured_job_requirement(self, job_name: str) -> dict[str, Any]:
        """读取外置岗位要求，并补充本次实际岗位名称。"""
        section = dict(self.config.section("recruitment"))
        # 如果存在 config/jobs/<岗位名>.yaml，则优先使用岗位专属要求；
        # 没有专属文件时继续走 recruitment.yaml，保证当前全职主播流程不受影响。
        loader = getattr(self.config, "load_job_profile", None)
        job_profile = loader(job_name) if callable(loader) else {}
        if job_profile:
            section.update(job_profile)
        return {
            "job_name": job_name or section.get("job_name", ""),
            "experience_required": section.get("experience_required", ""),
            "degree_required": section.get("degree_required", ""),
            "skill_requirements": section.get("skill_requirements", []),
            "additional_requirements": section.get("additional_requirements", ""),
            "card_prescreen": section.get("card_prescreen", {}),
            "resume_analysis": section.get("resume_analysis", {}),
            "stem_major_categories": section.get("stem_major_categories", {}),
        }

    @staticmethod
    def _extract_resume_companies(resume: dict[str, Any]) -> list[str]:
        """从简历的 work_experience 提取所有公司名。"""
        companies: list[str] = []
        we = resume.get("work_experience") or resume.get("workExperience") or []
        if isinstance(we, list):
            for w in we:
                if isinstance(w, dict):
                    name = str(w.get("company") or "").strip()
                    if name:
                        companies.append(name)
        return companies

    @staticmethod
    def _match_competitor(work_companies: list[str], competitor_list: list[str]) -> str:
        """检查公司名是否命中竞品名单（双向子串匹配）。

        返回命中的竞品名（未命中返回空串），用于 reason 展示。
        支持两种匹配：竞品是公司名的子串，或公司名是竞品的子串。
        """
        if not work_companies or not competitor_list:
            return ""
        for company in work_companies:
            company = company.strip()
            if not company:
                continue
            for competitor in competitor_list:
                competitor = competitor.strip()
                if not competitor:
                    continue
                # 双向子串匹配
                if competitor in company or company in competitor:
                    return competitor
        return ""

    @staticmethod
    def _coerce_age(value: Any) -> int | None:
        """兼容 ``28``、``28岁`` 等年龄格式。"""
        if value in (None, ""):
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if not digits:
            return None
        age = int(digits)
        return age if 14 <= age <= 100 else None

    @staticmethod
    def _extract_degree(resume: dict[str, Any]) -> str:
        direct = resume.get("degree") or resume.get("education_level")
        if direct:
            return str(direct)
        education = resume.get("education") or resume.get("education_experience") or []
        if isinstance(education, list) and education and isinstance(education[0], dict):
            return str(education[0].get("degree") or education[0].get("education") or "")
        return ""

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        """先写临时文件再原子替换，避免中断后留下半截结果文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(temporary, path)

    def _find_candidate_for_analysis(self, uow: UnitOfWork, item: dict[str, Any], job_name: str):
        boss_id = item.get("geek_id") or item.get("boss_id") or item.get("encrypt_geek_id")
        if boss_id:
            found = uow.candidates.get_by_boss_id(str(boss_id), job_name)
            if found:
                return found
        return self._find_candidate_by_name(uow, str(item.get("name") or ""), job_name)

    def _find_candidate_for_greeting(self, uow: UnitOfWork, item: dict[str, Any], job_name: str):
        boss_id = (
            item.get("target_encrypt_geek_id")
            or item.get("encrypt_geek_id")
            or item.get("geek_id")
            or item.get("boss_id")
        )
        if boss_id:
            found = uow.candidates.get_by_boss_id(str(boss_id), job_name)
            if found:
                return found
        return self._find_candidate_by_name(uow, str(item.get("name") or ""), job_name)

    @staticmethod
    def _find_candidate_by_name(uow: UnitOfWork, name: str, job_name: str):
        if not name:
            return None
        for candidate in uow.candidates.list_by_job(job_name):
            if candidate.name == name:
                return candidate
        return None

    def _default_message_template(self) -> str:
        default_name = self.config.get("message.default_template", "standard")
        return self.config.get(f"message.templates.{default_name}", "")

    def _audit(self, action: str, detail: dict[str, Any], result: str) -> None:
        with UnitOfWork(self.database.session_factory) as uow:
            uow.audit_logs.add(action=action, detail=detail, result=result)
        self.system_logger.info("audit | action=%s | result=%s | detail=%s", action, result, detail)
        self.audit_logger.info("action=%s | result=%s | detail=%s", action, result, detail)
