"""端到端工作流测试：使用模拟外部服务，真实验证文件、SQLite 与同步接线。"""
from __future__ import annotations

import json

from boss_hr.config_manager import ConfigManager
from boss_hr.contracts.results import ok
from database.connection import Database
from database.repositories import UnitOfWork
from workflow.recruit_workflow import RecruitWorkflow


class FakeAnalyzer:
    def analyze(self, *, job_requirement, resume):
        assert job_requirement["job_name"] == "Python工程师"
        return {
            "passed": True,
            "reason": "Python经验和岗位要求一致",
        }


class FakeBitableClient:
    def __init__(self):
        self.records = {}

    def ensure_candidate_table(self, *, job=None):
        return "tbl-e2e"

    def list_records(self, table_id, *, page_size=500):
        return list(self.records.values())

    def find_record(self, table_id, fields):
        for record in self.records.values():
            if record["fields"].get("BOSS候选人ID") == fields.get("BOSS候选人ID"):
                return record
        return None

    def create_record(self, table_id, fields):
        record_id = f"rec-{len(self.records) + 1}"
        self.records[record_id] = {"record_id": record_id, "fields": dict(fields)}
        return record_id

    def update_record(self, table_id, record_id, fields):
        self.records[record_id] = {"record_id": record_id, "fields": dict(fields)}


def _config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n  logging:\n    dir: logs\n",
        encoding="utf-8",
    )
    (config_dir / "ai.yaml").write_text("ai:\n  model: fake-model\n", encoding="utf-8")
    (config_dir / "recruitment.yaml").write_text(
        "recruitment:\n"
        "  daily_send_limit: 10\n"
        "  daily_candidate_limit: 20\n"
        "  work_time:\n    enabled: false\n"
        "  risk_control:\n    random_wait_seconds:\n      min: 0\n      max: 0\n",
        encoding="utf-8",
    )
    (config_dir / "message.yaml").write_text(
        "message:\n  default_template: standard\n  templates:\n    standard: 您好，方便聊一下吗？\n",
        encoding="utf-8",
    )
    (config_dir / "feishu.yaml").write_text(
        "feishu:\n  fields:\n    boss_id: BOSS候选人ID\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def test_end_to_end_persists_greeting_and_syncs_feishu(tmp_path, monkeypatch):
    candidate_file = tmp_path / "process" / "recommend_geek_ids.json"
    resume_file = tmp_path / "process" / "new_resumes.json"
    failed_file = tmp_path / "process" / "failed_resumes.json"
    candidate_file.parent.mkdir()
    candidate_file.write_text(json.dumps([{
        "encryptGeekId": "geek-1",
        "geekCard": {"geekName": "张三", "ageDesc": "29岁", "cityName": "上海"},
    }], ensure_ascii=False), encoding="utf-8")
    resume_file.write_text(json.dumps([{
        "ok": True,
        "name": "张三",
        "age": "29岁",
        "degree": "本科",
        "expect_city": "上海",
        "expect_salary": "25-35K",
        "skills": ["Python", "SQL"],
        "_meta": {"encrypt_geek_id": "geek-1"},
    }], ensure_ascii=False), encoding="utf-8")
    failed_file.write_text("[]", encoding="utf-8")

    def fake_fetch_candidates(**kwargs):
        return ok(status="candidates_fetched", run_id=kwargs["run_id"], data={
            "candidate_list_file": str(candidate_file),
            "new_resumes_file": str(resume_file),
            "failed_resumes_file": str(failed_file),
            "listed_count": 1,
            "downloaded_count": 1,
            "failed_count": 0,
        })

    def fake_greet_candidates(**kwargs):
        # AI 阶段必须先生成招呼脚本兼容的筛选文件。
        screening = resume_file.parent / "screening_results.json"
        assert screening.is_file()
        greet_log = resume_file.parent / "greet_log.json"
        greet_log.write_text(json.dumps({"results": [{
            "target_encrypt_geek_id": "geek-1",
            "name": "张三",
            "status": "greeted",
            "message": "您好，方便聊一下吗？",
        }]}, ensure_ascii=False), encoding="utf-8")
        return ok(status="greet_complete", run_id=kwargs["run_id"], data={
            "greeted": 1, "not_found": 0, "greet_log_file": str(greet_log),
        })

    monkeypatch.setattr("boss_hr.application.fetch_service.fetch_candidates", fake_fetch_candidates)
    monkeypatch.setattr("boss_hr.application.greet_service.greet_candidates", fake_greet_candidates)

    cfg = _config(tmp_path)
    database = Database(cfg)
    fake_feishu = FakeBitableClient()
    workflow = RecruitWorkflow(config=cfg, database=database, analyzer=FakeAnalyzer())
    result = workflow.run_end_to_end(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
        feishu_client=fake_feishu,
    )

    assert result.ok is True
    assert result.status == "automation_complete"
    assert result.data["feishu"]["created"] == 1
    fields = next(iter(fake_feishu.records.values()))["fields"]
    assert fields["年龄"] == 29
    assert fields["简历筛选结果"] == "通过"
    assert fields["简历筛选原因"] == "Python经验和岗位要求一致"
    assert fields["筛选结论"] == "recommend"
    assert fields["是否已打招呼"] == "是"
    assert fields["打招呼日期"] is not None

    # 同一 run 重试应更新原分析、招呼和飞书记录，而不是重复插入业务数据。
    retry = workflow.run_end_to_end(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
        feishu_client=fake_feishu,
    )
    assert retry.ok is True
    assert retry.data["feishu"]["updated"] == 1

    class BrokenFeishuClient(FakeBitableClient):
        def ensure_candidate_table(self, *, job=None):
            raise RuntimeError("Feishu API error: TableIdNotFound")

    partial = workflow.run_end_to_end(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
        feishu_client=BrokenFeishuClient(),
    )
    assert partial.ok is True
    assert partial.data["feishu"]["failed"] == 1
    assert "TableIdNotFound" in partial.data["feishu"]["error"]

    with UnitOfWork(database.session_factory) as uow:
        candidate = uow.candidates.get_by_boss_id("geek-1", "Python工程师")
        assert candidate.age == 29
        assert candidate.degree == "本科"
        latest_analysis = uow.analyses.latest_for_candidate(candidate.id)
        assert latest_analysis.passed is True
        assert latest_analysis.reason == "Python经验和岗位要求一致"
        assert latest_analysis.decision == "recommend"
        assert latest_analysis.run_id == "run-1"
        assert uow.greetings.latest_for_candidate(candidate.id).run_id == "run-1"
        assert len(candidate.analyses) == 1
        assert len(candidate.greetings) == 1
