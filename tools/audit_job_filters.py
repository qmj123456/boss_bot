"""Audit card and BOSS-page filters for every configured job."""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def main() -> None:
    jobs_dir = Path(__file__).resolve().parents[1] / "config" / "jobs"
    rows = []
    for path in sorted(jobs_dir.glob("*.yaml"), key=lambda item: item.name.casefold()):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        recruitment = payload.get("recruitment") or {}
        card = recruitment.get("card_prescreen") or {}
        boss = recruitment.get("boss_filter") or {}
        rows.append(
            {
                "file": path.name,
                "job_name": recruitment.get("job_name"),
                "card_active": card.get("active_status_keywords"),
                "boss_active": boss.get("active_status"),
                "card_max_age": card.get("max_age"),
                "boss_age_max": boss.get("age_max"),
            }
        )
    print(json.dumps(rows, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
