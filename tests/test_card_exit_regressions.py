"""Regression coverage for configured card gates and bounded list termination."""
import json
from types import SimpleNamespace

import pytest

from boss_hr.domain.card_prescreen import evaluate_card_prescreen
from tests.test_card_prescreen import _complete_candidate, _recruitment
from tests.test_live_card_transaction import LIST_SCRIPT, _load


@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_optional_work_experience_is_not_overridden(enabled):
    config = _recruitment()
    config["card_prescreen"].update(enabled=enabled, require_work_experience=False)
    card = _complete_candidate(geekWorks=[], workExperienceDesc="未填写工作经历")

    result = evaluate_card_prescreen(card, config)

    assert result.passed is True
    assert result.allow_full_resume is True


def test_disabled_unconfigured_work_gate_allows_card_but_explicit_requirement_stays():
    config = _recruitment()
    config["card_prescreen"]["enabled"] = False
    card = _complete_candidate(geekWorks=[])
    assert evaluate_card_prescreen(card, config).allow_full_resume is True

    config["card_prescreen"]["require_work_experience"] = True
    assert evaluate_card_prescreen(card, config).allow_full_resume is False


def test_enabled_historical_default_still_requires_work():
    assert not evaluate_card_prescreen(
        _complete_candidate(geekWorks=[]), _recruitment()
    ).allow_full_resume


class Response:
    def __init__(self, data, *, status=200, job_id="job-1"):
        self.data = data
        self.status = status
        self.url = "https://example.test/geek/list" + (
            f"?encryptJobId={job_id}" if job_id else ""
        )
        self.request = SimpleNamespace(post_data=None)

    def json(self):
        return self.data


@pytest.mark.parametrize("response", [
    Response({}, status=403),
    Response({"code": 37, "zpData": {"geekList": []}}),
    Response({}),
    Response({"zpData": {"geekList": None}}),
    Response({"zpData": {"geekList": [None]}}),
    Response({"zpData": {"geekList": [{"geekCard": {"encryptJobId": "job-1"}}]}}),
])
def test_invalid_api_response_is_not_a_valid_empty_list(response):
    module = _load(LIST_SCRIPT, "card_exit_invalid_response")
    with pytest.raises(ValueError):
        module._read_recommend_response(response, "job-1")


@pytest.mark.parametrize(("job_id", "payload", "expected"), [
    ("job-1", {"geekList": []}, False),
    ("job-1", {"geekList": [], "hasMore": False}, True),
    ("job-1", {"geekList": [], "hasMore": "false"}, False),
    ("old-job", {"geekList": [], "hasMore": False}, False),
    ("", {"geekList": [], "hasMore": False}, False),
])
def test_exhaustion_needs_explicit_signal_and_current_job_scope(job_id, payload, expected):
    module = _load(LIST_SCRIPT, "card_exit_explicit_end")
    assert module._read_recommend_response(
        Response({"zpData": payload}, job_id=job_id), "job-1"
    )[2] is expected


def _list_harness(monkeypatch, tmp_path, responses, *, rendered=None):
    module = _load(LIST_SCRIPT, "card_exit_mocked_list")
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module, "detect_captcha", lambda *_: None)
    monkeypatch.setattr(module, "ensure_recommend_job_selected", lambda *_, **__: {"ok": True})

    class Frame:
        def evaluate(self, _script):
            return rendered or []

    frame = Frame()
    iframe = SimpleNamespace(content_frame=lambda: frame, bounding_box=lambda: {})

    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"

        def on(self, _event, handler):
            self.handler = handler

        def query_selector(self, _selector):
            return iframe

        def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    browser = SimpleNamespace(contexts=[object()])
    calls = {"scrolls": 0}

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda _: browser))

        def __exit__(self, *_args):
            return None

    def scroll(*_args, **_kwargs):
        calls["scrolls"] += 1
        page.handler(responses[min(calls["scrolls"] - 1, len(responses) - 1)])

    monkeypatch.setattr(module, "sync_playwright", Playwright)
    monkeypatch.setattr(module, "get_or_create_recommend_page", lambda *_, **__: page)
    monkeypatch.setattr(module, "human_scroll", scroll)
    monkeypatch.setattr(module, "apply_job_filters", lambda *_, **__: {"ok": True})
    monkeypatch.setattr(module, "RunOrchestrator", lambda *_, **__: SimpleNamespace(
        bind_existing_run=lambda run_id: run_id, is_confirmed=lambda _: True,
    ))
    monkeypatch.setattr(module, "JobOutputManager", lambda *_, **__: SimpleNamespace(
        get_process_path=lambda name: tmp_path / name,
        recommend_geek_ids_path=tmp_path / "recommend_geek_ids.json",
    ))
    monkeypatch.setattr(module, "get_config", lambda: SimpleNamespace(
        section=lambda _: _recruitment(), load_job_profile=lambda _: {},
        get=lambda _key, default: default,
    ))
    return module, calls


