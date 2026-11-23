from boss_hr.config_manager import ConfigManager
from database.connection import Database
from database.repositories import UnitOfWork
import sqlite3


def _config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def test_database_initializes_and_tracks_recruitment_flow(tmp_path):
    db = Database(_config(tmp_path))
    db.init()

    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.upsert(
            boss_id="geek-1",
            job="Python工程师",
            name="张三",
            resume={"education": [{"school": "测试大学"}]},
            company="上一家公司",
            experience="5年",
            skills=["Python", "SQLAlchemy"],
            age=29,
            degree="本科",
            city="上海",
            expected_salary="25-35K",
            current_title="Python工程师",
            card_prescreen_reason="卡片条件全部满足",
            card_prescreen_status="passed",
            card_prescreen_passed=True,
            card_prescreen_detail={"passed": True},
        )
        uow.session.flush()

        uow.analyses.add(
            candidate_id=candidate.id,
            passed=True,
            reason="满足岗位全部硬性条件",
            decision="recommend",
            model="test-model",
            run_id="run-1",
        )
        uow.greetings.add(
            candidate_id=candidate.id,
            message="您好，方便聊一下岗位吗？",
            status="sent",
            run_id="run-1",
        )
        uow.sync_records.add(
            candidate_id=candidate.id,
            feishu_record_id="rec001",
            status="synced",
        )
        uow.audit_logs.add(
            action="send_greeting",
            detail={"candidate_id": candidate.id},
            result="success",
        )

    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.get_by_boss_id("geek-1", "Python工程师")
        assert candidate is not None
        assert candidate.skills == ["Python", "SQLAlchemy"]
        assert candidate.age == 29
        assert candidate.degree == "本科"
        assert candidate.city == "上海"
        assert candidate.card_prescreen_reason == "卡片条件全部满足"
        assert candidate.card_prescreen_status == "passed"
        assert candidate.card_prescreen_passed is True
        assert uow.analyses.latest_for_candidate(candidate.id).passed is True
        assert uow.analyses.latest_for_candidate(candidate.id).decision == "recommend"
        assert uow.analyses.latest_for_candidate(candidate.id).run_id == "run-1"
        assert uow.greetings.latest_for_candidate(candidate.id).status == "sent"
        assert uow.greetings.latest_for_candidate(candidate.id).run_id == "run-1"
        assert uow.sync_records.latest_for_candidate(candidate.id).feishu_record_id == "rec001"
        assert uow.audit_logs.list_recent(1)[0].action == "send_greeting"


def test_candidate_upsert_is_unique_per_boss_id_and_job(tmp_path):
    db = Database(_config(tmp_path))
    db.init()

    with UnitOfWork(db.session_factory) as uow:
        first = uow.candidates.upsert(boss_id="geek-1", job="算法工程师", name="候选人A")
        uow.session.flush()
        second = uow.candidates.upsert(boss_id="geek-1", job="算法工程师", name="候选人A更新")
        uow.session.flush()

        assert first.id == second.id
        assert second.name == "候选人A更新"


def test_existing_sqlite_database_gets_additive_fields(tmp_path):
    """旧数据库升级时应自动补字段，且不能删除已有候选人数据。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "hr_agent.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE candidate ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, boss_id VARCHAR(128) NOT NULL, "
            "name VARCHAR(128) NOT NULL DEFAULT '', job VARCHAR(256) NOT NULL DEFAULT '', "
            "resume JSON, company VARCHAR(256) NOT NULL DEFAULT '', "
            "experience VARCHAR(128) NOT NULL DEFAULT '', skills JSON, "
            "created_time DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
        connection.execute(
            "INSERT INTO candidate (boss_id, name, job) VALUES ('old-1', '旧候选人', '测试岗位')"
        )

    database = Database(_config(tmp_path))
    database.init()
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(candidate)")}
        assert {
            "age", "degree", "city", "expected_salary", "current_title",
            "card_prescreen_reason", "card_prescreen_status",
            "card_prescreen_passed", "card_prescreen_detail", "run_id",
        } <= columns
        # 旧评分列应被迁移逻辑删除，保持与飞书字段一致。
        assert "card_score" not in columns
        assert connection.execute(
            "SELECT name FROM candidate WHERE boss_id='old-1'"
        ).fetchone()[0] == "旧候选人"
