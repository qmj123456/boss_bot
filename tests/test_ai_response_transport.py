"""Offline checks for completion integrity and per-candidate response audits."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai.client import AIResponseError, OpenAICompatibleClient


class Config:
    def get(self, key, default=None):
        return {
            "ai.api_key": "private-test-key",
            "ai.model": "text-model",
            "ai.vision_model": "vision-model",
            "ai.base_url": "https://example.invalid/v1",
        }.get(key, default)


class Response:
    def __init__(self, content, *, finish_reason="stop", request_id="req-1", **extra):
        self.headers = {"x-request-id": request_id}
        self.data = {
            "id": "completion-1",
            "model": "returned-model",
            "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            **extra,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.data).encode("utf-8")


def _client_with_responses(monkeypatch, *responses):
    calls = []
    iterator = iter(responses)

    def open_response(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return next(iterator)

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    return OpenAICompatibleClient(Config()), calls


def test_completion_audit_keeps_metadata_and_content_without_request_images(monkeypatch):
    original = '{"passed": true, "reason": "符合"}'
    client, _calls = _client_with_responses(monkeypatch, Response(original))
    result = client.complete_json([{
        "role": "user",
        "content": [
            {"type": "text", "text": "resume"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret-image"}},
        ],
    }])
    assert result["passed"] is True
    audit = client.get_completion_audit()
    assert audit["attempts"] == [{
        "stage": "initial",
        "model": "returned-model",
        "request_id": "req-1",
        "completion_id": "completion-1",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
        "content": original,
    }]
    assert audit["repair_attempted"] is False
    serialized = json.dumps(audit)
    assert "secret-image" not in serialized
    assert "private-test-key" not in serialized
    audit["attempts"][0]["content"] = "changed"
    assert client.last_completion_audit["attempts"][0]["content"] == original


@pytest.mark.parametrize("content", [None, "", "  \n  ", [], {"passed": True}])
def test_empty_or_nontext_completion_raises_with_audit_and_does_not_repair(monkeypatch, content):
    client, calls = _client_with_responses(monkeypatch, Response(content))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "response_empty"
    assert captured.value.audit["attempts"][0]["content"] == content
    assert captured.value.audit["repair_attempted"] is False
    json.dumps(captured.value.audit)
    assert len(calls) == 1


@pytest.mark.parametrize("content", ["null", "[]", '"approved"', "not JSON", '{"passed": true'])
def test_nonobject_or_incomplete_json_does_not_trigger_syntax_repair(monkeypatch, content):
    client, calls = _client_with_responses(monkeypatch, Response(content))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "invalid_json"
    assert captured.value.audit["attempts"][0]["content"] == content
    assert captured.value.audit["repair_attempted"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("content", ['{"passed": true', '{"passed": true, "reason": "符合"}'])
def test_length_finish_reason_never_becomes_candidate_decision(monkeypatch, content):
    client, calls = _client_with_responses(monkeypatch, Response(content, finish_reason="length"))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "response_truncated"
    assert captured.value.audit["attempts"][0]["finish_reason"] == "length"
    assert captured.value.audit["repair_attempted"] is False
    assert len(calls) == 1


def test_empty_object_reaches_analyzer_semantic_validation(monkeypatch):
    client, calls = _client_with_responses(monkeypatch, Response("{}"))
    assert client.complete_json([]) == {}
    assert len(calls) == 1


def test_json_syntax_repair_preserves_both_responses_and_metadata(monkeypatch):
    original = '{"passed": true "reason": "符合"}'
    repaired = '{"passed": true, "reason": "符合"}'
    client, calls = _client_with_responses(
        monkeypatch, Response(original, request_id="initial-id"), Response(repaired, request_id="repair-id")
    )
    assert client.complete_json([]) == {"passed": True, "reason": "符合"}
    audit = client.get_completion_audit()
    assert audit["repair_attempted"] is True
    assert [a["stage"] for a in audit["attempts"]] == ["initial", "json_repair"]
    assert [a["content"] for a in audit["attempts"]] == [original, repaired]
    assert [a["request_id"] for a in audit["attempts"]] == ["initial-id", "repair-id"]
    assert calls[1]["messages"][1]["content"] == original


def test_failed_syntax_repair_has_original_and_failed_response_audit(monkeypatch):
    original = '{"passed": true "reason": "符合"}'
    client, _calls = _client_with_responses(monkeypatch, Response(original), Response("null"))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "invalid_json"
    assert [a["content"] for a in captured.value.audit["attempts"]] == [original, "null"]
    assert all("parse_error" in a for a in captured.value.audit["attempts"])


def test_truncated_repair_keeps_both_attempts(monkeypatch):
    original = '{"passed": true "reason": "符合"}'
    client, _calls = _client_with_responses(
        monkeypatch, Response(original), Response('{"passed":', finish_reason="length")
    )
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "response_truncated"
    assert len(captured.value.audit["attempts"]) == 2
    assert captured.value.audit["attempts"][0]["content"] == original


def test_audit_redacts_gateway_echoed_secrets_and_images(monkeypatch):
    content = json.dumps({"reason": "private-test-key data:image/png;base64,secret-image"})
    client, _calls = _client_with_responses(monkeypatch, Response(content))
    client.complete_json([])
    audit_text = json.dumps(client.get_completion_audit())
    assert "private-test-key" not in audit_text
    assert "secret-image" not in audit_text
    assert "REDACTED" in audit_text


def test_shared_client_audits_are_thread_local(monkeypatch):
    barrier = threading.Barrier(2)

    def open_response(request, timeout):
        candidate = json.loads(request.data.decode("utf-8"))["messages"][0]["content"]
        return Response(json.dumps({"passed": True, "reason": candidate}), request_id=candidate)

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    client = OpenAICompatibleClient(Config())

    def analyze(candidate):
        result = client.complete_json([{"role": "user", "content": candidate}])
        barrier.wait(timeout=5)
        return result, client.get_completion_audit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(analyze, ["candidate-A", "candidate-B"]))
    for (result, audit), expected in zip(results, ["candidate-A", "candidate-B"]):
        assert result["reason"] == expected
        assert audit["attempts"][0]["request_id"] == expected
        assert json.loads(audit["attempts"][0]["content"])["reason"] == expected
    assert client.get_completion_audit() == {}


def test_next_completion_resets_audit_without_mutating_previous_exception(monkeypatch):
    client, _calls = _client_with_responses(monkeypatch, Response(None), Response('{"passed": true}'))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    client.complete_json([])
    assert captured.value.audit["attempts"][0]["content"] is None
    assert "error" not in client.get_completion_audit()
    assert len(client.get_completion_audit()["attempts"]) == 1


@pytest.mark.parametrize("choices", [[], None, [None], ["wrong"], [{}]])
def test_invalid_completion_envelope_preserves_diagnostics(monkeypatch, choices):
    client, _calls = _client_with_responses(monkeypatch, Response(None, choices=choices))
    with pytest.raises(AIResponseError) as captured:
        client.complete_json([])
    assert captured.value.code == "invalid_envelope"
    assert captured.value.audit["attempts"][0]["request_id"] == "req-1"
