import json
import sys
from types import SimpleNamespace

import pytest

from boss_hr.contracts.errors import ErrorCode, UnifiedError
from boss_hr.contracts.results import error, ok
from tools.scheduled_boss_run import (
    _claim_job_id,
    _resolve_encrypt_job_id_for_job,
    _successful_greeting_keys,
    _terminal_card_count,
    _uses_live_card_flow,
)


def test_multi_job_id_resolution_never_uses_global_fallback(monkeypatch):
    monkeypatch.delenv("BOSS_JOB_ID_OPERATION", raising=False)
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "global-job-id")

    assert _resolve_encrypt_job_id_for_job(
        "运营", None, multi_job=True
    ) == ""
    assert _resolve_encrypt_job_id_for_job(
        "运营", None, multi_job=False
    ) == "global-job-id"


def test_multi_job_runtime_rejects_duplicate_job_id():
    owners = {}

    assert _claim_job_id(owners, "运营", "same-id") == (True, "运营")
    assert _claim_job_id(owners, "高级剪拍", "same-id") == (False, "运营")
    assert _claim_job_id(owners, "运营", "same-id") == (True, "运营")


def test_live_card_flow_disables_legacy_retry_greet_detection():
    payload = {
        "automation": {
            "data": {
                "fetch": {
                    "fetch": {
                        "data": {"live_card_flow": True},
                    },
                },
            },
        },
    }
    assert _uses_live_card_flow(payload) is True
    assert _uses_live_card_flow({"automation": {"data": {}}}) is False


def test_daemon_counts_real_terminal_cards_instead_of_requested_count():
    payload = {
        "automation": {
            "data": {
                "fetch": {
                    "fetch": {
                        "data": {
                            "requested_count": 30,
                            "terminal_card_count": 17,
                            "listed_count": 13,
                        }
                    }
                }
            }
        }
    }

    assert _terminal_card_count(payload) == 17


def test_failed_child_run_still_credits_successful_live_greetings(
    tmp_path, monkeypatch
):
    process_dir = tmp_path / "process"
    process_dir.mkdir()
    (process_dir / "greet_log.json").write_text(json.dumps({
        "results": [
            {
                "name": "甲",
                "encrypt_geek_id": "gid-1",
                "status": "greeted",
                "message_sent": True,
            },
            {
                "name": "乙",
                "encrypt_geek_id": "gid-2",
                "status": "failed",
                "message_sent": False,
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")

    class Output:
        def __init__(self, *_args, **_kwargs):
            self.process_dir = str(process_dir)

    monkeypatch.setitem(
        sys.modules,
        "output_manager",
        SimpleNamespace(JobOutputManager=Output),
    )

    assert _successful_greeting_keys("主播", "job-1", "failed-run") == {
        "gid:gid-1"
    }


@pytest.fixture(autouse=True)
def _patch_notifications(monkeypatch):
    monkeypatch.setattr("tools.scheduled_boss_run.notify_scheduler_event", lambda *a, **kw: [])


def _patch_sync_ok(monkeypatch, *, env_updates: dict | None = None):
    """默认定时任务会先同步岗位信息；测试里替换为假的同步成功。"""
    def fake_sync(cdp_url: str = "http://localhost:9222"):
        for key, value in (env_updates or {}).items():
            import os
            os.environ[key] = value
        return ok(status="synced", data={"matched": [], "unmatched": [], "warnings": []})

    monkeypatch.setattr("tools.scheduled_boss_run._sync_jobs", fake_sync)


def _write_config(tmp_path, *, allow_auto_confirm=False):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n"
        "  default_job_name: 全职主播\n"
        "  database:\n    path: data/hr_agent.db\n"
        "  scheduler:\n"
        "    enabled: true\n"
        f"    allow_auto_confirm: {'true' if allow_auto_confirm else 'false'}\n"
        "    count: 2\n"
        "    max_greetings: 1\n",
        encoding="utf-8",
    )
    jobs_dir = config_dir / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "全职主播.yaml").write_text(
        "recruitment:\n  job_name: 主播\n",
        encoding="utf-8",
    )
    return config_dir


def _write_config_with_scheduler_jobs(tmp_path, *, allow_auto_confirm=True):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n"
        "  default_job_name: 全职主播\n"
        "  database:\n    path: data/hr_agent.db\n"
        "  scheduler:\n"
        "    enabled: true\n"
        f"    allow_auto_confirm: {'true' if allow_auto_confirm else 'false'}\n"
        "    job_names:\n"
        "      - 运营\n"
        "      - 高级剪拍\n"
        "    count: 2\n"
        "    max_greetings: 1\n",
        encoding="utf-8",
    )
    jobs_dir = config_dir / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "全职主播.yaml").write_text(
        "recruitment:\n  job_name: 全职主播\n",
        encoding="utf-8",
    )
    return config_dir


def _write_config_with_per_job_limits(tmp_path, *, allow_auto_confirm=True):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n"
        "  default_job_name: 全职主播\n"
        "  database:\n    path: data/hr_agent.db\n"
        "  scheduler:\n"
        "    enabled: true\n"
        f"    allow_auto_confirm: {'true' if allow_auto_confirm else 'false'}\n"
        "    jobs:\n"
        "      - name: 全职主播\n"
        "        count: 80\n"
        "        max_greetings: 80\n"
        "      - name: 运营\n"
        "        count: 50\n"
        "        max_greetings: 50\n"
        "      - name: 兼职主播\n"
        "        count: 0\n"
        "        max_greetings: 0\n"
        "    count: 2\n"
        "    max_greetings: 1\n",
        encoding="utf-8",
    )
    jobs_dir = config_dir / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "全职主播.yaml").write_text(
        "recruitment:\n  job_name: 全职主播\n",
        encoding="utf-8",
    )
    return config_dir


