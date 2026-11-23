import json

from boss_hr.config_manager import ConfigManager
from boss_hr.contracts.results import ok
from database.connection import Database
from database.repositories import UnitOfWork
from workflow.recruit_workflow import RecruitWorkflow


def test_recommend_candidate_keeps_raw_employment_status_in_audit_detail():
    item = {
        "encryptGeekId": "gid-status-audit",
        "geekCard": {"geekName": "状态审计"},
        "_card_prescreen": {
            "passed": False,
            "allow_full_resume": True,
            "status": "pending_confirmation",
            "reasons": ["待确认：求职状态"],
        },
        "_raw_employment_status": {
            "candidate": {},
            "geekCard": {"applyStatus": 3, "applyStatusDesc": ""},
        },
    }

    candidate = RecruitWorkflow._candidate_from_recommend_item(item, "运营助理")

    assert candidate["card_prescreen_status"] == "pending_confirmation"
    assert candidate["card_prescreen_detail"]["allow_full_resume"] is True
    assert candidate["card_prescreen_detail"]["raw_employment_status"] == {
        "candidate": {},
        "geekCard": {"applyStatus": 3, "applyStatusDesc": ""},
    }


class FakeBitableClient:
    def __init__(self):
        self.records = {}
        self.created = []
        self.table_id = "tbl001"

    def ensure_candidate_table(self, *, job=None):
        return self.table_id

    def list_records(self, table_id, *, page_size=500):
        return list(self.records.get(table_id, {}).values())

    def create_record(self, table_id, fields):
        table_records = self.records.setdefault(table_id, {})
        record_id = f"rec{len(table_records) + 1:03d}"
        table_records[record_id] = {"record_id": record_id, "fields": dict(fields)}
        self.created.append((table_id, fields))
        return record_id

    def update_record(self, table_id, record_id, fields):
        self.records.setdefault(table_id, {})[record_id] = {
            "record_id": record_id,
            "fields": dict(fields),
        }

    def find_record(self, table_id, fields):
        return None


