# -*- coding: utf-8 -*-
"""boss_hr.commands.sync_jobs — `boss-hr sync-jobs` 命令处理。

登录后从 BOSS 拉当前账号岗位，更新 .env 的 BOSS_JOB_NAME_* / BOSS_JOB_ID_*。
不创建 run，不写业务输出。定时任务（tools/scheduled_boss_run.py）默认在
start 之前自动执行本同步。
"""
from __future__ import annotations
import argparse

from boss_hr.contracts.results import CommandResult
from boss_hr.application.sync_jobs_service import run_sync_jobs


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """sync-jobs 不要求 --job-name / --run-id / --encrypt-job-id。"""
    parser.add_argument(
        "--cdp-url", default="http://localhost:9222",
        help="已登录 BOSS 的 Edge CDP 地址，默认 http://localhost:9222",
    )
    parser.add_argument(
        "--env-file", default=None,
        help="要更新的 .env 路径，默认项目根目录下的 .env。",
    )
    parser.add_argument(
        "--keep-unmatched", action="store_true",
        help="保留未匹配岗位旧值；默认清空，防止沿用其他账号的岗位 ID。",
    )
    parser.add_argument(
        "--no-launch-edge", action="store_true",
        help="CDP 不可达时不自动拉起专用 Edge（默认自动拉起重试一次）。",
    )


def run(args: argparse.Namespace) -> CommandResult:
    return run_sync_jobs(
        cdp_url=str(getattr(args, "cdp_url", "") or "http://localhost:9222"),
        env_file=getattr(args, "env_file", None),
        clear_unmatched=not bool(getattr(args, "keep_unmatched", False)),
        launch_edge=not bool(getattr(args, "no_launch_edge", False)),
    )


__all__ = ["add_arguments", "run"]
