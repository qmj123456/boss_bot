"""Resume-analysis retries must widen the token budget instead of repeating it."""
from __future__ import annotations

from ai.errors import ResumeAnalysisUnavailableError
from boss_hr.config_manager import ConfigManager
from shared.analysis_result import analyze_with_retries


class _Config:
    def __init__(self, values=None):
        self.values = values or {
            "ai.vision_max_tokens": 8000,
            "ai.vision_max_tokens_step": 4000,
            "ai.vision_max_tokens_ceiling": 16000,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Analyzer:
    def __init__(self, *, failures, code="response_truncated", config=None, audit=None):
        self.failures = failures
        self.code = code
        self.config = config or _Config()
        self.audit = audit
        self.calls: list[int | None] = []

    def analyze(self, *, job_requirement, resume, max_tokens_override=None):
        self.calls.append(max_tokens_override)
        if self.failures > 0:
            self.failures -= 1
            raise ResumeAnalysisUnavailableError("truncated", code=self.code, audit=self.audit)
        return {"passed": True, "reason": "ok"}


def test_truncation_escalates_budget_on_each_retry():
    analyzer = _Analyzer(failures=2)
    result, error, attempts = analyze_with_retries(
        analyzer, {}, "resume", attempts=3, retry_delay=0, retry_max_delay=0,
    )
    assert error is None
    assert result["passed"] is True
    assert attempts == 3
    assert analyzer.calls == [None, 12000, 16000]


def test_truncation_stops_at_ceiling():
    analyzer = _Analyzer(failures=5)
    result, error, attempts = analyze_with_retries(
        analyzer, {}, "resume", attempts=3, retry_delay=0, retry_max_delay=0,
    )
    assert result is None
    assert error is not None
    assert attempts == 3
    assert analyzer.calls == [None, 12000, 16000]
    assert len(error.audit["failed_attempts"]) == 3


def test_wrapped_truncation_still_escalates():
    # ResumeAnalyzer 会把客户端错误包成 completion_unavailable，截断码留在 audit 里。
    analyzer = _Analyzer(
        failures=2,
        code="completion_unavailable",
        audit={"completion_error_code": "response_truncated"},
    )
    result, error, attempts = analyze_with_retries(
        analyzer, {}, "resume", attempts=3, retry_delay=0, retry_max_delay=0,
    )
    assert error is None
    assert result["passed"] is True
    assert attempts == 3
    assert analyzer.calls == [None, 12000, 16000]


def test_non_truncation_errors_do_not_escalate():
    analyzer = _Analyzer(failures=3, code="invalid_json")
    result, error, _attempts = analyze_with_retries(
        analyzer, {}, "resume", attempts=3, retry_delay=0, retry_max_delay=0,
    )
    assert result is None
    assert error is not None
    assert analyzer.calls == [None, None, None]


def test_escalation_uses_configured_step_and_ceiling():
    analyzer = _Analyzer(
        failures=5,
        config=_Config({
            "ai.vision_max_tokens": 4000,
            "ai.vision_max_tokens_step": 2000,
            "ai.vision_max_tokens_ceiling": 7000,
        }),
    )
    analyze_with_retries(
        analyzer, {}, "resume", attempts=3, retry_delay=0, retry_max_delay=0,
    )
    assert analyzer.calls == [None, 6000, 7000]


def test_ai_config_defaults_are_raised_for_long_resumes():
    config = ConfigManager()
    assert int(config.get("ai.vision_max_tokens")) == 8000
    assert int(config.get("ai.vision_max_tokens_step")) == 4000
    assert int(config.get("ai.vision_max_tokens_ceiling")) == 16000
