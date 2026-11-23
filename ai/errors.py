"""Failures to obtain a usable resume analysis, separate from business rejection."""
from __future__ import annotations

import json
from typing import Any


class ResumeAnalysisUnavailableError(RuntimeError):
    """The response cannot safely determine whether the candidate qualifies."""

    def __init__(self, message: str, *, code: str = "invalid_response", audit: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        # Audits are persisted by both the batch and live recruiting pipelines.
        self.audit = json.loads(json.dumps(audit or {}, ensure_ascii=False, default=str))


__all__ = ["ResumeAnalysisUnavailableError"]
