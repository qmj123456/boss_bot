"""SQLAlchemy ORM models for recruitment data."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    """Candidate information fetched from BOSS."""

    __tablename__ = "candidate"
    __table_args__ = (
        UniqueConstraint("boss_id", "job", name="uq_candidate_boss_job"),
        Index("ix_candidate_job_created", "job", "created_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    boss_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    job: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    resume: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    company: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # 标准字段：工作年限。experience 为旧字段兼容，新代码会同步写入。
    work_years: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    experience: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    skills: Mapped[list | str | None] = mapped_column(JSON, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    degree: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    expected_salary: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    current_title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    card_prescreen_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    card_prescreen_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    card_prescreen_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    card_prescreen_detail: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    analyses: Mapped[list["CandidateAnalysis"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )
    greetings: Mapped[list["GreetingRecord"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )
    sync_records: Mapped[list["SyncRecord"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class CandidateAnalysis(Base):
    """AI resume screening result against job hard requirements."""

    __tablename__ = "candidate_analysis"
    __table_args__ = (Index("ix_candidate_analysis_candidate_time", "candidate_id", "created_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    # decision 使用 recommend / reject，方便 JSON、SQLite、飞书统一筛选口径。
    decision: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # passed 表示是否满足岗位全部硬性条件，允许进入自动打招呼池。
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # AI 对照岗位硬性要求逐条判断的通过/不通过原因。
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    candidate: Mapped[Candidate] = relationship(back_populates="analyses")


class GreetingRecord(Base):
    """Traceable greeting message send record."""

    __tablename__ = "greeting_record"
    __table_args__ = (Index("ix_greeting_candidate_send_time", "candidate_id", "send_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    send_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    candidate: Mapped[Candidate] = relationship(back_populates="greetings")


class SyncRecord(Base):
    """Feishu bitable sync state for one candidate."""

    __tablename__ = "sync_record"
    __table_args__ = (
        Index("ix_sync_candidate_sync_time", "candidate_id", "sync_time"),
        Index("ix_sync_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feishu_record_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    feishu_table_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    candidate: Mapped[Candidate] = relationship(back_populates="sync_records")


class AuditLog(Base):
    """Audit trail for automated actions."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_time", "time"),
        Index("ix_audit_log_action", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
