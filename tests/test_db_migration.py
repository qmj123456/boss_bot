# -*- coding: utf-8 -*-
"""database.connection._migrate_existing_sqlite 迁移测试。

重点覆盖 v1.2 修复的崩溃场景：
  旧库（card_score_reason 存在、card_prescreen_reason 不存在）在 additions
  先 ADD 新列后，renames 因 Inspector info_cache 拿到旧列清单误判执行
  RENAME → duplicate column name 崩溃。修复后 rename 先行 + PRAGMA 实时查列。
"""
from sqlalchemy import create_engine, text

from database.connection import _migrate_existing_sqlite
from database.models import Base

# v1.2 新增列：造 v1.1 旧库时先摘掉
_V12_CANDIDATE_COLUMNS = [
    "card_prescreen_reason",
    "card_prescreen_status",
    "card_prescreen_passed",
    "card_prescreen_detail",
]


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return {str(row[1]) for row in rows}


def _make_legacy_db(path, *, with_new_column: bool = False):
    """造 v1.1 评分体系旧库：create_all 建新 schema 后摘掉 v1.2 新列，
    再挂旧评分列。with_new_column=True 时补回 card_prescreen_reason
    （模拟半迁移库：新旧列并存）。"""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in _V12_CANDIDATE_COLUMNS:
            conn.execute(text(f'ALTER TABLE candidate DROP COLUMN "{column}"'))
        conn.execute(text(
            "ALTER TABLE candidate ADD COLUMN card_score REAL NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE candidate ADD COLUMN card_score_reason TEXT NOT NULL DEFAULT ''"
        ))
        if with_new_column:
            conn.execute(text(
                "ALTER TABLE candidate ADD COLUMN card_prescreen_reason TEXT NOT NULL DEFAULT ''"
            ))
    return engine


def test_migrate_renames_old_score_reason(tmp_path):
    """旧库：card_score_reason rename 成 card_prescreen_reason，card_score 删除。"""
    engine = _make_legacy_db(tmp_path / "legacy.db")
    _migrate_existing_sqlite(engine)
    cols = _columns(engine, "candidate")
    assert "card_prescreen_reason" in cols
    assert "card_score_reason" not in cols
    assert "card_score" not in cols
    engine.dispose()


def test_migrate_survives_half_migrated_db(tmp_path):
    """半迁移库（曾崩溃的线上场景）：两列并存 → 跳过 rename，
    旧列转入 drops 清理，不再 duplicate column name。"""
    engine = _make_legacy_db(tmp_path / "half.db", with_new_column=True)
    _migrate_existing_sqlite(engine)  # 修复前这里 raise OperationalError
    cols = _columns(engine, "candidate")
    assert "card_prescreen_reason" in cols
    assert "card_score_reason" not in cols
    assert "card_score" not in cols
    engine.dispose()


def test_migrate_idempotent_on_fresh_db(tmp_path):
    """新库（ORM create_all 产物）：迁移无副作用、可重复执行。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    before = _columns(engine, "candidate")
    _migrate_existing_sqlite(engine)
    _migrate_existing_sqlite(engine)  # 幂等
    assert _columns(engine, "candidate") == before
    engine.dispose()
