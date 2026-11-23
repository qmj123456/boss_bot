# -*- coding: utf-8 -*-
"""boss-hr sync-jobs 命令 / sync_jobs_service 单元测试。

fake 层打在 resolve_boss_job_ids.run_sync（服务在函数体内 import，
monkeypatch 模块属性即可生效），不触网、不启动 Edge。
"""
import argparse
import json

import pytest

# 顶层导入：sync_jobs_service 会把 tools/ 加进 sys.path，
# 之后 monkeypatch 才能用字符串目标 patch resolve_boss_job_ids。
from boss_hr.application import sync_jobs_service  # noqa: E402


def _sync_payload(
    *,
    matched: list | None = None,
    unmatched: list | None = None,
    env_updates: dict | None = None,
    validation_errors: list | None = None,
):
    resolved = []
    for item in (matched or []):
        resolved.append({
            "profile_name": item["job_name"],
            "boss_name_env": f"BOSS_JOB_NAME_{item.get('env_suffix', 'X')}",
            "boss_id_env": f"BOSS_JOB_ID_{item.get('env_suffix', 'X')}",
            "feishu_table_env": "FEISHU_TABLE_X",
            "queries": [],
            "matched": True,
            "match_error": "",
            "boss_job": {},
            "boss_job_name": item["boss_job_name"],
            "encrypt_job_id": item["encrypt_job_id"],
        })
    for item in (unmatched or []):
        resolved.append({
            "profile_name": item["job_name"],
            "boss_name_env": f"BOSS_JOB_NAME_{item.get('env_suffix', 'X')}",
            "boss_id_env": f"BOSS_JOB_ID_{item.get('env_suffix', 'X')}",
            "feishu_table_env": "FEISHU_TABLE_X",
            "queries": [],
            "matched": False,
            "match_error": item.get("reason", "未找到精确匹配"),
            "boss_job": None,
            "boss_job_name": "",
            "encrypt_job_id": "",
        })
    return {
        "ok": not unmatched and not (validation_errors or []),
        "env_updated": True,
        "env_file": "D:/boss_bot/.env",
        "env_updates": env_updates or {},
        "database_migration": {"renamed": 0, "merged": 0},
        "resolved": resolved,
        "validation": {"errors": validation_errors or [], "warnings": []},
        "boss_jobs": [],
    }


@pytest.fixture()
def service():
    return sync_jobs_service


def test_sync_ok_updates_process_env(service, monkeypatch):
    monkeypatch.setenv("BOSS_JOB_ID_OPERATION", "stale-id")
    payload = _sync_payload(
        matched=[
            {
                "job_name": "运营",
                "env_suffix": "OPERATION",
                "boss_job_name": "国内电商运营（精品/3C品类）",
                "encrypt_job_id": "fresh-id",
            }
        ],
        env_updates={
            "BOSS_JOB_NAME_OPERATION": "国内电商运营（精品/3C品类）",
            "BOSS_JOB_ID_OPERATION": "fresh-id",
        },
    )
    monkeypatch.setattr("resolve_boss_job_ids.run_sync", lambda **kwargs: payload)

    import os

    result = service.run_sync_jobs(launch_edge=False)

    assert result.ok is True
    assert result.status == "synced"
    assert os.environ["BOSS_JOB_ID_OPERATION"] == "fresh-id"
    assert result.data["matched"][0]["encrypt_job_id"] == "fresh-id"
    assert result.data["unmatched"] == []


def test_sync_unmatched_jobs_are_warnings_not_failures(service, monkeypatch):
    monkeypatch.setenv("BOSS_JOB_ID_PARTTIME_ANCHOR", "old-id")
    payload = _sync_payload(
        unmatched=[{"job_name": "兼职主播", "env_suffix": "PARTTIME_ANCHOR", "reason": "未匹配"}],
        env_updates={"BOSS_JOB_ID_PARTTIME_ANCHOR": "", "BOSS_JOB_NAME_PARTTIME_ANCHOR": ""},
    )
    monkeypatch.setattr("resolve_boss_job_ids.run_sync", lambda **kwargs: payload)

    import os

    result = service.run_sync_jobs(launch_edge=False)

    assert result.ok is True
    assert result.data["unmatched"][0]["job_name"] == "兼职主播"
    assert any("兼职主播" in w for w in result.data["warnings"])
    # 未匹配岗位 env 值被清空（防沿用旧账号 ID）
    assert os.environ["BOSS_JOB_ID_PARTTIME_ANCHOR"] == ""


