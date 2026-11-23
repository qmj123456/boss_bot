# -*- coding: utf-8 -*-
"""greet_service 防陈旧 greet_log.json 的单元测试。

覆盖 2026-09-06 修复：
  - auto_greet 子进程走"没有候选人"早退路径时不会写新 greet_log.json
  - greet_service 调子进程前记录 mtime，返回后检查 mtime 是否变化
  - mtime 没变 → 当成本次 no_candidates，不读旧文件
  - mtime 变了 → 正常读新 greet_log
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent.parent


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_run(workspace: Path, eid: str, rid: str, jn: str) -> Path:
    """建一个真实 run 目录（job_detail + run.json + screening_results）。"""
    run_dir = workspace / eid / "runs" / rid
    process = run_dir / "process"
    process.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "started_at": "2026-09-06 15:58:34",
        "confirmed": True, "user_confirmed_at": "2026-09-06 15:58:48",
        "steps_done": ["jd", "download"], "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (process / "job_detail.json").write_text(json.dumps({
        "jobName": jn, "encryptJobId": eid,
    }, ensure_ascii=False), encoding="utf-8")
    (process / "screening_results.json").write_text(json.dumps({
        "candidates": [{"name": "甲", "passed": False, "decision": "reject"}],
    }, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _write_old_greet_log(workspace: Path, eid: str, rid: str, jn: str,
                          *, greeted: int = 2) -> Path:
    """写一份"上一次跑"的旧 greet_log.json（模拟历史产物）。"""
    process = workspace / eid / "runs" / rid / "process"
    process.mkdir(parents=True, exist_ok=True)
    path = process / "greet_log.json"
    results = [{"name": f"候选人{i}", "status": "greeted",
                "found": True, "clicked": True, "verified": True}
               for i in range(greeted)]
    path.write_text(json.dumps({
        "job": jn, "run_id": rid, "status": "complete",
        "summary": {"greeted": greeted, "not_found": 0, "total": greeted},
        "results": results,
    }, ensure_ascii=False), encoding="utf-8")
    # 确保 mtime 比当前时间旧（模拟历史文件）
    old_time = time.time() - 3600  # 1 小时前
    os.utime(path, (old_time, old_time))
    return path


def _write_new_greet_log(workspace: Path, eid: str, rid: str, jn: str,
                          *, greeted: int = 1) -> Path:
    """写一份"本次跑"的新 greet_log.json（模拟 auto_greet 刚写的）。"""
    process = workspace / eid / "runs" / rid / "process"
    process.mkdir(parents=True, exist_ok=True)
    path = process / "greet_log.json"
    results = [{"name": f"新人{i}", "status": "greeted",
                "found": True, "clicked": True, "verified": True}
               for i in range(greeted)]
    path.write_text(json.dumps({
        "job": jn, "run_id": rid, "status": "complete",
        "summary": {"greeted": greeted, "not_found": 0, "total": greeted},
        "results": results,
    }, ensure_ascii=False), encoding="utf-8")
    # mtime 是当前时间（模拟刚写）
    return path


def _parse_args(args: list) -> dict:
    out = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--job-name", "--encrypt-job-id", "--run-id", "--max") and i + 1 < len(args):
            out[a.lstrip("-").replace("-", "_")] = args[i + 1]
            i += 2
            continue
        i += 1
    return out


# ============================================================
# 1. 旧 greet_log 存在 + 子进程没写新文件 → 返回 no_candidates
# ============================================================

def test_stale_greet_log_not_read_when_subprocess_skipped(monkeypatch, tmp_path):
    """auto_greet 走"没有候选人"早退，不写新 greet_log → 不应读旧文件。"""
    eid, rid, jn = "test_eid_stale1", "2026-09-06_155834", "stale_job1"
    _make_run(tmp_path, eid, rid, jn)
    # 先写一份旧 greet_log（greeted=2）
    _write_old_greet_log(tmp_path, eid, rid, jn, greeted=2)

    calls = []
    def _fake_no_write(tool, args, *, timeout=60, **kwargs):
        calls.append({"tool": tool, "args": list(args)})
        # 模拟 auto_greet 走"没有候选人"早退：rc=0 但不写新 greet_log
        return _FakeProc(0, "没有通过筛选的候选人，结束\n", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake_no_write)
    from boss_hr.application import greet_service as gs_mod
    monkeypatch.setattr(gs_mod, "ensure_browser_ready", lambda *a, **kw: type("R", (), {"ok": True})())

    result = gs_mod.greet_candidates(
        job_name=jn, encrypt_job_id=eid, run_id=rid, max_count=10, dry_run=False,
    )
    d = result.to_dict("greet")
    assert d["ok"] is True
    assert d["status"] == "no_candidates"
    assert d["data"]["greeted"] == 0
    assert d["data"]["total"] == 0
    assert d["data"]["no_candidates"] is True
    # 关键：没有把旧 greet_log 的 greeted=2 透出来
    assert d["data"]["greet_log_file"] is None


# ============================================================
# 2. 旧 greet_log 存在 + 子进程写了新文件 → 读新文件
# ============================================================

def test_new_greet_log_read_when_subprocess_wrote_new(monkeypatch, tmp_path):
    """auto_greet 写了新 greet_log → 应读新文件（greeted=1），不读旧的。"""
    eid, rid, jn = "test_eid_stale2", "2026-09-06_155834", "stale_job2"
    _make_run(tmp_path, eid, rid, jn)
    # 先写旧 greet_log（greeted=2）
    _write_old_greet_log(tmp_path, eid, rid, jn, greeted=2)

    calls = []
    def _fake_writes_new(tool, args, *, timeout=60, **kwargs):
        calls.append({"tool": tool, "args": list(args)})
        p = _parse_args(list(args))
        # 模拟 auto_greet 写新 greet_log（greeted=1）
        _write_new_greet_log(tmp_path, p["encrypt_job_id"], p["run_id"], p["job_name"], greeted=1)
        return _FakeProc(0, "=== 完成：greeted=1 ===\n", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake_writes_new)
    from boss_hr.application import greet_service as gs_mod
    monkeypatch.setattr(gs_mod, "ensure_browser_ready", lambda *a, **kw: type("R", (), {"ok": True})())

    result = gs_mod.greet_candidates(
        job_name=jn, encrypt_job_id=eid, run_id=rid, max_count=10, dry_run=False,
    )
    d = result.to_dict("greet")
    assert d["ok"] is True
    assert d["status"] == "greet_complete"
    # 关键：读到的是新文件的 greeted=1，不是旧文件的 greeted=2
    assert d["data"]["greeted"] == 1
    assert d["data"]["total"] == 1


# ============================================================
# 3. 没有 greet_log + 子进程没写 → no_candidates
# ============================================================

def test_no_greet_log_and_subprocess_skipped(monkeypatch, tmp_path):
    """从未有过 greet_log + 子进程没写 → no_candidates。"""
    eid, rid, jn = "test_eid_stale3", "2026-09-06_155834", "stale_job3"
    _make_run(tmp_path, eid, rid, jn)
    # 不写旧 greet_log

    def _fake_no_write(tool, args, *, timeout=60, **kwargs):
        return _FakeProc(0, "没有通过筛选的候选人，结束\n", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake_no_write)
    from boss_hr.application import greet_service as gs_mod
    monkeypatch.setattr(gs_mod, "ensure_browser_ready", lambda *a, **kw: type("R", (), {"ok": True})())

    result = gs_mod.greet_candidates(
        job_name=jn, encrypt_job_id=eid, run_id=rid, max_count=10, dry_run=False,
    )
    d = result.to_dict("greet")
    assert d["ok"] is True
    assert d["status"] == "no_candidates"
    assert d["data"]["greeted"] == 0
