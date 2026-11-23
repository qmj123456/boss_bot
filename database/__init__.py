"""Database layer for the single-machine HR Agent."""

from database.connection import (
    Database,
    create_engine_from_config,
    create_session_factory,
    init_database,
)
from database.models import (
    AuditLog,
    Base,
    Candidate,
    CandidateAnalysis,
    GreetingRecord,
    SyncRecord,
)
from database.repositories import (
    AuditLogRepository,
    CandidateAnalysisRepository,
    CandidateRepository,
    GreetingRecordRepository,
    SyncRecordRepository,
    UnitOfWork,
)

__all__ = [
    "AuditLog",
    "AuditLogRepository",
    "Base",
    "Candidate",
    "CandidateAnalysis",
    "CandidateAnalysisRepository",
    "CandidateRepository",
    "Database",
    "GreetingRecord",
    "GreetingRecordRepository",
    "SyncRecord",
    "SyncRecordRepository",
    "UnitOfWork",
    "create_engine_from_config",
    "create_session_factory",
    "init_database",
]
