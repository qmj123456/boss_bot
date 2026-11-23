"""SQLAlchemy database connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from boss_hr.config_manager import ConfigManager, get_config
from database.models import Base


def _sqlite_url_from_path(path: str | Path) -> str:
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path.as_posix()}"


def create_engine_from_config(config: ConfigManager | None = None) -> Engine:
    """Create a SQLAlchemy engine from app.database.path.

    SQLite is the first implementation. The rest of the code depends only on
    SQLAlchemy's Engine/Session APIs, so a future PostgreSQL URL can be added
    without changing repositories.
    """
    cfg = config or get_config()
    explicit_url = cfg.get("app.database.url")
    if explicit_url:
        return create_engine(str(explicit_url), future=True)

    db_path = cfg.get_path("app.database.path")
    if db_path is None:
        raise ValueError("Missing app.database.path in config/app.yaml")
    engine = create_engine(
        _sqlite_url_from_path(db_path),
        future=True,
        connect_args={"check_same_thread": False},
    )
    _enable_sqlite_foreign_keys(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Enable SQLite foreign-key enforcement for each new connection."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a SQLAlchemy 2.x session factory."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    """Create all ORM tables if they do not already exist."""
    Base.metadata.create_all(engine)
    _migrate_existing_sqlite(engine)


def _migrate_existing_sqlite(engine: Engine) -> None:
    """为旧版本创建的 SQLite 数据库执行增量字段迁移。

    ``create_all`` 只会创建不存在的表，不会给已有表补列。当前迁移规模较小，
    暂时在这里集中维护迁移逻辑，避免为了几个字段重复引入迁移框架。

    除补列外，还负责把旧的评分体系字段从既有库中移除：
    - candidate.card_score / card_score_reason → 改名 card_prescreen_reason
    - candidate_analysis 的 score / greeting_score 等旧评分列
    """
    if engine.dialect.name != "sqlite":
        return
    # 只允许新增可空列或带默认值的列，确保旧数据库可以无损升级。
    additions = {
        "candidate": {
            "run_id": "VARCHAR(128) NOT NULL DEFAULT ''",
            "work_years": "VARCHAR(128) NOT NULL DEFAULT ''",
            "age": "INTEGER",
            "degree": "VARCHAR(128) NOT NULL DEFAULT ''",
            "city": "VARCHAR(128) NOT NULL DEFAULT ''",
            "expected_salary": "VARCHAR(128) NOT NULL DEFAULT ''",
            "current_title": "VARCHAR(256) NOT NULL DEFAULT ''",
            "card_prescreen_reason": "TEXT NOT NULL DEFAULT ''",
            "card_prescreen_status": "VARCHAR(64) NOT NULL DEFAULT ''",
            "card_prescreen_passed": "BOOLEAN",
            "card_prescreen_detail": "JSON",
        },
        "candidate_analysis": {
            "run_id": "VARCHAR(128) NOT NULL DEFAULT ''",
            "decision": "VARCHAR(64) NOT NULL DEFAULT ''",
            "passed": "BOOLEAN",
        },
        "greeting_record": {
            "run_id": "VARCHAR(128) NOT NULL DEFAULT ''",
        },
        "sync_record": {
            "feishu_table_id": "VARCHAR(128) NOT NULL DEFAULT ''",
            "error": "TEXT NOT NULL DEFAULT ''",
        },
    }
    # 旧评分体系字段改名（保留数据）。
    renames = {
        "candidate": {"card_score_reason": "card_prescreen_reason"},
    }
    # 旧评分体系字段删除（数据不再需要）。
    drops = {
        "candidate": ["card_score"],
        "candidate_analysis": [
            "score",
            "greeting_score",
            "greeting_score_reason",
            "advantages",
            "risks",
            "match_result",
            "ai_evaluation",
            "recommended",
        ],
    }
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    def _table_columns(conn, table_name: str) -> set[str]:
        # 直接 PRAGMA 查询，不用 Inspector.get_columns：后者带 info_cache，
        # 同一 Inspector 在 ALTER TABLE 之后仍返回旧结构，导致 rename/add
        # 阶段误判列存在性（曾因此 duplicate column name 崩溃）。
        rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
        return {str(row[1]) for row in rows}

    with engine.begin() as connection:
        # 1) rename 先于 add：旧列存在且新列不存在时，rename 保留数据并让
        #    additions 阶段自然跳过补列；两列并存（半迁移库）时跳过 rename，
        #    把旧列转入 drops 清理，避免重复 rename 崩溃。
        for table_name, table_renames in renames.items():
            if table_name not in existing_tables:
                continue
            existing_columns = _table_columns(connection, table_name)
            for old_name, new_name in table_renames.items():
                if old_name in existing_columns and new_name not in existing_columns:
                    connection.execute(text(
                        f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_name}" TO "{new_name}"'
                    ))
                elif old_name in existing_columns and new_name in existing_columns:
                    drops.setdefault(table_name, []).append(old_name)
        # 2) 补列（此时查到的列结构已包含 rename 的产物，不会重复 ADD）。
        for table_name, columns in additions.items():
            if table_name not in existing_tables:
                continue
            existing_columns = _table_columns(connection, table_name)
            for column_name, ddl in columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl}'
                    ))
        # 3) 删旧评分列（含 step 1 转入的半迁移残留列）。
        for table_name, drop_columns in drops.items():
            if table_name not in existing_tables:
                continue
            existing_columns = _table_columns(connection, table_name)
            for column_name in drop_columns:
                if column_name in existing_columns:
                    try:
                        connection.execute(text(
                            f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'
                        ))
                    except Exception:
                        # SQLite < 3.35 不支持 DROP COLUMN；旧列残留不影响运行。
                        pass
        # 老数据库通过 ALTER TABLE 补列后不会自动获得 ORM 声明中的索引。
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_greeting_record_run_id "
            "ON greeting_record (run_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_analysis_run_id "
            "ON candidate_analysis (run_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_run_id "
            "ON candidate (run_id)"
        ))


class Database:
    """Small runtime wrapper around Engine and Session factory."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or get_config()
        self.engine = create_engine_from_config(self.config)
        self.session_factory = create_session_factory(self.engine)

    def init(self) -> None:
        init_database(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
