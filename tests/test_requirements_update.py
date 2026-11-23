import copy
import json
from pathlib import Path

import pytest
import yaml

from ai.resume_analyzer import ResumeAnalyzer
from ai.errors import ResumeAnalysisUnavailableError
from ai.resume_rules import WORK_SCHEMA, enforce_evidence, evidence_rules
from boss_hr.domain.card_prescreen import evaluate_card_prescreen
from tests.test_ai_capabilities import _config
from tests.test_card_prescreen import _complete_candidate, _job_recruitment
from tests.test_live_card_transaction import _load, DOWNLOAD_SCRIPT


def work(*outcomes, title="运营", content="", start="2025.01", end="2026.08", region=""):
    return dict(title=title, content=content or "；".join(o[2] for o in outcomes), start=start, end=end,
                region=region, outcomes=[dict(metric=m, kind=k, evidence=e) for m, k, e in outcomes])


MONEY = ("GMV", "money", "GMV 10万元")
SPEND = ("消耗", "spend", "投放消耗20万元")
RATE = ("转化率", "rate", "转化率提升5%")


def check(job, works, **extra):
    return enforce_evidence(dict(work_evidence=works, **extra), evidence_rules(_job_recruitment(job)))


@pytest.mark.parametrize("name", ["全职主播", "兼职主播"])
def test_anchor_uses_current_job_schema_without_unconfigured_evidence_rule(name):
    # These jobs currently express their requirements in the AI prompt only.
    assert evidence_rules(_job_recruitment(name)) == {}
    payload = json.loads(ResumeAnalyzer._build_user_prompt(_job_recruitment(name), "测试"))
    assert "work_evidence" not in payload["required_output_json_schema"]


@pytest.mark.parametrize("name", ["运营助理"])
def test_two_outcomes_must_belong_to_one_recent_job(name):
    ratio = ("ROI", "ratio", "ROI达到3.2")
    assert not check(name, [work(ratio, RATE), work()])
    assert not check(name, [work(), work(ratio, RATE)])
    assert check(name, [work(ratio), work(RATE)])
    assert check(name, [work(), work(), work(ratio, RATE)])
    assert check(name, [work(ratio, ratio)])


def test_junior_single_outcome_in_one_recent_job_is_enough():
    ratio = ("ROI", "ratio", "ROI达到3.2")
    assert check("初级剪拍", [work(ratio), work()]) == []
    assert check("初级剪拍", [work(), work(ratio)]) == []
    assert check("初级剪拍", [work()]) != []


@pytest.mark.parametrize("kind,text", [
    ("quantity", "月产100条视频"), ("quantity", "举办10次活动"),
    ("quantity", "管理5人"), ("quantity", "剪辑20个素材"),
    ("date", "2026年8月"), ("zero_to_one", "0-1起号"),
    ("growth", "产量提升100条"), ("money", "制作100篇文章"),
])
@pytest.mark.parametrize("name", ["初级剪拍", "运营助理"])
def test_counts_dates_zero_to_one_are_not_outcomes(name, kind, text):
    # 只有一个无效成果时，两个岗位都不应放行。
    assert check(name, [work(("另一项", kind, text))]) != []


def test_junior_money_and_spend_are_valid_and_evidence_must_be_bound():
    # 金额/消耗算有效成果（1 条即可）
    assert check("初级剪拍", [work(MONEY, SPEND)]) == []
    # content 不含 evidence 时绑定失败，不计入有效成果
    assert check("初级剪拍", [work(MONEY, SPEND, content="参与制作素材")]) != []
    assert check("初级剪拍", [work(("利润", "money", "单场利润2000元"), RATE)]) == []


def test_biandao_counts_traffic_metrics_as_valid_outcomes():
    # 2026-11-12 口径：编导把播放量/曝光/观看/涨粉算有效成果，kind 不作要求。
    assert check("编导", [work(("播放量", "other", "单条播放量100w"))]) == []
    assert check("编导", [work(("播放量", "quantity", "累计播放500w"))]) == []
    assert check("编导", [work(("曝光", "other", "曝光300w"))]) == []
    assert check("编导", [work(("涨粉", "other", "涨粉2w"))]) == []
    # 纯产出量依旧不算：没有流量关键词，仍走严格白名单。
    assert check("编导", [work(("产出", "quantity", "产出100条脚本"))]) != []
    # 不带数字不算。
    assert check("编导", [work(("播放量", "other", "播放量表现优秀"))]) != []


def test_traffic_metrics_are_not_outcomes_for_other_jobs():
    # 只有显式配置 traffic_keywords 的岗位才认播放量。
    assert check("初级剪拍", [work(("播放量", "other", "单条播放量100w"))]) != []
    assert check("高级剪拍", [work(("播放量", "other", "单条播放量100w"))]) != []


