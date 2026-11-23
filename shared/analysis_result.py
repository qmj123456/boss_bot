"""Canonical analysis results shared by live, batch, and greeting consumers."""
from __future__ import annotations

from copy import deepcopy
import time


COMPLETED_STATUSES = (None, "", "completed", "passed", "rejected")
OPTIONAL_FIELDS = ("employment_status", "expected_salary", "work_evidence",
                   "selected_work_index", "analysis_audit")


def is_completed_analysis(result):
    if not isinstance(result, dict) or type(result.get("passed")) is not bool:
        return False
    status = result.get("analysis_status")
    return (status in COMPLETED_STATUSES
            and not (status == "passed" and result["passed"] is False)
            and not (status == "rejected" and result["passed"] is True))


def is_passed_analysis(result):
    """Never interpret a string, an error state, or a legacy alias as an override."""
    if not isinstance(result, dict):
        return False
    if "passed" in result:
        return is_completed_analysis(result) and result["passed"] is True
    return (result.get("recommended") is True
            and result.get("analysis_status") in (None, "", "completed", "passed"))


def validate_completed_analysis(result):
    if not is_completed_analysis(result):
        raise ValueError("AI 分析结果缺少有效的布尔 passed 或仍处于分析异常状态")
    if not isinstance(result.get("reason"), str) or not result["reason"].strip():
        raise ValueError("AI 分析结果缺少有效的 reason")
    return result


def analysis_record(result, resume, geek_id, name):
    """Preserve job-specific fields only when actually returned by the analyzer."""
    validate_completed_analysis(result)
    record = {
        "geek_id": geek_id, "encrypt_geek_id": geek_id, "name": name,
        "passed": result["passed"],
        "decision": "recommend" if result["passed"] else "reject",
        "reason": result["reason"],
        "analysis_status": "passed" if result["passed"] else "rejected",
        "age": resume.get("age") or "", "degree": resume.get("degree") or "",
        "city": resume.get("city") or resume.get("expect_city") or "",
        "expected_salary": resume.get("expected_salary") or "",
        "work_years": resume.get("work_years") or "",
        "current_role": resume.get("current_role") or resume.get("expect_position") or "",
    }
    for key in OPTIONAL_FIELDS:
        if key in result:
            record[key] = deepcopy(result[key])
    return record


def error_detail(exc):
    detail = {"error": str(exc)[:1000], "error_code": getattr(exc, "code", type(exc).__name__)}
    audit = getattr(exc, "audit", None)
    if isinstance(audit, dict):
        detail["analysis_audit"] = deepcopy(audit)
    return detail


def _is_truncated(exc) -> bool:
    """Recognize truncation even after ResumeAnalyzer wraps the client error.

    The analyzer re-raises everything as ``completion_unavailable`` but keeps the
    client's code in ``audit["completion_error_code"]``.
    """
    if getattr(exc, "code", "") == "response_truncated":
        return True
    audit = getattr(exc, "audit", None)
    return isinstance(audit, dict) and audit.get("completion_error_code") == "response_truncated"


def _vision_token_budget(analyzer):
    """Return (base, step, ceiling) used to widen a truncated analysis retry."""
    config = getattr(analyzer, "config", None)
    try:
        base = int(config.get("ai.vision_max_tokens", 8000)) if config is not None else 8000
        step = int(config.get("ai.vision_max_tokens_step", 4000)) if config is not None else 4000
        ceiling = int(config.get("ai.vision_max_tokens_ceiling", 16000)) if config is not None else 16000
    except Exception:
        base, step, ceiling = 8000, 4000, 16000
    return max(1, base), max(1, step), max(base, ceiling)


def analyze_with_retries(analyzer, requirement, resume, *, attempts=3,
                         retry_delay=2.0, retry_max_delay=8.0, log=None):
    """Retry analysis only; never operate the browser or turn errors into rejection.

    ``finish_reason=length`` truncation is retried with a larger output budget
    (base → base+step → base+2*step, capped).  Retrying with the same budget
    deterministically truncates again, which is what silently lost long resumes.
    """
    attempts = max(1, int(attempts))
    history = []
    last_error = None
    base_tokens, step_tokens, ceiling = _vision_token_budget(analyzer)
    override = None
    for attempt in range(1, attempts + 1):
        try:
            if override is None:
                result = analyzer.analyze(job_requirement=requirement, resume=resume)
            else:
                result = analyzer.analyze(
                    job_requirement=requirement, resume=resume,
                    max_tokens_override=override,
                )
            validate_completed_analysis(result)
            result = deepcopy(result)
            result.setdefault("analysis_status", "passed" if result["passed"] else "rejected")
            if history:
                result.setdefault("analysis_audit", {})["failed_attempts"] = history
            return result, None, attempt
        except Exception as exc:
            last_error = exc
            history.append({"attempt": attempt, **error_detail(exc)})
            if callable(log):
                log(f"DeepSeek 第 {attempt}/{attempts} 次分析失败；继续复用已保存截图：{str(exc)[:180]}")
            if attempt < attempts:
                if _is_truncated(exc):
                    # 输出被截断：抬高 token 预算后再试，而不是原样重试。
                    if override is None:
                        override = min(base_tokens + step_tokens, ceiling)
                    else:
                        override = min(override + step_tokens, ceiling)
                    if callable(log):
                        log(f"输出被截断，下一次重试预算提升到 {override} tokens")
                text = str(exc).casefold()
                transient = any(token in text for token in (
                    "http 429", "http 500", "http 502", "http 503", "http 504",
                    "too busy", "service_unavailable", "timeout", "timed out"))
                delay = retry_delay * (2 ** (attempt - 1)) if transient else retry_delay
                time.sleep(max(0, min(delay, retry_max_delay)))
    # Preserve the entire series, not just the final request's exception.
    last_error.audit = {"failed_attempts": history}
    return None, last_error, attempts
