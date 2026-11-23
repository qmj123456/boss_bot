"""Independent integration checks for decision consumers and job boundaries."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.repositories import UnitOfWork
from shared.analysis_result import (
    analysis_record,
    analyze_with_retries,
    is_passed_analysis,
)
from tests.test_recruit_workflow import _workflow
from workflow.recruit_workflow import RecruitWorkflow


@pytest.mark.parametrize("result,expected", [
    ({"passed": True}, True),
    ({"passed": False}, False),
    ({"passed": "false"}, False),
    ({"passed": "true"}, False),
    ({"passed": 1}, False),
    ({"passed": None}, False),
    ({"passed": False, "recommended": True}, False),
    ({"passed": "false", "recommended": True}, False),
    ({"passed": True, "analysis_status": "analysis_error"}, False),
    ({"passed": True, "analysis_status": "rejected"}, False),
    ({"passed": False, "analysis_status": "passed"}, False),
    ({"recommended": True}, True),
    ({"recommended": "true"}, False),
    ({"recommended": True, "analysis_status": "rejected"}, False),
])
def test_greeting_requires_unambiguous_completed_pass(result, expected):
    assert is_passed_analysis(result) is expected


def _greet_module():
    path = Path(__file__).resolve().parents[1] / "boss-hr-greet" / "scripts" / "auto_greet.py"
    spec = importlib.util.spec_from_file_location("pipeline_review_auto_greet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_screening(tmp_path, candidates, failures=None):
    output = tmp_path / "runs" / "run-1" / "process" / "screening_results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"candidates": candidates, "failures": failures or []}), encoding="utf-8")
    return output


def test_greeting_loader_does_not_allow_truthy_strings_or_legacy_alias_overrides(tmp_path):
    candidates = [
        {"name": "accepted", "encrypt_geek_id": "gid-1", "passed": True},
        {"name": "rejected", "encrypt_geek_id": "gid-2", "passed": False, "recommended": True},
        {"name": "string-false", "encrypt_geek_id": "gid-3", "passed": "false", "recommended": True},
        {"name": "pending", "encrypt_geek_id": "gid-4", "passed": True, "analysis_status": "analysis_error"},
    ]
    _write_screening(tmp_path, candidates)
    result = _greet_module().load_high_score_candidates(str(tmp_path), "run-1", 60)
    assert [(r["name"], r["encrypt_geek_id"]) for r in result] == [("accepted", "gid-1")]


def test_named_greeting_pool_fallback_cannot_revive_known_rejection_or_failure(tmp_path):
    _write_screening(tmp_path, [
        {"name": "rejected", "encrypt_geek_id": "gid-reject", "passed": False},
    ], [{"name": "pending", "geek_id": "gid-pending", "terminal_status": "analysis_unavailable"}])
    state = tmp_path / "state"
    state.mkdir()
    (state / "candidate_pool.json").write_text(json.dumps({"items": {
        "rejected": {"name": "rejected", "encrypt_geek_id": "gid-reject"},
        "pending": {"name": "pending", "encrypt_geek_id": "gid-pending"},
        "unknown": {"name": "unknown", "encrypt_geek_id": "gid-unknown"},
    }}), encoding="utf-8")
    result = _greet_module().load_high_score_candidates(
        str(tmp_path), "run-1", 60, only_names=["rejected", "pending", "unknown"]
    )
    assert [r["name"] for r in result] == ["unknown"]


@pytest.mark.parametrize("record", [
    {"candidates": [{"name": "candidate", "passed": False}]},
    {"failures": [{"name": "candidate", "terminal_status": "analysis_unavailable"}]},
])
def test_named_greeting_stub_fallback_never_opens_browser_for_known_nonpass(tmp_path, monkeypatch, record):
    _write_screening(tmp_path, record.get("candidates", []), record.get("failures", []))
    module = _greet_module()
    output = SimpleNamespace(job_dir=str(tmp_path), run_id="run-1", ensure_run_dir=lambda: None)
    monkeypatch.setattr(module, "JobOutputManager", lambda *_args, **_kwargs: output)
    monkeypatch.setattr("run_orchestrator.RunOrchestrator", lambda *_args, **_kwargs: SimpleNamespace(
        bind_existing_run=lambda run_id: run_id
    ))
    monkeypatch.setattr(module, "log", lambda *_args: None)
    monkeypatch.setattr(module.atexit, "register", lambda _callback: None)
    opened_browser = []

    def browser_forbidden():
        opened_browser.append(True)
        raise AssertionError("Known rejection/error must stop before browser actions")

    monkeypatch.setattr(module, "sync_playwright", browser_forbidden)
    module.auto_greet(job_name="job", encrypt_job_id="job-id", run_id="run-1", only_names=["candidate"])
    assert opened_browser == []


@pytest.mark.parametrize("passed,stale_decision,expected", [
    (True, "reject", "recommend"),
    (False, "recommend", "reject"),
])
def test_sql_decision_follows_canonical_boolean(tmp_path, passed, stale_decision, expected):
    workflow = _workflow(tmp_path)
    with UnitOfWork(workflow.database.session_factory) as uow:
        candidate = uow.candidates.upsert(boss_id="gid", job="job", name="candidate")
        uow.session.flush()
        candidate_id = candidate.id
    path = _write_screening(tmp_path, [{
        "name": "candidate", "geek_id": "gid", "passed": passed,
        "reason": "complete analysis", "decision": stale_decision,
    }])
    assert workflow.persist_analysis_from_screening_results(
        job_name="job", screening_results_file=path, run_id="run-1"
    ) == 1
    with UnitOfWork(workflow.database.session_factory) as uow:
        result = uow.analyses.latest_for_candidate(candidate_id)
        assert result.passed is passed
        assert result.decision == expected


@pytest.mark.parametrize("broken", [
    {"passed": "false", "recommended": True},
    {"passed": False, "analysis_status": "analysis_error"},
    {"passed": True, "analysis_status": "rejected"},
    {"recommended": "false"},
])
def test_later_invalid_analysis_cannot_overwrite_completed_sql_result(tmp_path, broken):
    workflow = _workflow(tmp_path)
    with UnitOfWork(workflow.database.session_factory) as uow:
        candidate = uow.candidates.upsert(boss_id="gid", job="job", name="candidate")
        uow.session.flush()
        candidate_id = candidate.id
        uow.analyses.add(candidate_id=candidate_id, passed=True, reason="complete", decision="recommend", run_id="run-1")
    path = _write_screening(tmp_path, [{"geek_id": "gid", "name": "candidate", **broken}])
    assert workflow.persist_analysis_from_screening_results(
        job_name="job", screening_results_file=path, run_id="run-1"
    ) == 0
    with UnitOfWork(workflow.database.session_factory) as uow:
        saved = uow.analyses.latest_for_candidate(candidate_id)
        assert saved.passed is True
        assert saved.reason == "complete"


def test_job_specific_fields_are_preserved_without_synthesizing_platform_index():
    original = {
        "passed": True, "reason": "complete", "work_evidence": [{"title": "work", "outcomes": []}],
        "analysis_audit": {"raw_result": {"passed": "符合"}},
    }
    snapshot = deepcopy(original)
    record = analysis_record(original, {}, "gid", "candidate")
    assert "selected_work_index" not in record
    assert record["work_evidence"] == snapshot["work_evidence"]
    record["analysis_audit"]["raw_result"]["passed"] = False
    record["work_evidence"][0]["outcomes"].append({"metric": "test"})
    assert original == snapshot


def test_retry_success_keeps_previous_failures_without_mutating_analyzer_result():
    returned = {"passed": True, "reason": "complete", "analysis_audit": {"raw_result": {"passed": True}}}

    class Analyzer:
        calls = 0

        def analyze(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                failure = RuntimeError("empty completion")
                failure.audit = {"attempts": [{"content": ""}]}
                raise failure
            return returned

    result, error, attempts = analyze_with_retries(Analyzer(), {}, {}, attempts=2, retry_delay=0)
    assert error is None and attempts == 2
    assert result["analysis_audit"]["failed_attempts"][0]["analysis_audit"]["attempts"][0]["content"] == ""
    assert "failed_attempts" not in returned["analysis_audit"]


def test_workflow_requirement_loader_keeps_job_specific_analysis_rules():
    rules = {"recent_outcomes": {"min_metrics": 2}, "expected_salary_min": 5000}
    config = SimpleNamespace(
        section=lambda _section: {"degree_required": "本科"},
        load_job_profile=lambda _name: {"resume_analysis": rules},
    )
    workflow = RecruitWorkflow.__new__(RecruitWorkflow)
    workflow.config = config
    requirement = workflow._configured_job_requirement("运营助理")
    assert requirement["resume_analysis"] == rules
