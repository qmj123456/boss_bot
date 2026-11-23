"""Batch resume recovery must preserve saved data and finish pending greetings."""
import importlib
import json
from types import SimpleNamespace

import pytest

from tests.test_live_card_transaction import DOWNLOAD_SCRIPT, _load


def _fixture(monkeypatch, tmp_path, *, empty=False, status="success", cached=None,
             greet_status="greeted", prior_greetings=None):
    module = _load(DOWNLOAD_SCRIPT, "download_resume_preservation")
    process_dir = tmp_path / "run-1" / "process"
    process_dir.mkdir(parents=True)
    candidate = {"encryptGeekId": "g1", "geekCard": {
        "encryptJobId": "job-1", "geekName": "甲",
    }}
    saved = [{"name": "甲", "resume_screenshots": [str(process_dir / "resume.jpg")],
              "_meta": {"encrypt_geek_id": "g1"}}]
    failed = [{"encrypt_geek_id": "g1", "stage": "deepseek_analysis", "reason": "old failure"}]
    for name, value in (
        ("recommend_geek_ids.json", [] if empty else [candidate]),
        ("new_resumes.json", saved), ("failed_resumes.json", failed),
        ("screening_results.json", {"job": "客服", "candidates": [cached] if cached else [], "failures": []}),
        ("greet_log.json", {"job": "客服", "results": prior_greetings or []}),
    ):
        (process_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    output = SimpleNamespace(
        run_dir=str(process_dir.parent), get_process_path=lambda name: str(process_dir / name),
        new_resumes_path=str(process_dir / "new_resumes.json"),
        failed_resumes_path=str(process_dir / "failed_resumes.json"),
        recommend_geek_ids_path=str(process_dir / "recommend_geek_ids.json"),
        ensure_run_dir=lambda: None, prune_if_empty=lambda: False,
    )
    calls = {"browser": 0, "analysis": 0, "greet": 0, "success": 0, "failed": 0}

    class Store:
        def get_status(self, *_args): return status
        def save_resume(self, *_args): return False
        def mark_success(self, *_args): calls["success"] += 1
        def mark_failed(self, *_args): calls["failed"] += 1
        def count_resumes(self): return 1

    class Analyzer:
        def analyze(self, **_kwargs):
            calls["analysis"] += 1
            return {"passed": True, "reason": "通过"}

    orchestrator = SimpleNamespace(
        bind_existing_run=lambda value: value, is_confirmed=lambda _: True,
        mark_done=lambda *_, **__: None,
    )
    monkeypatch.setattr(importlib.import_module("run_orchestrator"), "RunOrchestrator", lambda *_, **__: orchestrator)
    monkeypatch.setattr(module, "JobOutputManager", lambda *_, **__: output)
    monkeypatch.setattr(module, "JobResumeStore", lambda *_, **__: Store())
    monkeypatch.setattr(module, "ResumeAnalyzer", lambda **_: Analyzer())
    monkeypatch.setattr(module, "get_config", lambda: object())
    monkeypatch.setattr(module, "_job_requirement", lambda *_, **__: {"job_name": "客服"})
    monkeypatch.setattr(module, "_performance_int", lambda _key, default, **_: default)
    monkeypatch.setattr(module, "_performance_float", lambda _key, default, **_: 0)
    monkeypatch.setattr(module, "_performance_range", lambda _key, default, **_: default)
    monkeypatch.setattr(module, "_performance_random", lambda *_, **__: 0)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "_check_card_before_open", lambda *args: SimpleNamespace(allow_full_resume=True))
    detail = object()
    iframe = SimpleNamespace(content_frame=lambda: detail, bounding_box=lambda: {})
    page = SimpleNamespace(url="https://www.zhipin.com/web/chat/recommend", query_selector=lambda _: iframe)

    class Playwright:
        def __enter__(self):
            calls["browser"] += 1
            return SimpleNamespace(chromium=SimpleNamespace(
                connect_over_cdp=lambda _: SimpleNamespace(contexts=[object()]),
            ))

        def __exit__(self, *_args): return None

    monkeypatch.setattr(module, "sync_playwright", Playwright)
    monkeypatch.setattr(module, "get_or_create_recommend_page", lambda *_, **__: page)
    monkeypatch.setattr(module, "ensure_recommend_job_selected", lambda *_, **__: {"ok": True})
    monkeypatch.setattr(module, "_find_and_open_resume", lambda *_, **__: (detail, False, page.url))
    monkeypatch.setattr(module, "_capture_all_resume_screens", lambda *_, **__: [str(process_dir / "resume.jpg")])
    monkeypatch.setattr(module, "_validate_resume_screenshot_files", lambda paths: paths)
    monkeypatch.setattr(module, "_close_resume", lambda *_: True)
    monkeypatch.setattr(module, "_resume_context_for_greeting", lambda *_, **__: (detail, None))

    def greet(*_args, **_kwargs):
        calls["greet"] += 1
        return {"encrypt_geek_id": "g1", "status": greet_status,
                "message_sent": greet_status == "greeted", "reason": "test greeting"}

    monkeypatch.setattr(module, "_greet_current_resume", greet)
    run = lambda: module.download_resumes(
        "客服", run_id="run-1", encrypt_job_id="job-1", transactional=True, greet_message="测试",
    )
    return process_dir, calls, run, (saved, failed)


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_previously_successful_input_preserves_resume_artifacts(monkeypatch, tmp_path, empty):
    directory, calls, run, previous = _fixture(monkeypatch, tmp_path, empty=empty)
    paths = [directory / "new_resumes.json", directory / "failed_resumes.json"]
    original = [path.read_bytes() for path in paths]

    assert run() == previous

    assert [path.read_bytes() for path in paths] == original
    assert calls["browser"] == 0


