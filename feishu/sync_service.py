"""Sync SQLite recruitment records to Feishu Bitable."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select

from boss_hr.config_manager import ConfigManager, get_config
from database.connection import Database
from database.models import Candidate, CandidateAnalysis, GreetingRecord
from database.repositories import UnitOfWork
from feishu.client import BitableClient, FeishuBitableClient
from logger.log_manager import get_audit_logger, get_automation_logger, log_exception


class FeishuSyncService:
    """Synchronize SQLite data to Feishu. SQLite remains the source of truth."""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        database: Database | None = None,
        client: BitableClient | None = None,
    ) -> None:
        self.config = config or get_config()
        self.database = database or Database(self.config)
        self.database.init()
        self.client = client or FeishuBitableClient(self.config)
        self.automation_logger = get_automation_logger(self.config)
        self.audit_logger = get_audit_logger(self.config)

    def initialize_structure(self, *, job: str | None = None) -> str:
        """Ensure the Feishu candidate table for one job exists and return table_id."""
        table_id = self.client.ensure_candidate_table(job=job)
        self._audit(
            "feishu.initialize_structure",
            {
                "job": job or "",
                "table_id": table_id,
                "table_name": self._table_name_for_audit(job),
            },
            "success",
        )
        self.automation_logger.info("feishu_initialize_structure | table_id=%s", table_id)
        return table_id

    def sync_all_candidates(self, *, job: str | None = None) -> dict[str, int]:
        """Sync all candidates, optionally limited to one job."""
        table_id = self.initialize_structure(job=job) if job else None
        with UnitOfWork(self.database.session_factory) as uow:
            stmt = select(Candidate).order_by(Candidate.created_time.asc(), Candidate.id.asc())
            if job:
                stmt = stmt.where(Candidate.job == job)
            candidate_ids = [item.id for item in uow.session.execute(stmt).scalars()]
        return self._sync_candidate_ids(candidate_ids, table_id=table_id, log_name="all")

    def sync_run_candidates(
        self, *, job: str | None = None, run_id: str | None = None,
    ) -> dict[str, int]:
        """同步某个 run 的全部候选人，包括未通过和已打招呼的人。"""
        table_id = self.initialize_structure(job=job) if job else None
        with UnitOfWork(self.database.session_factory) as uow:
            stmt = (
                select(Candidate.id)
                .outerjoin(CandidateAnalysis, CandidateAnalysis.candidate_id == Candidate.id)
                .outerjoin(GreetingRecord, GreetingRecord.candidate_id == Candidate.id)
                .distinct()
                .order_by(Candidate.created_time.asc(), Candidate.id.asc())
            )
            if job:
                stmt = stmt.where(Candidate.job == job)
            if run_id:
                stmt = stmt.where(or_(
                    Candidate.run_id == run_id,
                    CandidateAnalysis.run_id == run_id,
                    GreetingRecord.run_id == run_id,
                ))
            candidate_ids = [int(item) for item in uow.session.execute(stmt).scalars()]

        return self._sync_candidate_ids(candidate_ids, table_id=table_id, log_name="run")

    def sync_greeted_candidates(
        self, *, job: str | None = None, run_id: str | None = None,
    ) -> dict[str, int]:
        """只同步成功招呼的候选人，可按岗位和本次 run 精确限制范围。"""
        table_id = self.initialize_structure(job=job) if job else None
        with UnitOfWork(self.database.session_factory) as uow:
            stmt = (
                select(Candidate)
                .join(GreetingRecord, GreetingRecord.candidate_id == Candidate.id)
                .where(GreetingRecord.status.in_(("greeted", "sent", "success")))
                .distinct()
                .order_by(Candidate.created_time.asc(), Candidate.id.asc())
            )
            if job:
                stmt = stmt.where(Candidate.job == job)
            if run_id:
                stmt = stmt.where(GreetingRecord.run_id == run_id)
            candidate_ids = [item.id for item in uow.session.execute(stmt).scalars()]

        return self._sync_candidate_ids(candidate_ids, table_id=table_id, log_name="greeted")

    def _sync_candidate_ids(
        self, candidate_ids: list[int], *, table_id: str | None, log_name: str,
    ) -> dict[str, int]:
        """批量同步的唯一实现，所有筛选方式只负责产生候选人 ID 列表。

        优先走批量 API（batch_create / batch_update + 全表索引查找），
        批量异常时降级到逐条 sync_candidate，保证单条失败不阻塞其他候选人。
        """
        stats = {"created": 0, "updated": 0, "failed": 0, "total": len(candidate_ids)}
        if not candidate_ids:
            self.automation_logger.info("feishu_sync_%s_finish | stats=%s", log_name, stats)
            return stats
        # 优先批量；批量整体失败时降级到逐条（单条失败已被 try 兜住）
        try:
            batch_stats = self._sync_candidate_ids_batch(candidate_ids, table_id=table_id, log_name=log_name)
            # 批量成功直接返回；批量内的单条失败已由降级路径兜住并写 failed
            for k in ("created", "updated", "failed"):
                stats[k] = batch_stats.get(k, 0)
            self.automation_logger.info("feishu_sync_%s_finish | stats=%s | mode=batch", log_name, stats)
            return stats
        except Exception as exc:
            log_exception(
                "feishu.sync_batch_fallback", exc, self.config,
                detail={"count": len(candidate_ids)},
            )
            # 批量整体失败 → 降级到逐条
            stats = {"created": 0, "updated": 0, "failed": 0, "total": len(candidate_ids)}
            for candidate_id in candidate_ids:
                try:
                    action = self.sync_candidate(candidate_id, table_id=table_id)
                    stats[action] += 1
                except Exception as inner_exc:
                    log_exception(
                        "feishu.sync_greeted_candidate", inner_exc, self.config,
                        detail={"candidate_id": candidate_id},
                    )
                    stats["failed"] += 1
                    self._write_sync_failure(candidate_id, str(inner_exc), table_id=table_id)
            self.automation_logger.info("feishu_sync_%s_finish | stats=%s | mode=sequential_fallback", log_name, stats)
            return stats

    def _sync_candidate_ids_batch(
        self, candidate_ids: list[int], *, table_id: str | None, log_name: str,
    ) -> dict[str, int]:
        """批量同步实现：一次拉全表建索引 + 分批 batch_update / batch_create。

        单条候选人在批量接口失败时降级到逐条 sync_candidate，不影响其他候选人。
        """
        stats = {"created": 0, "updated": 0, "failed": 0, "total": len(candidate_ids)}
        if table_id is None:
            # 批量需要明确 table_id；未传时按第一个候选人的岗位推导
            with UnitOfWork(self.database.session_factory) as uow:
                cand = uow.candidates.get(candidate_ids[0])
                if cand is None:
                    raise ValueError(f"candidate_id={candidate_ids[0]} not found")
                table_id = self.initialize_structure(job=cand.job)

        # 1) 一次性读 DB：candidate + analysis + greeting + sync_record
        rows: list[dict[str, Any]] = []
        with UnitOfWork(self.database.session_factory) as uow:
            for cid in candidate_ids:
                candidate = uow.candidates.get(cid)
                if candidate is None:
                    raise ValueError(f"candidate_id={cid} not found")
                analysis = uow.analyses.latest_for_candidate(cid)
                greeting = uow.greetings.latest_for_candidate(cid)
                sync_record = uow.sync_records.latest_for_candidate(
                    cid, feishu_table_id=table_id,
                )
                fields = self._candidate_fields(candidate, analysis, greeting)
                record_id = sync_record.feishu_record_id if sync_record else ""
                rows.append({
                    "candidate_id": cid,
                    "fields": fields,
                    "record_id": record_id,
                    "sync_key": str(fields.get(
                        self.config.get("feishu.fields.sync_key", "同步键"), "") or ""),
                })

        # 2) 预取全表 + 建 sync_key → record_id 索引（只对没有 record_id 的行用）
        missing_record_id = [r for r in rows if not r["record_id"]]
        remote_index: dict[str, str] = {}
        if missing_record_id:
            remote_records = self.client.list_records(table_id)
            sync_key_field = self.config.get("feishu.fields.sync_key", "同步键")
            for rec in remote_records:
                rec_fields = rec.get("fields") or {}
                key_val = rec_fields.get(sync_key_field)
                # 飞书对纯文本字段可能返回 [{"text": "..."}] 或 "..."；统一取字符串
                key_str = self._extract_text(key_val)
                rid = str(rec.get("record_id", "") or "")
                if key_str and rid:
                    remote_index[key_str] = rid

        # 3) 分流到 update / create
        to_update: list[dict[str, Any]] = []  # {"record_id", "fields", "candidate_id"}
        to_create: list[dict[str, Any]] = []  # {"fields", "candidate_id"}
        for r in rows:
            if r["record_id"]:
                to_update.append(r)
            else:
                rid = remote_index.get(r["sync_key"], "")
                if rid:
                    r["record_id"] = rid
                    to_update.append(r)
                else:
                    to_create.append(r)

        # 4) 执行 batch_update（分批 500）
        update_failed_ids: set[int] = set()
        if to_update:
            BATCH = 500
            for i in range(0, len(to_update), BATCH):
                chunk = to_update[i:i + BATCH]
                payload = [{"record_id": r["record_id"], "fields": r["fields"]} for r in chunk]
                try:
                    self.client.batch_update_records(table_id, payload)
                except Exception as exc:
                    log_exception(
                        "feishu.batch_update_chunk_failed", exc, self.config,
                        detail={"chunk_size": len(chunk), "start": i},
                    )
                    # 分批失败 → 这批逐条降级
                    for r in chunk:
                        try:
                            self.client.update_record(table_id, r["record_id"], r["fields"])
                        except Exception as inner:
                            # record not found → record_id 已失效（飞书表被重建/记录被删）
                            # 转为 create 新记录
                            err_msg = str(inner)
                            if "not found" in err_msg.lower() or "1254043" in err_msg:
                                try:
                                    rid = self.client.create_record(table_id, r["fields"])
                                    r["record_id"] = rid
                                    new_record_ids.append(rid)
                                    stats["created"] += 1
                                    stats["updated"] -= 1  # 原本计入 update，实际是 create
                                except Exception as create_err:
                                    log_exception(
                                        "feishu.create_record_fallback_after_update_fail", create_err, self.config,
                                        detail={"candidate_id": r["candidate_id"]},
                                    )
                                    update_failed_ids.add(r["candidate_id"])
                            else:
                                log_exception(
                                    "feishu.update_record_fallback", inner, self.config,
                                    detail={"candidate_id": r["candidate_id"]},
                                )
                                update_failed_ids.add(r["candidate_id"])

        # 5) 执行 batch_create（分批 500）
        create_failed_ids: set[int] = set()
        new_record_ids: list[str] = []
        if to_create:
            BATCH = 500
            for i in range(0, len(to_create), BATCH):
                chunk = to_create[i:i + BATCH]
                payload = [{"fields": r["fields"]} for r in chunk]
                try:
                    rids = self.client.batch_create_records(table_id, payload)
                    new_record_ids.extend(rids)
                except Exception as exc:
                    log_exception(
                        "feishu.batch_create_chunk_failed", exc, self.config,
                        detail={"chunk_size": len(chunk), "start": i},
                    )
                    # 分批失败 → 这批逐条降级
                    for r in chunk:
                        try:
                            rid = self.client.create_record(table_id, r["fields"])
                            new_record_ids.append(rid)
                        except Exception as inner:
                            log_exception(
                                "feishu.create_record_fallback", inner, self.config,
                                detail={"candidate_id": r["candidate_id"]},
                            )
                            create_failed_ids.add(r["candidate_id"])

        # 6) 对齐 create 的 record_id（batch_create 返回顺序与输入对齐）
        for idx, r in enumerate(to_create):
            if idx < len(new_record_ids):
                r["record_id"] = new_record_ids[idx]

        # 7) 写回 sync_records + audit
        with UnitOfWork(self.database.session_factory) as uow:
            for r in rows:
                action = "updated" if r in to_update else "created"
                if r["candidate_id"] in update_failed_ids or r["candidate_id"] in create_failed_ids:
                    stats["failed"] += 1
                    self._write_sync_failure(r["candidate_id"], "batch fallback failed", table_id=table_id)
                    continue
                if not r["record_id"]:
                    stats["failed"] += 1
                    self._write_sync_failure(r["candidate_id"], "missing record_id after batch", table_id=table_id)
                    continue
                uow.sync_records.add(
                    candidate_id=r["candidate_id"],
                    feishu_record_id=r["record_id"],
                    feishu_table_id=table_id,
                    status="synced",
                )
                uow.audit_logs.add(
                    action="feishu.sync_candidate",
                    detail={"candidate_id": r["candidate_id"], "feishu_record_id": r["record_id"], "action": action},
                    result="success",
                )
                stats[action] += 1

        self.automation_logger.info(
            "feishu_sync_%s_batch | stats=%s | update=%s | create=%s",
            log_name, stats, len(to_update), len(to_create),
        )
        return stats

    @staticmethod
    def _extract_text(value: Any) -> str:
        """飞书字段值可能是 [{"text": "x"}] 或 "x" 或 [{"type": "text", "text": "x"}]；统一取字符串。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text", "") or item.get("name", "") or ""))
            return "".join(parts)
        return str(value)

    def sync_candidate(self, candidate_id: int, *, table_id: str | None = None) -> str:
        """Sync one candidate and return ``created`` or ``updated``."""
        if table_id is None:
            with UnitOfWork(self.database.session_factory) as uow:
                candidate = uow.candidates.get(candidate_id)
                if candidate is None:
                    raise ValueError(f"candidate_id={candidate_id} not found")
                job = candidate.job
            table_id = self.initialize_structure(job=job)

        with UnitOfWork(self.database.session_factory) as uow:
            candidate = uow.candidates.get(candidate_id)
            if candidate is None:
                raise ValueError(f"candidate_id={candidate_id} not found")
            # 最新记录查询统一复用 Repository，避免服务层维护第二套 SQL。
            analysis = uow.analyses.latest_for_candidate(candidate_id)
            greeting = uow.greetings.latest_for_candidate(candidate_id)
            sync_record = uow.sync_records.latest_for_candidate(
                candidate_id,
                feishu_table_id=table_id,
            )
            fields = self._candidate_fields(candidate, analysis, greeting)

            record_id = sync_record.feishu_record_id if sync_record else ""
            action = "updated"
            if record_id:
                self.client.update_record(table_id, record_id, fields)
            else:
                found = self.client.find_record(table_id, fields)
                if found:
                    record_id = str(found.get("record_id", "") or "")
                    if not record_id:
                        raise RuntimeError(f"Feishu record missing record_id: {found}")
                    self.client.update_record(table_id, record_id, fields)
                else:
                    record_id = self.client.create_record(table_id, fields)
                    action = "created"

            uow.sync_records.add(
                candidate_id=candidate_id,
                feishu_record_id=record_id,
                feishu_table_id=table_id,
                status="synced",
            )
            uow.audit_logs.add(
                action="feishu.sync_candidate",
                detail={"candidate_id": candidate_id, "feishu_record_id": record_id, "action": action},
                result="success",
            )
            self.automation_logger.info(
                "feishu_sync_candidate | candidate_id=%s | record_id=%s | action=%s",
                candidate_id,
                record_id,
                action,
            )
            return action

    def query_records(self, *, table_id: str | None = None) -> list[dict[str, Any]]:
        """Query Feishu records from the candidate table."""
        return self.client.list_records(table_id or self.initialize_structure())

    def create_record(self, fields: dict[str, Any], *, table_id: str | None = None) -> str:
        """Create one raw Feishu record. Mostly useful for maintenance tools."""
        record_id = self.client.create_record(table_id or self.initialize_structure(), fields)
        self._audit("feishu.create_record", {"record_id": record_id, "fields": fields}, "success")
        return record_id

    def update_record(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        table_id: str | None = None,
    ) -> None:
        """Update one raw Feishu record. Mostly useful for maintenance tools."""
        self.client.update_record(table_id or self.initialize_structure(), record_id, fields)
        self._audit("feishu.update_record", {"record_id": record_id, "fields": fields}, "success")

    def _candidate_fields(
        self,
        candidate: Candidate,
        analysis: CandidateAnalysis | None,
        greeting: GreetingRecord | None,
    ) -> dict[str, Any]:
        fields = self.config.section("feishu").get("fields", {})
        card_passed = candidate.card_prescreen_passed
        # 卡片预筛被拒绝的候选人：不应该有后续的简历分析和打招呼记录
        # 旧数据可能残留（候选人先通过旧代码的预筛后被打招呼，新代码重新预筛拒绝）
        # 此时清空招呼和简历分析相关字段，避免飞书显示矛盾数据
        if card_passed is False:
            resume_passed = None
            resume_reason = ""
            greeting_status = ""
            greeting_date = None
            was_greeted = False
            greeting_message = ""
            screening_stage = "卡片预筛"
        else:
            resume_passed = analysis.passed if analysis is not None else None
            resume_reason = analysis.reason if analysis is not None else ""
            greeting_status = greeting.status if greeting is not None else ""
            greeting_date = (
                int(greeting.send_time.timestamp() * 1000)
                if greeting is not None and greeting.send_time is not None else None
            )
            was_greeted = greeting_status in ("greeted", "sent", "success")
            greeting_message = greeting.message if greeting is not None else ""
            screening_stage = self._screening_stage(candidate, analysis, greeting)
        updated_at = int(datetime.now().timestamp() * 1000)
        rejection_reason = self._rejection_reason(candidate, analysis)
        return {
            fields.get("boss_id", "BOSS候选人ID"): candidate.boss_id,
            fields.get("name", "姓名"): candidate.name,
            fields.get("age", "年龄"): candidate.age,
            fields.get("degree", "学历"): candidate.degree,
            fields.get("work_years", "工作年限"): candidate.work_years or candidate.experience,
            fields.get("city", "城市"): candidate.city,
            fields.get("expected_salary", "期望薪资"): candidate.expected_salary,
            fields.get("job", "岗位"): candidate.job,
            fields.get("sync_key", "同步键"): f"{candidate.boss_id}|{candidate.job}",
            fields.get("company", "公司"): candidate.company,
            fields.get("current_title", "当前/期望岗位"): candidate.current_title,
            fields.get("card_prescreen_status", "卡片筛选状态"): candidate.card_prescreen_status,
            fields.get("card_prescreen_passed", "卡片筛选结果"): (
                "通过" if card_passed else ("未通过" if card_passed is False else "")
            ),
            fields.get("card_prescreen_reason", "卡片筛选原因"): candidate.card_prescreen_reason,
            fields.get("screening_stage", "筛选阶段"): screening_stage,
            fields.get("rejection_reason", "未通过原因"): rejection_reason,
            fields.get("passed", "简历筛选结果"): (
                "通过" if resume_passed else ("未通过" if resume_passed is False else "")
            ),
            fields.get("reason", "简历筛选原因"): resume_reason,
            fields.get("decision", "筛选结论"): (
                analysis.decision if analysis is not None and card_passed is not False else ""
            ),
            fields.get("greeting_status", "发送状态"): greeting_status,
            fields.get("was_greeted", "是否已打招呼"): "是" if was_greeted else "否",
            fields.get("greeting_message", "招呼语"): greeting_message,
            fields.get("greeting_date", "打招呼日期"): greeting_date,
            fields.get("reply_status", "回复状态"): "",
            fields.get("updated_time", "时间"): updated_at,
        }

    @staticmethod
    def _screening_stage(
        candidate: Candidate,
        analysis: CandidateAnalysis | None,
        greeting: GreetingRecord | None,
    ) -> str:
        # 卡片预筛拒绝优先：不应进入后续阶段
        if candidate.card_prescreen_passed is False:
            return "卡片预筛"
        if greeting is not None:
            return "打招呼"
        if analysis is not None:
            return "简历筛选"
        if candidate.card_prescreen_status:
            return "卡片预筛"
        return "已抓取"

    @staticmethod
    def _rejection_reason(
        candidate: Candidate,
        analysis: CandidateAnalysis | None,
    ) -> str:
        # 卡片预筛拒绝优先：如果最新的 card_prescreen_passed=False，
        # 说明候选人被卡片预筛拒绝，应返回卡片拒因（不管 analysis 的旧状态）
        if candidate.card_prescreen_passed is False:
            return candidate.card_prescreen_reason or ""
        if analysis is not None:
            decision = str(analysis.decision or "").strip()
            if analysis.passed is False or (decision and decision != "recommend"):
                return analysis.reason or ""
            return ""
        status = str(candidate.card_prescreen_status or "").strip()
        if status in ("rejected", "reject", "pending_confirmation"):
            return candidate.card_prescreen_reason or ""
        return ""

    def _write_sync_failure(self, candidate_id: int, error: str, *, table_id: str = "") -> None:
        with UnitOfWork(self.database.session_factory) as uow:
            uow.sync_records.add(
                candidate_id=candidate_id,
                feishu_record_id="",
                feishu_table_id=table_id,
                status="failed",
                error=error[:500],
            )
            uow.audit_logs.add(
                action="feishu.sync_candidate",
                detail={"candidate_id": candidate_id, "error": error[:500]},
                result="failed",
            )

    def _audit(self, action: str, detail: dict[str, Any], result: str) -> None:
        with UnitOfWork(self.database.session_factory) as uow:
            uow.audit_logs.add(action=action, detail=detail, result=result)
        self.audit_logger.info("action=%s | result=%s | detail=%s", action, result, detail)

    def _table_name_for_audit(self, job: str | None = None) -> str:
        job_key = str(job or "").strip()
        job_tables = self.config.get("feishu.bitable.job_tables", {}) or {}
        if isinstance(job_tables, dict):
            item = job_tables.get(job_key)
            if isinstance(item, dict) and item.get("table_name"):
                return str(item["table_name"])
            if isinstance(item, str) and item:
                return job_key
        return str(self.config.get("feishu.bitable.table_name", "招聘候选人") or "招聘候选人")


__all__ = ["FeishuSyncService"]
