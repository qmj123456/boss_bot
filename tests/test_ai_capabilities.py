import json
from pathlib import Path

import yaml
import pytest

from ai.client import OpenAICompatibleClient
from ai.message_generator import MessageGenerator
from ai.resume_analyzer import ResumeAnalyzer
from ai.errors import ResumeAnalysisUnavailableError
from boss_hr.config_manager import ConfigManager


class FakeAIClient:
    def __init__(self):
        self.json_messages = None
        self.text_messages = None

    def complete_json(self, messages):
        self.json_messages = messages
        return {
            "passed": True,
            "reason": "候选人经验与岗位要求吻合",
        }

    def complete_text(self, messages):
        self.text_messages = messages
        return "  您好，看到您的 Python 经验和我们岗位比较匹配，方便聊一下吗？  "


def _config(tmp_path):
    config_dir = tmp_path / "config"
    prompt_dir = config_dir / "prompt"
    prompt_dir.mkdir(parents=True)
    (config_dir / "ai.yaml").write_text(
        "ai:\n"
        "  api_key: test-key\n"
        "  base_url: https://api.example.test/v1\n"
        "  model: external-model\n"
        "  temperature: 0.1\n"
        "  max_tokens: 800\n"
        "  timeout_seconds: 9\n"
        "  prompts:\n"
        "    resume_analysis: custom_resume.md\n"
        "    message_generation: custom_message.md\n",
        encoding="utf-8",
    )
    (prompt_dir / "custom_resume.md").write_text("外置简历分析Prompt", encoding="utf-8")
    (prompt_dir / "custom_message.md").write_text("外置招呼语Prompt", encoding="utf-8")
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def test_resume_analyzer_uses_external_prompt_and_normalizes_json(tmp_path):
    fake = FakeAIClient()
    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=fake)

    result = analyzer.analyze(
        job_requirement={"job_name": "Python工程师"},
        resume={"name": "张三", "skills": ["Python"]},
    )

    assert fake.json_messages[0]["content"] == "外置简历分析Prompt"
    assert "required_output_json_schema" in fake.json_messages[1]["content"]
    user_payload = json.loads(fake.json_messages[1]["content"])
    assert user_payload["current_date"]
    assert sorted(user_payload["required_output_json_schema"]) == [
        "employment_status", "passed", "reason",
    ]
    assert {key: result[key] for key in ("passed", "reason")} == {
        "passed": True,
        "reason": "候选人经验与岗位要求吻合",
    }


def test_resume_analyzer_sends_online_resume_screenshots_as_multimodal_images(tmp_path):
    first = tmp_path / "page_01.jpg"
    second = tmp_path / "page_02.png"
    first.write_bytes(b"jpeg-image")
    second.write_bytes(b"png-image")
    fake = FakeAIClient()
    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=fake)

    analyzer.analyze(
        job_requirement={"job_name": "Python工程师"},
        resume={
            "name": "张三",
            "source": "online_resume_screenshot",
            "resume_screenshots": [str(first), str(second)],
        },
    )

    content = fake.json_messages[1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["resume"]["resume_screenshots"]["count"] == 2
    assert str(first) not in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_resume_analyzer_moves_legacy_employment_blacklist_to_resume_analysis(tmp_path):
    class StatusAIClient:
        def __init__(self):
            self.messages = None

        def complete_json(self, messages):
            self.messages = messages
            return {
                "passed": True,
                "reason": "其他硬性条件均满足。综上，通过。",
                "employment_status": "在职-暂不考虑",
            }

    fake = StatusAIClient()
    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=fake)
    result = analyzer.analyze(
        job_requirement={
            "job_name": "初级剪拍",
            "card_prescreen": {
                "employment_status_blacklist": ["在职-暂不考虑", "在校-暂不考虑"],
                "max_age": 30,
            },
        },
        resume={"name": "张三"},
    )

    payload = json.loads(fake.messages[1]["content"])
    assert "card_prescreen" not in payload["job_requirement"]
    assert payload["job_requirement"]["resume_analysis"] == {
        "employment_status_blacklist": ["在职-暂不考虑", "在校-暂不考虑"],
    }
    assert result["passed"] is False
    assert result["employment_status"] == "在职-暂不考虑"
    assert "命中黑名单" in result["reason"]


