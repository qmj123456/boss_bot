# -*- coding: utf-8 -*-
"""boss_hr.application.greet_service — greet 命令业务逻辑。

职责：
  - 校验 encrypt_job_id / run_id / run 存在（预校验，收敛为 23 / 24）
  - 通过 adapters/legacy_runner 调 auto_greet.py（子进程）
  - 读 runs/<run_id>/process/greet_log.json 取 summary
  - 把结果包装成 CommandResult

不直接调 patchright / 不复制 CDP 扫描 / 倒序招呼 / DOM click 逻辑。

⚠️ 必须走子进程（cli_runner），**不能** `from auto_greet import auto_greet`
直接调函数：`auto_greet()` 函数体内引用了只在 `if __name__ == '__main__'`
块里定义的全局 `args`（auto_greet.py:744/753/766），直接调用会在收尾阶段
抛 NameError。详见 docs/refactor/unified-cli/greet-baseline.md §6.2。

⚠️ greet_log.json 可能不存在：无高分候选人时旧脚本提前 return（rc=0）且
atexit 的 prune_if_empty() 会删掉整个 run 目录。本模块容忍该文件缺失，
不断言其存在。详见 greet-baseline.md §6.3。
"""
from __future__ import annotations
import json
import os
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
# 这样 monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", ...) 才生效。
from boss_hr.adapters.legacy_runner import legacy_error, try_extract_blocked_message
from boss_hr.adapters.browser_environment import ensure_browser_ready
from boss_page_lock import BossPageLock, BossPageLockBusy


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


def _greet_log_path(job_name: str, eid: str, run_id: str) -> str:
    """算 runs/<run_id>/process/greet_log.json 路径（与旧脚本同款公式）。"""
    from output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    return os.path.join(out.runs_dir, run_id, "process", "greet_log.json")


def _read_greet_log(path: str) -> Optional[dict]:
    """读 greet_log.json；不存在 / 损坏都返回 None（调用方按无产物处理）。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _greet_log_mtime(path: str) -> float:
    """返回 greet_log.json 的 mtime（不存在返回 0）。

    用于检测 auto_greet 子进程是否写了新文件：调子进程前后各读一次，
    mtime 没变说明子进程没写新 greet_log（走的是"没有候选人"早退路径），
    此时不应该读旧文件当成本次结果。
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _boss_display_name(job_name: str) -> str:
    """Read the BOSS page display name from config/jobs/<job_name>.yaml."""
    try:
        from boss_hr.config_manager import get_config

        profile = get_config().load_job_profile(job_name)
        for key in (
            "boss_display_name",
            "boss_job_name",
            "boss_title",
            "boss_position_name",
            "display_name",
        ):
            value = profile.get(key) if isinstance(profile, dict) else None
            if value:
                return str(value).strip()
    except Exception:
        pass
    return ""


def _get_greet_message(job_name: str = "") -> str:
    """Read the configured greet message for a specific job.

    Resolution order (highest priority first):
      1) config/jobs/<job_name>.yaml 的 greet_message 字段（结构化，岗位专属）
      2) message.yaml: message.jobs.<job_name>.message  (inline message)
      3) message.yaml: message.jobs.<job_name>.template -> message.templates.<that name>
      4) message.yaml: message.default_template -> message.templates.<that name>  (fallback)

    Returns empty string if nothing configured.
    """
    try:
        from boss_hr.config_manager import get_config

        # 1) 优先从岗位 yaml 的 greet_message 字段读取
        if job_name:
            try:
                config = get_config()
                profile = config.load_job_profile(job_name) or {}
                msg = profile.get("greet_message", "")
                if msg:
                    return str(msg)
            except Exception:
                pass

        config = get_config()
        if job_name:
            # 2) message.yaml 岗位专属内联消息
            direct = config.get(f"message.jobs.{job_name}.message", "")
            if direct:
                return direct
            # 3) message.yaml 岗位指定模板名
            template_name = config.get(f"message.jobs.{job_name}.template", "")
            if template_name:
                text = config.get(f"message.templates.{template_name}", "")
                if text:
                    return text
        # 4) 兜底默认模板
        default_name = config.get("message.default_template", "standard")
        return config.get(f"message.templates.{default_name}", "")
    except Exception:
        return ""


def _tail(text: str, *, lines: int = 20) -> list[str]:
    if not text:
        return []
    return text.splitlines()[-lines:]


