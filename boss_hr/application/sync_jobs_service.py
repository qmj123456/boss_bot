# -*- coding: utf-8 -*-
"""boss_hr.application.sync_jobs_service — `boss-hr sync-jobs` 业务逻辑。

登录后从 BOSS 推荐页账号拉取全部岗位，按 config/jobs/*.yaml 的
boss_display_name / boss_match_names 精确匹配，把真实岗位名 + encryptJobId
写回 .env，并把更新同步到当前进程 os.environ（load_project_dotenv 是
setdefault 语义，进程内不会自动刷新）。

boss-hr sync-jobs 命令与 tools/scheduled_boss_run.py 定时任务共用本服务：
定时任务先 sync 再 start→confirm→automate，保证用最新岗位信息执行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
for _path in (str(_TOOLKIT_ROOT), str(_TOOLKIT_ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from boss_hr.contracts.errors import ErrorCode, ExitCode, UnifiedError
from boss_hr.contracts.results import CommandResult, error, ok

# list_jobs 错误码 → 统一错误码
_ERROR_CODE_MAP = {
    "CDP_UNREACHABLE": ErrorCode.CDP_NOT_RUNNING,
    "AUTH_REQUIRED": ErrorCode.BOSS_LOGIN_REQUIRED,
    "BOSS_HTTP_ERROR": ErrorCode.INTERNAL,
    "BOSS_API_ERROR": ErrorCode.INTERNAL,
    "FETCH_ERROR": ErrorCode.INTERNAL,
    "PARSE_ERROR": ErrorCode.INTERNAL,
}


def run_sync_jobs(
    *,
    cdp_url: str = "http://localhost:9222",
    env_file: str | None = None,
    update_env: bool = True,
    clear_unmatched: bool = True,
    launch_edge: bool = True,
) -> CommandResult:
    """同步 BOSS 岗位信息到 .env 并刷新当前进程环境变量。

    失败（ok=False）：CDP 不可达（拉起 Edge 重试后仍失败）/ 未登录 /
    BOSS 接口异常 / 配置校验错误。
    部分岗位未匹配不算硬失败：未匹配岗位在 .env 中被清空，调用方
    （定时任务）按岗位逐个跳过。
    """
    from resolve_boss_job_ids import run_sync

    payload = run_sync(
        cdp_url=cdp_url,
        env_file=env_file,
        update_env=update_env,
        clear_unmatched=clear_unmatched,
    )

    # CDP 不可达 → 尝试拉起专用 Edge 重试一次（定时任务无人值守场景）。
    err = payload.get("error") or {}
    if err.get("code") == "CDP_UNREACHABLE" and launch_edge:
        from boss_hr.application.doctor_service import launch_edge

        launch = launch_edge()
        if launch.get("ok"):
            payload = run_sync(
                cdp_url=cdp_url,
                env_file=env_file,
                update_env=update_env,
                clear_unmatched=clear_unmatched,
            )
            err = payload.get("error") or {}

    if err and not payload.get("resolved"):
        code_str = str(err.get("code") or "INTERNAL")
        code = _ERROR_CODE_MAP.get(code_str, ErrorCode.INTERNAL)
        message = str(err.get("message") or code_str)
        return error(
            error_obj=UnifiedError(code=code, message=message, recoverable=True),
            exit_code=ExitCode.GENERIC,
            next_action="doctor" if code is ErrorCode.CDP_NOT_RUNNING else "login_boss",
            remediation={
                "command": "boss-hr doctor",
                "instructions": [
                    "启动专用 Edge（boss-hr doctor --launch-edge）",
                    "在该 Edge 中登录 BOSS 招聘者后台",
                    "重新运行 boss-hr sync-jobs",
                ],
            },
        )

    # 配置校验错误（飞书分表绑定缺失 / job_name 不一致等）→ 硬失败。
    validation = payload.get("validation") or {}
    if validation.get("errors"):
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message="岗位绑定配置校验失败：" + "；".join(validation["errors"]),
                recoverable=True,
            ),
            exit_code=ExitCode.GENERIC,
            next_action="fix_job_bindings",
            data={"validation": validation},
        )

    # 刷新当前进程环境变量：后续 resolve_encrypt_job_id / 子进程继承新值。
    env_updates = payload.get("env_updates") or {}
    if update_env:
        for key, value in env_updates.items():
            os.environ[key] = str(value)

    resolved = payload.get("resolved") or []
    matched = [
        {
            "job_name": item["profile_name"],
            "boss_job_name": item["boss_job_name"],
            "encrypt_job_id": item["encrypt_job_id"],
        }
        for item in resolved if item.get("matched")
    ]
    unmatched = [
        {
            "job_name": item["profile_name"],
            "reason": item.get("match_error") or "未匹配",
        }
        for item in resolved if not item.get("matched")
    ]
    warnings = list(validation.get("warnings") or [])
    if unmatched:
        warnings.append(
            "BOSS 账号未匹配岗位（.env 已清空对应值，相关任务会跳过）："
            + "、".join(item["job_name"] for item in unmatched)
        )
    return ok(
        status="synced",
        data={
            "env_updated": bool(payload.get("env_updated")),
            "env_file": payload.get("env_file"),
            "matched": matched,
            "unmatched": unmatched,
            "warnings": warnings,
        },
        next_action="start",
    )


__all__ = ["run_sync_jobs"]
