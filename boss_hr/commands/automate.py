"""``boss-hr automate`` 端到端命令。"""
from __future__ import annotations

import argparse

from boss_hr.application.automation_service import run_automation
from boss_hr.commands._argparse_helpers import add_required_arguments, require_encrypt_job_id
from boss_hr.contracts.results import CommandResult


def add_arguments(parser: argparse.ArgumentParser) -> None:
    add_required_arguments(parser)
    parser.add_argument("--count", type=int, default=10, help="本次最多抓取的候选人数")
    parser.add_argument("--max-greetings", type=int, default=None, help="本次最多打招呼人数")
    parser.add_argument("--requirements-file", default=None, help="独立岗位要求文件（JSON/YAML/Markdown）")
    parser.add_argument("--dry-run", action="store_true", help="只定位候选人，不真实点击、不同步飞书")
    parser.add_argument("--skip-fetch", action="store_true", help="复用当前 run 已有抓取产物，直接进入 AI 分析")
    parser.add_argument("--skip-feishu", action="store_true", help="完成本地流程但跳过飞书同步")


def run(args: argparse.Namespace) -> CommandResult:
    eid = require_encrypt_job_id(args._parser, args)
    return run_automation(
        job_name=args.job_name,
        encrypt_job_id=eid,
        run_id=args.run_id,
        count=args.count,
        max_greetings=args.max_greetings,
        dry_run=args.dry_run,
        sync_feishu=not args.skip_feishu,
        skip_fetch=args.skip_fetch,
        requirements_file=args.requirements_file,
    )


__all__ = ["add_arguments", "run"]