def _payload(capsys):
    return json.loads(capsys.readouterr().out.strip())


def test_scheduled_run_requires_auto_confirm_opt_in(tmp_path, monkeypatch, capsys):
    config_dir = _write_config(tmp_path, allow_auto_confirm=False)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "job-1")

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main([])

    assert rc == 1
    payload = _payload(capsys)
    assert payload["status"] == "auto_confirm_required"


def test_scheduled_run_executes_start_confirm_automate(tmp_path, monkeypatch, capsys):
    config_dir = _write_config(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "job-1")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id="run-1")

    def fake_confirm_run(**kwargs):
        calls.append(("confirm", kwargs))
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        calls.append(("automate", kwargs))
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main([])

    assert rc == 0
    payload = _payload(capsys)
    assert payload["ok"] is True
    assert payload["run_id"] == "run-1"
    assert [name for name, _ in calls] == ["start", "confirm", "automate"]
    assert calls[2][1]["count"] == 2
    assert calls[2][1]["max_greetings"] == 1
    assert payload["job_sync"]["ok"] is True
    assert payload["scheduler_log_file"]
    assert payload["scheduler_latest_file"]
    assert payload["scheduler_history_file"]

    latest_path = __import__("pathlib").Path(payload["scheduler_latest_file"])
    assert latest_path.exists()
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    assert latest["ok"] is True
    assert latest["summary"]["run_id"] == "run-1"


def test_scheduled_run_uses_per_job_env_id(tmp_path, monkeypatch, capsys):
    config_dir = _write_config(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "operation-id")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id="run-1")

    def fake_confirm_run(**kwargs):
        calls.append(("confirm", kwargs))
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        calls.append(("automate", kwargs))
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main(["--job-name", "运营"])

    assert rc == 0
    payload = _payload(capsys)
    assert payload["ok"] is True
    assert calls[0][1]["encrypt_job_id"] == "operation-id"
    assert calls[1][1]["encrypt_job_id"] == "operation-id"
    assert calls[2][1]["encrypt_job_id"] == "operation-id"


def test_scheduled_run_executes_configured_jobs_in_order(tmp_path, monkeypatch, capsys):
    config_dir = _write_config_with_scheduler_jobs(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "operation-id")
    monkeypatch.setenv("BOSS_JOB_ID_SENIOR_SHOOT_EDIT", "senior-id")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id=f"run-{len(calls)}")

    def fake_confirm_run(**kwargs):
        calls.append(("confirm", kwargs))
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        calls.append(("automate", kwargs))
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main([])

    assert rc == 0
    payload = _payload(capsys)
    assert payload["ok"] is True
    assert payload["job_count"] == 2
    assert [item["job_name"] for item in payload["results"]] == ["运营", "高级剪拍"]
    assert [call[1]["encrypt_job_id"] for call in calls if call[0] == "start"] == [
        "operation-id",
        "senior-id",
    ]


