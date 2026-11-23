# -*- coding: utf-8 -*-
"""Load local .env settings for legacy shared scripts."""
from __future__ import annotations

import os
import re
from pathlib import Path


_ENV_LINE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
DISABLE_DOTENV_ENV = "HR_AGENT_DISABLE_DOTENV"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_dotenv() -> None:
    """读取项目根目录 .env，且不覆盖已存在的系统环境变量。

    对于路径类环境变量（如 BOSS_HR_OUTPUT_DIR），如果是相对路径，
    自动解析为项目根目录下的绝对路径，避免依赖 CWD。
    """
    if os.environ.get(DISABLE_DOTENV_ENV) == "1":
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    # 路径类环境变量：相对路径自动解析为项目根目录下的绝对路径
    _PATH_ENV_KEYS = {"BOSS_HR_OUTPUT_DIR", "BOSS_HR_JOBS_REGISTRY"}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key in _PATH_ENV_KEYS and not os.path.isabs(value):
            value = str((PROJECT_ROOT / value).resolve())
        os.environ.setdefault(key, value)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _ENV_LINE_PATTERN.match(line)
    if not match:
        return None
    value = match.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return match.group(1), value


__all__ = ["DISABLE_DOTENV_ENV", "PROJECT_ROOT", "load_project_dotenv"]
