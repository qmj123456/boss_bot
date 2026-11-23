from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from boss_hr.config_manager import ConfigManager
from database.connection import Database
from database.repositories import UnitOfWork
from logger.log_manager import (
    get_audit_logger,
    get_automation_logger,
    get_system_logger,
    log_exception,
    log_shutdown,
    log_startup,
)
from logger.risk_control import RiskControl, RiskStop


def _write_config(tmp_path, *, daily_limit=2, work_time_enabled=False, start="09:00", end="18:00"):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n"
        "  log_level: INFO\n"
        "  logging:\n"
        "    dir: logs\n"
        "    max_bytes: 102400\n"
        "    backup_count: 1\n"
        "    console: false\n"
        "  database:\n"
        "    path: data/hr_agent.db\n",
        encoding="utf-8",
    )
    (config_dir / "recruitment.yaml").write_text(
        "recruitment:\n"
        f"  daily_send_limit: {daily_limit}\n"
        "  work_time:\n"
        f"    enabled: {'true' if work_time_enabled else 'false'}\n"
        f"    start: \"{start}\"\n"
        f"    end: \"{end}\"\n"
        "  risk_control:\n"
        "    random_wait_seconds:\n"
        "      min: 5\n"
        "      max: 20\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def test_production_log_files_are_created(tmp_path):
    cfg = _write_config(tmp_path)

    log_startup(cfg, detail={"mode": "test"})
    get_system_logger(cfg).info("system event")
    get_automation_logger(cfg).info("automation event")
    get_audit_logger(cfg).info("audit event")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        log_exception("unit-test", exc, cfg)
    log_shutdown(cfg, detail={"mode": "test"})

    assert (tmp_path / "logs" / "system.log").read_text(encoding="utf-8")
    assert "automation event" in (tmp_path / "logs" / "automation.log").read_text(encoding="utf-8")
    assert "audit event" in (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8")


def test_daily_send_limit_blocks_when_quota_is_exhausted(tmp_path):
    cfg = _write_config(tmp_path, daily_limit=1)
    db = Database(cfg)
    db.init()
    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.upsert(boss_id="geek-1", job="Python工程师", name="张三")
        uow.session.flush()
        uow.greetings.add(candidate_id=candidate.id, message="hello", status="greeted")

    risk = RiskControl(config=cfg, database=db)
    with pytest.raises(RiskStop) as caught:
        risk.ensure_can_send(1)

    assert caught.value.code == "daily_limit_exceeded"


def test_daily_send_limit_clamps_requested_count(tmp_path):
    cfg = _write_config(tmp_path, daily_limit=2)
    db = Database(cfg)
    db.init()
    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.upsert(boss_id="geek-1", job="Python工程师", name="张三")
        uow.session.flush()
        uow.greetings.add(candidate_id=candidate.id, message="hello", status="sent")

    risk = RiskControl(config=cfg, database=db)
    assert risk.ensure_can_send(5) == 1


def test_daily_send_count_resets_at_business_timezone_midnight(tmp_path):
    cfg = _write_config(tmp_path, daily_limit=10)
    db = Database(cfg)
    db.init()
    business_tz = ZoneInfo("Asia/Shanghai")
    local_start = datetime.combine(
        datetime.now(business_tz).date(), datetime.min.time(), tzinfo=business_tz
    )

    with UnitOfWork(db.session_factory) as uow:
        old_candidate = uow.candidates.upsert(
            boss_id="old-geek", job="主播", name="昨日候选人"
        )
        today_candidate = uow.candidates.upsert(
            boss_id="today-geek", job="主播", name="今日候选人"
        )
        uow.session.flush()
        old_record = uow.greetings.add(
            candidate_id=old_candidate.id, message="old", status="greeted"
        )
        today_record = uow.greetings.add(
            candidate_id=today_candidate.id, message="today", status="greeted"
        )
        old_record.send_time = (
            local_start - timedelta(minutes=1)
        ).astimezone(timezone.utc).replace(tzinfo=None)
        today_record.send_time = (
            local_start + timedelta(minutes=1)
        ).astimezone(timezone.utc).replace(tzinfo=None)

    assert RiskControl(config=cfg, database=db).count_today_sent(job_name="主播") == 1


def test_work_time_limit_blocks_outside_configured_window(tmp_path):
    now = datetime.now()
    start = (now + timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=2)).strftime("%H:%M")
    cfg = _write_config(tmp_path, work_time_enabled=True, start=start, end=end)
    db = Database(cfg)
    db.init()

    with pytest.raises(RiskStop) as caught:
        RiskControl(config=cfg, database=db).ensure_work_time()

    assert caught.value.code == "outside_work_time"


def test_random_wait_uses_configured_range(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    db = Database(cfg)
    db.init()
    calls = {}

    def fake_uniform(min_seconds, max_seconds):
        calls["range"] = (min_seconds, max_seconds)
        return 7.5

    monkeypatch.setattr("random.uniform", fake_uniform)
    risk = RiskControl(config=cfg, database=db)

    assert risk.random_wait_seconds() == 7.5
    assert calls["range"] == (5.0, 20.0)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("请完成验证码安全验证", "captcha_detected"),
        ("请登录后继续操作", "login_expired"),
        ("iframe timeout 页面异常", "page_exception"),
    ],
)
def test_page_exception_detection_stops_immediately(tmp_path, text, code):
    cfg = _write_config(tmp_path)
    db = Database(cfg)
    db.init()

    with pytest.raises(RiskStop) as caught:
        RiskControl(config=cfg, database=db).detect_page_exception(text=text)

    assert caught.value.code == code
