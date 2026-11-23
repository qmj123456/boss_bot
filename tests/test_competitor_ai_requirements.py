"""Verify real job profiles reach AI even when a resume company matches."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from ai.resume_analyzer import ResumeAnalyzer
from tests.test_ai_capabilities import _config
from tests.test_recruit_workflow import _workflow


ROOT = Path(__file__).resolve().parents[1]


class RejectingClient:
    def __init__(self):
        self.calls = []

    def complete_json(self, messages):
        self.calls.append(messages)
        return {
            "passed": False,
            "reason": "公司命中，但其他硬性条件不满足，最终结论：不通过。",
            "employment_status": "离职-随时到岗",
            "work_evidence": [{
                "title": "拍摄剪辑",
                "content": "转化率提升5%；ROI达到3.2",
                "start": "2025.01", "end": "2026.08", "region": "深圳",
                "outcomes": [
                    {"metric": "转化率", "kind": "rate", "evidence": "转化率提升5%"},
                    {"metric": "ROI", "kind": "ratio", "evidence": "ROI达到3.2"},
                ],
            }],
            "selected_work_index": 0,
        }


@pytest.mark.parametrize("job_name", ["运营", "高级剪拍"])
def test_matching_company_still_reaches_ai_with_entire_configured_list(tmp_path, monkeypatch, job_name):
    job = yaml.safe_load((ROOT / "config" / "jobs" / f"{job_name}.yaml").read_text(encoding="utf-8"))["recruitment"]
    original = copy.deepcopy(job)
    workflow = _workflow(tmp_path)
    client = RejectingClient()
    workflow.analyzer = ResumeAnalyzer(config=_config(tmp_path / "analyzer"), client=client)
    monkeypatch.setattr(workflow, "persist_analysis_from_screening_results", lambda **kwargs: 0)
    monkeypatch.setattr(workflow, "_audit", lambda *args: None)
    monkeypatch.setattr(workflow, "_record_stage_timing", lambda **kwargs: None)
    resume_file = tmp_path / "new_resumes.json"
    resume_file.write_text(json.dumps([{
        "name": "测试候选人", "encrypt_geek_id": "gid-competitor",
        "work_experience": [{"company": "深圳市图拉斯科技有限公司"}],
    }], ensure_ascii=False), encoding="utf-8")

    result = workflow.analyze_fetched_candidates(
        job_name=job_name, run_id="run-test",
        fetch_data={"new_resumes_file": str(resume_file)}, job_requirement=job,
    )

    assert result.ok
    assert len(client.calls) == 1
    requirement = json.loads(client.calls[0][1]["content"])["job_requirement"]
    for field, value in job["card_prescreen"].items():
        # competitor_card_check 只作用于卡片阶段，不传给 AI。
        if field.startswith("competitor_") and field != "competitor_card_check":
            assert requirement[field] == value
    assert len(requirement["competitor_companies"]) == 53
    assert "card_prescreen" not in requirement
    assert "active_status_keywords" not in requirement
    assert "max_age" not in requirement
    assert job == original
    saved = json.loads((tmp_path / "screening_results.json").read_text(encoding="utf-8"))
    assert saved["candidates"][0]["passed"] is False


def test_jobs_without_competitor_rules_do_not_receive_them(tmp_path):
    client = RejectingClient()
    ResumeAnalyzer(config=_config(tmp_path), client=client).analyze(
        job_requirement={"job_name": "其他岗位", "card_prescreen": {"max_age": 30}},
        resume="测试简历",
    )
    requirement = json.loads(client.calls[0][1]["content"])["job_requirement"]
    assert not any(field.startswith("competitor_") for field in requirement)