def test_sync_auth_required_maps_to_login_error(service, monkeypatch):
    monkeypatch.setattr(
        "resolve_boss_job_ids.run_sync",
        lambda **kwargs: {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "BOSS 未登录"}},
    )

    result = service.run_sync_jobs(launch_edge=False)

    assert result.ok is False
    assert result.error.code.value == "BOSS_LOGIN_REQUIRED"


def test_sync_cdp_unreachable_retries_after_launching_edge(service, monkeypatch):
    calls = {"run_sync": 0, "launch_edge": 0}
    payloads = [
        {"ok": False, "error": {"code": "CDP_UNREACHABLE", "message": "CDP 不可达"}},
        _sync_payload(
            matched=[{
                "job_name": "运营",
                "env_suffix": "OPERATION",
                "boss_job_name": "国内电商运营",
                "encrypt_job_id": "id-1",
            }],
            env_updates={"BOSS_JOB_ID_OPERATION": "id-1"},
        ),
    ]

    def fake_run_sync(**kwargs):
        idx = min(calls["run_sync"], len(payloads) - 1)
        calls["run_sync"] += 1
        return payloads[idx]

    def fake_launch_edge():
        calls["launch_edge"] += 1
        return {"ok": True, "pid": 123, "edge_path": "edge", "profile_dir": "p", "error": None}

    monkeypatch.setattr("resolve_boss_job_ids.run_sync", fake_run_sync)
    monkeypatch.setattr("boss_hr.application.doctor_service.launch_edge", fake_launch_edge)

    result = service.run_sync_jobs(launch_edge=True)

    assert result.ok is True
    assert calls["run_sync"] == 2
    assert calls["launch_edge"] == 1


def test_sync_validation_errors_fail(service, monkeypatch):
    payload = _sync_payload(
        matched=[{
            "job_name": "运营",
            "env_suffix": "OPERATION",
            "boss_job_name": "国内电商运营",
            "encrypt_job_id": "id-1",
        }],
        validation_errors=["feishu.yaml 缺少 运营 的 table_id 绑定"],
    )
    monkeypatch.setattr("resolve_boss_job_ids.run_sync", lambda **kwargs: payload)

    result = service.run_sync_jobs(launch_edge=False)

    assert result.ok is False
    assert "岗位绑定配置校验失败" in result.error.message


def test_sync_jobs_command_registered_in_cli():
    from boss_hr.cli import COMMANDS

    assert "sync-jobs" in COMMANDS


def test_sync_jobs_command_runs_and_emits_json(monkeypatch, capsys):
    payload = _sync_payload(
        matched=[{
            "job_name": "运营",
            "env_suffix": "OPERATION",
            "boss_job_name": "国内电商运营",
            "encrypt_job_id": "id-1",
        }],
        env_updates={"BOSS_JOB_ID_OPERATION": "id-1"},
    )
    monkeypatch.setattr("resolve_boss_job_ids.run_sync", lambda **kwargs: payload)

    from boss_hr.cli import main

    rc = main(["sync-jobs", "--no-launch-edge"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert out["command"] == "sync-jobs"
    assert out["status"] == "synced"
    assert out["data"]["matched"][0]["encrypt_job_id"] == "id-1"


def test_sync_jobs_command_keeps_unmatched_flag(monkeypatch):
    seen = {}

    def fake_run_sync(**kwargs):
        seen.update(kwargs)
        return _sync_payload()

    monkeypatch.setattr("resolve_boss_job_ids.run_sync", fake_run_sync)

    from boss_hr.commands import sync_jobs as sync_jobs_cmd

    args = argparse.Namespace(
        cdp_url="http://localhost:9222",
        env_file=None,
        keep_unmatched=True,
        no_launch_edge=True,
    )
    result = sync_jobs_cmd.run(args)

    assert result.ok is True
    assert seen["clear_unmatched"] is False
    assert seen["update_env"] is True
