# -*- coding: utf-8 -*-
"""boss_hr.application.fetch_service — fetch 命令业务逻辑。

职责（C1）：
  - 校验 run 存在 + confirmed=true + count > 0
  - 自动化主流程由 recommend_list 在同一浏览器循环内完成卡片预筛、简历截图、AI 和招呼
  - 非 transactional 的独立 fetch 命令仍兼容旧 recommend_download 两阶段入口
  - download 失败时不回滚（保留 list 产物）
  - 返回 {requested_count, listed_count, downloaded_count, failed_count,
          candidate_list_file, new_resumes_file, failed_resumes_file}

不直接调 patchright / 不读 recommend_list.py 的 print 输出。
所有调用走 adapters/legacy_runner。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.contracts.results import CommandResult, ok, error
from boss_hr.contracts.errors import ExitCode, ErrorCode, UnifiedError
from boss_hr.adapters import legacy_runner
# 重要：用全限定名 `legacy_runner.run_legacy_cli(...)` 而非局部 `run_legacy_cli(...)`；
# 这样 monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", ...) 才生效
# （import-from 后的引用在调用方命名空间独立，setattr 不会传播）。
from boss_hr.adapters.legacy_runner import legacy_error, try_extract_blocked_message
from boss_hr.adapters.browser_environment import ensure_browser_ready
from boss_page_lock import BossPageLock, BossPageLockBusy


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


# download 超时策略：
# - 在线简历需要真实滚动、懒加载和长图落盘，不能沿用下载接口时代的 60 秒/人。
# - 保留有限超时防止浏览器永久卡死；按 20 分钟启动成本 + 5 分钟/候选人计算。
_DOWNLOAD_TIMEOUT_MIN_SECONDS = 3600
_DOWNLOAD_TIMEOUT_BASE_SECONDS = 1200
_DOWNLOAD_TIMEOUT_PER_CANDIDATE_SECONDS = 300


def _tail(text: str, *, lines: int = 20) -> list[str]:
    """取文本最后 N 行，用于把子进程 stdout/stderr 尾部带到 error data。

    和 greet_service._tail 同款实现。子进程崩了但 stdout 被吞掉时，
    用户只看到 rc=1 不知道崩在哪。带 tail 到 data 里让用户能看到。
    """
    if not text:
        return []
    return text.splitlines()[-lines:]

def _calculate_download_timeout(count: int) -> int:
    return max(
        _DOWNLOAD_TIMEOUT_MIN_SECONDS,
        _DOWNLOAD_TIMEOUT_BASE_SECONDS
        + count * _DOWNLOAD_TIMEOUT_PER_CANDIDATE_SECONDS,
    )


def _infer_run_paths(job_name: str, eid: str, run_id: str) -> dict:
    """从 JobOutputManager 算 list / download 产物路径。"""
    # 与旧脚本统一使用顶层模块名，避免同一文件被加载成两份模块实例。
    from output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    runs_dir = out.runs_dir
    process_dir = os.path.join(runs_dir, run_id, "process")
    return {
        "runs_dir": runs_dir,
        "process_dir": process_dir,
        "candidate_list_file": os.path.join(process_dir, "recommend_geek_ids.json"),
        "card_prescreen_rejected_file": os.path.join(process_dir, "card_prescreen_rejected.json"),
        "card_prescreen_summary_file": os.path.join(process_dir, "card_prescreen_summary.json"),
        "new_resumes_file": os.path.join(process_dir, "new_resumes.json"),
        "failed_resumes_file": os.path.join(process_dir, "failed_resumes.json"),
        "screening_results_file": os.path.join(process_dir, "screening_results.json"),
        "greet_log_file": os.path.join(process_dir, "greet_log.json"),
        "live_flow_log_file": os.path.join(process_dir, "transaction_live.log"),
    }


def _pre_check(job_name: str, eid: str, run_id: str, count: int
               ) -> tuple[int, Optional[str], Optional[dict]]:
    """前置校验：run 存在 + confirmed=true + count>0 + new_resumes 不必存在（fetch 会建）。

    返回 (exit_code, error_msg, paths)。
    - (0, None, paths)：通过
    - (rc!=0, msg, None)：失败
    """
    if count is None or count <= 0:
        return ExitCode(1).value if False else 1, f"--count 必须为正整数（当前 {count}）", None
    from shared.run_orchestrator import (
        RunOrchestrator,
        EXIT_CODE_RUN_NOT_FOUND,
        EXIT_CODE_RUN_JOB_MISMATCH,
    )
    orch = RunOrchestrator(job_name, encrypt_job_id=eid)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return EXIT_CODE_RUN_NOT_FOUND, f"run_id={run_id} 在岗位目录下不存在", None
    except RuntimeError as e:
        return EXIT_CODE_RUN_JOB_MISMATCH, str(e), None

    run_json_path = os.path.join(orch._mgr.runs_dir, run_id, "run.json")
    # 人工确认门采用 fail-closed：状态缺失、损坏或未确认都禁止继续抓取。
    if not os.path.isfile(run_json_path):
        return 20, f"run_id={run_id} 缺少 run.json，无法确认用户授权", None
    try:
        with open(run_json_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return 20, f"run_id={run_id} 的 run.json 无法读取：{exc}", None
    if state.get("confirmed") is not True:
        return 20, f"run_id={run_id} 尚未用户确认", None

    paths = _infer_run_paths(job_name, eid, run_id)
    return 0, None, paths


def _read_json_array(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read_json_object(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_array_if_missing(path: str, data: list) -> None:
    """写空产物占位，确保后续诊断能看到本轮 run 的完整文件边界。"""
    if os.path.isfile(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_candidates(*, job_name: str, encrypt_job_id: Optional[str],
                     run_id: Optional[str], count: int,
                     transactional: bool = False,
                     max_greetings: int = 10,
                     dry_run: bool = False,
                     job_requirement: dict | str | None = None) -> CommandResult:
    """fetch 命令业务实现。

    流程：
      1) 预校验（run/confirmed/count）
      2) transactional 模式调 recommend_list.py --transactional-live，在当前卡片完成全流程
      3) list 失败 → 透传 rc（list 阶段失败）
      4) list 成功 → 读 recommend_geek_ids.json 算 listed_count
      5) 仅兼容模式再调 recommend_download.py --max N
      6) download 失败 → 透传 rc（download 阶段失败，list 产物保留）
      7) download 成功 → 读 new_resumes.json / failed_resumes.json
      8) 返回 candidates_fetched
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                message="缺少 encrypt_job_id",
            ),
            run_id=run_id, exit_code=ExitCode.GENERIC,
        )
    if not run_id:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_RUN_ID,
                message="缺少 --run-id",
            ),
            exit_code=ExitCode.MISSING_RUN_ID,
        )

    rc, msg, paths = _pre_check(job_name, eid, run_id, count)
    if rc != 0:
        if rc == 23:
            code = ErrorCode.RUN_NOT_FOUND
        elif rc == 24:
            code = ErrorCode.JOB_MISMATCH
        elif rc == 20:
            code = ErrorCode.AWAITING_CONFIRMATION
        else:
            code = ErrorCode.INTERNAL
        return error(
            error_obj=UnifiedError(code=code, message=msg or "前置校验失败"),
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode(rc),
        )
    # 兼容测试替身和旧调用方构造的路径字典。
    paths.setdefault(
        "screening_results_file",
        os.path.join(paths["process_dir"], "screening_results.json"),
    )
    paths.setdefault(
        "greet_log_file",
        os.path.join(paths["process_dir"], "greet_log.json"),
    )
    paths.setdefault(
        "live_flow_log_file",
        os.path.join(paths["process_dir"], "transaction_live.log"),
    )

    try:
        with BossPageLock(
            command="fetch",
            job_name=job_name,
            encrypt_job_id=eid,
            run_id=run_id,
        ):
            sys.stderr.write(
                "[boss_hr] BOSS 页面独占中：正在抓取推荐候选人，请暂停手动操作 BOSS 网页。\n"
            )
            sys.stderr.flush()
            # v1.1.2: 自动启动 Edge + 登录态（不再要求用户预跑 doctor）。
            # v1.1.3: fetch 保留 v1.1.2 旧轮询路径（wait_for_user_login=True，
            # 默认 20s）。start 由单一参数 --login-wait-seconds 推导，
            # =0 不阻塞、>0 阻塞；fetch/greet 保持兼容接线不变。
            ready = ensure_browser_ready(auto_launch=True, wait_for_user_login=True)
            if not ready.ok:
                return error(
                    error_obj=ready.error_obj,
                    run_id=run_id, encrypt_job_id=eid, job_name=job_name,
                    exit_code=ExitCode.GENERIC,
                    next_action=ready.next_action,
                    remediation=ready.remediation,
                )

            # Step 1：严格遵守用户请求数量，避免隐式翻倍增加平台风控风险。
            list_args = [
                "--job-name", job_name,
                "--encrypt-job-id", eid,
                "--run-id", run_id,
                "--max", str(count),
            ]
            if transactional:
                from boss_hr.application.greet_service import _get_greet_message
                list_args.extend([
                    "--transactional-live",
                    "--max-greetings", str(max_greetings),
                    "--greet-message", _get_greet_message(job_name),
                ])
                if dry_run:
                    list_args.append("--dry-run")
                if job_requirement is not None:
                    list_args.extend([
                        "--job-requirement-json",
                        json.dumps(job_requirement, ensure_ascii=False),
                    ])
            list_result = legacy_runner.run_legacy_cli(
                "recommend_list", list_args,
                timeout=_calculate_download_timeout(count) if transactional else 600,
            )
    except BossPageLockBusy as exc:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_PAGE_BUSY,
                message=str(exc),
                recoverable=True,
            ),
            run_id=run_id,
            encrypt_job_id=eid,
            job_name=job_name,
            exit_code=ExitCode.GENERIC,
            next_action="retry_after_boss_page_free",
            remediation={
                "instructions": [
                    "等待当前 BOSS 自动化流程结束后重试",
                    "不要在程序抓取候选人或下载简历时手动操作 BOSS 网页",
                    "本地 AI 简历分析、数据库处理、飞书同步可以和人工回复并行",
                ],
            },
        )

    if list_result.returncode != 0:
        better_msg = try_extract_blocked_message(list_result.stdout)
        unified = legacy_error(list_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        # 验证码退出码 30 → 标记 CAPTCHA，让 daemon 能识别并立即退出
        if list_result.returncode == 30:
            unified = UnifiedError(code=ErrorCode.CAPTCHA, message=better_msg or unified.message,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            # 保留任意旧脚本退出码，不强转枚举，避免未知退出码导致二次崩溃。
            exit_code=list_result.returncode,
            data={
                "stdout_tail": _tail(list_result.stdout),
                "stderr_tail": _tail(list_result.stderr),
            },
        )

    # 读 list 产物
    if not os.path.isfile(paths["candidate_list_file"]):
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message=(
                    "推荐列表脚本未生成 recommend_geek_ids.json；"
                    "请确认 Edge 停在 BOSS 推荐牛人页并已加载候选人 iframe"
                ),
            ),
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode.GENERIC,
            data={
                "candidate_list_file": paths["candidate_list_file"],
                "card_prescreen_rejected_file": paths["card_prescreen_rejected_file"],
                "card_prescreen_summary_file": paths["card_prescreen_summary_file"],
            },
        )

    listed = _read_json_array(paths["candidate_list_file"])
    listed_count = len(listed)
    prescreen_summary = _read_json_object(paths["card_prescreen_summary_file"])
    accepted_card_count = int(prescreen_summary.get("accepted_count") or listed_count)
    rejected_card_count = int(prescreen_summary.get("rejected_count") or 0)
    terminal_card_count = accepted_card_count + rejected_card_count

    if listed_count == 0:
        _write_json_array_if_missing(paths["new_resumes_file"], [])
        _write_json_array_if_missing(paths["failed_resumes_file"], [])
        return ok(
            status="candidates_fetched",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "requested_count": count,
                "listed_count": 0,
                "terminal_card_count": terminal_card_count,
                "downloaded_count": 0,
                "failed_count": 0,
                "boss_page_exclusive_required": True,
                "candidate_list_file": paths["candidate_list_file"],
                "card_prescreen_rejected_file": paths["card_prescreen_rejected_file"],
                "card_prescreen_summary_file": paths["card_prescreen_summary_file"],
                "new_resumes_file": paths["new_resumes_file"],
                "failed_resumes_file": paths["failed_resumes_file"],
                "screening_results_file": paths["screening_results_file"],
                "greet_log_file": paths["greet_log_file"],
                "live_flow_log_file": paths["live_flow_log_file"],
                "transactional_flow": transactional,
            },
            next_action="inspect_card_prescreen",
        )

    # 新模式已经在 recommend_list 的同一浏览器循环中逐人完成了截图、AI 和
    # 招呼，禁止再启动 recommend_download 二次按 ID 搜索卡片。
    if transactional:
        new_resumes = _read_json_array(paths["new_resumes_file"])
        failed_resumes = _read_json_array(paths["failed_resumes_file"])
        screening = _read_json_object(paths["screening_results_file"])
        greet_log = _read_json_object(paths["greet_log_file"])
        candidates = screening.get("candidates") or []
        greet_items = greet_log.get("results") or []
        return ok(
            status="candidates_fetched",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "requested_count": count,
                "listed_count": listed_count,
                "terminal_card_count": terminal_card_count,
                "downloaded_count": sum(1 for item in new_resumes if item.get("ok", True)),
                "failed_count": len(failed_resumes),
                "download_partial": False,
                "download_timeout_seconds": _calculate_download_timeout(count),
                "boss_page_exclusive_required": True,
                "candidate_list_file": paths["candidate_list_file"],
                "card_prescreen_rejected_file": paths["card_prescreen_rejected_file"],
                "card_prescreen_summary_file": paths["card_prescreen_summary_file"],
                "new_resumes_file": paths["new_resumes_file"],
                "failed_resumes_file": paths["failed_resumes_file"],
                "screening_results_file": paths["screening_results_file"],
                "greet_log_file": paths["greet_log_file"],
                "live_flow_log_file": paths["live_flow_log_file"],
                "transactional_flow": True,
                "live_card_flow": True,
                "analyzed_count": len(candidates),
                "passed_count": sum(1 for item in candidates if item.get("passed")),
                "greeted_count": sum(
                    1 for item in greet_items if item.get("status") == "greeted"
                ),
                "message_sent_count": sum(
                    1 for item in greet_items if item.get("message_sent")
                ),
                "transaction_incomplete": bool(failed_resumes),
            },
            next_action="sync",
        )

    download_timed_out = False
    download_timeout_seconds = _calculate_download_timeout(listed_count)
    try:
        with BossPageLock(
            command="download",
            job_name=job_name,
            encrypt_job_id=eid,
            run_id=run_id,
        ):
            sys.stderr.write(
                "[boss_hr] BOSS 页面独占中：正在下载候选人简历，请暂停手动操作 BOSS 网页。\n"
            )
            sys.stderr.flush()
            # Step 2: 调 recommend_download（仍用用户请求的 --max，从 list 的 2x 缓冲池中最多下载 count 份）
            try:
                download_args = [
                    "--job-name", job_name,
                    "--encrypt-job-id", eid,
                    "--run-id", run_id,
                    "--max", str(count),
                ]
                if transactional:
                    from boss_hr.application.greet_service import _get_greet_message
                    download_args.extend([
                        "--transactional",
                        "--max-greetings", str(max_greetings),
                        "--greet-message", _get_greet_message(job_name),
                    ])
                    if dry_run:
                        download_args.append("--dry-run")
                    if job_requirement is not None:
                        requirement_value = json.dumps(job_requirement, ensure_ascii=False)
                        download_args.extend(["--job-requirement-json", requirement_value])
                dl_result = legacy_runner.run_legacy_cli(
                    "recommend_download",
                    download_args,
                    # 用实际通过卡片预筛的人数计算；在线简历截图比旧接口下载耗时长。
                    timeout=download_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                # recommend_download 会逐人原子写入。若整体时间上限仍被触发，
                # 已完成的完整长图继续进入 AI，而不是让整轮结果全部作废。
                partial_resumes = _read_json_array(paths["new_resumes_file"])
                if not partial_resumes:
                    raise
                download_timed_out = True
                dl_result = None
                sys.stderr.write(
                    "[boss_hr] 在线简历截图达到本轮时间上限；"
                    f"保留并继续分析已完成的 {len(partial_resumes)} 份。\n"
                )
                sys.stderr.flush()
    except BossPageLockBusy as exc:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_PAGE_BUSY,
                message=str(exc),
                recoverable=True,
            ),
            run_id=run_id,
            encrypt_job_id=eid,
            job_name=job_name,
            exit_code=ExitCode.GENERIC,
            next_action="retry_after_boss_page_free",
        )

    if not download_timed_out and dl_result.returncode != 0:
        # 保留 list 产物，不回滚
        better_msg = try_extract_blocked_message(dl_result.stdout)
        unified = legacy_error(dl_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        # 验证码退出码 30 → 标记 CAPTCHA，让 daemon 能识别并立即退出
        if dl_result.returncode == 30:
            unified = UnifiedError(code=ErrorCode.CAPTCHA, message=better_msg or unified.message,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=dl_result.returncode,
            data={
                "stdout_tail": _tail(dl_result.stdout),
                "stderr_tail": _tail(dl_result.stderr),
            },
        )

    # 读 download 产物
    new_resumes = _read_json_array(paths["new_resumes_file"])
    failed_resumes = _read_json_array(paths["failed_resumes_file"])
    downloaded_count = sum(1 for r in new_resumes if r.get("ok", True))
    failed_count = len(failed_resumes)
    transaction_stats = {
        "analyzed_count": 0,
        "passed_count": 0,
        "greeted_count": 0,
        "message_sent_count": 0,
        "transaction_incomplete": False,
    }
    if transactional:
        screening = _read_json_object(paths["screening_results_file"])
        greet_log = _read_json_object(paths["greet_log_file"])
        candidates = screening.get("candidates") or []
        greet_items = greet_log.get("results") or []
        resume_ids = {
            str((item.get("_meta") or {}).get("encrypt_geek_id") or "")
            for item in new_resumes if isinstance(item, dict)
        } - {""}
        analyzed_ids = {
            str(item.get("encrypt_geek_id") or item.get("geek_id") or "")
            for item in candidates if isinstance(item, dict)
        } - {""}
        passed_ids = {
            str(item.get("encrypt_geek_id") or item.get("geek_id") or "")
            for item in candidates if isinstance(item, dict) and item.get("passed")
        } - {""}
        resolved_greet_ids = {
            str(item.get("encrypt_geek_id") or "")
            for item in greet_items
            if isinstance(item, dict)
            and item.get("status") in ("greeted", "already_contacted", "dry_run")
        } - {""}
        transaction_stats = {
            "analyzed_count": len(analyzed_ids),
            "passed_count": len(passed_ids),
            "greeted_count": sum(
                1 for item in greet_items
                if isinstance(item, dict) and item.get("status") == "greeted"
            ),
            "message_sent_count": sum(
                1 for item in greet_items
                if isinstance(item, dict) and item.get("message_sent")
            ),
            "transaction_incomplete": bool(
                (resume_ids - analyzed_ids) or (passed_ids - resolved_greet_ids)
            ),
        }

    return ok(
        status="candidates_fetched",
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "requested_count": count,
                "listed_count": listed_count,
                "terminal_card_count": terminal_card_count,
                "downloaded_count": downloaded_count,
                "failed_count": failed_count,
                "download_partial": download_timed_out,
                "download_timeout_seconds": download_timeout_seconds,
                "boss_page_exclusive_required": True,
                "candidate_list_file": paths["candidate_list_file"],
            "card_prescreen_rejected_file": paths["card_prescreen_rejected_file"],
            "card_prescreen_summary_file": paths["card_prescreen_summary_file"],
            "new_resumes_file": paths["new_resumes_file"],
            "failed_resumes_file": paths["failed_resumes_file"],
            "screening_results_file": paths["screening_results_file"],
            "greet_log_file": paths["greet_log_file"],
            "transactional_flow": transactional,
            **transaction_stats,
        },
        next_action="sync" if transactional else "automate",
    )


__all__ = ["fetch_candidates"]
