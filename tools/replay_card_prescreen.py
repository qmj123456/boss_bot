# -*- coding: utf-8 -*-
"""Replay card prescreen from an existing audit file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boss_hr.config_manager import get_config
from boss_hr.domain.card_prescreen import evaluate_card_prescreen


def main() -> int:
    parser = argparse.ArgumentParser(description="重放卡片预筛并重建 recommend_geek_ids.json")
    parser.add_argument("--process-dir", required=True, help="当前 run 的 process 目录")
    args = parser.parse_args()

    process_dir = Path(args.process_dir)
    rejected_path = process_dir / "card_prescreen_rejected.json"
    accepted_path = process_dir / "recommend_geek_ids.json"
    summary_path = process_dir / "card_prescreen_summary.json"
    if not rejected_path.is_file():
        print(f"找不到审计文件：{rejected_path}")
        return 2

    recruitment = get_config().section("recruitment")
    source = json.loads(rejected_path.read_text(encoding="utf-8"))
    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in source:
        raw = item.get("raw") or item
        result = evaluate_card_prescreen(raw, recruitment)
        raw["_card_prescreen"] = result.to_dict()
        if result.allow_full_resume:
            accepted.append(raw)
        else:
            rejected.append({
                "encryptGeekId": raw.get("encryptGeekId", ""),
                "name": (raw.get("geekCard") or {}).get("geekName", "") or raw.get("name", ""),
                "status": result.status,
                "reasons": result.reasons,
                "missing_fields": result.missing_fields,
                "extracted": result.extracted,
                "raw": raw,
            })

    accepted_path.write_text(json.dumps(accepted, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected_path.write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    pending_proceeded = sum(
        1
        for item in accepted
        if (item.get("_card_prescreen") or {}).get("status") == "pending_confirmation"
    )
    pending_blocked = sum(
        1 for item in rejected if item.get("status") == "pending_confirmation"
    )
    summary = {
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "pending_confirmation_count": pending_proceeded + pending_blocked,
        "pending_proceeded_count": pending_proceeded,
        "pending_blocked_count": pending_blocked,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
