"""Best-effort notifications for scheduled automation runs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from boss_hr.config_manager import ConfigManager, get_config


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    skipped: bool = False
    reason: str = ""


def notify_desktop(title: str, message: str, *, timeout_seconds: int = 12) -> NotifyResult:
    """Show a timed Windows desktop popup when an interactive desktop exists."""
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$null = $ws.Popup('{safe_message}', {int(timeout_seconds)}, '{safe_title}', 64)"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            text=True,
            capture_output=True,
            timeout=max(int(timeout_seconds) + 5, 10),
        )
    except Exception as exc:
        return NotifyResult("desktop", False, reason=f"{type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return NotifyResult("desktop", False, reason=detail[:300] or f"returncode={proc.returncode}")
    return NotifyResult("desktop", True)


def _feishu_bot_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def notify_feishu_bot(
    title: str,
    message: str,
    *,
    config: ConfigManager | None = None,
) -> NotifyResult:
    cfg = config or get_config()
    webhook = str(cfg.get("feishu.robot.webhook_url", "") or "").strip()
    if not webhook:
        return NotifyResult("feishu_bot", True, skipped=True, reason="missing_webhook")
    secret = str(cfg.get("feishu.robot.secret", "") or "").strip()
    text = f"{title}\n{message}".strip()
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_bot_sign(timestamp, secret)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return NotifyResult("feishu_bot", False, reason=f"HTTP {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        return NotifyResult("feishu_bot", False, reason=f"request_failed: {exc.reason}")
    except Exception as exc:
        return NotifyResult("feishu_bot", False, reason=f"{type(exc).__name__}: {exc}")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return NotifyResult("feishu_bot", True)
    if int(body.get("code", 0)) != 0:
        return NotifyResult("feishu_bot", False, reason=str(body)[:300])
    return NotifyResult("feishu_bot", True)


def notify_scheduler_event(
    event: str,
    title: str,
    message: str,
    *,
    config: ConfigManager | None = None,
) -> list[NotifyResult]:
    cfg = config or get_config()
    enabled = bool(cfg.get("app.scheduler.notifications.enabled", True))
    if not enabled:
        return [NotifyResult("all", True, skipped=True, reason="notifications_disabled")]
    desktop_enabled = bool(cfg.get("app.scheduler.notifications.desktop", True))
    feishu_enabled = bool(cfg.get("app.scheduler.notifications.feishu_bot", True))
    results: list[NotifyResult] = []
    if desktop_enabled:
        results.append(notify_desktop(title, message))
    else:
        results.append(NotifyResult("desktop", True, skipped=True, reason="desktop_disabled"))
    if feishu_enabled:
        results.append(notify_feishu_bot(title, message, config=cfg))
    else:
        results.append(NotifyResult("feishu_bot", True, skipped=True, reason="feishu_bot_disabled"))
    return results


def notification_results_to_dict(results: list[NotifyResult]) -> list[dict[str, Any]]:
    return [result.__dict__.copy() for result in results]


__all__ = [
    "NotifyResult",
    "notification_results_to_dict",
    "notify_desktop",
    "notify_feishu_bot",
    "notify_scheduler_event",
]