def _config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n",
        encoding="utf-8",
    )
    (config_dir / "ai.yaml").write_text(
        "ai:\n  model: test-model\n",
        encoding="utf-8",
    )
    (config_dir / "message.yaml").write_text(
        "message:\n"
        "  default_template: standard\n"
        "  templates:\n"
        "    standard: 测试招呼语\n",
        encoding="utf-8",
    )
    (config_dir / "recruitment.yaml").write_text(
        "recruitment:\n  daily_send_limit: 3\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def _workflow(tmp_path):
    cfg = _config(tmp_path)
    db = Database(cfg)
    return RecruitWorkflow(config=cfg, database=db)


def test_fetch_workflow_persists_candidates_from_existing_json(tmp_path, monkeypatch):
    wf = _workflow(tmp_path)
    candidate_file = tmp_path / "recommend_geek_ids.json"
    resume_file = tmp_path / "new_resumes.json"
    failed_file = tmp_path / "failed_resumes.json"

    candidate_file.write_text(
        json.dumps([
            {
                "encryptGeekId": "geek-1",
                "geekCard": {
                    "geekName": "张三",
                    "company": "候选人公司",
                    "workYearDesc": "5年",
                    "tagList": ["Python"],
                },
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    resume_file.write_text(
        json.dumps([
            {
                "ok": True,
                "name": "张三",
                "work_years": "5年",
                "skills": "Python, SQL",
                "work_experience": [{"company": "上一家公司"}],
                "_meta": {"encrypt_geek_id": "geek-1"},
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    failed_file.write_text("[]", encoding="utf-8")

    def fake_fetch_candidates(**_kwargs):
        return ok(
            status="candidates_fetched",
            run_id="run-1",
            encrypt_job_id="job-1",
            job_name="Python工程师",
            data={
                "candidate_list_file": str(candidate_file),
                "new_resumes_file": str(resume_file),
                "failed_resumes_file": str(failed_file),
            },
        )

    monkeypatch.setattr(
        "boss_hr.application.fetch_service.fetch_candidates",
        fake_fetch_candidates,
    )

    result = wf.fetch_and_save_candidates(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
    )

    assert result.ok is True
    assert result.data["saved_candidates"] == 1
    with UnitOfWork(wf.database.session_factory) as uow:
        candidate = uow.candidates.get_by_boss_id("geek-1", "Python工程师")
        assert candidate is not None
        assert candidate.company == "上一家公司"
        assert candidate.resume["name"] == "张三"
        assert uow.audit_logs.list_recent(1)[0].action == "workflow.fetch_candidates.finish"


def test_fetch_workflow_persists_card_prescreen_rejected_candidates(tmp_path, monkeypatch):
    wf = _workflow(tmp_path)
    candidate_file = tmp_path / "recommend_geek_ids.json"
    rejected_file = tmp_path / "card_prescreen_rejected.json"
    resume_file = tmp_path / "new_resumes.json"
    failed_file = tmp_path / "failed_resumes.json"

    candidate_file.write_text("[]", encoding="utf-8")
    resume_file.write_text("[]", encoding="utf-8")
    failed_file.write_text("[]", encoding="utf-8")
    rejected_file.write_text(
        json.dumps([
            {
                "encryptGeekId": "geek-reject",
                "name": "李四",
                "status": "rejected",
                "reasons": ["年龄不符合", "期望岗位不匹配"],
                "missing_fields": [],
                "extracted": {"age": 38},
                # recommend_list.py 真实产物格式：精简卡片放在 "card"。
                "card": {
                    "encryptGeekId": "geek-reject",
                    "geekCard": {
                        "geekName": "李四",
                        "ageDesc": "38岁",
                        "geekDegree": "大专",
                        "workYearDesc": "10年",
                        "expectLocationName": "深圳",
                        "expectPositionName": "销售",
                    },
                    "_card_prescreen": {
                        "passed": False,
                        "status": "rejected",
                        "reasons": ["年龄不符合", "期望岗位不匹配"],
                        "missing_fields": [],
                        "extracted": {"age": 38},
                    },
                },
            },
            {
                # 旧审计文件格式：原始卡片放在 "raw"，保持兼容。
                "encryptGeekId": "geek-old",
                "name": "王五",
                "status": "pending_confirmation",
                "reasons": ["待确认：学历"],
                "raw": {
                    "encryptGeekId": "geek-old",
                    "geekCard": {"geekName": "王五", "ageDesc": "25岁"},
                },
            },
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_fetch_candidates(**_kwargs):
        return ok(
            status="candidates_fetched",
            run_id="run-1",
            encrypt_job_id="job-1",
            job_name="Python工程师",
            data={
                "candidate_list_file": str(candidate_file),
                "card_prescreen_rejected_file": str(rejected_file),
                "new_resumes_file": str(resume_file),
                "failed_resumes_file": str(failed_file),
            },
        )

    monkeypatch.setattr(
        "boss_hr.application.fetch_service.fetch_candidates",
        fake_fetch_candidates,
    )

    result = wf.fetch_and_save_candidates(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
    )

    assert result.ok is True
    assert result.data["saved_candidates"] == 2
    with UnitOfWork(wf.database.session_factory) as uow:
        candidate = uow.candidates.get_by_boss_id("geek-reject", "Python工程师")
        assert candidate is not None
        assert candidate.run_id == "run-1"
        assert candidate.name == "李四"
        # card（slim）里的卡片字段应完整落库，不能因为格式名不同而丢失。
        assert candidate.age == 38
        assert candidate.degree == "大专"
        assert candidate.city == "深圳"
        assert candidate.work_years == "10年"
        assert candidate.current_title == "销售"
        assert candidate.card_prescreen_passed is False
        assert candidate.card_prescreen_status == "rejected"
        assert candidate.card_prescreen_reason == "年龄不符合；期望岗位不匹配"

        legacy = uow.candidates.get_by_boss_id("geek-old", "Python工程师")
        assert legacy is not None
        assert legacy.name == "王五"
        assert legacy.card_prescreen_status == "pending_confirmation"
        assert legacy.card_prescreen_reason == "待确认：学历"


def test_end_to_end_syncs_rejected_candidates_when_no_resumes(tmp_path, monkeypatch):
    wf = _workflow(tmp_path)
    candidate_file = tmp_path / "recommend_geek_ids.json"
    rejected_file = tmp_path / "card_prescreen_rejected.json"
    resume_file = tmp_path / "new_resumes.json"
    failed_file = tmp_path / "failed_resumes.json"
    fake_client = FakeBitableClient()

    candidate_file.write_text("[]", encoding="utf-8")
    resume_file.write_text("[]", encoding="utf-8")
    failed_file.write_text("[]", encoding="utf-8")
    rejected_file.write_text(
        json.dumps([
            {
                "encryptGeekId": "geek-reject",
                "name": "李四",
                "status": "rejected",
                "reasons": ["年龄不符合"],
                "raw": {
                    "encryptGeekId": "geek-reject",
                    "geekCard": {"geekName": "李四"},
                },
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_fetch_candidates(**_kwargs):
        return ok(
            status="candidates_fetched",
            run_id="run-1",
            encrypt_job_id="job-1",
            job_name="Python工程师",
            data={
                "candidate_list_file": str(candidate_file),
                "card_prescreen_rejected_file": str(rejected_file),
                "new_resumes_file": str(resume_file),
                "failed_resumes_file": str(failed_file),
            },
        )

    monkeypatch.setattr(
        "boss_hr.application.fetch_service.fetch_candidates",
        fake_fetch_candidates,
    )

    result = wf.run_end_to_end(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        count=1,
        sync_feishu=True,
        feishu_client=fake_client,
    )

    assert result.ok is True
    assert result.status == "no_resumes_synced"
    assert result.data["feishu"]["total"] == 1
    fields = fake_client.created[0][1]
    assert fields["姓名"] == "李四"
    assert fields["筛选阶段"] == "卡片预筛"
    assert fields["未通过原因"] == "年龄不符合"


def test_analysis_and_greeting_are_saved_from_json_artifacts(tmp_path, monkeypatch):
    wf = _workflow(tmp_path)
    with UnitOfWork(wf.database.session_factory) as uow:
        candidate = uow.candidates.upsert(
            boss_id="geek-1",
            job="Python工程师",
            name="张三",
        )
        uow.session.flush()
        candidate_id = candidate.id

    screening_file = tmp_path / "screening_results.json"
    screening_file.write_text(
        json.dumps({
            "candidates": [
                {
                    "geek_id": "geek-1",
                    "name": "张三",
                    "passed": True,
                    "decision": "recommend",
                    "reason": "满足岗位全部硬性条件",
                }
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    analysis = wf.save_analysis_from_screening(
        job_name="Python工程师",
        screening_results_file=screening_file,
    )
    assert analysis.ok is True
    assert analysis.data["saved_analysis"] == 1

    greet_log = tmp_path / "greet_log.json"
    greet_log.write_text(
        json.dumps({
            "results": [
                {
                    "target_encrypt_geek_id": "geek-1",
                    "name": "张三",
                    "status": "greeted",
                    "message": "您好，方便聊一下吗？",
                }
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_greet_candidates(**_kwargs):
        return ok(
            status="greet_complete",
            run_id="run-1",
            encrypt_job_id="job-1",
            job_name="Python工程师",
            data={"greet_log_file": str(greet_log)},
        )

    monkeypatch.setattr(
        "boss_hr.application.greet_service.greet_candidates",
        fake_greet_candidates,
    )
    greeted = wf.send_greetings_and_save_records(
        job_name="Python工程师",
        encrypt_job_id="job-1",
        run_id="run-1",
        dry_run=True,
    )
    assert greeted.ok is True
    assert greeted.data["saved_greeting_records"] == 1

    with UnitOfWork(wf.database.session_factory) as uow:
        latest_analysis = uow.analyses.latest_for_candidate(candidate_id)
        latest_greeting = uow.greetings.latest_for_candidate(candidate_id)
        assert latest_analysis.passed is True
        assert latest_analysis.decision == "recommend"
        assert latest_analysis.reason == "满足岗位全部硬性条件"
        assert latest_analysis.model == "test-model"
        assert latest_greeting.status == "greeted"
        assert latest_greeting.message == "您好，方便聊一下吗？"