@pytest.mark.parametrize("keyword", ["亚马逊", "Amazon", "AMAZON", "fba", "FBA"])
@pytest.mark.parametrize("field", ["title", "content"])
def test_amazon_platform_can_be_in_title_or_content(keyword, field):
    item = work(region="美国", **{field: keyword + "运营"})
    assert not check("亚马逊运营", [item], selected_work_index=0)


@pytest.mark.parametrize("region", ["美国", "美区", "北美", "加拿大", "墨西哥"])
def test_tiktok_region_on_selected_experience(region):
    assert not check("tiktok运营", [work(content="负责TIKTOK店铺", region=region)], selected_work_index=0)
    assert check("tiktok运营", [work(content="负责TIKTOK店铺", region="东南亚"), work(region=region)], selected_work_index=0)


def test_platform_longest_relevant_job_and_minimum_duration():
    assert check("亚马逊运营", [work(title="跨境运营", region="美国")], selected_work_index=0)
    assert check("亚马逊运营", [work(title="Amazon运营", start="2026.01", region="美国")], selected_work_index=0)
    assert check("tiktok运营", [work(title="TikTok运营", start="2026.01", region="美国")], selected_work_index=0)
    works = [work(title="TikTok运营", start="2025.08", region="美国"),
             work(title="TikTok运营", start="2023.01", region="东南亚")]
    assert check("tiktok运营", works, selected_work_index=0)
    assert check("tiktok运营", works, selected_work_index=1)
    works[1]["region"] = "加拿大"
    assert not check("tiktok运营", works, selected_work_index=1)
    works[1]["end"] = ""
    assert check("tiktok运营", works, selected_work_index=1)


def test_all_job_yamls_require_work_experience_even_if_switch_disabled():
    for source in (Path(__file__).resolve().parents[1] / "config/jobs").glob("*.yaml"):
        cfg = yaml.safe_load(source.read_text(encoding="utf-8"))
        job = cfg.get("recruitment") or cfg["job_profile"]
        assert job["card_prescreen"]["require_work_experience"] is True
        card = _complete_candidate()
        card["geekCard"]["geekWorks"] = []
        for disabled in (False, True):
            current = copy.deepcopy(job)
            current["card_prescreen"]["enabled"] = not disabled
            result = evaluate_card_prescreen(card, current)
            assert not result.allow_full_resume
            assert result.status == "rejected"


def test_explicit_missing_work_marker_blocks_stale_api_work():
    card = _complete_candidate()
    card["geekCard"]["workExperienceDesc"] = "未填写工作经历"
    assert not evaluate_card_prescreen(card, _job_recruitment("全职主播")).allow_full_resume


def test_model_pass_without_required_evidence_is_unavailable(tmp_path):
    class Fake:
        def complete_json(self, messages):
            payload = json.loads(messages[1]["content"])
            assert "work_evidence" in payload["required_output_json_schema"]
            return dict(passed=True, reason="综上，通过。")
    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=Fake())
    with pytest.raises(ResumeAnalysisUnavailableError) as captured:
        analyzer.analyze(job_requirement=_job_recruitment("运营助理"), resume="测试")
    assert captured.value.code == "incomplete_evidence"


def test_work_evidence_schema_and_validator_use_same_array_shape(tmp_path):
    assert WORK_SCHEMA["type"] == "array"
    assert "直接是JSON数组" in WORK_SCHEMA["description"]

    wrapped = {
        "items": [work(
            ("转化率", "rate", "转化率提升5%"),
            ("ROI", "ratio", "ROI达到3.2"),
        )]
    }
    assert not enforce_evidence(
        {"work_evidence": wrapped},
        evidence_rules(_job_recruitment("运营助理")),
    )

    class Fake:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "综上，满足全部硬性条件。",
                # Compatibility guard for responses emitted with the old prompt.
                "work_evidence": wrapped,
            }

    result = ResumeAnalyzer(config=_config(tmp_path), client=Fake()).analyze(
        job_requirement=_job_recruitment("运营助理"), resume="测试"
    )
    assert result["passed"] is True
    assert isinstance(result["work_evidence"], list)
    assert len(result["work_evidence"]) == 1
    assert "缺少可核验" not in result["reason"]


