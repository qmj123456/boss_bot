# -*- coding: utf-8 -*-
"""RunOrchestrator.record_stage_timing / get_stage_timings 单元测试。

覆盖：
1. record_stage_timing 把阶段时间写入 run.json 的 stage_timings 字段。
2. duration_seconds 正确计算（同分钟内 = 0，跨分钟 = 正数）。
3. extra 字段被合并进 entry。
4. 同一阶段多次调用会覆盖上一次的 entry。
5. get_stage_timings 在没有 stage_timings 时返回空字典。
6. 空入参安全跳过（不抛异常、不写文件）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# shared/ 不是 package，需要手动加入 sys.path（与 test_boss_page_lock.py 同义）。
_SHARED = Path(__file__).resolve().parent.parent.parent / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from run_orchestrator import RunOrchestrator, _seconds_between  # noqa: E402


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """创建一个空 run 目录，返回路径。"""
    d = tmp_path / "runs" / "2026-09-06_120000"
    d.mkdir(parents=True)
    return d


def _make_orchestrator(run_root: Path) -> RunOrchestrator:
    """构造一个 RunOrchestrator，runs_dir 指向临时目录。

    用 monkeypatch 替换 _mgr_lazy 让 _ensure_run_dir_state 计算到临时目录。
    """
    orch = RunOrchestrator.__new__(RunOrchestrator)
    orch.job_name = "test_job"
    orch.encrypt_job_id = "test_eid"
    orch._mgr = None
    # 替换 _mgr_lazy 让 runs_dir 返回我们的临时目录
    class _FakeMgr:
        runs_dir = str(run_root.parent)  # 父目录，因为 _ensure_run_dir_state 会拼 run_id
    orch._mgr = _FakeMgr()
    return orch


def test_record_stage_timing_writes_stage_timings_field(tmp_path: Path) -> None:
    """1. record_stage_timing 写入 stage_timings 字段。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    run_json = run_root / "run.json"
    run_json.write_text(json.dumps({"run_id": "x", "started_at": "2026-09-06 12:00:00"}), encoding="utf-8")

    orch = _make_orchestrator(run_root)
    orch.record_stage_timing(
        run_id="2026-09-06_120000",
        stage="fetch",
        started_at="2026-09-06 12:00:10",
        finished_at="2026-09-06 12:05:30",
        extra={"downloaded": 21, "failed": 0},
    )

    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert "stage_timings" in state
    fetch = state["stage_timings"]["fetch"]
    assert fetch["started_at"] == "2026-09-06 12:00:10"
    assert fetch["finished_at"] == "2026-09-06 12:05:30"
    assert fetch["duration_seconds"] == 320
    assert fetch["downloaded"] == 21
    assert fetch["failed"] == 0


def test_record_stage_timing_duration_seconds_correct(tmp_path: Path) -> None:
    """2. duration_seconds 正确计算。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    (run_root / "run.json").write_text("{}", encoding="utf-8")

    orch = _make_orchestrator(run_root)
    # 同分钟内 → 0
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="analysis",
        started_at="2026-09-06 12:30:10", finished_at="2026-09-06 12:30:20",
    )
    state = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    assert state["stage_timings"]["analysis"]["duration_seconds"] == 10

    # 跨分钟
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="greet",
        started_at="2026-09-06 12:30:10", finished_at="2026-09-06 12:35:45",
    )
    state = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    assert state["stage_timings"]["greet"]["duration_seconds"] == 335


def test_record_stage_timing_extra_fields_merged(tmp_path: Path) -> None:
    """3. extra 字段被合并进 entry。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    (run_root / "run.json").write_text("{}", encoding="utf-8")

    orch = _make_orchestrator(run_root)
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="greet",
        started_at="2026-09-06 12:00:00", finished_at="2026-09-06 12:01:00",
        extra={"greeted": 5, "not_found": 1, "dry_run": False},
    )
    state = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    greet = state["stage_timings"]["greet"]
    assert greet["greeted"] == 5
    assert greet["not_found"] == 1
    assert greet["dry_run"] is False


def test_record_stage_timing_overwrites_previous_entry(tmp_path: Path) -> None:
    """4. 同一阶段多次调用会覆盖上一次的 entry。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    (run_root / "run.json").write_text("{}", encoding="utf-8")

    orch = _make_orchestrator(run_root)
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="fetch",
        started_at="2026-09-06 12:00:00", finished_at="2026-09-06 12:05:00",
        extra={"downloaded": 5},
    )
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="fetch",
        started_at="2026-09-06 12:00:00", finished_at="2026-09-06 12:10:00",
        extra={"downloaded": 21},
    )
    state = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    fetch = state["stage_timings"]["fetch"]
    assert fetch["duration_seconds"] == 600
    assert fetch["downloaded"] == 21


def test_get_stage_timings_returns_empty_when_missing(tmp_path: Path) -> None:
    """5. get_stage_timings 在没有 stage_timings 时返回空字典。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    (run_root / "run.json").write_text("{}", encoding="utf-8")

    orch = _make_orchestrator(run_root)
    assert orch.get_stage_timings("2026-09-06_120000") == {}


def test_record_stage_timing_skips_empty_inputs(tmp_path: Path) -> None:
    """6. 空入参安全跳过（不抛异常、不写文件）。"""
    run_root = tmp_path / "runs" / "2026-09-06_120000"
    run_root.mkdir(parents=True)
    run_json = run_root / "run.json"
    run_json.write_text("{}", encoding="utf-8")

    orch = _make_orchestrator(run_root)
    # 空 run_id
    orch.record_stage_timing(
        run_id="", stage="fetch",
        started_at="2026-09-06 12:00:00", finished_at="2026-09-06 12:01:00",
    )
    # 空 stage
    orch.record_stage_timing(
        run_id="2026-09-06_120000", stage="",
        started_at="2026-09-06 12:00:00", finished_at="2026-09-06 12:01:00",
    )
    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert "stage_timings" not in state


def test_seconds_between_handles_invalid_inputs() -> None:
    """_seconds_between 解析失败返回 0，反向时间也返回 0。"""
    assert _seconds_between("invalid", "2026-09-06 12:00:00") == 0
    assert _seconds_between("2026-09-06 12:00:00", "invalid") == 0
    # 反向时间（结束早于开始）→ 0，不返回负数
    assert _seconds_between("2026-09-06 12:05:00", "2026-09-06 12:00:00") == 0
    # 正常情况
    assert _seconds_between("2026-09-06 12:00:00", "2026-09-06 12:05:30") == 330
