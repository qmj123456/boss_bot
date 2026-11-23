"""Production loggers for system, automation, and audit trails."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from boss_hr.config_manager import ConfigManager, get_config


class LoggerManager:
    """Configure and provide named production loggers."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or get_config()
        self.log_dir = self._resolve_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.level = self._resolve_level()
        self.max_bytes = int(self.config.get("app.logging.max_bytes", 5 * 1024 * 1024))
        self.backup_count = int(self.config.get("app.logging.backup_count", 5))

    def get_logger(self, name: str, filename: str) -> logging.Logger:
        logger = logging.getLogger(f"hr_agent.{name}")
        logger.setLevel(self.level)
        logger.propagate = False

        target = self.log_dir / filename
        if not self._has_handler(logger, target):
            handler = RotatingFileHandler(
                target,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
            ))
            logger.addHandler(handler)

        if bool(self.config.get("app.logging.console", False)):
            if not any(isinstance(h, logging.StreamHandler) and getattr(h, "_hr_console", False)
                       for h in logger.handlers):
                console = logging.StreamHandler(sys.stderr)
                console._hr_console = True
                console.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
                logger.addHandler(console)
        return logger

    def system(self) -> logging.Logger:
        return self.get_logger("system", "system.log")

    def automation(self) -> logging.Logger:
        return self.get_logger("automation", "automation.log")

    def audit(self) -> logging.Logger:
        return self.get_logger("audit", "audit.log")

    def _resolve_log_dir(self) -> Path:
        configured = self.config.get_path("app.logging.dir")
        if configured:
            return configured
        return (self.config.paths.runtime_dir / "logs").resolve()

    def _resolve_level(self) -> int:
        raw = str(self.config.get("app.log_level", "INFO") or "INFO").upper()
        return getattr(logging, raw, logging.INFO)

    @staticmethod
    def _has_handler(logger: logging.Logger, target: Path) -> bool:
        target = target.resolve()
        for handler in logger.handlers:
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve() == target:
                return True
        return False


_MANAGER: LoggerManager | None = None


def get_logger_manager(config: ConfigManager | None = None, *, reload: bool = False) -> LoggerManager:
    global _MANAGER
    if reload or _MANAGER is None or config is not None:
        _MANAGER = LoggerManager(config)
    return _MANAGER


def get_system_logger(config: ConfigManager | None = None) -> logging.Logger:
    return get_logger_manager(config).system()


def get_automation_logger(config: ConfigManager | None = None) -> logging.Logger:
    return get_logger_manager(config).automation()


def get_audit_logger(config: ConfigManager | None = None) -> logging.Logger:
    return get_logger_manager(config).audit()


def log_startup(config: ConfigManager | None = None, *, detail: dict[str, Any] | None = None) -> None:
    logger = get_system_logger(config)
    logger.info("startup | %s", detail or {})


def log_shutdown(config: ConfigManager | None = None, *, detail: dict[str, Any] | None = None) -> None:
    logger = get_system_logger(config)
    logger.info("shutdown | %s", detail or {})


def log_exception(
    action: str,
    exc: BaseException,
    config: ConfigManager | None = None,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    logger = get_system_logger(config)
    logger.exception("exception | action=%s | detail=%s | error=%s", action, detail or {}, exc)


__all__ = [
    "LoggerManager",
    "get_audit_logger",
    "get_automation_logger",
    "get_logger_manager",
    "get_system_logger",
    "log_exception",
    "log_shutdown",
    "log_startup",
]
