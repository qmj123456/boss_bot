# -*- coding: utf-8 -*-
"""Local exclusive lock for BOSS web-page automation.

The BOSS recruiter site is effectively a single interactive surface for this
project. Any step that scrolls, switches jobs, downloads resumes through the
page session, or clicks greet buttons should own this lock.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BossPageLockBusy(RuntimeError):
    """Raised when another process already owns the BOSS page lock."""

    def __init__(self, lock_path: Path, owner: dict[str, Any]) -> None:
        self.lock_path = lock_path
        self.owner = owner
        super().__init__(self._message())

    def _message(self) -> str:
        owner_text = ""
        if self.owner:
            parts = []
            if self.owner.get("pid"):
                parts.append(f"pid={self.owner['pid']}")
            if self.owner.get("command"):
                parts.append(f"command={self.owner['command']}")
            if self.owner.get("run_id"):
                parts.append(f"run_id={self.owner['run_id']}")
            if parts:
                owner_text = "，当前占用：" + " ".join(parts)
        return (
            "BOSS 页面正在被另一个程序流程占用，请等它结束后再重试。"
            "为避免和人工回复/其他脚本抢同一个 BOSS 页面，本步骤需要独占。"
            f"{owner_text}"
        )


class BossPageLock:
    """Context manager backed by an exclusive local lock file."""

    def __init__(
        self,
        *,
        command: str,
        job_name: str = "",
        encrypt_job_id: str = "",
        run_id: str = "",
        lock_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.command = command
        self.job_name = job_name
        self.encrypt_job_id = encrypt_job_id
        self.run_id = run_id
        self.lock_path = Path(lock_path) if lock_path else _default_lock_path()
        self._fd: int | None = None

    def __enter__(self) -> "BossPageLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self._fd = os.open(str(self.lock_path), flags)
        except FileExistsError:
            owner = _read_owner(self.lock_path)
            if _is_stale(owner, self.lock_path):
                _remove_stale(self.lock_path)
                self._fd = os.open(str(self.lock_path), flags)
            elif _is_orphaned_by_other_run(owner, self.run_id):
                # 锁被另一个 run_id 的进程占用且仍存活 → 主动 kill 进程树释放锁。
                # 场景：上一个 automate 异常退出后，派生的 recommend_list/download
                # 子进程没被回收，继续占用 BOSS 页面锁，新 run 无法启动。
                # 同一 run_id 内多次获取锁（fetch→download→greet 阶段切换）不 kill。
                old_pid = int(owner.get("pid") or 0)
                old_run = owner.get("run_id") or ""
                logger.warning(
                    "检测到旧 run 的 BOSS 页面锁仍被占用（pid=%s, run_id=%s, "
                    "command=%s），主动 kill 进程树以释放锁",
                    old_pid, old_run, owner.get("command") or "",
                )
                _kill_process_tree(old_pid)
                _remove_stale(self.lock_path)
                self._fd = os.open(str(self.lock_path), flags)
            else:
                raise BossPageLockBusy(self.lock_path, owner) from None

        payload = {
            "pid": os.getpid(),
            "command": self.command,
            "job_name": self.job_name,
            "encrypt_job_id": self.encrypt_job_id,
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        os.write(self._fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        os.close(self._fd)
        self._fd = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError:
            pass


def _default_lock_path() -> Path:
    output_root = os.environ.get("BOSS_HR_OUTPUT_DIR")
    if output_root:
        return Path(output_root).expanduser().resolve() / ".locks" / "boss_page.lock"
    try:
        from boss_hr.config_manager import get_config

        root = get_config().paths.runtime_dir
    except Exception:
        root = Path.cwd()
    return Path(root) / "data" / "boss_page.lock"


def _read_owner(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


STALE_EMPTY_LOCK_SECONDS = 60


def _is_stale(owner: dict[str, Any], path: Path | None = None) -> bool:
    pid = owner.get("pid")
    if not pid:
        # 空文件/损坏 JSON（进程被强杀时正写到一半）没有 pid，旧逻辑会永久 BUSY。
        # 刚创建的可能仍在写入，给 60 秒宽限后再当作残留锁清理。
        if path is None:
            return False
        try:
            return (time.time() - path.stat().st_mtime) > STALE_EMPTY_LOCK_SECONDS
        except OSError:
            return False
    if os.name == "nt":
        return not _pid_alive_windows(int(pid))
    try:
        os.kill(int(pid), 0)
    except OSError:
        return True
    except Exception:
        return False
    return False


def _is_orphaned_by_other_run(owner: dict[str, Any], current_run_id: str) -> bool:
    """锁被另一个 run_id 的进程占用且仍存活，视为孤儿锁，可主动清理。

    判定条件（全部满足才返回 True）：
    1. owner 有 pid 且该 pid 仍存活（否则走 _is_stale 死锁分支即可）。
    2. owner.run_id 与当前 run_id 不同（同一 run 内多次获取锁不视为孤儿，
       例如 fetch→download→greet 阶段切换）。
    3. current_run_id 非空（首次启动无 run_id 时不主动 kill，避免误伤）。
    """
    pid = owner.get("pid")
    if not pid:
        return False
    if not current_run_id:
        return False
    owner_run = owner.get("run_id") or ""
    if not owner_run or owner_run == current_run_id:
        return False
    if os.name == "nt":
        return _pid_alive_windows(int(pid))
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _kill_process_tree(pid: int) -> None:
    """杀掉指定 pid 及其所有子进程。

    Windows 用 taskkill /F /T 强制结束进程树（覆盖 fetch 派生的
    recommend_download 子进程）；POSIX 用 killpg 发 SIGTERM 给整个进程组。
    """
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("taskkill pid=%s 失败: %s", pid, exc)
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except Exception:
        try:
            os.kill(pid, 15)
        except Exception as exc:
            logger.warning("kill pid=%s 失败: %s", pid, exc)


def _pid_alive_windows(pid: int) -> bool:
    """Windows 进程存活探测。

    禁止用 os.kill(pid, 0)：在 Windows 上 sig=0 等于 CTRL_C_EVENT，
    会真的向目标进程发 Ctrl+C（其他 sig 则走 TerminateProcess），
    曾因此把持有锁的进程（包括 pytest 自己）杀掉。
    改用 OpenProcess + GetExitCodeProcess 判断。
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # ERROR_INVALID_PARAMETER(87) = 进程不存在；其他错误（如权限不足）
        # 视为进程仍在，避免误删活锁。
        return kernel32.GetLastError() != 87
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _remove_stale(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = ["BossPageLock", "BossPageLockBusy"]