def test_resume_analyzer_enforces_employment_whitelist_after_screenshot_read(tmp_path):
    class StatusAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "其他硬性条件均满足。综上，通过。",
                "employment_status": "在校-暂不考虑",
            }

    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=StatusAIClient())
    result = analyzer.analyze(
        job_requirement={
            "job_name": "全职主播",
            "resume_analysis": {
                "employment_status_whitelist": ["离职-随时到岗"],
            },
        },
        resume={"name": "李四"},
    )

    assert result["passed"] is False
    assert "不在白名单" in result["reason"]


def test_operation_resume_does_not_filter_employment_status(tmp_path):
    requirement_path = Path(__file__).resolve().parents[1] / "config" / "jobs" / "运营.yaml"
    requirement = yaml.safe_load(requirement_path.read_text(encoding="utf-8"))["recruitment"]

    class StatusAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "岗位其他硬性条件满足。综上，通过。",
                "employment_status": "在职-月内到岗",
            }

    result = ResumeAnalyzer(config=_config(tmp_path), client=StatusAIClient()).analyze(
        job_requirement=requirement,
        resume={"name": "测试候选人"},
    )

    assert result["passed"] is True
    assert "求职状态规则" not in result["reason"]


def test_operation_assistant_major_rule_is_not_rechecked_in_resume_prompt():
    requirement_path = Path(__file__).resolve().parents[1] / "config" / "jobs" / "运营助理.yaml"
    requirement = yaml.safe_load(requirement_path.read_text(encoding="utf-8"))["recruitment"]

    payload = json.loads(ResumeAnalyzer._build_user_prompt(requirement, {"name": "测试候选人"}))
    sent_requirement = payload["job_requirement"]

    assert "stem_major_categories" not in sent_requirement
    assert "专业须属于工科或理科" not in str(sent_requirement)
    assert "最近一段教育经历的专业必须属于" not in str(sent_requirement)


def test_resume_analyzer_fails_closed_when_configured_status_is_unreadable(tmp_path):
    class MissingStatusAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "其他硬性条件均满足。综上，通过。",
                "employment_status": "",
            }

    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=MissingStatusAIClient())
    result = analyzer.analyze(
        job_requirement={
            "job_name": "初级剪拍",
            "resume_analysis": {
                "employment_status_blacklist": ["在职-暂不考虑", "在校-暂不考虑"],
            },
        },
        resume={"name": "王五"},
    )

    assert result["passed"] is False
    assert result["employment_status"] == ""
    assert "无法从完整简历截图确认" in result["reason"]


def test_resume_analyzer_enforces_expected_salary_lower_bound_range(tmp_path):
    class SalaryAIClient:
        def __init__(self, salary):
            self.salary = salary

        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "其他硬性条件均满足。综上，通过。",
                "expected_salary": self.salary,
            }

    requirement = {
        "job_name": "客服",
        "resume_analysis": {
            "expected_salary_min": 5000,
            "expected_salary_max": 6000,
            "expected_salary_basis": "lower_bound",
            "expected_salary_accept_negotiable": True,
        },
    }
    config = _config(tmp_path)

    accepted = ResumeAnalyzer(
        config=config, client=SalaryAIClient("5-7K")
    ).analyze(job_requirement=requirement, resume={"name": "甲"})
    too_high = ResumeAnalyzer(
        config=config, client=SalaryAIClient("7-8K")
    ).analyze(job_requirement=requirement, resume={"name": "乙"})
    too_low = ResumeAnalyzer(
        config=config, client=SalaryAIClient("4-6K")
    ).analyze(job_requirement=requirement, resume={"name": "丁"})
    negotiable = ResumeAnalyzer(
        config=config, client=SalaryAIClient("面议")
    ).analyze(job_requirement=requirement, resume={"name": "丙"})

    assert accepted["passed"] is True
    assert accepted["expected_salary"] == "5-7K"
    assert too_high["passed"] is False
    assert "下限未落在5K-6K范围内" in too_high["reason"]
    assert too_low["passed"] is False
    assert "下限未落在5K-6K范围内" in too_low["reason"]
    assert negotiable["passed"] is True


