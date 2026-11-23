# -*- coding: utf-8 -*-
"""Inspect card prescreen audit files without printing secrets."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boss_hr.config_manager import get_config
from boss_hr.domain.card_prescreen import evaluate_card_prescreen


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tools/inspect_card_prescreen.py <card_prescreen_rejected.json>")
        return 2
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    recruitment = get_config().section("recruitment")
    replayed = [evaluate_card_prescreen((item.get("raw") or item), recruitment) for item in data]
    print("replay_summary:", {
        "total": len(replayed),
        "would_pass": sum(1 for item in replayed if item.passed),
        "would_pending": sum(1 for item in replayed if item.status == "pending_confirmation"),
        "would_reject": sum(1 for item in replayed if item.status == "rejected"),
    })
    for index, item in enumerate(data[:5], 1):
        replay = evaluate_card_prescreen((item.get("raw") or item), recruitment)
        raw = item.get("raw") or {}
        card = raw.get("geekCard") or {}
        print(f"#{index}")
        print("status:", item.get("status"))
        print("reasons:", item.get("reasons"))
        print("missing_fields:", item.get("missing_fields"))
        print("extracted:", item.get("extracted"))
        print("replay:", replay.to_dict())
        print("raw_top_keys:", sorted(raw.keys()))
        print("geekCard_keys:", sorted(card.keys()))
        print("geekCard_sample:", {
            key: card.get(key)
            for key in sorted(card.keys())
            if any(token in key.lower() for token in (
                "active", "online", "status", "age", "degree", "expect",
                "city", "position", "job", "focus", "recent"
            ))
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
