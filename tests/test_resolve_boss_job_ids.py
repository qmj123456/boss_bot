import sqlite3

from boss_hr.config_manager import ConfigManager
from tools.resolve_boss_job_ids import (
    _env_updates,
    _match_boss_job,
    _migrate_legacy_database_jobs,
    _read_env_values,
    _reject_duplicate_bindings,
    _update_env_file,
)


def test_match_requires_exact_normalized_name():
    jobs = [
        {"jobName": "国内电商运营助理", "encryptJobId": "assistant-id"},
        {"jobName": "国内电商运营", "encryptJobId": "operation-id"},
    ]

    match, reason = _match_boss_job(["运营"], jobs)

    assert match is None
    assert "不使用模糊匹配" in reason


def test_duplicate_boss_id_is_rejected_for_both_jobs():
    items = [
        {
            "profile_name": "运营",
            "matched": True,
            "encrypt_job_id": "same-id",
            "boss_job": {},
            "boss_job_name": "岗位A",
        },
        {
            "profile_name": "运营助理",
            "matched": True,
            "encrypt_job_id": "same-id",
            "boss_job": {},
            "boss_job_name": "岗位A",
        },
    ]

    _reject_duplicate_bindings(items)

    assert [item["matched"] for item in items] == [False, False]
    assert all("多个内部岗位" in item["match_error"] for item in items)


def test_update_env_preserves_unrelated_values_and_updates_names(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SECRET=keep-me\n"
        "BOSS_JOB_NAME_OPERATION=old-name\n"
        "BOSS_JOB_ID_OPERATION=old-id\n",
        encoding="utf-8",
    )
    resolved = [{
        "matched": True,
        "boss_name_env": "BOSS_JOB_NAME_OPERATION",
        "boss_id_env": "BOSS_JOB_ID_OPERATION",
        "boss_job_name": "国内电商运营",
        "encrypt_job_id": "new-id",
    }]

    _update_env_file(env_path, _env_updates(resolved, clear_unmatched=True))
    values = _read_env_values(env_path)

    assert values["SECRET"] == "keep-me"
    assert values["BOSS_JOB_NAME_OPERATION"] == "国内电商运营"
    assert values["BOSS_JOB_ID_OPERATION"] == "new-id"


def test_migrate_legacy_database_job_preserves_related_rows(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n", encoding="utf-8",
    )
    db_path = tmp_path / "data" / "hr_agent.db"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "CREATE TABLE candidate (id INTEGER PRIMARY KEY, boss_id TEXT, job TEXT, "
            "UNIQUE(boss_id, job));"
            "CREATE TABLE candidate_analysis (id INTEGER PRIMARY KEY, candidate_id INTEGER);"
            "CREATE TABLE greeting_record (id INTEGER PRIMARY KEY, candidate_id INTEGER);"
            "CREATE TABLE sync_record (id INTEGER PRIMARY KEY, candidate_id INTEGER);"
            "INSERT INTO candidate VALUES (1, 'geek-1', '主播');"
            "INSERT INTO greeting_record VALUES (1, 1);"
        )
    cfg = ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)

    result = _migrate_legacy_database_jobs(cfg)

    assert result == {"renamed": 1, "merged": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT job FROM candidate").fetchone()[0] == "全职主播"
        assert connection.execute("SELECT candidate_id FROM greeting_record").fetchone()[0] == 1
