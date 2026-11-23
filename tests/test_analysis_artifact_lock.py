"""Real OS-lock tests; no API, browser, or production artifact access."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shared.analysis_artifact_lock import AnalysisArtifactLockError, RunArtifactLock


ROOT = Path(__file__).resolve().parents[1]


def test_exclusive_lock_is_nonblocking_and_context_releases_it(tmp_path):
    owner = RunArtifactLock(tmp_path / "process")
    contender = RunArtifactLock(tmp_path / "process")
    with owner:
        assert owner.locked
        assert owner.acquire() is owner
        started = time.monotonic()
        with pytest.raises(AnalysisArtifactLockError) as captured:
            contender.acquire()
        assert time.monotonic() - started < 1
        assert captured.value.code == "analysis_artifact_locked"
        assert captured.value.path == owner.path
        assert not contender.locked
    assert not owner.locked
    assert owner.path.is_file()
    with contender:
        assert contender.locked
    contender.close()
    owner.close()


def test_context_exception_releases_lock_and_existing_file_is_not_stale_lock(tmp_path):
    lock = RunArtifactLock(tmp_path)
    lock.path.write_bytes(b"old sentinel from an earlier run")
    with pytest.raises(ValueError, match="test failure"):
        with lock:
            raise ValueError("test failure")
    with RunArtifactLock(tmp_path) as following:
        assert following.locked
    assert lock.path.read_bytes() == b"old sentinel from an earlier run"


def test_different_runs_have_independent_locks_and_same_path_alias_contends(tmp_path):
    first = tmp_path / "one" / "process"
    second = tmp_path / "two" / "process"
    with RunArtifactLock(first), RunArtifactLock(second):
        with pytest.raises(AnalysisArtifactLockError):
            RunArtifactLock(first / ".." / "process").acquire()


def test_subprocess_cannot_acquire_parent_lock_but_can_after_release(tmp_path):
    code = """
import sys
from shared.analysis_artifact_lock import AnalysisArtifactLockError, RunArtifactLock
try:
    with RunArtifactLock(sys.argv[1]):
        print('acquired')
except AnalysisArtifactLockError:
    print('locked')
    sys.exit(23)
"""
    with RunArtifactLock(tmp_path):
        held = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
            text=True, capture_output=True, timeout=10,
        )
    assert held.returncode == 23, held.stderr
    assert held.stdout.strip() == "locked"
    released = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
        text=True, capture_output=True, timeout=10,
    )
    assert released.returncode == 0, released.stderr
    assert released.stdout.strip() == "acquired"


def test_killed_owner_releases_os_lock_without_removing_persistent_file(tmp_path):
    ready = tmp_path / "owner-ready"
    code = """
import pathlib
import sys
import time
from shared.analysis_artifact_lock import RunArtifactLock
lock = RunArtifactLock(sys.argv[1]).acquire()
pathlib.Path(sys.argv[2]).write_text('ready')
time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(ready)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "Child process did not acquire its artifact lock"
        with pytest.raises(AnalysisArtifactLockError):
            RunArtifactLock(tmp_path).acquire()
        child.kill()
        child.wait(timeout=5)
        with RunArtifactLock(tmp_path) as following:
            assert following.locked
            assert following.path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stderr is not None:
            child.stderr.close()


def test_lock_descriptor_is_not_inheritable(tmp_path):
    with RunArtifactLock(tmp_path) as owner:
        assert os.get_inheritable(owner._fd) is False