def test_resume_analyzer_does_not_apply_salary_rule_to_other_jobs(tmp_path):
    class OtherJobAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "岗位自身的硬性条件均满足。综上，通过。",
                "expected_salary": "20-30K",
            }

    result = ResumeAnalyzer(
        config=_config(tmp_path), client=OtherJobAIClient()
    ).analyze(
        job_requirement={"job_name": "高级剪拍"},
        resume={"name": "丁"},
    )

    # 没有 expected_salary_min/max 的岗位不会启用客服薪资规则。
    assert result["passed"] is True
    assert "期望薪资规则" not in result["reason"]


def test_junior_video_counts_two_results_even_when_metric_name_repeats(tmp_path):
    class OutcomeAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "全部硬性条件满足。综上，通过。",
                "work_evidence": [{
                    "title": "内容运营",
                    "content": "项目A实现GMV 10万元，项目B实现GMV 20万元",
                    "start": "2025.01",
                    "end": "2026.01",
                    "region": "深圳",
                    "outcomes": [
                        {"metric": "GMV", "kind": "money", "evidence": "GMV 10万元"},
                        {"metric": "GMV", "kind": "money", "evidence": "GMV 20万元"},
                    ],
                }],
            }

    result = ResumeAnalyzer(
        config=_config(tmp_path), client=OutcomeAIClient()
    ).analyze(
        job_requirement={
            "job_name": "初级剪拍",
            "resume_analysis": {
                "recent_outcomes": {
                    "limit": 2,
                    "min_experiences": 1,
                    "min_metrics": 2,
                    "mode": "any",
                    "exclude_quantity_only": True,
                    "distinct_metrics": False,
                },
            },
        },
        resume={"name": "戊"},
    )

    assert result["passed"] is True


def test_operation_assistant_does_not_count_money_as_data_outcome(tmp_path):
    class MoneyOutcomeAIClient:
        def complete_json(self, messages):
            return {
                "passed": True,
                "reason": "全部硬性条件满足。综上，通过。",
                "work_evidence": [{
                    "title": "运营实习生",
                    "content": "项目营收10万元，广告消耗2万元",
                    "start": "2025.01",
                    "end": "2026.01",
                    "region": "深圳",
                    "outcomes": [
                        {"metric": "营收", "kind": "money", "evidence": "营收10万元"},
                        {"metric": "广告消耗", "kind": "spend", "evidence": "广告消耗2万元"},
                    ],
                }],
            }

    result = ResumeAnalyzer(
        config=_config(tmp_path), client=MoneyOutcomeAIClient()
    ).analyze(
        job_requirement={
            "job_name": "运营助理",
            "resume_analysis": {
                "recent_outcomes": {
                    "limit": 2,
                    "min_experiences": 1,
                    "min_metrics": 2,
                    "mode": "any",
                    "exclude_quantity_only": True,
                    "distinct_metrics": False,
                    "exclude_money": True,
                },
            },
        },
        resume={"name": "己"},
    )

    assert result["passed"] is False
    assert "数据成果不满足要求" in result["reason"]