def test_model_pass_repairs_incomplete_platform_evidence_from_same_screenshot(tmp_path):
    screenshot = tmp_path / "resume.jpg"
    screenshot.write_bytes(b"resume-image")
    complete_short = work(
        title="亚马逊运营",
        content="负责日本站",
        start="2026.07",
        end="2026.08",
        region="日本",
    )
    complete_long = work(
        MONEY,
        RATE,
        title="亚马逊运营",
        content="负责亚马逊美国站；GMV 10万元；转化率提升5%",
        start="2025.03",
        end="2026.05",
        region="美国",
    )

    class Fake:
        def __init__(self):
            self.calls = 0

        def complete_json(self, messages):
            self.calls += 1
            if self.calls == 1:
                return {
                    "passed": True,
                    "reason": "最长亚马逊经历14个月，负责美国站，全部硬性条件满足，通过。",
                    "employment_status": "离职-随时到岗",
                    "work_evidence": [
                        complete_short,
                        {
                            "title": "亚马逊运营",
                            "content": "负责亚马逊美国站；GMV 10万元；转化率提升5%",
                        },
                    ],
                    "selected_work_index": None,
                }
            assert any(
                isinstance(message.get("content"), list)
                for message in messages
            ), "repair must reuse the same screenshot-bearing conversation"
            return {
                "work_evidence": [complete_short, complete_long],
                "selected_work_index": 1,
            }

    fake = Fake()
    result = ResumeAnalyzer(config=_config(tmp_path), client=fake).analyze(
        job_requirement=_job_recruitment("亚马逊运营"),
        resume={
            "name": "孙立宇",
            "resume_screenshots": [str(screenshot)],
        },
    )

    assert fake.calls == 2
    assert result["passed"] is True
    assert result["selected_work_index"] == 1
    assert result["work_evidence"][1]["start"] == "2025.03"
    assert "工作经历规则" not in result["reason"]


@pytest.mark.parametrize(("job", "title"), [
    ("亚马逊运营", "Amazon运营"),
    ("tiktok运营", "TikTok运营"),
])
def test_legacy_wrapped_platform_evidence_is_not_reported_missing(job, title):
    wrapped = {
        "items": [work(
            ("转化率", "rate", "转化率提升5%"),
            title=title,
            start="2025.01",
            end="2026.08",
            region="美国",
        )]
    }
    errors = enforce_evidence(
        {"work_evidence": wrapped, "selected_work_index": 0},
        evidence_rules(_job_recruitment(job)),
    )
    assert errors == []


def test_all_configured_evidence_jobs_treat_list_and_wrapper_identically():
    jobs_dir = Path(__file__).resolve().parents[1] / "config/jobs"
    checked = set()
    sample = [work(
        ("转化率", "rate", "转化率提升5%"),
        ("ROI", "ratio", "ROI达到3.2"),
        title="Amazon TikTok运营",
        start="2025.01",
        end="2026.08",
        region="美国",
    )]
    for source in jobs_dir.glob("*.yaml"):
        cfg = yaml.safe_load(source.read_text(encoding="utf-8"))
        job = cfg.get("recruitment") or cfg["job_profile"]
        rules = evidence_rules(job)
        if not rules:
            continue
        checked.add(job["job_name"])
        common = {"selected_work_index": "0"}
        list_errors = enforce_evidence(
            {**common, "work_evidence": sample}, rules,
        )
        wrapped_errors = enforce_evidence(
            {**common, "work_evidence": {"items": sample}}, rules,
        )
        assert wrapped_errors == list_errors, source.name
        assert "缺少可核验的逐段工作经历证据" not in wrapped_errors

    assert checked == {
        "亚马逊运营",
        "tiktok运营",
        "初级剪拍",
        "高级剪拍",
        "编导",
        "运营助理",
        "运营助理（前期需要在中控岗位轮岗）",
    }


def test_model_reject_cannot_be_upgraded_by_narrative(tmp_path):
    class Fake:
        def complete_json(self, messages):
            return dict(passed=False, reason="综上，通过。", work_evidence=[work(MONEY, RATE)])
    with pytest.raises(ResumeAnalysisUnavailableError) as captured:
        ResumeAnalyzer(config=_config(tmp_path), client=Fake()).analyze(
            job_requirement=_job_recruitment("运营助理"), resume="测试")
    assert captured.value.code == "conflicting_conclusion"


@pytest.mark.parametrize("change", [
    {"geekWorks": []}, {"workExperienceDesc": "未填写工作经历"},
    {"activeTimeDesc": "3日内活跃"}, {"expectCityName": "北京"},
    {"expectPosition": "行政"},
])
def test_live_processor_rechecks_before_any_browser_access(change):
    module = _load(DOWNLOAD_SCRIPT, "requirements_gate_test")
    processor = module.LiveCandidateProcessor.__new__(module.LiveCandidateProcessor)
    processor.processed_ids = set()
    processor.failed = []
    processor.requirement = _job_recruitment("全职主播")
    processor._commit = lambda: None
    candidate = _complete_candidate(**change)
    candidate["_card_prescreen"] = {"passed": True, "allow_full_resume": True}
    # Bare objects expose no browser API: touching the page would fail the test.
    result = processor.process(candidate, object(), object())
    assert result["terminal_status"] == "card_prescreen_rejected"
    assert processor.failed[0]["stage"] == "card_prescreen"