def test_saved_pass_with_success_download_and_missing_greeting_resumes_greeting(monkeypatch, tmp_path):
    cached = {"geek_id": "g1", "passed": True, "reason": "原始通过", "analysis_status": "passed"}
    directory, calls, run, _previous = _fixture(monkeypatch, tmp_path, cached=cached)

    _resumes, failures = run()

    assert calls["browser"] == calls["greet"] == calls["success"] == 1
    assert calls["analysis"] == 0
    assert not failures
    screening = json.loads((directory / "screening_results.json").read_text(encoding="utf-8"))
    assert screening["candidates"][0]["reason"] == "原始通过"
    run()
    assert calls["greet"] == 1


def test_nonterminal_analysis_placeholder_does_not_skip_retry(monkeypatch, tmp_path):
    cached = {"geek_id": "g1", "passed": False, "reason": "提取失败", "analysis_status": "analysis_unavailable"}
    _directory, calls, run, _previous = _fixture(monkeypatch, tmp_path, cached=cached, status="failed")

    run()

    assert calls["analysis"] == calls["greet"] == calls["success"] == 1


@pytest.mark.parametrize("greet_status", ["failed", "pending", "blocked", None])
def test_nonterminal_greeting_cannot_mark_batch_success(monkeypatch, tmp_path, greet_status):
    cached = {"geek_id": "g1", "passed": True, "reason": "原始通过"}
    directory, calls, run, _previous = _fixture(
        monkeypatch, tmp_path, cached=cached, greet_status=greet_status,
    )

    _resumes, failures = run()

    assert calls["greet"] == calls["failed"] == 1
    assert calls["success"] == calls["analysis"] == 0
    assert len(failures) == 1
    assert "test greeting" in failures[0]["reason"]
    screening = json.loads((directory / "screening_results.json").read_text(encoding="utf-8"))
    assert screening["candidates"][0]["passed"] is True


def test_already_rejected_analysis_does_not_open_browser(monkeypatch, tmp_path):
    cached = {"geek_id": "g1", "passed": False, "reason": "原始拒绝"}
    _directory, calls, run, previous = _fixture(monkeypatch, tmp_path, cached=cached, status="failed")
    assert run() == previous
    assert not any(calls.values())
