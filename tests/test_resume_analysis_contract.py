"""Offline regressions for schema failures vs genuine job-rule rejections."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from ai.errors import ResumeAnalysisUnavailableError
from ai.resume_analyzer import ResumeAnalyzer, _coerce_bool, _reason_concludes_pass
from ai.resume_rules import evidence_contract_errors, evidence_rules
from tests.test_ai_capabilities import _config


OUTCOME_JOB = {"resume_analysis": {"recent_outcomes": {
    "limit": 2, "min_experiences": 1, "min_metrics": 2, "mode": "any",
    "exclude_quantity_only": True, "distinct_metrics": False,
}}}
PLATFORM_JOB = {"resume_analysis": {"platform_experience": {
    "keywords": ["Amazon"], "min_months": 12, "regions": ["美国"],
}}}
FALLBACK_JOB = {"resume_analysis": {"platform_experience": {
    "keywords": ["Amazon", "亚马逊"], "min_months": 8, "regions": ["美国", "美元", "美金"],
    "market_match_in_content": True,
    "fallback": {
        "self_intro": True, "recent_works": 2,
        "allow_missing_platform": True, "allow_missing_region": True,
    },
}}}


def measured_work():
    return {
        "title": "运营", "content": "转化率提高5%；ROI达到3.2",
        "outcomes": [
            {"metric": "转化率", "kind": "rate", "evidence": "转化率提高5%"},
            {"metric": "ROI", "kind": "ratio", "evidence": "ROI达到3.2"},
        ],
    }


class Responses:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def complete_json(self, messages):
        self.calls.append(messages)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def get_completion_audit(self):
        return {"attempts": [{"finish_reason": "stop", "number": len(self.calls)}]}


def analyze(tmp_path, raw, job=None, **resume):
    return ResumeAnalyzer(config=_config(tmp_path), client=Responses(raw)).analyze(
        job_requirement=job or {}, resume=resume or "测试简历",
    )


@pytest.mark.parametrize("value", [True, 1, "true", " YES ", "通过", "符合", "合格", "满足全部", "OK"])
def test_explicit_true_values_with_valid_outcome_evidence_are_accepted(tmp_path, value):
    result = analyze(tmp_path, {
        "passed": value, "reason": "最终结论：通过。", "work_evidence": [measured_work()],
    }, OUTCOME_JOB)
    assert result["passed"] is True
    assert result["analysis_status"] == "passed"
    assert "selected_work_index" not in result
    assert result["analysis_audit"]["raw_result"]["passed"] == value
    assert "analysis_audit" not in result["analysis_audit"]["final_result"]
    json.dumps(result)


@pytest.mark.parametrize("value", [False, 0, "false", "no", "不通过", "不符合", "未满足", "fail"])
def test_explicit_false_values_are_never_truthy_strings(tmp_path, value):
    assert analyze(tmp_path, {"passed": value, "reason": "最终结论：不通过。"})["passed"] is False


@pytest.mark.parametrize("value", [None, 2, -1, [], {}, "可能", "符合部分条件", "false-ish"])
def test_unknown_boolean_is_invalid_not_rejection(tmp_path, value):
    assert _coerce_bool(value) is None
    with pytest.raises(ResumeAnalysisUnavailableError) as exc:
        analyze(tmp_path, {"passed": value, "reason": "需要核实。"})
    assert exc.value.code == "invalid_response"


@pytest.mark.parametrize("raw", [None, [], {}, {"passed": True}, {"passed": True, "reason": ""}])
def test_missing_base_fields_never_become_rejection(tmp_path, raw):
    with pytest.raises(ResumeAnalysisUnavailableError):
        analyze(tmp_path, raw)


@pytest.mark.parametrize("reason", [
    "综上，第3、4项硬性条件未满足。",
    "综上，通过其他条件，但缺英语六级，最终结论：不通过。",
    "综上，条件不满足。", "综上，部分条件通过。", "工作条件满足，但未通过筛选。",
])
def test_negative_or_partial_conclusions_do_not_count_as_pass(tmp_path, reason):
    assert not _reason_concludes_pass(reason)
    assert analyze(tmp_path, {"passed": False, "reason": reason})["passed"] is False


@pytest.mark.parametrize("passed,reason", [
    (False, "最终结论：通过。"), (False, "综上，所有硬性条件均满足，通过。"),
    (True, "综上，条件未满足。"), (True, "最终结论：不通过。"),
])
def test_conflicting_final_decision_requires_reanalysis(tmp_path, passed, reason):
    with pytest.raises(ResumeAnalysisUnavailableError) as exc:
        analyze(tmp_path, {"passed": passed, "reason": reason})
    assert exc.value.code == "conflicting_conclusion"
    assert exc.value.audit["raw_result"]["passed"] is passed


@pytest.mark.parametrize("works", [
    None, {}, [], [None], [{"title": "实习生", "content": "转化率提高5%"}],
    [{"title": "实习生", "content": "转化率提高5%", "outcomes": None}],
    [{"title": "实习生", "content": "转化率提高5%", "outcomes": [{"metric": "转化率", "kind": "rate"}]}],
])
def test_historical_missing_or_partial_evidence_is_not_business_failure(tmp_path, works):
    # Reproduces the saved title/content-only and outcome-without-evidence cases.
    with pytest.raises(ResumeAnalysisUnavailableError) as exc:
        analyze(tmp_path, {"passed": True, "reason": "最终结论：通过。", "work_evidence": works}, OUTCOME_JOB)
    assert exc.value.code == "incomplete_evidence"
    assert exc.value.audit["repair"]["attempted"] is False


def test_valid_empty_outcomes_remain_business_rejection(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "work_evidence": [{"title": "实习生", "content": "协助日常运营", "outcomes": []}],
    }, OUTCOME_JOB)
    assert result["passed"] is False
    assert result["analysis_status"] == "rejected"
    assert "各段有效指标数：[0]" in result["reason"]
    assert result["analysis_audit"]["repair"]["attempted"] is False


def test_explicit_no_experience_is_business_rejection(tmp_path):
    result = analyze(tmp_path, {
        "passed": False, "reason": "简历没有工作经历。最终结论：不通过。", "work_evidence": [],
    }, OUTCOME_JOB)
    assert result["passed"] is False


def test_platform_only_job_does_not_require_outcomes(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。", "selected_work_index": "0",
        "work_evidence": [{"title": "Amazon运营", "content": "负责美国站", "start": "2023.01", "end": "2025.01", "region": "美国"}],
    }, PLATFORM_JOB)
    assert result["passed"] is True
    assert result["selected_work_index"] == 0


def test_no_relevant_platform_job_does_not_need_selected_index(tmp_path):
    result = analyze(tmp_path, {
        "passed": False, "reason": "没有所需平台经历。最终结论：不通过。",
        "work_evidence": [{"title": "客服", "content": "服务客户"}],
    }, PLATFORM_JOB)
    assert result["passed"] is False


def test_platform_without_region_requirement_does_not_invent_one(tmp_path):
    job = copy.deepcopy(PLATFORM_JOB)
    del job["resume_analysis"]["platform_experience"]["regions"]
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。", "selected_work_index": 0,
        "work_evidence": [{"title": "Amazon运营", "content": "运营店铺", "start": "2023.01", "end": "2025.01"}],
    }, job)
    assert result["passed"] is True


def test_platform_required_field_missing_differs_from_explicit_unknown():
    work = {"title": "Amazon运营", "content": "负责美国站", "start": "", "end": "", "region": ""}
    rules = evidence_rules(PLATFORM_JOB)
    assert not evidence_contract_errors({"work_evidence": [work], "selected_work_index": 0}, rules)
    del work["start"]
    assert evidence_contract_errors({"work_evidence": [work], "selected_work_index": 0}, rules)


@pytest.mark.parametrize("repair", [RuntimeError("服务暂不可用"), {}, {"work_evidence": []}])
def test_failed_repair_is_audited_and_never_converted_to_rejection(tmp_path, repair):
    image = tmp_path / "resume.png"
    image.write_bytes(b"image")
    client = Responses({"passed": True, "reason": "最终结论：通过。"}, repair)
    with pytest.raises(ResumeAnalysisUnavailableError) as exc:
        ResumeAnalyzer(config=_config(tmp_path), client=client).analyze(
            job_requirement=OUTCOME_JOB, resume={"resume_screenshots": [str(image)]},
        )
    assert len(client.calls) == 2
    assert exc.value.audit["repair"]["attempted"] is True
    json.dumps(exc.value.audit)


def test_repaired_evidence_cannot_override_employment_rejection(tmp_path):
    image = tmp_path / "resume.png"
    image.write_bytes(b"image")
    job = copy.deepcopy(OUTCOME_JOB)
    job["resume_analysis"]["employment_status_blacklist"] = ["在职-暂不考虑"]
    raw = {"passed": True, "reason": "最终结论：通过。", "employment_status": "在职-暂不考虑"}
    client = Responses(raw, {"passed": True, "work_evidence": [measured_work()]})
    result = ResumeAnalyzer(config=_config(tmp_path), client=client).analyze(
        job_requirement=job, resume={"resume_screenshots": [str(image)]},
    )
    assert result["passed"] is False
    assert "命中黑名单" in result["reason"]
    assert len(client.calls) == 2
    assert result["analysis_audit"]["raw_result"] == raw


@pytest.mark.parametrize("job_file", sorted((Path(__file__).resolve().parents[1] / "config/jobs").glob("*.yaml")), ids=lambda path: path.stem)
def test_each_current_job_accepts_its_own_required_field_shape(tmp_path, job_file):
    config = yaml.safe_load(job_file.read_text(encoding="utf-8"))
    job = config.get("recruitment") or config["job_profile"]
    schema = json.loads(ResumeAnalyzer._build_user_prompt(job, "测试"))["required_output_json_schema"]
    rules = evidence_rules(job)
    raw = {"passed": True, "reason": "全部硬性条件满足。最终结论：通过。", "employment_status": "离职-随时到岗"}
    if "expected_salary" in schema:
        raw["expected_salary"] = "5-6K"
    if "self_intro" in schema:
        raw["self_intro"] = "两年亚马逊精铺经验，主攻美国站"
    if rules:
        work = measured_work()
        if not rules.get("recent_outcomes"):
            del work["outcomes"]
        if rules.get("platform_experience"):
            work.update(title="Amazon TikTok运营", start="2023.01", end="2025.01", region="美国")
            raw["selected_work_index"] = 0
            if not rules.get("recent_outcomes"):
                assert "outcomes" not in schema["work_evidence"]["items"]
        else:
            assert "selected_work_index" not in schema
            assert not {"start", "end", "region"} & schema["work_evidence"]["items"].keys()
        raw["work_evidence"] = [work]
    else:
        assert "work_evidence" not in schema
    result = analyze(tmp_path, raw, job)
    assert result["passed"] is True, job_file.name


def _fallback_work(**overrides):
    work = {
        "title": "跨境电商运营", "content": "负责店铺日常运营",
        "start": "2025.01", "end": "2026.01", "region": "",
    }
    work.update(overrides)
    return work


def test_platform_fallback_reads_self_intro_when_no_work_mentions_platform(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": None,
        "self_intro": "两年亚马逊精铺经验，主攻美国站。",
        "work_evidence": [_fallback_work(content="负责店铺运营，月销3万美金", region="美国")],
    }, FALLBACK_JOB)
    assert result["passed"] is True


def test_platform_fallback_combines_self_intro_and_recent_work_market(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": None,
        "self_intro": "两年亚马逊精铺经验。",
        "work_evidence": [
            _fallback_work(content="负责店铺运营，月销3万美金"),
        ],
    }, FALLBACK_JOB)
    assert result["passed"] is True


def test_platform_fallback_uses_self_intro_for_market_signal(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": 0,
        "self_intro": "主攻美国站，客单价30-90美金。",
        "work_evidence": [
            {"title": "Amazon运营", "content": "负责店铺运营",
             "start": "2025.01", "end": "2026.01", "region": ""},
        ],
    }, FALLBACK_JOB)
    assert result["passed"] is True


def test_platform_fallback_does_not_relax_duration(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": None,
        "self_intro": "两年亚马逊运营经验，主攻美国站。",
        "work_evidence": [
            _fallback_work(start="2025.12", end="2026.06", region="美国",
                           content="月销3万美金"),
        ],
    }, FALLBACK_JOB)
    assert result["passed"] is False
    assert "未满8个月" in result["reason"]


def test_platform_fallback_without_self_intro_stays_strict(tmp_path):
    # self_intro 是兜底用字段，不是硬性条件：缺失时不能把缺平台经历误判为通过。
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": None,
        "work_evidence": [_fallback_work(region="美国")],
    }, FALLBACK_JOB)
    assert result["passed"] is False
    assert "没有可核验任职时长的平台相关经历" in result["reason"]


def test_platform_without_fallback_stays_strict(tmp_path):
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": None,
        "work_evidence": [_fallback_work(region="美国")],
    }, {"resume_analysis": {"platform_experience": PLATFORM_JOB["resume_analysis"]["platform_experience"]}})
    assert result["passed"] is False
    assert "没有可核验任职时长的平台相关经历" in result["reason"]


MARKET_JOB = {"resume_analysis": {"platform_experience": {
    "keywords": ["Amazon", "亚马逊"], "min_months": 8,
    "regions": ["北美", "美国", "加拿大", "墨西哥", "美元", "美金", "$"],
    "market_match_in_content": True,
    "market_geo_keywords": ["北美", "美国", "加拿大", "墨西哥"],
    "market_veto_keywords": ["日本", "欧洲", "英国", "德国"],
    "fallback": {
        "self_intro": True, "recent_works": 2,
        "allow_missing_platform": True, "allow_missing_region": True,
    },
}}}


def _market_work(content, region="", start="2025.01", end="2026.01"):
    return {
        "title": "Amazon运营", "content": content,
        "start": start, "end": end, "region": region,
    }


def test_currency_only_market_passes_without_geo_word(tmp_path):
    # 胡慧玲型：只写美金、没有站点 → C 方案放行。
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": 0,
        "work_evidence": [_market_work("店铺稳定月销8-9万美金，毛利率15%")],
    }, MARKET_JOB)
    assert result["passed"] is True


def test_non_north_america_market_vetoes_currency(tmp_path):
    # 郭晓玲型：明确日本站，即使有美金也不算通过。
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": 0,
        "work_evidence": [_market_work("负责日本站运营，月销售额约10万美金", region="日本站")],
    }, MARKET_JOB)
    assert result["passed"] is False
    assert "工作地区/负责市场不符合要求或无法确认" in result["reason"]


def test_north_america_geo_wins_over_non_na_market(tmp_path):
    # 同时做美国站和日本站：以北美为准，放行。
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": 0,
        "work_evidence": [_market_work("负责美国站与日本站，月销10万美金", region="美国站")],
    }, MARKET_JOB)
    assert result["passed"] is True


def test_veto_in_self_intro_blocks_fallback(tmp_path):
    # 工作经历没有市场信息，自我介绍写的是日本站 → 兜底也不能放行。
    result = analyze(tmp_path, {
        "passed": True, "reason": "最终结论：通过。",
        "selected_work_index": 0,
        "self_intro": "日本站运营经验，月销8万美金。",
        "work_evidence": [_market_work("负责店铺日常运营")],
    }, MARKET_JOB)
    assert result["passed"] is False
    assert "工作地区/负责市场不符合要求或无法确认" in result["reason"]
