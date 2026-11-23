"""YAML configuration loader for the HR Agent.

The loader is intentionally independent from the current JSON workflow. It can
be adopted by existing services step by step without changing their behavior.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only in broken envs
    raise RuntimeError(
        "PyYAML is required for configuration loading. "
        "Install dependencies with: python -m pip install -e ."
    ) from exc


CONFIG_DIR_ENV = "HR_AGENT_CONFIG_DIR"
RUNTIME_DIR_ENV = "HR_AGENT_RUNTIME_DIR"
DISABLE_DOTENV_ENV = "HR_AGENT_DISABLE_DOTENV"

DEFAULT_CONFIG_FILES = {
    "app": "app.yaml",
    "ai": "ai.yaml",
    "performance": "performance.yaml",
    "feishu": "feishu.yaml",
    "recruitment": "recruitment.yaml",
    "message": "message.yaml",
}

_ENV_PATTERN = re.compile(r"\$\{(?:ENV:)?([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_LINE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


@dataclass(frozen=True)
class ConfigPaths:
    """Resolved paths used by ConfigManager."""

    runtime_dir: Path
    config_dir: Path
    prompt_dir: Path


class ConfigManager:
    """Load and access external YAML configuration.

    Resolution order:
      1. Explicit ``config_dir`` argument.
      2. ``HR_AGENT_CONFIG_DIR`` environment variable.
      3. ``<exe-dir>/config`` when running as a PyInstaller executable.
      4. ``<project-root>/config`` during source/development runs.

    Relative paths in configuration values can be resolved with ``get_path``.
    By default they are relative to ``runtime_dir``:
      - explicit ``runtime_dir`` argument
      - ``HR_AGENT_RUNTIME_DIR`` environment variable
      - executable directory when frozen
      - project root during source runs
    """

    def __init__(
        self,
        config_dir: str | os.PathLike[str] | None = None,
        runtime_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        resolved_runtime_dir = self._resolve_runtime_dir(runtime_dir)
        resolved_config_dir = self._resolve_config_dir(config_dir)
        self.paths = ConfigPaths(
            runtime_dir=resolved_runtime_dir,
            config_dir=resolved_config_dir,
            prompt_dir=resolved_config_dir / "prompt",
        )
        self._config: dict[str, Any] = {}
        self._load_dotenv()
        self.reload()

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parent.parent

    @staticmethod
    def _is_frozen() -> bool:
        return bool(getattr(sys, "frozen", False))

    @classmethod
    def _default_runtime_dir(cls) -> Path:
        if os.environ.get(RUNTIME_DIR_ENV):
            return Path(os.environ[RUNTIME_DIR_ENV]).expanduser().resolve()
        if cls._is_frozen():
            return Path(sys.executable).resolve().parent
        return cls._project_root()

    @classmethod
    def _resolve_runtime_dir(
        cls,
        runtime_dir: str | os.PathLike[str] | None,
    ) -> Path:
        if runtime_dir:
            return Path(runtime_dir).expanduser().resolve()
        return cls._default_runtime_dir()

    @classmethod
    def _resolve_config_dir(
        cls,
        config_dir: str | os.PathLike[str] | None,
    ) -> Path:
        if config_dir:
            return Path(config_dir).expanduser().resolve()
        if os.environ.get(CONFIG_DIR_ENV):
            return Path(os.environ[CONFIG_DIR_ENV]).expanduser().resolve()
        if cls._is_frozen():
            return Path(sys.executable).resolve().parent / "config"
        return cls._project_root() / "config"

    def reload(self) -> None:
        """Reload all standard YAML files from disk."""
        loaded: dict[str, Any] = {}
        for section, filename in DEFAULT_CONFIG_FILES.items():
            data = self._load_yaml_file(self.paths.config_dir / filename)
            loaded[section] = self._unwrap_section(section, data)
        if not loaded.get("recruitment"):
            loaded["recruitment"] = self._load_default_job_profile(loaded.get("app", {}))
        self._config = loaded

    @staticmethod
    def _unwrap_section(section: str, data: dict[str, Any]) -> dict[str, Any]:
        """Allow either ``app.yaml -> {app: {...}}`` or rootless mappings."""
        inner = data.get(section)
        if isinstance(inner, dict):
            return inner
        return data

    def _load_yaml_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"YAML config must contain a mapping: {path}")
        return self._expand_env(data)

    def _load_dotenv(self) -> None:
        """Load local secrets from ``.env``, overriding inherited env vars.

        项目 .env 是项目级覆盖配置，优先级高于父进程继承的系统/用户环境
        变量。这样用户在 .env 里写 ``OPENAI_API_KEY=sk-...`` 后，不受 IDE
        或父进程残留的旧 key 偷偷覆盖，避免发到错误的 AI 后端导致 401。
        受 ``HR_AGENT_DISABLE_DOTENV=1`` 限制时跳过加载。
        """
        if os.environ.get(DISABLE_DOTENV_ENV) == "1":
            return
        env_path = self.paths.runtime_dir / ".env"
        if not env_path.exists():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw_line)
            if parsed is None:
                continue
            key, value = parsed
            # .env 优先于父进程继承的系统/用户环境变量，避免旧 key 偷偷覆盖。
            os.environ[key] = value

    def _expand_env(self, value: Any) -> Any:
        """Expand ${ENV_NAME} and ${ENV:ENV_NAME} placeholders."""
        if isinstance(value, str):
            return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
        if isinstance(value, list):
            return [self._expand_env(v) for v in value]
        if isinstance(value, dict):
            return {k: self._expand_env(v) for k, v in value.items()}
        return value

    @property
    def all(self) -> Mapping[str, Any]:
        """Return the merged configuration by top-level file section."""
        return self._config

    def section(self, name: str) -> dict[str, Any]:
        """Return one top-level section, for example ``ai`` or ``feishu``."""
        value = self._config.get(name, {})
        if not isinstance(value, dict):
            return {}
        return value

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Read a value with dotted syntax, e.g. ``app.database.path``."""
        node: Any = self._config
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get_path(self, dotted_key: str, default: Any = None) -> Path | None:
        """Read a path value and resolve relative paths against runtime_dir."""
        value = self.get(dotted_key, default)
        if value in (None, ""):
            return None
        p = Path(str(value)).expanduser()
        if p.is_absolute():
            return p
        return (self.paths.runtime_dir / p).resolve()

    def prompt_path(self, name: str) -> Path:
        """Return an absolute prompt file path under ``config/prompt``."""
        return (self.paths.prompt_dir / name).resolve()

    def load_prompt(self, name: str) -> str:
        """Read one prompt template from ``config/prompt``."""
        path = self.prompt_path(name)
        return path.read_text(encoding="utf-8")

    def load_job_profile(self, name: str) -> dict[str, Any]:
        """按岗位名称读取 ``config/jobs/<name>.yaml``。

        岗位文件支持两种根节点：
          - ``job_profile:``：岗位专属配置；
          - ``recruitment:``：兼容旧的单岗位配置格式。

        没有找到时返回空字典，调用方可继续使用默认招聘配置。
        """
        if not name:
            return {}
        jobs_dir = self.paths.config_dir / "jobs"
        candidates = [
            jobs_dir / f"{name}.yaml",
            jobs_dir / f"{name}.yml",
        ]
        for path in candidates:
            if path.exists():
                data = self._load_yaml_file(path)
                return self._unwrap_job_profile(data)
        return {}

    def list_job_profiles(self) -> list[str]:
        """Return configurable job profile names under ``config/jobs``."""
        jobs_dir = self.paths.config_dir / "jobs"
        if not jobs_dir.exists():
            return []
        names = []
        for path in sorted(jobs_dir.iterdir(), key=lambda p: p.name):
            if path.is_file() and path.suffix.lower() in (".yaml", ".yml"):
                names.append(path.stem)
        return names

    def default_job_profile_name(self, app_section: dict[str, Any] | None = None) -> str | None:
        """Return the configured default profile name under ``config/jobs``."""
        app_section = app_section if app_section is not None else self.section("app")
        candidates: list[str] = []
        env_name = os.environ.get("HR_AGENT_DEFAULT_JOB_NAME")
        if env_name:
            candidates.append(env_name)
        for key in ("default_job_name", "default_job_profile"):
            value = app_section.get(key)
            if value:
                candidates.append(str(value))
        configured = self.list_job_profiles()
        if "全职主播" in configured:
            candidates.append("全职主播")
        candidates.extend(configured)

        seen: set[str] = set()
        for name in candidates:
            if name in seen:
                continue
            seen.add(name)
            if self.load_job_profile(name):
                return name
        return None

    def _load_default_job_profile(self, app_section: dict[str, Any]) -> dict[str, Any]:
        """Load the default recruitment section from a selected job profile.

        Selection order:
          1. ``HR_AGENT_DEFAULT_JOB_NAME`` environment variable.
          2. ``app.default_job_name`` / ``app.default_job_profile``.
          3. ``全职主播`` when that profile exists, preserving current behavior.
          4. The first yaml file under ``config/jobs`` for deterministic fallback.
        """
        name = self.default_job_profile_name(app_section)
        if name:
            return self.load_job_profile(name)
        return {}

    @staticmethod
    def _unwrap_job_profile(data: dict[str, Any]) -> dict[str, Any]:
        for key in ("job_profile", "recruitment"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data


_DEFAULT_MANAGER: ConfigManager | None = None


def get_config(
    config_dir: str | os.PathLike[str] | None = None,
    runtime_dir: str | os.PathLike[str] | None = None,
    *,
    reload: bool = False,
) -> ConfigManager:
    """Return a process-wide ConfigManager instance."""
    global _DEFAULT_MANAGER
    if reload or _DEFAULT_MANAGER is None or config_dir or runtime_dir:
        _DEFAULT_MANAGER = ConfigManager(config_dir=config_dir, runtime_dir=runtime_dir)
    return _DEFAULT_MANAGER


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


__all__ = [
    "CONFIG_DIR_ENV",
    "DISABLE_DOTENV_ENV",
    "RUNTIME_DIR_ENV",
    "ConfigManager",
    "ConfigPaths",
    "get_config",
]
