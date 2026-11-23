# -*- coding: utf-8 -*-
"""BossPageLock 自动清理旧 run 僵尸进程的单元测试。

覆盖三个分支：
1. 同一 run_id 多次获取锁 → 不 kill，直接报 BUSY（fetch→download→greet 阶段切换）。
2. 锁被另一个 run_id 的存活进程占用 → 主动 kill 进程树 + 删锁 + 重新获取。
3. owner pid 已死 → 走 _is_stale 分支，不触发 kill。
4. 新 run_id 为空时不主动 kill（避免误伤）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# shared/ 不是 package，需要手动加入 sys.path（与 test_fetch.py 子进程
# 用 PYTHONPATH=str(_SHARED) 同义）。
_SHARED = Path(__file__).resolve().parent.parent.parent / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import boss_page_lock as bpl_mod  # noqa: E402


def _write_lock(lock_path: Path, *, pid: int, run_id: str, command: str = "fetch") -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "command": command,
        "job_name": "test-job",
        "encrypt_job_id": "eid-test",
        "run_id": run_id,
        "started_at": "2026-09-06 16:00:00",
    }
    lock_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_same_run_id_does_not_kill(tmp_path):
    """同一 run_id 内多次获取锁（阶段切换）不应触发 kill，应直接报 BUSY。"""
    lock_path = tmp_path / "boss_page.lock"
    # 用当前进程 pid 占锁，run_id 与后续相同
    _write_lock(lock_path, pid=os.getpid(), run_id="run-A")

    kill_calls: list[int] = []

    def fake_kill(pid: int) -> None:
        kill_calls.append(pid)

    with patch.object(bpl_mod, "_kill_process_tree", side_effect=fake_kill):
        with pytest.raises(bpl_mod.BossPageLockBusy):
            with bpl_mod.BossPageLock(
                command="download",
                run_id="run-A",
                lock_path=lock_path,
            ):
                pass

    assert kill_calls == [], "同一 run_id 不应触发 kill"


def test_different_run_id_kills_process_tree_and_reacquires(tmp_path):
    """锁被另一个 run_id 的存活进程占用 → kill 进程树 + 删锁 + 重新获取。"""
    lock_path = tmp_path / "boss_page.lock"
    # 旧 run 的锁，pid 用当前进程（保证 _pid_alive_windows 返回 True）
    _write_lock(lock_path, pid=os.getpid(), run_id="run-OLD")

    kill_calls: list[int] = []

    def fake_kill(pid: int) -> None:
        kill_calls.append(pid)

    with patch.object(bpl_mod, "_kill_process_tree", side_effect=fake_kill):
        with bpl_mod.BossPageLock(
            command="fetch",
            run_id="run-NEW",
            lock_path=lock_path,
        ) as lock:
            assert lock is not None
            # kill 被调用一次，参数是旧 pid
            assert len(kill_calls) == 1
            assert kill_calls[0] == os.getpid()
            # 锁已被当前 run 重新写入（owner.run_id == run-NEW），
            # 必须在 with 块内读，__exit__ 会删锁文件。
            new_owner = json.loads(lock_path.read_text(encoding="utf-8"))
            assert new_owner["run_id"] == "run-NEW"
            assert new_owner["command"] == "fetch"


def test_dead_owner_goes_through_stale_not_kill(tmp_path):
    """owner pid 已死 → 走 _is_stale 分支删锁，不触发 kill。"""
    lock_path = tmp_path / "boss_page.lock"
    # 用一个不存在的 pid（Windows 上 _pid_alive_windows 会返回 False）
    _write_lock(lock_path, pid=99999999, run_id="run-OLD")

    kill_calls: list[int] = []

    def fake_kill(pid: int) -> None:
        kill_calls.append(pid)

    with patch.object(bpl_mod, "_kill_process_tree", side_effect=fake_kill):
        with bpl_mod.BossPageLock(
            command="fetch",
            run_id="run-NEW",
            lock_path=lock_path,
        ) as lock:
            assert lock is not None

    # 走 stale 分支，没调 kill
    assert kill_calls == [], "死锁应走 _is_stale，不应触发 kill"


def test_corrupted_lock_is_reclaimed_after_ttl(tmp_path):
    """锁文件被写坏（无 pid 的空白内容）且已过期 → 应自动接管，而不是永久 BUSY。"""
    lock_path = tmp_path / "boss_page.lock"
    lock_path.write_text(" " * 180, encoding="utf-8")
    old = time.time() - bpl_mod.STALE_EMPTY_LOCK_SECONDS - 30
    os.utime(lock_path, (old, old))

    with bpl_mod.BossPageLock(command="fetch", run_id="run-NEW", lock_path=lock_path) as lock:
        assert lock is not None
        new_owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert new_owner["run_id"] == "run-NEW"


def test_fresh_corrupted_lock_still_busy(tmp_path):
    """刚创建的空锁可能仍在写入，60 秒内保持 BUSY，避免两个进程同时抢。"""
    lock_path = tmp_path / "boss_page.lock"
    lock_path.write_text(" " * 180, encoding="utf-8")

    with pytest.raises(bpl_mod.BossPageLockBusy):
        with bpl_mod.BossPageLock(command="fetch", run_id="run-NEW", lock_path=lock_path):
            pass


def test_empty_current_run_id_does_not_kill(tmp_path):
    """新 run_id 为空时不主动 kill，避免误伤（走 BUSY）。"""
    lock_path = tmp_path / "boss_page.lock"
    _write_lock(lock_path, pid=os.getpid(), run_id="run-OLD")

    kill_calls: list[int] = []

    def fake_kill(pid: int) -> None:
        kill_calls.append(pid)

    with patch.object(bpl_mod, "_kill_process_tree", side_effect=fake_kill):
        with pytest.raises(bpl_mod.BossPageLockBusy):
            with bpl_mod.BossPageLock(
                command="fetch",
                run_id="",  # 空 run_id
                lock_path=lock_path,
            ):
                pass

    assert kill_calls == [], "空 run_id 不应触发 kill"
