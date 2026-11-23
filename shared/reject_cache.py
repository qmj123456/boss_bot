"""卡片预筛拒绝缓存：当天已扫过、不打开的卡片，下一轮直接跳过。

只缓存「硬性拒绝」（status == rejected），不缓存「待确认」——候选人信息
可能补全，待确认的卡片下次还应重新判断。

存储：SQLite 表 ``card_reject_cache``，默认落在 ``data/hr_agent.db``，
可用环境变量 ``HR_AGENT_DB_PATH`` 覆盖。按天生效，默认保留 7 天。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_TABLE = "card_reject_cache"
_ENSURED: set[str] = set()
_SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "  job TEXT NOT NULL,"
    "  geek_id TEXT NOT NULL,"
    "  reason TEXT,"
    "  day TEXT NOT NULL,"
    "  created_at TEXT NOT NULL,"
    "  PRIMARY KEY (job, geek_id, day)"
    ")",
    f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_day ON {_TABLE} (day)",
)


def _db_path() -> Path:
    override = os.environ.get("HR_AGENT_DB_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "hr_agent.db"


def day_key(value: date | None = None) -> str:
    return (value or date.today()).isoformat()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=5)
    connection.execute("PRAGMA busy_timeout=3000")
    key = str(path)
    if key not in _ENSURED:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.commit()
        _ENSURED.add(key)
    return connection


def load_rejected_ids(job_name: str, day: str | None = None) -> set[str]:
    """返回该岗位当天已拒绝的候选人 ID 集合；失败时返回空集合，不影响主流程。"""
    connection = None
    try:
        connection = _connect()
        rows = connection.execute(
            f"SELECT geek_id FROM {_TABLE} WHERE job = ? AND day = ?",
            (job_name or "", day or day_key()),
        )
        return {str(row[0]) for row in rows if row and row[0]}
    except sqlite3.Error as exc:
        print(f"[reject-cache] 读取失败：{exc}", file=sys.stderr)
        return set()
    finally:
        if connection is not None:
            connection.close()


def is_rejected(job_name: str, geek_id: str, day: str | None = None) -> bool:
    if not geek_id:
        return False
    connection = None
    try:
        connection = _connect()
        row = connection.execute(
            f"SELECT 1 FROM {_TABLE} WHERE job = ? AND geek_id = ? AND day = ?",
            (job_name or "", str(geek_id), day or day_key()),
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        print(f"[reject-cache] 查询失败：{exc}", file=sys.stderr)
        return False
    finally:
        if connection is not None:
            connection.close()


def record_rejected(job_name: str, geek_id: str, reasons=None, day: str | None = None) -> None:
    """记录一条硬性拒绝；同一岗位同一天同一人只记一次。"""
    if not geek_id:
        return
    if isinstance(reasons, (list, tuple)):
        reason = "；".join(str(item) for item in reasons[:3])
    else:
        reason = str(reasons or "")
    connection = None
    try:
        connection = _connect()
        connection.execute(
            f"INSERT OR IGNORE INTO {_TABLE} (job, geek_id, reason, day, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                job_name or "",
                str(geek_id),
                reason[:300],
                day or day_key(),
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        connection.commit()
    except sqlite3.Error as exc:
        print(f"[reject-cache] 写入失败：{exc}", file=sys.stderr)
    finally:
        if connection is not None:
            connection.close()


def cleanup_reject_cache(keep_days: int = 7) -> int:
    """删除超过保留天数的记录，返回删除条数。"""
    cutoff = day_key(date.today() - timedelta(days=max(1, keep_days)))
    connection = None
    try:
        connection = _connect()
        cursor = connection.execute(f"DELETE FROM {_TABLE} WHERE day < ?", (cutoff,))
        connection.commit()
        return cursor.rowcount or 0
    except sqlite3.Error as exc:
        print(f"[reject-cache] 清理失败：{exc}", file=sys.stderr)
        return 0
    finally:
        if connection is not None:
            connection.close()