def _pre_check(job_name: str, eid: str, run_id: str
               ) -> tuple[int, Optional[str]]:
    """前置校验：run 存在 + 岗位匹配。返回 (exit_code, error_msg)。

    与 report / fetch 一致：把旧脚本的裸 FileNotFoundError / RuntimeError
    （都是 rc=1）收敛成统一 CLI 的 23 / 24。

    不校验 confirmed：旧 greet 脚本没有 is_confirmed() 守卫
    （全仓库只有 recommend_list / recommend_download 有），保持一致不加。
    """
    from shared.run_orchestrator import (
        RunOrchestrator,
        EXIT_CODE_RUN_NOT_FOUND,
        EXIT_CODE_RUN_JOB_MISMATCH,
    )
    orch = RunOrchestrator(job_name, encrypt_job_id=eid)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return EXIT_CODE_RUN_NOT_FOUND, f"run_id={run_id} 在岗位目录下不存在"
    except RuntimeError as e:
        return EXIT_CODE_RUN_JOB_MISMATCH, str(e)
    return 0, None


def greet_candidates(*, job_name: str, encrypt_job_id: Optional[str],
                     run_id: Optional[str], only_names: Optional[str] = None,
                     max_count: int = 10,
                     dry_run: bool = False,
                     refresh_page: bool = False) -> CommandResult:
    """greet 命令业务实现。

    流程：
      1) 校验 encrypt_job_id / run_id
      2) 预校验 run 存在 + 岗位匹配（23 / 24）
      3) 调 auto_greet.py 子进程
      4) 失败 → 透传 rc
      5) 成功 → 读 greet_log.json 取 summary（可能不存在 → no_candidates）
      6) 返回 greet_complete
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                message="缺少 encrypt_job_id（传 --encrypt-job-id 或设环境变量 BOSS_HR_ENCRYPT_JOB_ID）",
            ),
            run_id=run_id, exit_code=ExitCode.GENERIC,  # 1
        )

    if not run_id:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_RUN_ID,
                message="缺少 --run-id（run_id 是数据边界，禁止自动选择历史 run）",
            ),
            exit_code=ExitCode.MISSING_RUN_ID,  # 2
        )

    rc, msg = _pre_check(job_name, eid, run_id)
    if rc != 0:
        code = ErrorCode.RUN_NOT_FOUND if rc == 23 else ErrorCode.JOB_MISMATCH
        return error(
            error_obj=UnifiedError(code=code, message=msg or "前置校验失败"),
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode(rc),
        )

    try:
        with BossPageLock(
            command="greet_dry_run" if dry_run else "greet",
            job_name=job_name,
            encrypt_job_id=eid,
            run_id=run_id,
        ):
            sys.stderr.write(
                "[boss_hr] BOSS 页面独占中：正在定位/打招呼，请暂停手动操作 BOSS 网页。\n"
            )
            sys.stderr.flush()
            # v1.1.2: 自动启动 Edge + 登录态
            # v1.1.3: greet 保留 v1.1.2 旧轮询路径（wait_for_user_login=True，
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

            # 构造子脚本参数。
            # --only-names 模式下旧脚本自己会把 max 置名单长度
            # （auto_greet.py），这里只透传原始参数，不重复该逻辑。
            # 不再透传 --threshold：新筛选文件只有 passed 布尔值，无分数概念；
            # auto_greet.py 内部保留旧 score 格式的兜底兼容。
            args_list = [
                "--job-name", job_name,
                "--encrypt-job-id", eid,
                "--run-id", run_id,
                "--max", str(max_count),
            ]
            display_name = _boss_display_name(job_name)
            if display_name:
                args_list += ["--boss-display-name", display_name]
            if only_names:
                args_list += ["--only-names", only_names]
            if dry_run:
                args_list.append("--dry-run")
            if refresh_page:
                args_list.append("--refresh-page")
            # 读取 config/message.yaml 中该岗位配置的自定义打招呼消息
            greet_message = _get_greet_message(job_name)
            if greet_message:
                args_list += ["--message", greet_message]

            # 调子进程前记录 greet_log.json 的 mtime（不存在返回 0）。
            # 子进程走"没有候选人"早退路径时不会写新 greet_log，mtime 不变。
            # 此时不能读旧文件当成本次结果（否则会出现"greeted=2"的陈旧数据）。
            log_path_for_mtime = _greet_log_path(job_name, eid, run_id)
            mtime_before = _greet_log_mtime(log_path_for_mtime)

            result = legacy_runner.run_legacy_cli(
                "auto_greet",
                args_list,
                timeout=1800,  # 招呼逐人节流 3-6s + 扫描，取与 download 同量级
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
                    "打招呼和 dry-run 定位期间不要手动操作 BOSS 网页",
                    "需要人工回复消息时，先等 BOSS 自动化步骤完成",
                ],
            },
        )

    if result.returncode != 0:
        better_msg = try_extract_blocked_message(result.stdout)
        unified = legacy_error(result)
        if better_msg:
            unified = UnifiedError(
                code=unified.code, message=better_msg,
                subprocess_returncode=unified.subprocess_returncode,
            )
        # 验证码退出码 30 → 标记 CAPTCHA，让 daemon 能识别并立即退出
        if result.returncode == 30:
            unified = UnifiedError(
                code=ErrorCode.CAPTCHA, message=better_msg or unified.message,
                subprocess_returncode=unified.subprocess_returncode,
            )
        # 子脚本 rc 可能不在 ExitCode enum 里；用 int 透传避免 ValueError
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=result.returncode,
            data={
                "stdout_tail": _tail(result.stdout),
                "stderr_tail": _tail(result.stderr),
            },
        )

    # 成功：读 greet_log.json
    log_path = _greet_log_path(job_name, eid, run_id)
    mtime_after = _greet_log_mtime(log_path)
    # mtime 没变说明子进程没写新 greet_log（走"没有候选人"早退路径）。
    # 不能读旧文件当成本次结果——那会是上一次跑的陈旧数据（greeted=N）。
    # 当成本次"没有候选人"处理。
    if mtime_after == mtime_before:
        return ok(
            status="no_candidates",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "greeted": 0,
                "clicked_unverified": 0,
                "not_found": 0,
                "total": 0,
                "candidates_targeted": 0,
                "dry_run": dry_run,
                "boss_page_exclusive_required": True,
                "greet_log_file": None,
                "no_candidates": True,
            },
            next_action="done",
        )

    greet_log = _read_greet_log(log_path)

    if greet_log is None:
        # 无高分候选人（旧脚本提前 return，未写日志；run 目录可能已被
        # prune_if_empty 删除）。见 greet-baseline.md §6.3。
        # v1.1.3: status 语义化 → no_candidates；next_action=done。
        return ok(
            status="no_candidates",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "greeted": 0,
                "clicked_unverified": 0,
                "not_found": 0,
                "total": 0,
                "candidates_targeted": 0,
                "dry_run": dry_run,
                "boss_page_exclusive_required": True,
                "greet_log_file": None,
                "no_candidates": True,
            },
            next_action="done",
        )

    summary = greet_log.get("summary") or {}
    results = greet_log.get("results") or []
    greeted = int(summary.get("greeted", 0))
    not_found = int(summary.get("not_found", 0))
    # 顶层 status 决定 CLI 状态语义：
    #
    #   非 dry-run 路径：
    #     - complete        → greet_complete
    #     - partial_success → partial_success（next_action=review_warnings）
    #     - all_not_found   → greet_complete 但 data.partial_success_warnings=True
    #     - no_candidates   → no_candidates
    #
    #   dry-run 路径（v1.1.3 final）：
    #     - no_candidates     → no_candidates（目标列表为空）
    #     - dry_run_complete  → dry_run_complete（全部定位到；不 click）
    #     - dry_run_review    → dry_run_review（部分/全部 not_found；需人工核对）
    top_status = (greet_log.get("status")
                  or summary.get("status")
                  or ("complete" if greeted > 0 and not_found == 0
                      else "partial_success" if greeted > 0 and not_found > 0
                      else "no_candidates"))
    if top_status == "partial_success":
        cli_status = "partial_success"
        next_action = "review_warnings"
    elif top_status == "complete":
        cli_status = "greet_complete"
        next_action = "done"
    elif top_status == "all_not_found":
        cli_status = "greet_complete"
        next_action = "review_warnings"
    elif top_status == "dry_run_complete":
        cli_status = "dry_run_complete"
        next_action = "done"
    elif top_status == "dry_run_review":
        cli_status = "dry_run_review"
        next_action = "review_warnings"
    else:
        cli_status = "no_candidates"
        next_action = "done"
    return ok(
        status=cli_status,
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        data={
            "greeted": greeted,
            "clicked_unverified": int(summary.get("clicked_unverified", 0)),
            "not_found": not_found,
            "total": int(summary.get("total", len(results))),
            "candidates_targeted": len(results),
            "dry_run": dry_run,
            "boss_page_exclusive_required": True,
            "greet_log_file": os.path.abspath(log_path),
            "no_candidates": (cli_status == "no_candidates"),
            "partial_success_warnings": (top_status in ("partial_success", "all_not_found", "dry_run_review")),
            "greet_log_status": top_status,
            "not_found_names": [r.get("name") for r in results
                                 if r.get("status") in ("not_found", "not_found_after_full_scan")],
        },
        next_action=next_action,
    )


__all__ = ["greet_candidates"]
