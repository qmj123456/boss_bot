"""Recover only failed AI analyses from a specific run's saved screenshots."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from PIL import Image
from shared.analysis_artifact_lock import RunArtifactLock
from shared.analysis_result import (analysis_record, analyze_with_retries, error_detail,
                                    is_completed_analysis)


def _gid(item):
    meta = item.get("_meta") or {}
    return str(item.get("geek_id") or item.get("encrypt_geek_id") or meta.get("encrypt_geek_id") or "")


def _atomic_write(path, payload, expected):
    if path.read_text(encoding="utf-8") != expected:
        raise RuntimeError("本轮结果已被其他进程更新，停止回补以避免覆盖；请在本轮任务结束后重试")
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, temporary = tempfile.mkstemp(prefix=".analysis-recovery-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return content


def _sync_recovered_artifacts(screening_path, candidates):
    """Reconcile derived metadata after the authoritative screening commit.

    Safe on rerun after a crash between files; never clears greeting/capture failures.
    """
    completed = {_gid(c): c for c in candidates if is_completed_analysis(c)}
    failed_path = screening_path.with_name("failed_resumes.json")
    if failed_path.is_file():
        expected = failed_path.read_text(encoding="utf-8")
        failed = json.loads(expected)
        remaining = [f for f in failed if not (
            _gid(f) in completed and f.get("stage") == "deepseek_analysis")]
        if remaining != failed:
            _atomic_write(failed_path, remaining, expected)
    resume_path = screening_path.with_name("new_resumes.json")
    expected = resume_path.read_text(encoding="utf-8")
    resumes = json.loads(expected)
    changed = False
    for resume in resumes:
        record = completed.get(_gid(resume))
        if not record or not record.get("analysis_audit", {}).get("recovery"):
            continue
        meta = resume.setdefault("_meta", {})
        if meta.get("deepseek_passed") is not record["passed"] or "deepseek_error" in meta:
            meta["deepseek_passed"] = record["passed"]
            meta.pop("deepseek_error", None)
            meta["analysis_recovered_at"] = record["analysis_audit"]["recovery"]["recovered_at"]
            changed = True
    if changed:
        _atomic_write(resume_path, resumes, expected)


def retry_saved_analyses(screening_path, *, analyzer, requirement, job_name,
                         attempts=3, retry_delay=2.0, retry_max_delay=8.0):
    with RunArtifactLock(Path(screening_path).resolve().parent):
        return _retry_saved_analyses_unlocked(
            screening_path, analyzer=analyzer, requirement=requirement, job_name=job_name,
            attempts=attempts, retry_delay=retry_delay, retry_max_delay=retry_max_delay)


def _retry_saved_analyses_unlocked(screening_path, *, analyzer, requirement, job_name,
                         attempts=3, retry_delay=2.0, retry_max_delay=8.0):
    """No browser calls or greetings. Passed results feed the normal greet stage.

    Existing completed decisions are immutable. Only the failures queue is retried,
    and each completed candidate is committed immediately so reruns are idempotent.
    """
    screening_path = Path(screening_path).resolve()
    expected = screening_path.read_text(encoding="utf-8")
    screening = json.loads(expected)
    if not isinstance(screening, dict) or screening.get("job") != job_name:
        raise ValueError("回补岗位与筛选结果岗位不一致，禁止跨岗位回补")
    if isinstance(requirement, dict) and requirement.get("job_name", job_name) != job_name:
        raise ValueError("回补要求与筛选结果岗位不一致")
    if screening.get("run_id") and screening["run_id"] != screening_path.parent.parent.name:
        raise ValueError("回补路径与结果 run_id 不一致")
    resume_path = screening_path.with_name("new_resumes.json")
    resumes = json.loads(resume_path.read_text(encoding="utf-8"))
    resume_by_id = {}
    for resume in resumes:
        if not isinstance(resume, dict) or not _gid(resume):
            continue
        if _gid(resume) in resume_by_id:
            raise ValueError("同一运行中存在重复候选人简历，无法确定截图归属")
        resume_by_id[_gid(resume)] = resume
    run_dir = screening_path.parent.parent
    run_file = run_dir / "run.json"
    run_meta = json.loads(run_file.read_text(encoding="utf-8")) if run_file.is_file() else {}
    expected_job_id = screening.get("encrypt_job_id") or run_meta.get("encrypt_job_id")
    if run_meta.get("run_id") and run_meta["run_id"] != run_dir.name:
        raise ValueError("运行元数据与当前目录不一致")
    candidates = list(screening.get("candidates") or [])
    completed = {_gid(c) for c in candidates if is_completed_analysis(c)}
    failures = [f for f in screening.get("failures", []) if _gid(f) not in completed]
    targets = list(dict.fromkeys(_gid(f) for f in failures if _gid(f) and _gid(f) not in completed))
    stats = {"retried": 0, "recovered": 0, "passed": 0, "rejected": 0, "failed": 0}
    for gid in targets:
        previous = [deepcopy(f) for f in failures if _gid(f) == gid]
        resume = resume_by_id.get(gid)
        name = (resume or {}).get("name") or previous[0].get("name") or ""
        stats["retried"] += 1
        try:
            if not resume:
                raise ValueError("未找到该候选人的已保存简历，不能回补")
            meta = resume.get("_meta") or {}
            if expected_job_id and meta.get("encrypt_job_id") and meta["encrypt_job_id"] != expected_job_id:
                raise ValueError("简历截图岗位ID与本轮岗位不一致")
            if meta.get("first_run_id") and meta["first_run_id"] != run_dir.name:
                raise ValueError("简历来源运行与本轮不一致")
            if meta.get("candidate_key"):
                parts = str(meta["candidate_key"]).rsplit(":", 1)
                if len(parts) != 2 or parts[1] != gid or (expected_job_id and parts[0] != expected_job_id):
                    raise ValueError("简历候选人身份与本轮岗位不一致")
            screenshots = resume.get("resume_screenshots")
            if not isinstance(screenshots, list) or not screenshots:
                raise ValueError("未找到已保存的完整简历截图，不能回补")
            for image_path in screenshots:
                resolved = Path(image_path).resolve()
                if not resolved.is_relative_to(run_dir):
                    raise ValueError("截图不属于当前运行目录，禁止跨运行回补")
                with Image.open(resolved) as image:
                    image.verify()
            result, error, count = analyze_with_retries(
                analyzer, requirement, resume, attempts=attempts,
                retry_delay=retry_delay, retry_max_delay=retry_max_delay)
            if error is not None:
                raise error
            record = analysis_record(result, resume, gid, name)
            record.setdefault("analysis_audit", {})["recovery"] = {
                "source_run": screening_path.parent.parent.name,
                "recovered_at": datetime.now().isoformat(timespec="seconds"),
                "previous_failures": previous, "attempts": count,
            }
            # Replace only nonterminal placeholders for this ID.
            candidates[:] = [c for c in candidates if _gid(c) != gid]
            candidates.append(record)
            failures[:] = [f for f in failures if _gid(f) != gid]
            completed.add(gid)
            stats["recovered"] += 1
            stats["passed" if record["passed"] else "rejected"] += 1
        except Exception as exc:
            failures[:] = [f for f in failures if _gid(f) != gid]
            failures.append({"geek_id": gid, "name": name, **error_detail(exc),
                             "stage": "deepseek_analysis", "terminal_status": "analysis_unavailable",
                             "retry_mode": "saved_screenshot_only"})
            stats["failed"] += 1
        screening["candidates"] = candidates
        screening["failures"] = failures
        screening["summary"] = {
            "total": sum(is_completed_analysis(c) for c in candidates),
            "passed": sum(is_completed_analysis(c) and c["passed"] for c in candidates),
            "rejected": sum(is_completed_analysis(c) and not c["passed"] for c in candidates),
            "failed": len(failures),
        }
        expected = _atomic_write(screening_path, screening, expected)
    _sync_recovered_artifacts(screening_path, candidates)
    return stats
