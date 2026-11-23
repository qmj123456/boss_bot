"""Nonblocking, process-safe ownership of a run's analysis/greeting artifacts.

All writers for one run lock its ``process`` directory for the complete
read/modify/write operation. The lock file intentionally remains on disk:
ownership belongs to the OS handle, not the presence or age of that file.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path


class AnalysisArtifactLockError(RuntimeError):
    """Another live writer owns this run's analysis artifacts."""

    code = "analysis_artifact_locked"

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"本轮分析文件正在被其他流程使用，请待其结束后重试：{path.parent}")


class RunArtifactLock:
    """Hold one run's artifact lock until ``close`` or context-manager exit.

    ``acquire`` returns immediately or raises ``AnalysisArtifactLockError``.
    Repeated acquisition by this same instance and repeated close are harmless.
    A separate instance, including one in the same process, must acquire its
    own exclusive handle. The operating system releases ownership on crashes.
    """

    def __init__(self, process_dir: str | os.PathLike[str]) -> None:
        self.process_dir = Path(process_dir).resolve()
        self.path = self.process_dir / ".analysis-state.lock"
        self._fd: int | None = None

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> RunArtifactLock:
        if self._fd is not None:
            return self
        self.process_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # Explicitly prevent child processes from extending the lifetime
            # of a parent's lock via an accidentally inherited file handle.
            os.set_inheritable(fd, False)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                # Windows permits locking a byte beyond EOF; initialize the
                # persistent sentinel only after ownership is established.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            self._fd = fd
            return self
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise AnalysisArtifactLockError(self.path) from exc
            raise
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        """Release ownership without removing the shared lock file."""
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            # Closing also releases the lock if explicit unlock itself fails.
            os.close(fd)

    def __enter__(self) -> RunArtifactLock:
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


__all__ = ["RunArtifactLock", "AnalysisArtifactLockError"]
