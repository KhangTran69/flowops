"""Database setup and models for FlowOps (v0.3).

SQLAlchemy 2.0 with a SQLite default for local development and tests.
The same code runs on PostgreSQL by setting FLOWOPS_DATABASE_URL to a
postgresql+psycopg:// connection string.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExecutionLogRecord(Base):
    """One workflow execution step: what ran, whether it worked, how long."""

    __tablename__ = "execution_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success|failure
    ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class IdempotencyRecord(Base):
    """Stores the response produced for an Idempotency-Key (v0.5)."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class NotificationOutboxRecord(Base):
    """A failed outbound webhook delivery kept for a later retry (v1.0 preview).

    When ``deliver_webhook`` exhausts its in-request retries, the payload is
    stored here so operators can see it and retry manually via
    ``POST /notifications/retry`` instead of losing the event silently.
    """

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
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
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


_tables_ready = False
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


def get_session():
    """FastAPI dependency yielding a session; commits on success."""
    global _tables_ready
    if not _tables_ready:
        _ensure_sqlite_dir()
        Base.metadata.create_all(engine)
        _tables_ready = True
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
        )
    )