def _summary(tmp_path):
    return json.loads((tmp_path / "card_prescreen_summary.json").read_text(encoding="utf-8"))


def test_repeated_api_cards_have_bounded_stop_and_are_not_pool_exhaustion(monkeypatch, tmp_path):
    candidate = _complete_candidate()
    candidate["geekCard"]["encryptJobId"] = "job-1"
    response = Response({"code": 0, "zpData": {"geekList": [candidate]}})
    module, calls = _list_harness(monkeypatch, tmp_path, [response])

    results = module.get_recommend_candidates(run_id="run-1", encrypt_job_id="job-1")

    assert len(results) == 1
    assert calls["scrolls"] == 6  # First round adds a card; five repeated rounds stop.
    summary = _summary(tmp_path)
    assert summary["accepted_count"] == 1
    assert summary["list_diagnostics"]["termination_reason"] == "repeated_cards"
    assert summary["list_diagnostics"]["exhaustion_confirmed"] is False


def test_api_business_error_is_saved_and_fails_instead_of_empty_success(monkeypatch, tmp_path):
    module, calls = _list_harness(monkeypatch, tmp_path, [Response({"code": 37})])

    with pytest.raises(RuntimeError, match="推荐接口持续未恢复"):
        module.get_recommend_candidates(run_id="run-1", encrypt_job_id="job-1")

    assert calls["scrolls"] == 5
    diagnostics = _summary(tmp_path)["list_diagnostics"]
    assert diagnostics["termination_reason"] == "api_error"
    assert diagnostics["api_error_count"] == 5
    assert diagnostics["exhaustion_confirmed"] is False


def test_success_after_api_error_clears_active_failure(monkeypatch, tmp_path):
    module, _calls = _list_harness(monkeypatch, tmp_path, [
        Response({}, status=429),
        Response({"code": 0, "zpData": {"geekList": [], "hasMore": False}}),
    ])

    assert module.get_recommend_candidates(run_id="run-1", encrypt_job_id="job-1") == []
    diagnostics = _summary(tmp_path)["list_diagnostics"]
    assert diagnostics["termination_reason"] == "list_exhausted"
    assert diagnostics["api_error_count"] == 1
    assert diagnostics["active_api_error"] == ""
    assert diagnostics["last_api_error"]


def test_missing_metadata_is_saved_before_abort_without_scrolling(monkeypatch, tmp_path):
    module, calls = _list_harness(monkeypatch, tmp_path, [Response({})], rendered=[
        {"dom_gid": "missing", "token": "visible-card", "name": "测试人"},
    ])

    with pytest.raises(RuntimeError, match="连续等待接口元数据失败"):
        module.get_recommend_candidates(
            run_id="run-1", encrypt_job_id="job-1", candidate_handler=lambda *_: {},
        )

    assert calls["scrolls"] == 0
    diagnostics = _summary(tmp_path)["list_diagnostics"]
    assert diagnostics["termination_reason"] == "metadata_unavailable"
    assert diagnostics["metadata_wait_count"] == 10
    assert diagnostics["exhaustion_confirmed"] is False


def test_end_of_api_pages_does_not_hide_unrendered_candidates(monkeypatch, tmp_path):
    candidate = _complete_candidate()
    candidate["geekCard"]["encryptJobId"] = "job-1"
    module, calls = _list_harness(monkeypatch, tmp_path, [Response({
        "code": 0, "zpData": {"geekList": [candidate], "hasMore": False},
    })])

    assert module.get_recommend_candidates(
        run_id="run-1", encrypt_job_id="job-1", candidate_handler=lambda *_: {},
    ) == []
    assert calls["scrolls"] == 5
    diagnostics = _summary(tmp_path)["list_diagnostics"]
    assert diagnostics["api_end_of_list"] is True
    assert diagnostics["remaining_api_candidate_count"] == 1
    assert diagnostics["exhaustion_confirmed"] is False
    assert diagnostics["termination_reason"] == "repeated_cards"
