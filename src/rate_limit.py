"""Atomic PostgreSQL fixed-window rate limits shared by API processes.

A separate short transaction commits quota before the protected operation.
Database time, not client/worker clocks, defines the minute boundary. Failure
to obtain quota is fail-closed. This is authenticated request admission,
not a substitute for edge DDoS/unauthenticated login protection.
"""
import hashlib
import json
import logging
import os
import threading
from collections import Counter

from sqlalchemy import case, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from src import db

_LOCK = threading.Lock()
_COUNTS: Counter[str] = Counter()


class Unavailable(RuntimeError):
    pass


def configured_limit() -> int:
    production = os.environ.get("FLOWOPS_ENVIRONMENT", "development").strip().lower() != "development"
    limit = int(os.environ.get("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0"))
    if not 0 <= limit <= 100000 or (production and limit == 0):
        raise ValueError("A positive bounded rate limit is required outside development.")
    return limit


def validate_configuration() -> None:
    configured_limit()
    if (os.environ.get("FLOWOPS_ENVIRONMENT", "development").strip().lower() != "development"
            and db.engine.dialect.name != "postgresql"):
        raise ValueError("Protected deployment requires PostgreSQL shared rate limiting.")


def actor_key(subject: str, tenant_key: str | None) -> str:
    # JSON framing avoids separator collisions and separates global principals.
    return "actor:" + json.dumps([tenant_key, subject], ensure_ascii=False, separators=(",", ":"))


def _record(outcome: str) -> None:
    with _LOCK:
        _COUNTS[outcome] += 1


def metric_lines() -> list[str]:
    with _LOCK:
        return [
            "# HELP flowops_rate_limit_decisions_total Shared admission outcomes per process",
            "# TYPE flowops_rate_limit_decisions_total counter",
            *[f'flowops_rate_limit_decisions_total{{outcome="{key}"}} {_COUNTS[key]}'
              for key in ("allowed", "denied", "unavailable")],
        ]


def consume(key: str, limit: int) -> tuple[bool, int]:
    """Return (allowed, retry seconds); all callers share one atomic row."""
    if not 1 <= limit <= 100000:
        raise ValueError("Invalid admission limit.")
    digest = hashlib.sha256(key.encode()).hexdigest()
    table = db.RateLimitBucketRecord.__table__
    try:
        with db.engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            connection.execute(text("SET LOCAL statement_timeout = '3s'"))
            seconds = int(connection.execute(
                select(func.floor(func.extract("epoch", func.clock_timestamp())))
            ).scalar_one())
            window = seconds // 60
            stmt = insert(table).values(digest=digest, window_id=window, count=1)
            stmt = stmt.on_conflict_do_update(
                index_elements=[table.c.digest],
                set_={"window_id": window,
                      "count": case((table.c.window_id < window, 1), else_=table.c.count + 1)},
                where=or_(table.c.window_id < window,
                          (table.c.window_id == window) & (table.c.count < limit)),
            ).returning(table.c.count)
            allowed = connection.execute(stmt).scalar_one_or_none() is not None
    except SQLAlchemyError as error:
        _record("unavailable")
        logging.getLogger(__name__).warning("Shared rate limiter unavailable: %s", type(error).__name__)
        raise Unavailable("Shared admission unavailable.") from error
    _record("allowed" if allowed else "denied")
    return allowed, max(1, 60 - seconds % 60)


def sweep_expired() -> int:
    """Delete obsolete counters; safe against a concurrently renewed row."""
    table = db.RateLimitBucketRecord.__table__
    with db.engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            return 0
        connection.execute(text("SET LOCAL lock_timeout = '2s'"))
        connection.execute(text("SET LOCAL statement_timeout = '3s'"))
        window = int(connection.execute(
            select(func.floor(func.extract("epoch", func.clock_timestamp()) / 60))
        ).scalar_one())
        rows = select(table.c.digest).where(table.c.window_id < window - 2).limit(1000).with_for_update(skip_locked=True)
        result = connection.execute(delete(table).where(
            table.c.digest.in_(rows), table.c.window_id < window - 2))
        return int(result.rowcount or 0)
