# -*- coding: utf-8 -*-
"""Audit one run's process JSON artifacts."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


class DuplicateKeyDetector:
    def __init__(self) -> None:
        self.duplicates: list[tuple[str, str]] = []
        self._stack: list[str] = []

    def hook(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                self.duplicates.append((".".join(self._stack), key))
            seen.add(key)
            result[key] = value
        return result


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tools/audit_process_json.py <process-dir>")
        return 2
    process_dir = Path(sys.argv[1])
    if not process_dir.is_dir():
        print(f"process dir not found: {process_dir}")
        return 2

    for path in sorted(process_dir.glob("*.json")):
        detector = DuplicateKeyDetector()
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text, object_pairs_hook=detector.hook)
        except json.JSONDecodeError as exc:
            print(f"{path.name}: INVALID JSON {exc}")
            continue
        print(f"\n{path.name}")
        print(f"  size_bytes: {path.stat().st_size}")
        print(f"  duplicate_keys: {len(detector.duplicates)}")
        if detector.duplicates:
            print(f"  duplicate_samples: {detector.duplicates[:10]}")
        print(f"  shape: {_shape(data)}")
        if isinstance(data, list):
            print(f"  item_count: {len(data)}")
            print(f"  top_level_keys: {_key_counts(data)}")
        elif isinstance(data, dict):
            print(f"  top_level_keys: {sorted(data.keys())}")

    return 0


def _shape(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _key_counts(items: list[Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        if isinstance(item, dict):
            counter.update(item.keys())
    return dict(sorted(counter.items()))


if __name__ == "__main__":
    raise SystemExit(main())
