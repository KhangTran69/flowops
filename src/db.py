"""Database setup and models for FlowOps (v0.3).

SQLAlchemy 2.0 with a SQLite default for local development and tests.
The same code runs on PostgreSQL by setting FLOWOPS_DATABASE_URL to a
postgresql+psycopg:// connection string.
"""

import os
import threading
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

DEFAULT_DATABASE_URL = os.environ.get(
    "FLOWOPS_DATABASE_URL", "sqlite:///data/flowops.db"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TicketRecord(Base):
    """A normalized ticket as stored by the intake API."""

    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requester_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    queue: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Ticket closing (v1.0 preview): both columns are nullable, so rows
    # written before this change stay valid (NULL = never closed).
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExecutionLogRecord(Base):
    """One workflow execution step: what ran, whether it worked, how long."""

    __tablename__ = "execution_logs"
    __table_args__ = (
        Index("ix_execution_logs_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success|failure
    ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_subject: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class IdempotencyRecord(Base):
    """Reservation and stored response for an Idempotency-Key."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    owner_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionExecutionRecord(Base):
    """Durable audit record for one onboarding action attempt."""

    __tablename__ = "action_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    checklist_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_subject: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_reference: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class WorkflowJobRecord(Base):
    """A durable, leased unit of workflow work.

    ``lease_owner_token`` and ``lease_expires_at`` fence completion: a worker
    that lost its lease cannot mark the job succeeded or schedule a retry.
    """

    __tablename__ = "workflow_jobs"
    __table_args__ = (
        Index("ix_workflow_jobs_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued"
    )  # queued|running|succeeded|retry_scheduled|failed|cancelled
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    lease_owner_token: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NotificationOutboxRecord(Base):
    """A failed outbound webhook delivery kept for a later retry (v1.0 preview).

    When ``deliver_webhook`` exhausts its in-request retries, the payload is
    stored here so operators can see it and retry manually via
    ``POST /notifications/retry`` instead of losing the event silently.
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("ix_notification_outbox_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lease_owner_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OnboardingRequestRecord(Base):
    """An onboarding or offboarding request (v0.5)."""

    __tablename__ = "onboarding_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_type: Mapped[str] = mapped_column(String(16), nullable=False)
    employee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    employee_email: Mapped[str] = mapped_column(String(320), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    requester_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ChecklistItemRecord(Base):
    """One step of an onboarding/offboarding checklist (v0.5)."""

    __tablename__ = "checklist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    step: Mapped[str] = mapped_column(String(200), nullable=False)
    # v0.6.1: stable identifier for the step (e.g. "disable_user_account").
    # Nullable so rows written before this change stay valid; the API fills
    # it in for every new checklist, and _add_missing_columns() migrates
    # pre-existing SQLite files additively (no data is touched).
    step_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # v0.6 placeholder: which automation runs when this step completes
    # ("none" for manual steps, "m365_provision" for the simulated one).
    # Both new columns are nullable so older rows stay valid; the API fills
    # them in, and _add_missing_columns() migrates pre-existing SQLite files.
    action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON from the action
    # v1.1 DAG: prerequisite step keys and structured action output.
    # Both nullable so pre-v1.1 rows stay valid and SQLite can add them.
    depends_on_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OffboardingCompletionRecord(Base):
    """Idempotent completion ledger for offboarding steps (v0.6.1).

    One row per ``(checklist_id, step_key)`` records that an offboarding
    step was executed and stores its result. The unique constraint is the
    hard guarantee: even if two requests race past the pre-check, only one
    row can exist, so account-disable side effects never run twice.

    Onboarding uses a different mechanism (the checklist item's own status
    plus ``result`` column) — see the note in src/api.py near
    ``complete_offboarding_step`` for why they are not unified yet.
    """

    __tablename__ = "offboarding_completions"
    __table_args__ = (
        UniqueConstraint("checklist_id", "step_key", name="uq_offboarding_completion_step"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checklist_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL means a legacy completed row (the table predates status tracking).
    status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    owner_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    result_json: Mapped[str] = mapped_column(Text, nullable=False)  # stored response body


_tables_ready = False
_tables_lock = threading.Lock()
engine = create_engine(
    DEFAULT_DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DEFAULT_DATABASE_URL.startswith("sqlite")
    else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def use_database(url: str) -> None:
    """Point the module at a different database (used by tests)."""
    global engine, SessionLocal, _tables_ready
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    _tables_ready = False


def _ensure_sqlite_dir() -> None:
    url = str(engine.url)
    if not url.startswith("sqlite:///") or url.endswith(":memory"):
        return
    parent = os.path.dirname(url[len("sqlite:///"):])
    if parent:
        os.makedirs(parent, exist_ok=True)


def _add_missing_columns() -> None:
    """Add newly introduced nullable columns to pre-existing SQLite tables.

    ``Base.metadata.create_all`` only creates *missing tables*; it never
    changes an existing table. So an older ``flowops.db`` would keep its
    old ``tickets`` shape and break new code that reads, for example,
    ``resolution_note``. This tiny migration adds any missing nullable
    column with ``ALTER TABLE ... ADD COLUMN`` (no data is touched).
    New databases created by ``create_all`` already have every column,
    so this is a no-op for them.
    """
    if engine.dialect.name != "sqlite":
        return  # PostgreSQL deployments should use Alembic-style migrations.
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable:
                    continue  # Only safe to auto-add optional columns.
                spec = column.type.compile(dialect=engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {spec}')
                )


def _ensure_idempotency_owner_token() -> None:
    """Add the fencing column to databases created before it existed.

    ``create_all`` only creates missing tables, so an existing local
    ``idempotency_keys`` table needs a small, additive migration. The column is
    nullable so pre-fencing rows remain readable and can be taken over.
    """
    inspector = inspect(engine)
    if "idempotency_keys" not in inspector.get_table_names():
        return
    column_names = {
        column["name"] for column in inspector.get_columns("idempotency_keys")
    }
    if "owner_token" in column_names:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE idempotency_keys ADD COLUMN owner_token VARCHAR(64)")
        )


def _ensure_idempotency_resource_type() -> None:
    """Add the ticket/onboarding scope to databases created before it existed."""
    inspector = inspect(engine)
    if "idempotency_keys" not in inspector.get_table_names():
        return
    column_names = {
        column["name"] for column in inspector.get_columns("idempotency_keys")
    }
    if "resource_type" in column_names:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE idempotency_keys ADD COLUMN resource_type VARCHAR(32)")
        )


def _ensure_execution_log_actor() -> None:
    """Add actor attribution to execution logs created before it existed."""
    inspector = inspect(engine)
    if "execution_logs" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("execution_logs")
    }
    if "actor_subject" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE execution_logs ADD COLUMN actor_subject VARCHAR(256)")
        )


def _ensure_outbox_lease_columns() -> None:
    """Add dispatcher lease columns to databases created before they existed."""
    inspector = inspect(engine)
    if "notification_outbox" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("notification_outbox")
    }
    additions = {
        "lease_owner_token": "VARCHAR(64)",
        "lease_expires_at": "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME",
        "next_attempt_at": "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME",
    }
    with engine.begin() as connection:
        for name, ddl_type in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE notification_outbox ADD COLUMN {name} {ddl_type}")
                )


def _ensure_tables() -> None:
    """Create missing tables once and apply additive compatibility migrations."""
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        _ensure_sqlite_dir()
        Base.metadata.create_all(engine)
        _add_missing_columns()
        _ensure_idempotency_owner_token()
        _ensure_idempotency_resource_type()
        _ensure_execution_log_actor()
        _ensure_outbox_lease_columns()
        _tables_ready = True


def get_session():
    """FastAPI dependency yielding a session; commits on success."""
    _ensure_tables()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_execution(
    session: Session,
    *,
    run_id: str,
    action: str,
    status: str,
    ticket_id: Optional[str] = None,
    duration_ms: int = 0,
    detail: Optional[str] = None,
    actor_subject: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> None:
    """Append an execution-log row for the current run."""
    session.add(
        ExecutionLogRecord(
            run_id=run_id,
            action=action,
            status=status,
            ticket_id=ticket_id,
            duration_ms=duration_ms,
            detail=detail,
            actor_subject=actor_subject,
            correlation_id=correlation_id,
        )
    )


class ChatOpsTokenUseRecord(Base):
    """Single-use record for signed ChatOps approval tokens (v1.1)."""

    __tablename__ = "chatops_token_uses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(120), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
