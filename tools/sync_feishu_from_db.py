#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sync existing SQLite candidates to Feishu Bitable.

Default mode is preview-only. Pass --yes to create/update Feishu records.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import or_, select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHARED = ROOT / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from shared.env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from boss_hr.config_manager import ConfigManager  # noqa: E402
from database.connection import Database  # noqa: E402
from database.models import Candidate, CandidateAnalysis, GreetingRecord  # noqa: E402
from feishu.sync_service import FeishuSyncService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or sync existing SQLite candidate data to Feishu Bitable.",
    )
    parser.add_argument("--job", default=None, help="只同步指定岗位")
    parser.add_argument("--run-id", default=None, help="只同步指定 run 的候选人")
    parser.add_argument(
        "--greeted-only",
        action="store_true",
        help="只同步已有成功打招呼记录的候选人",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认真实写入飞书；不加时只预览数量",
    )
    return parser


def count_candidates(database: Database, *, job: str | None, greeted_only: bool,
                     run_id: str | None) -> int:
    with database.session() as session:
        if greeted_only:
            stmt = (
                select(Candidate.id)
                .join(GreetingRecord, GreetingRecord.candidate_id == Candidate.id)
                .where(GreetingRecord.status.in_(("greeted", "sent", "success")))
                .distinct()
            )
            if run_id:
                stmt = stmt.where(GreetingRecord.run_id == run_id)
        else:
            stmt = select(Candidate.id).distinct()
            if run_id:
                stmt = (
                    stmt
                    .outerjoin(CandidateAnalysis, CandidateAnalysis.candidate_id == Candidate.id)
                    .outerjoin(GreetingRecord, GreetingRecord.candidate_id == Candidate.id)
                    .where(or_(
                        Candidate.run_id == run_id,
                        CandidateAnalysis.run_id == run_id,
                        GreetingRecord.run_id == run_id,
                    ))
                )
        if job:
            stmt = stmt.where(Candidate.job == job)
        return len(list(session.execute(stmt).scalars()))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = ConfigManager()
    database = Database(cfg)
    database.init()

    total = count_candidates(
        database,
        job=args.job,
        greeted_only=args.greeted_only,
        run_id=args.run_id,
    )
    preview = {
        "ok": True,
        "mode": "preview" if not args.yes else "sync",
        "database": str(cfg.get_path("app.database.path")),
        "job": args.job,
        "run_id": args.run_id,
        "greeted_only": args.greeted_only,
        "candidate_count": total,
        "will_write_feishu": bool(args.yes),
    }
    if not args.yes:
        print(json.dumps(preview, ensure_ascii=False))
        return 0

    service = FeishuSyncService(config=cfg, database=database)
    if args.greeted_only:
        stats = service.sync_greeted_candidates(job=args.job, run_id=args.run_id)
    elif args.run_id:
        stats = service.sync_run_candidates(job=args.job, run_id=args.run_id)
    else:
        stats = service.sync_all_candidates(job=args.job)
    preview["sync_stats"] = stats
    print(json.dumps(preview, ensure_ascii=False))
    return 0 if stats.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
