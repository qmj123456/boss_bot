import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from shared.analysis_recovery import retry_saved_analyses
from shared.analysis_result import analyze_with_retries

ROOT = Path(__file__).resolve().parents[1]


def download_module():
    spec = importlib.util.spec_from_file_location("download_result_flow", ROOT / "boss-recommend-downloader/scripts/recommend_download.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAnalyzer:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def analyze(self, **kwargs):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def live_fixture(monkeypatch, tmp_path, *, cached=None, result=None, greet=None, storage_error=False):
    module = download_module()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    processor = module.LiveCandidateProcessor.__new__(module.LiveCandidateProcessor)
    for key in ("failed", "analysis_candidates", "analysis_failures", "new_resumes", "greet_results"):
        setattr(processor, key, [])
    processor.processed_ids = set()
    processor.job_id, processor.run_id = "job-1", "run-1"
    processor.max_greetings, processor.greet_message, processor.dry_run = 10, "固定话术", False
    processor.requirement = {"job_name": "岗位"}
    processor.output = SimpleNamespace(run_dir=str(tmp_path))
    processor._commit = lambda: None
    processor._log = lambda message: None
    calls = {"capture": 0, "greet": 0, "success": 0, "failed": 0}
    class Store:
        def get_status(self, *args): return None
        def save_resume(self, *args): return True
        def mark_success(self, *args):
            calls["success"] += 1
            if storage_error: raise OSError("storage failed after greeting")
        def mark_failed(self, *args): calls["failed"] += 1
    processor.store = Store()
    processor.analyzer = FakeAnalyzer(result or [{"passed": True, "reason": "全部条件满足，最终结论：通过。"}])
    if cached:
        processor.analysis_candidates.append(cached)
    detail = object()
    monkeypatch.setattr(module, "_check_card_before_open", lambda *args: SimpleNamespace(allow_full_resume=True))
    monkeypatch.setattr(module, "_ensure_no_resume_open", lambda *args: True)
    monkeypatch.setattr(module, "_find_and_open_resume", lambda *args, **kwargs: (detail, False, "recommend"))
    monkeypatch.setattr(module, "_verify_open_resume_identity", lambda *args, **kwargs: "甲")
    def capture(*args, **kwargs):
        calls["capture"] += 1
        return [str(tmp_path / "resume.jpg")]
    monkeypatch.setattr(module, "_capture_all_resume_screens", capture)
    monkeypatch.setattr(module, "_validate_resume_screenshot_files", lambda paths: paths)
    monkeypatch.setattr(module, "_resume_record_from_card", lambda candidate, paths: {"resume_screenshots": paths})
    monkeypatch.setattr(module, "_close_resume", lambda *args: True)
    monkeypatch.setattr(module, "_performance_int", lambda key, default, **kwargs: 2)
    monkeypatch.setattr(module, "_performance_float", lambda *args, **kwargs: 0)
    monkeypatch.setattr(module, "_resume_context_for_greeting", lambda *args, **kwargs: (detail, None))
    def greeting(*args, **kwargs):
        calls["greet"] += 1
        return {"encrypt_geek_id": "g1", "name": "甲", "status": "greeted", "clicked": True, "message_sent": True, **(greet or {})}
    monkeypatch.setattr(module, "_greet_current_resume", greeting)
    frame = SimpleNamespace(content_frame=lambda: object(), bounding_box=lambda: {})
    page = SimpleNamespace(url="recommend", query_selector=lambda selector: frame)
    candidate = {"encryptGeekId": "g1", "geekCard": {"geekName": "甲"}, "_live_dom_card": {"token": "token"}}
    return processor, calls, lambda: processor.process(candidate, page, object())


def test_valid_analysis_reaches_greeting_once(monkeypatch, tmp_path):
    processor, calls, run = live_fixture(monkeypatch, tmp_path)
    assert run()["ok"]
    assert run()["skipped"]
    assert calls == {"capture": 1, "greet": 1, "success": 1, "failed": 0}
    assert processor.analysis_candidates[0]["passed"] is True
    assert "selected_work_index" not in processor.analysis_candidates[0]


def test_transport_failure_retries_saved_images_then_greets(monkeypatch, tmp_path):
    processor, calls, run = live_fixture(monkeypatch, tmp_path, result=[ValueError("invalid JSON"), {"passed": True, "reason": "通过"}])
    assert run()["ok"]
    assert processor.analyzer.calls == 2
    assert calls["capture"] == calls["greet"] == 1
    assert processor.analysis_candidates[0]["analysis_audit"]["failed_attempts"][0]["error"] == "invalid JSON"


def test_exhausted_analysis_never_rejects_or_greets(monkeypatch, tmp_path):
    processor, calls, run = live_fixture(monkeypatch, tmp_path, result=[ValueError("missing evidence"), ValueError("missing evidence")])
    assert run()["terminal_status"] == "analysis_unavailable"
    assert calls["greet"] == calls["success"] == 0
    assert not processor.analysis_candidates
    assert len(processor.analysis_failures[0]["analysis_audit"]["failed_attempts"]) == 2


def test_cached_pass_only_resumes_greeting(monkeypatch, tmp_path):
    cached = {"geek_id": "g1", "passed": True, "reason": "原始通过", "analysis_audit": {"raw_result": {"passed": True}}}
    processor, calls, run = live_fixture(monkeypatch, tmp_path, cached=cached)
    assert run()["ok"]
    assert calls["capture"] == processor.analyzer.calls == 0
    assert calls["greet"] == 1
    assert processor.analysis_candidates == [cached]


def test_failed_greeting_is_not_marked_success(monkeypatch, tmp_path):
    processor, calls, run = live_fixture(monkeypatch, tmp_path, greet={"status": "failed", "message_sent": False, "reason": "send failed"})
    assert run()["terminal_status"] == "greeting_failed"
    assert calls["success"] == 0
    assert processor.greet_results[0]["clicked"] is True
    assert processor.greet_results[0]["reason"] == "send failed"


def test_storage_failure_does_not_erase_actual_greeting(monkeypatch, tmp_path):
    processor, calls, run = live_fixture(monkeypatch, tmp_path, storage_error=True)
    run()
    assert processor.greet_results[0]["status"] == "greeted"
    assert processor.greet_results[0]["message_sent"] is True
    assert processor.analysis_candidates[0]["passed"] is True


def recovery_fixture(tmp_path):
    process = tmp_path / "run-1" / "process"
    process.mkdir(parents=True)
    image = process / "resume.png"
    Image.new("RGB", (40, 40), "gray").save(image)
    screen = {"job": "客服", "run_id": "run-1", "candidates": [{"geek_id": "done", "passed": True, "reason": "原始通过"}],
              "failures": [{"geek_id": "g1", "name": "甲", "error": "empty JSON"}]}
    file = process / "screening_results.json"
    file.write_text(json.dumps(screen), encoding="utf-8")
    (process / "new_resumes.json").write_text(json.dumps([{"name": "甲", "_meta": {"encrypt_geek_id": "g1"}, "resume_screenshots": [str(image)]}]), encoding="utf-8")
    return file


def test_recovery_uses_images_preserves_completed_and_is_idempotent(tmp_path):
    file = recovery_fixture(tmp_path)
    analyzer = FakeAnalyzer([{"passed": True, "reason": "符合", "expected_salary": "5-7K", "employment_status": "离职-随时到岗"}])
    kwargs = dict(analyzer=analyzer, requirement={"job_name": "客服"}, job_name="客服", retry_delay=0)
    assert retry_saved_analyses(file, **kwargs)["recovered"] == 1
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["candidates"][0] == {"geek_id": "done", "passed": True, "reason": "原始通过"}
    assert data["candidates"][1]["expected_salary"] == "5-7K"
    assert "selected_work_index" not in data["candidates"][1]
    assert data["failures"] == []
    before = file.read_bytes()
    assert retry_saved_analyses(file, **kwargs)["retried"] == 0
    assert file.read_bytes() == before
    assert analyzer.calls == 1


def test_recovery_failure_stays_pending_and_never_overwrites_completed(tmp_path):
    file = recovery_fixture(tmp_path)
    analyzer = FakeAnalyzer([ValueError("invalid")])
    stats = retry_saved_analyses(file, analyzer=analyzer, requirement={}, job_name="客服", attempts=1)
    data = json.loads(file.read_text(encoding="utf-8"))
    assert stats["failed"] == 1
    assert len(data["candidates"]) == 1
    assert data["failures"][0]["terminal_status"] == "analysis_unavailable"


def test_recovery_refuses_wrong_job(tmp_path):
    file = recovery_fixture(tmp_path)
    analyzer = FakeAnalyzer([])
    with pytest.raises(ValueError, match="岗位"):
        retry_saved_analyses(file, analyzer=analyzer, requirement={}, job_name="运营助理")
    assert analyzer.calls == 0


def test_recovery_reconciles_failure_artifacts_without_erasing_greeting_failure(tmp_path):
    file = recovery_fixture(tmp_path)
    failed = file.with_name("failed_resumes.json")
    failed.write_text(json.dumps([
        {"encrypt_geek_id": "g1", "stage": "deepseek_analysis"},
        {"encrypt_geek_id": "g1", "stage": "greeting", "reason": "pending"},
    ]), encoding="utf-8")
    analyzer = FakeAnalyzer([{"passed": True, "reason": "通过"}])
    retry_saved_analyses(file, analyzer=analyzer, requirement={}, job_name="客服", attempts=1)
    assert json.loads(failed.read_text(encoding="utf-8")) == [
        {"encrypt_geek_id": "g1", "stage": "greeting", "reason": "pending"}]
    resume = json.loads(file.with_name("new_resumes.json").read_text(encoding="utf-8"))[0]
    assert resume["_meta"]["deepseek_passed"] is True


@pytest.mark.parametrize("metadata", [
    {"encrypt_job_id": "other-job"},
    {"first_run_id": "other-run"},
    {"candidate_key": "job-1:other-person"},
])
def test_recovery_rejects_mixed_resume_identity(tmp_path, metadata):
    file = recovery_fixture(tmp_path)
    (file.parent.parent / "run.json").write_text(json.dumps({"run_id": "run-1", "encrypt_job_id": "job-1"}), encoding="utf-8")
    resume_file = file.with_name("new_resumes.json")
    resumes = json.loads(resume_file.read_text(encoding="utf-8"))
    resumes[0]["_meta"].update(metadata)
    resume_file.write_text(json.dumps(resumes), encoding="utf-8")
    analyzer = FakeAnalyzer([])
    stats = retry_saved_analyses(file, analyzer=analyzer, requirement={}, job_name="客服")
    assert stats["failed"] == 1
    assert analyzer.calls == 0


def test_recovery_rejects_cross_run_image(tmp_path):
    file = recovery_fixture(tmp_path)
    image = tmp_path / "other-run.png"
    Image.new("RGB", (40, 40), "gray").save(image)
    resume_file = file.with_name("new_resumes.json")
    resumes = json.loads(resume_file.read_text(encoding="utf-8"))
    resumes[0]["resume_screenshots"] = [str(image)]
    resume_file.write_text(json.dumps(resumes), encoding="utf-8")
    analyzer = FakeAnalyzer([])
    assert retry_saved_analyses(file, analyzer=analyzer, requirement={}, job_name="客服")["failed"] == 1
    assert analyzer.calls == 0


def test_closed_live_processor_cannot_write_or_process(monkeypatch, tmp_path):
    from shared.analysis_artifact_lock import RunArtifactLock
    processor, calls, run = live_fixture(monkeypatch, tmp_path)
    processor._artifact_lock = RunArtifactLock(tmp_path).acquire()
    processor.close()
    with pytest.raises(RuntimeError, match="已经关闭"):
        run()
    assert calls["capture"] == calls["greet"] == 0
    with pytest.raises(RuntimeError, match="已经关闭"):
        type(processor)._commit(processor)


def test_download_guard_does_not_create_nonexistent_run(monkeypatch, tmp_path):
    module = download_module()
    import output_manager
    absent = tmp_path / "nonexistent-run"
    monkeypatch.setattr(module, "JobOutputManager", lambda *args, **kwargs: SimpleNamespace(run_dir=str(absent)))
    monkeypatch.setattr(output_manager, "resolve_encrypt_job_id", lambda value: value)
    calls = []
    @module._guard_download_artifacts
    def entry(job_name, run_id=None, encrypt_job_id=None):
        calls.append(True)
    with pytest.raises(FileNotFoundError):
        entry("岗位", run_id="nonexistent-run", encrypt_job_id="job-1")
    assert calls == []
    assert not absent.exists()
