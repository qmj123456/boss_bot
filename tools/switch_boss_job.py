#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Switch the current BOSS recommend page to the configured job."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
for path in (ROOT, SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from boss_job_switcher import ensure_recommend_job_selected, get_or_create_recommend_page  # noqa: E402
from boss_page_lock import BossPageLock, BossPageLockBusy  # noqa: E402
from boss_hr.config_manager import get_config  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402


def _default_job_name() -> str | None:
    try:
        return get_config().default_job_profile_name()
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只切换 BOSS 推荐牛人页岗位，不抓取、不打招呼。",
    )
    parser.add_argument("--encrypt-job-id", default=None, help="默认读 BOSS_HR_ENCRYPT_JOB_ID")
    parser.add_argument("--job-name", default=None, help="本地岗位名兜底，如 全职主播")
    args = parser.parse_args(argv)

    eid = args.encrypt_job_id or os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")
    if not eid:
        raise SystemExit("缺少 --encrypt-job-id 或 env BOSS_HR_ENCRYPT_JOB_ID")

    job_name = args.job_name or _default_job_name()
    try:
        with BossPageLock(command="switch_job", job_name=job_name or "", encrypt_job_id=eid):
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://localhost:9222")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = get_or_create_recommend_page(ctx, log=print)
                result = ensure_recommend_job_selected(
                    page,
                    encrypt_job_id=eid,
                    job_name=job_name,
                    log=print,
                )
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result.get("ok") else 1
    except BossPageLockBusy as exc:
        print(json.dumps({
            "ok": False,
            "reason": "boss_page_busy",
            "message": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