def test_scheduled_run_uses_per_job_limits(tmp_path, monkeypatch, capsys):
    config_dir = _write_config_with_per_job_limits(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_JOB_ID_FULLTIME_ANCHOR", "anchor-id")
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "operation-id")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id=f"run-{len(calls)}")

    def fake_confirm_run(**kwargs):
        calls.append(("confirm", kwargs))
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        calls.append(("automate", kwargs))
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main([])

    assert rc == 0
    payload = _payload(capsys)
    assert payload["job_count"] == 3
    assert payload["results"][2]["stage"] == "skipped"
    automate_calls = [kwargs for name, kwargs in calls if name == "automate"]
    assert [(item["job_name"], item["count"], item["max_greetings"]) for item in automate_calls] == [
        ("全职主播", 80, 80),
        ("运营", 50, 50),
    ]


def test_scheduled_single_job_uses_configured_limits(tmp_path, monkeypatch, capsys):
    config_dir = _write_config_with_per_job_limits(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "operation-id")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id="run-1")

    def fake_confirm_run(**kwargs):
        calls.append(("confirm", kwargs))
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        calls.append(("automate", kwargs))
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main(["--job-name", "运营"])

    assert rc == 0
    _payload(capsys)
    automate = [kwargs for name, kwargs in calls if name == "automate"][0]
    assert automate["count"] == 50
    assert automate["max_greetings"] == 50


def test_scheduled_run_aborts_when_job_sync_fails(tmp_path, monkeypatch, capsys):
    config_dir = _write_config(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "job-1")

    def fake_sync(cdp_url: str = "http://localhost:9222"):
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.CDP_NOT_RUNNING,
                message="未检测到 Edge 远程调试端口 9222",
                recoverable=True,
            ),
            exit_code=1,
        )

    monkeypatch.setattr("tools.scheduled_boss_run._sync_jobs", fake_sync)

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main([])

    assert rc == 1
    payload = _payload(capsys)
    assert payload["ok"] is False
    assert payload["stage"] == "sync_jobs"
    assert payload["sync"]["error"]["code"] == "CDP_NOT_RUNNING"


def test_scheduled_run_sync_refreshes_job_env_before_start(tmp_path, monkeypatch, capsys):
    """同步岗位信息后，start 必须拿到刷新后的 encryptJobId。"""
    config_dir = _write_config(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_HR_ENCRYPT_JOB_ID", raising=False)
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "stale-id")

    calls = []

    def fake_start_new_run(**kwargs):
        calls.append(("start", kwargs))
        return ok(status="waiting_user_confirmation", run_id="run-1")

    def fake_confirm_run(**kwargs):
        return ok(status="confirmed", run_id=kwargs["run_id"])

    def fake_run_automation(**kwargs):
        return ok(status="automation_complete", run_id=kwargs["run_id"], data={"done": True})

    monkeypatch.setattr("tools.scheduled_boss_run.start_new_run", fake_start_new_run)
    monkeypatch.setattr("tools.scheduled_boss_run.confirm_run", fake_confirm_run)
    monkeypatch.setattr("tools.scheduled_boss_run.run_automation", fake_run_automation)
    _patch_sync_ok(monkeypatch, env_updates={"BOSS_JOB_ID_OPERATION": "fresh-id"})

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main(["--job-name", "运营"])

    assert rc == 0
    assert calls[0][1]["encrypt_job_id"] == "fresh-id"


def test_scheduled_run_skip_sync_flag(tmp_path, monkeypatch, capsys):
    config_dir = _write_config(tmp_path, allow_auto_confirm=True)
    monkeypatch.setenv("HR_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOSS_HR_ENCRYPT_JOB_ID", "job-1")

    calls = []

    def fake_sync(cdp_url: str = "http://localhost:9222"):
        calls.append("sync")
        return ok(status="synced", data={"matched": [], "unmatched": [], "warnings": []})

    monkeypatch.setattr("tools.scheduled_boss_run._sync_jobs", fake_sync)
    monkeypatch.setattr(
        "tools.scheduled_boss_run.start_new_run",
        lambda **kwargs: ok(status="waiting_user_confirmation", run_id="run-1"),
    )
    monkeypatch.setattr(
        "tools.scheduled_boss_run.confirm_run",
        lambda **kwargs: ok(status="confirmed", run_id=kwargs["run_id"]),
    )
    monkeypatch.setattr(
        "tools.scheduled_boss_run.run_automation",
        lambda **kwargs: ok(status="automation_complete", run_id=kwargs["run_id"]),
    )

    from tools import scheduled_boss_run

    rc = scheduled_boss_run.main(["--skip-sync"])

    assert rc == 0
    payload = _payload(capsys)
    assert calls == []
    assert "job_sync" not in payload
