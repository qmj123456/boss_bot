"""OpenAI-compatible chat completion client."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Protocol

from boss_hr.config_manager import ConfigManager, get_config


class AIClient(Protocol):
    """Small protocol used by analyzers and tests."""

    def complete_json(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        ...

    def complete_text(self, messages: list[dict[str, Any]]) -> str:
        ...


class AIResponseError(RuntimeError):
    """An unusable completion, with a serializable audit for retry/storage."""

    def __init__(self, message: str, *, code: str, audit: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.audit = deepcopy(audit)


class OpenAICompatibleClient:
    """Client for OpenAI-compatible ``/chat/completions`` APIs."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or get_config()
        # One client is shared by screening workers. A global "last response"
        # would associate another candidate's completion with this candidate.
        self._completion_local = threading.local()

    def get_completion_audit(self) -> dict[str, Any]:
        """Return this thread's latest completion, without request/image data."""
        return deepcopy(getattr(self._completion_local, "audit", {}))

    @property
    def last_completion_audit(self) -> dict[str, Any]:
        return self.get_completion_audit()

    def _begin_completion(self) -> None:
        self._completion_local.audit = {"attempts": [], "repair_attempted": False}
        self._completion_local.stage = "initial"

    def _audit_value(self, value: Any) -> Any:
        """Redact secrets or image URLs even if a gateway echoes them back."""
        if isinstance(value, str):
            if self.api_key:
                value = value.replace(self.api_key, "[REDACTED_API_KEY]")
            return re.sub(r"data:image/[^\s\"']+", "[REDACTED_IMAGE_DATA]", value)
        if isinstance(value, dict):
            return {str(key): self._audit_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._audit_value(item) for item in value]
        return value

    def _response_error(self, message: str, code: str) -> AIResponseError:
        audit = getattr(self._completion_local, "audit", {})
        message = self._audit_value(message)
        audit["error"] = {"code": code, "message": message}
        return AIResponseError(message, code=code, audit=audit)

    @property
    def api_key(self) -> str:
        return str(self.config.get("ai.api_key", "") or "")

    @property
    def model(self) -> str:
        model = str(self.config.get("ai.model", "") or "")
        if not model:
            raise ValueError("Missing ai.model in config/ai.yaml")
        return model

    @property
    def base_url(self) -> str:
        return str(self.config.get("ai.base_url", "") or "https://api.openai.com/v1").rstrip("/")

    @staticmethod
    def _messages_have_images(messages: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in message["content"]
            )
            for message in messages
        )

    def _model_for_messages(self, messages: list[dict[str, Any]]) -> str:
        if self._messages_have_images(messages):
            return str(
                os.environ.get("DEEPSEEK_VISION_MODEL")
                or self.config.get("ai.vision_model", "")
                or self.model
            )
        return self.model

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        self._begin_completion()
        content = self._chat(
            messages, json_mode=True, max_tokens_override=max_tokens_override
        )
        try:
            return _parse_json_object(content)
        except (json.JSONDecodeError, ValueError, TypeError) as original_error:
            # Some OpenAI-compatible vision gateways occasionally ignore
            # response_format and return an almost-valid object (most often a
            # missing comma or unescaped quote).  Re-sending all resume images is
            # slow and can reproduce the same failure.  Ask the model to repair
            # only the already-produced text first; the caller's normal retry is
            # still available if the response was empty or genuinely truncated.
            # Scalars/arrays and unfinished objects are not syntax-repair jobs:
            # asking a model to "repair" them can fabricate missing analysis.
            audit = self._completion_local.audit
            audit["attempts"][-1]["parse_error"] = str(original_error)
            if not isinstance(original_error, json.JSONDecodeError) or not _is_repair_candidate(content):
                raise self._response_error(
                    f"AI JSON response is unusable: {original_error}", "invalid_json"
                ) from original_error
            audit["repair_attempted"] = True
            self._completion_local.stage = "json_repair"
            repaired = self._chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是 JSON 语法修复器。只修复语法，不新增、删除、"
                            "推断或改写任何字段和值；只输出一个合法 JSON 对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": content,
                    },
                ],
                json_mode=True,
                max_tokens_override=max(
                    int(max_tokens_override or 0),
                    int(self.config.get("ai.max_tokens", 1200)),
                    int(self.config.get("ai.vision_max_tokens", 8000)),
                ),
            )
            try:
                return _parse_json_object(repaired)
            except (json.JSONDecodeError, ValueError, TypeError) as repair_error:
                audit["attempts"][-1]["parse_error"] = str(repair_error)
                raise self._response_error(
                    f"AI JSON syntax repair failed: {repair_error}", "invalid_json"
                ) from repair_error

    def complete_text(self, messages: list[dict[str, Any]]) -> str:
        self._begin_completion()
        return self._chat(messages, json_mode=False).strip()

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool,
        max_tokens_override: int | None = None,
    ) -> str:
        if not self.api_key:
            raise self._response_error("Missing ai.api_key in config/ai.yaml", "missing_api_key")

        has_images = self._messages_have_images(messages)
        max_tokens_key = "ai.vision_max_tokens" if has_images else "ai.max_tokens"
        # 视觉模型默认给 8000：推理型网关即使传 enable_thinking=false 也可能
        # 先消耗 reasoning tokens，4000 会在长简历上被思考过程吃满。
        max_tokens_default = 8000 if has_images else 1200
        payload: dict[str, Any] = {
            "model": self._model_for_messages(messages),
            "messages": messages,
            "temperature": float(self.config.get("ai.temperature", 0.2)),
            "max_tokens": (
                int(max_tokens_override)
                if max_tokens_override is not None
                else int(self.config.get(max_tokens_key, max_tokens_default))
            ),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            payload["enable_thinking"] = False

        audit = getattr(self._completion_local, "audit", None)
        if audit is None:
            self._begin_completion()
            audit = self._completion_local.audit
        attempt: dict[str, Any] = {
            "stage": self._completion_local.stage,
            "model": payload["model"],
            "request_id": None,
            "finish_reason": None,
            "usage": None,
            "content": None,
        }
        audit["attempts"].append(attempt)

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        timeout_key = "ai.vision_timeout_seconds" if has_images else "ai.timeout_seconds"
        timeout_default = 40 if has_images else 60
        timeout = int(self.config.get(timeout_key, timeout_default))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = getattr(resp, "headers", {})
                attempt["request_id"] = self._audit_value(
                    headers.get("x-request-id") or headers.get("request-id")
                )
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Gateways may echo the submitted request in an error body. Do not
            # persist credentials or resume image data from that body.
            attempt["http_status"] = exc.code
            raise self._response_error(f"AI API HTTP {exc.code}", "http_error") from exc
        except urllib.error.URLError as exc:
            raise self._response_error(
                f"AI API request failed: {exc.reason}", "request_failed"
            ) from exc
        except (TimeoutError, UnicodeDecodeError, OSError) as exc:
            raise self._response_error(
                f"AI API response could not be read: {exc}", "request_failed"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._response_error("AI API response is not valid JSON", "invalid_envelope") from exc
        if not isinstance(data, dict):
            raise self._response_error("AI API response must be an object", "invalid_envelope")
        attempt["model"] = self._audit_value(data.get("model") or payload["model"])
        attempt["request_id"] = attempt["request_id"] or self._audit_value(data.get("id"))
        attempt["completion_id"] = self._audit_value(data.get("id"))
        attempt["usage"] = self._audit_value(data.get("usage"))
        try:
            choice = data["choices"][0]
            attempt["finish_reason"] = self._audit_value(choice.get("finish_reason"))
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise self._response_error(
                "AI API response missing choices[0].message.content", "invalid_envelope"
            ) from exc
        except AttributeError as exc:
            raise self._response_error("AI API choice must be an object", "invalid_envelope") from exc
        attempt["content"] = self._audit_value(content)
        if choice.get("finish_reason") == "length":
            raise self._response_error(
                "AI completion was truncated (finish_reason=length); retry the analysis", "response_truncated"
            )
        if not isinstance(content, str) or not content.strip():
            raise self._response_error("AI completion content is empty or not text", "response_empty")
        return content


def _is_repair_candidate(content: str) -> bool:
    """Only repair a bounded, complete-looking JSON object, never a fragment."""
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return len(content) <= 50000 and text.startswith("{") and text.endswith("}")


def _parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a short fenced-code wrapper."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
            if text.startswith("json"):
                text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI JSON response must be an object")
    return data


__all__ = ["AIClient", "AIResponseError", "OpenAICompatibleClient"]
