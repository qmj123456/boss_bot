"""卡片拒绝缓存的单元测试：当天去重、跨天失效、过期清理。"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_SHARED = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import reject_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_AGENT_DB_PATH", str(tmp_path / "test.db"))
    reject_cache._ENSURED.clear()
    yield
    reject_cache._ENSURED.clear()


def test_record_and_load_roundtrip():
    reject_cache.record_rejected("亚马逊运营", "gid-1", ["结束时间早于2026.09"])
    reject_cache.record_rejected("亚马逊运营", "gid-2", ["相关经历未满8个月"])

    ids = reject_cache.load_rejected_ids("亚马逊运营")

    assert ids == {"gid-1", "gid-2"}
    assert reject_cache.is_rejected("亚马逊运营", "gid-1") is True
    assert reject_cache.is_rejected("亚马逊运营", "gid-3") is False
    assert reject_cache.is_rejected("初级剪拍", "gid-1") is False


def test_same_day_duplicate_insert_is_ignored():
    reject_cache.record_rejected("运营助理", "gid-1", "专业不符")
    reject_cache.record_rejected("运营助理", "gid-1", "专业不符（重复）")

    with reject_cache._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM card_reject_cache WHERE job = ?", ("运营助理",)
        ).fetchone()[0]
    assert count == 1


def test_cache_expires_next_day():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    reject_cache.record_rejected("高级剪拍", "gid-1", "年龄超过32岁", day=yesterday)

    assert reject_cache.is_rejected("高级剪拍", "gid-1") is False
    assert reject_cache.is_rejected("高级剪拍", "gid-1", day=yesterday) is True


def test_cleanup_removes_old_days_only():
    old = (date.today() - timedelta(days=30)).isoformat()
    fresh = date.today().isoformat()
    reject_cache.record_rejected("初级剪拍", "old", "x", day=old)
    reject_cache.record_rejected("初级剪拍", "fresh", "x", day=fresh)

    removed = reject_cache.cleanup_reject_cache(keep_days=7)

    assert removed >= 1
    assert reject_cache.is_rejected("初级剪拍", "old", day=old) is False
    assert reject_cache.is_rejected("初级剪拍", "fresh", day=fresh) is True


def test_blank_geek_id_is_ignored():
    reject_cache.record_rejected("亚马逊运营", "", ["x"])
    assert reject_cache.load_rejected_ids("亚马逊运营") == set()
