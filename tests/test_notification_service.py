import json
from pathlib import Path

from boss_hr.config_manager import ConfigManager
from boss_hr.application import notification_service as ns


def _write_config(tmp_path, *, webhook="", secret="", notifications=True):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n"
        "  scheduler:\n"
        "    notifications:\n"
        f"      enabled: {'true' if notifications else 'false'}\n"
        "      desktop: true\n"
        "      feishu_bot: true\n",
        encoding="utf-8",
    )
    (config_dir / "feishu.yaml").write_text(
        "feishu:\n"
        "  robot:\n"
        f"    webhook_url: {webhook}\n"
        f"    secret: {secret}\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def test_notify_desktop_uses_timed_windows_popup(monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(ns.subprocess, "run", fake_run)

    result = ns.notify_desktop("开始", "任务启动", timeout_seconds=3)

    assert result.ok is True
    assert calls
    assert "WScript.Shell" in calls[0][0][-1]
    assert "Popup" in calls[0][0][-1]


def test_notify_feishu_bot_skips_when_webhook_missing(tmp_path):
    cfg = _write_config(tmp_path, webhook="")

    result = ns.notify_feishu_bot("开始", "任务启动", config=cfg)

    assert result.ok is True
    assert result.skipped is True
    assert result.reason == "missing_webhook"


def test_notify_feishu_bot_posts_text_payload(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, webhook="https://example.test/bot")
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":0}'

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        sent["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(ns.urllib.request, "urlopen", fake_urlopen)

    result = ns.notify_feishu_bot("完成", "招呼=2", config=cfg)

    assert result.ok is True
    assert sent["url"] == "https://example.test/bot"
    assert sent["body"]["msg_type"] == "text"
    assert "完成" in sent["body"]["content"]["text"]
    assert "招呼=2" in sent["body"]["content"]["text"]


def test_notify_feishu_bot_adds_signature_when_secret_configured(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, webhook="https://example.test/bot", secret="secret")
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":0}'

    monkeypatch.setattr(ns.time, "time", lambda: 1234567890)
    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return Resp()

    monkeypatch.setattr(ns.urllib.request, "urlopen", fake_urlopen)

    result = ns.notify_feishu_bot("完成", "OK", config=cfg)

    assert result.ok is True
    assert sent["body"]["timestamp"] == "1234567890"
    assert sent["body"]["sign"]


def test_scheduler_event_respects_global_disable(tmp_path):
    cfg = _write_config(tmp_path, notifications=False)

    results = ns.notify_scheduler_event("start", "开始", "任务启动", config=cfg)

    assert len(results) == 1
    assert results[0].skipped is True
    assert results[0].reason == "notifications_disabled"
