"""Feishu tenant token authentication."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from boss_hr.config_manager import ConfigManager, get_config


class FeishuAuth:
    """Authenticate as an internal Feishu app using app_id/app_secret."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or get_config()

    @property
    def base_url(self) -> str:
        return str(self.config.get("feishu.base_url", "https://open.feishu.cn")).rstrip("/")

    @property
    def app_id(self) -> str:
        return str(self.config.get("feishu.app_id", "") or "")

    @property
    def app_secret(self) -> str:
        return str(self.config.get("feishu.app_secret", "") or "")

    def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached or freshly requested tenant_access_token."""
        if not force_refresh:
            cached = self._read_cached_token()
            if cached:
                return cached
        return self._request_tenant_access_token()

    def _cache_path(self) -> Path | None:
        return self.config.get_path("feishu.tenant_access_token_cache")

    def _read_cached_token(self) -> str:
        path = self._cache_path()
        if not path or not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        expires_at = float(data.get("expires_at", 0) or 0)
        if expires_at <= time.time() + 120:
            return ""
        return str(data.get("tenant_access_token", "") or "")

    def _write_cached_token(self, token: str, expire_seconds: int) -> None:
        path = self._cache_path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tenant_access_token": token,
            "expires_at": time.time() + max(0, int(expire_seconds) - 120),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _request_tenant_access_token(self) -> str:
        if not self.app_id or not self.app_secret:
            raise ValueError("Missing feishu.app_id or feishu.app_secret in config/feishu.yaml")

        data = self._post_json(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
        )
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"Feishu auth failed: {data}")
        token = str(data.get("tenant_access_token", "") or "")
        if not token:
            raise RuntimeError("Feishu auth response missing tenant_access_token")
        self._write_cached_token(token, int(data.get("expire", 7200) or 7200))
        return token

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu auth HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Feishu auth request failed: {exc.reason}") from exc
        return json.loads(raw)


__all__ = ["FeishuAuth"]