def test_openai_client_uses_optional_vision_model_for_image_messages(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg._config["ai"]["vision_model"] = "deepseek-vision"
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": '{"passed": true, "reason": "匹配"}'}}]
            }).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(cfg)
    client.complete_json([{
        "role": "user",
        "content": [
            {"type": "text", "text": "分析简历"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
        ],
    }])

    assert captured["payload"]["model"] == "deepseek-vision"
    assert captured["payload"]["max_tokens"] == 8000


def test_message_generator_uses_external_prompt(tmp_path):
    fake = FakeAIClient()
    generator = MessageGenerator(config=_config(tmp_path), client=fake)

    message = generator.generate(
        candidate_info={"name": "张三", "skills": ["Python"]},
        job_requirement={"job_name": "Python工程师"},
    )

    assert fake.text_messages[0]["content"] == "外置招呼语Prompt"
    assert "candidate_info" in fake.text_messages[1]["content"]
    assert message == "您好，看到您的 Python 经验和我们岗位比较匹配，方便聊一下吗？"


def test_resume_analyzer_retries_when_json_passed_contradicts_reason_conclusion(tmp_path):
    """Conflicting historical output is unavailable, never silently upgraded."""

    class ContradictoryAIClient:
        def complete_json(self, messages):
            return {
                "passed": False,
                "reason": (
                    "1. 学历高中及以上：候选人学历为大专，满足。"
                    "2. 年龄不超过30岁：候选人年龄21岁，满足。"
                    "3. 毕业至今至少有一段工作满7个月：最近一段工作满1年，满足。"
                    "4. 最近实际岗位必须是主播：最近岗位为带货主播，满足。"
                    "5. 最近两份工作内，至少一段主播经历有明确成果：月度销冠，满足。"
                    "综上，所有硬性条件均满足，通过。"
                ),
            }

    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=ContradictoryAIClient())
    with pytest.raises(ResumeAnalysisUnavailableError) as captured:
        analyzer.analyze(
            job_requirement={"job_name": "全职主播"},
            resume={"name": "朱赣"},
        )
    assert captured.value.code == "conflicting_conclusion"
    assert captured.value.audit["raw_result"]["passed"] is False


def test_resume_analyzer_keeps_reject_when_reason_genuinely_concludes_reject(tmp_path):
    """reason 文本结论就是"不通过"时，passed 保持 False，不误兜底。"""

    class RejectAIClient:
        def complete_json(self, messages):
            return {
                "passed": False,
                "reason": (
                    "1. 最近一份工作涉及手机主销（苹果准新机），命中直接排除项。"
                    "综上，不通过。"
                ),
            }

    analyzer = ResumeAnalyzer(config=_config(tmp_path), client=RejectAIClient())
    result = analyzer.analyze(
        job_requirement={"job_name": "全职主播"},
        resume={"name": "蓝霞"},
    )

    assert result["passed"] is False
    assert "不通过" in result["reason"]


def test_openai_compatible_client_reads_config_and_posts_chat_completion(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps({
                "choices": [
                    {"message": {"content": "{\"passed\": true, \"reason\": \"匹配\"}"}}
                ]
            }).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = OpenAICompatibleClient(cfg)
    result = client.complete_json([{"role": "user", "content": "hi"}])

    assert result["passed"] is True
    assert result["reason"] == "匹配"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["timeout"] == 9
    assert captured["payload"]["model"] == "external-model"
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_client_repairs_malformed_json_without_resending_images(
    tmp_path, monkeypatch
):
    cfg = _config(tmp_path)
    requests = []
    responses = iter([
        '{"passed": true "reason": "匹配"}',
        '{"passed": true, "reason": "匹配"}',
    ])

    class FakeResponse:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": self.content}}]
            }).encode("utf-8")

    def fake_urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        requests.append(payload)
        return FakeResponse(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(cfg)
    result = client.complete_json([{
        "role": "user",
        "content": [
            {"type": "text", "text": "分析简历"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
        ],
    }])

    assert result == {"passed": True, "reason": "匹配"}
    assert len(requests) == 2
    assert any(part.get("type") == "image_url" for part in requests[0]["messages"][0]["content"])
    assert isinstance(requests[1]["messages"][1]["content"], str)
    assert "image_url" not in requests[1]["messages"][1]["content"]
