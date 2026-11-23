"""Repository layer for recruitment data."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    AuditLog,
    Candidate,
    CandidateAnalysis,
    GreetingRecord,
    SyncRecord,
)


class CandidateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, candidate_id: int) -> Candidate | None:
        return self.session.get(Candidate, candidate_id)

    def get_by_boss_id(self, boss_id: str, job: str) -> Candidate | None:
        stmt = select(Candidate).where(
            Candidate.boss_id == boss_id,
            Candidate.job == job,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert(
        self,
        *,
        boss_id: str,
        job: str,
        run_id: str = "",
        name: str = "",
        resume: dict | list | str | None = None,
        company: str = "",
        work_years: str = "",
        experience: str = "",
        skills: list | str | None = None,
        age: int | None = None,
        degree: str = "",
        city: str = "",
        expected_salary: str = "",
        current_title: str = "",
        card_prescreen_reason: str = "",
        card_prescreen_status: str = "",
        card_prescreen_passed: bool | None = None,
        card_prescreen_detail: dict | list | str | None = None,
    ) -> Candidate:
        candidate = self.get_by_boss_id(boss_id, job)
        if candidate is None:
            candidate = Candidate(boss_id=boss_id, job=job)
            self.session.add(candidate)
        candidate.run_id = run_id or candidate.run_id or ""
        candidate.name = name or candidate.name or ""
        candidate.resume = resume if resume is not None else candidate.resume
        candidate.company = company or candidate.company or ""
        normalized_work_years = work_years or experience or candidate.work_years or candidate.experience or ""
        candidate.work_years = normalized_work_years
        candidate.experience = normalized_work_years
        candidate.skills = skills if skills is not None else candidate.skills
        candidate.age = age if age is not None else candidate.age
        candidate.degree = degree or candidate.degree or ""
        candidate.city = city or candidate.city or ""
        candidate.expected_salary = expected_salary or candidate.expected_salary or ""
        candidate.current_title = current_title or candidate.current_title or ""
        candidate.card_prescreen_reason = card_prescreen_reason or candidate.card_prescreen_reason or ""
        candidate.card_prescreen_status = card_prescreen_status or candidate.card_prescreen_status or ""
        candidate.card_prescreen_passed = (
            card_prescreen_passed
            if card_prescreen_passed is not None else candidate.card_prescreen_passed
        )
        candidate.card_prescreen_detail = (
            card_prescreen_detail
            if card_prescreen_detail is not None else candidate.card_prescreen_detail
        )
        return candidate

    def list_by_job(self, job: str) -> list[Candidate]:
        stmt = select(Candidate).where(Candidate.job == job).order_by(Candidate.created_time.desc())
        return list(self.session.execute(stmt).scalars())


class CandidateAnalysisRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        candidate_id: int,
        passed: bool | None = None,
        reason: str = "",
        decision: str = "",
        model: str = "",
        run_id: str = "",
    ) -> CandidateAnalysis:
        # 同一候选人在同一 run 重试时覆盖原分析，避免生成重复业务记录。
        if run_id:
            stmt = select(CandidateAnalysis).where(
                CandidateAnalysis.candidate_id == candidate_id,
                CandidateAnalysis.run_id == run_id,
            ).order_by(CandidateAnalysis.id.desc()).limit(1)
            existing = self.session.execute(stmt).scalar_one_or_none()
            if existing is not None:
                existing.passed = passed
                existing.reason = reason
                existing.decision = decision
                existing.model = model
                return existing
        item = CandidateAnalysis(
            candidate_id=candidate_id,
            run_id=run_id,
            passed=passed,
            reason=reason,
            decision=decision,
            model=model,
        )
        self.session.add(item)
        return item

    def latest_for_candidate(self, candidate_id: int) -> CandidateAnalysis | None:
        stmt = (
            select(CandidateAnalysis)
            .where(CandidateAnalysis.candidate_id == candidate_id)
            .order_by(CandidateAnalysis.created_time.desc(), CandidateAnalysis.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()


class GreetingRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        candidate_id: int,
        message: str,
        status: str,
        error: str = "",
        run_id: str = "",
    ) -> GreetingRecord:
        if run_id:
            stmt = select(GreetingRecord).where(
                GreetingRecord.candidate_id == candidate_id,
                GreetingRecord.run_id == run_id,
            ).order_by(GreetingRecord.id.desc()).limit(1)
            existing = self.session.execute(stmt).scalar_one_or_none()
            if existing is not None:
                existing.message = message
                existing.status = status
                existing.error = error
                return existing
        item = GreetingRecord(
            candidate_id=candidate_id,
            message=message,
            status=status,
            error=error,
            run_id=run_id,
        )
        self.session.add(item)
        return item

    def latest_for_candidate(self, candidate_id: int) -> GreetingRecord | None:
        stmt = (
            select(GreetingRecord)
            .where(GreetingRecord.candidate_id == candidate_id)
            .order_by(GreetingRecord.send_time.desc(), GreetingRecord.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()


class SyncRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        candidate_id: int,
        feishu_record_id: str = "",
        status: str,
        feishu_table_id: str = "",
        error: str = "",
    ) -> SyncRecord:
        item = SyncRecord(
            candidate_id=candidate_id,
            feishu_record_id=feishu_record_id,
            status=status,
            feishu_table_id=feishu_table_id,
            error=error,
        )
        self.session.add(item)
        return item

    def latest_for_candidate(
        self,
        candidate_id: int,
        *,
        feishu_table_id: str | None = None,
    ) -> SyncRecord | None:
        stmt = select(SyncRecord).where(SyncRecord.candidate_id == candidate_id)
        if feishu_table_id is not None:
            stmt = stmt.where(SyncRecord.feishu_table_id == feishu_table_id)
        stmt = stmt.order_by(SyncRecord.sync_time.desc(), SyncRecord.id.desc()).limit(1)
        return self.session.execute(stmt).scalar_one_or_none()


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        action: str,
        detail: dict | list | str | None = None,
        result: str = "",
    ) -> AuditLog:
        item = AuditLog(action=action, detail=detail, result=result)
        self.session.add(item)
        return item

    def list_recent(self, limit: int = 100) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.time.desc(), AuditLog.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())


class UnitOfWork(AbstractContextManager["UnitOfWork"]):
    """Repository bundle with commit/rollback handling."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self.session_factory()
        self.candidates = CandidateRepository(self.session)
        self.analyses = CandidateAnalysisRepository(self.session)
        self.greetings = GreetingRecordRepository(self.session)
        self.sync_records = SyncRecordRepository(self.session)
        self.audit_logs = AuditLogRepository(self.session)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        if self.session is None:
            return None
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
        return None
