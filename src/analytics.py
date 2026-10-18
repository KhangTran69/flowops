"""Run analytics & SLA reporting (v1.5).

Pure aggregation over the workflow ledger the platform already keeps —
onboarding requests, workflow runs, step runs, checklist items, and SLA
timers. No new tables, no writes, no network: every number here can be
recomputed offline from the database, which keeps the module trivially
testable and safe to expose read-only to operators.

Two entry points power ``/analytics/*`` and ``/ui/analytics``:

* :func:`summary` — run outcomes, durations, per-definition and per-tenant
  breakdowns, approval stats, and SLA breach figures for a time window.
* :func:`steps` — per-step medians, p95s, and failure counts, ranked slowest
  first, so operators can see which checklist step is the bottleneck.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from src import db

DEFAULT_DAYS = 30
MAX_DAYS = 365
TOP_STEPS = 8
DEFAULT_STUCK_MINUTES = 30
MAX_STUCK_MINUTES = 7 * 24 * 60
#: A step's duration only counts when both bounds exist; below this sample
#: size medians are noise, so the UI shows them dimmed instead of ranked.
MIN_STEP_SAMPLE = 2


def _at(now: Optional[datetime]) -> datetime:
    """Return a timezone-aware reference time for comparisons.

    SQLite commonly gives us naive values even though the columns are
    declared with timezone support. Keeping the normalization in one place
    prevents operational metrics from failing only in local/demo environments.
    """
    at = now or db.utcnow()
    return at.replace(tzinfo=dt_timezone.utc) if at.tzinfo is None else at


def _cutoff(days: int, now: Optional[datetime]) -> datetime:
    return _at(now) - timedelta(days=days)


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile of seconds; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 1)


def _seconds(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt_timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt_timezone.utc)
    return round(max(0.0, (end - start).total_seconds()), 1)


def _label(value: Optional[str], fallback: str) -> str:
    return value if value else fallback


def _scoped(stmt: Select, tenant_key: Optional[str]) -> Select:
    if tenant_key is not None:
        return stmt.where(db.OnboardingRequestRecord.tenant_key == tenant_key)
    return stmt


def _request_ids(
    session: Session, *, days: int, tenant_key: Optional[str], now: Optional[datetime]
) -> list[int]:
    stmt = select(db.OnboardingRequestRecord.id).where(
        db.OnboardingRequestRecord.created_at >= _cutoff(days, now)
    )
    return list(session.execute(_scoped(stmt, tenant_key)).scalars())


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 1)


def health(
    session: Session,
    *,
    stuck_minutes: int = DEFAULT_STUCK_MINUTES,
    tenant_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return actionable health signals for active workflow runs.

    A run is considered stuck when its scheduler heartbeat (or, for a run
    that has never ticked, its start/creation time) is older than the
    threshold. The query joins requests in one round trip so tenant labels
    and filtering remain safe without per-run lookups.
    """
    at = _at(now)
    threshold = timedelta(minutes=stuck_minutes)
    query = (
        select(db.WorkflowRunRecord, db.OnboardingRequestRecord)
        .join(
            db.OnboardingRequestRecord,
            db.OnboardingRequestRecord.id == db.WorkflowRunRecord.request_id,
        )
        .where(db.WorkflowRunRecord.status == "running")
        .order_by(db.WorkflowRunRecord.id.asc())
    )
    if tenant_key is not None:
        query = query.where(db.OnboardingRequestRecord.tenant_key == tenant_key)

    stuck_runs: list[dict[str, Any]] = []
    for run, request in session.execute(query).all():
        heartbeat = run.last_tick_at or run.started_at or run.created_at
        age = _seconds(heartbeat, at)
        if age is None or age <= threshold.total_seconds():
            continue
        stuck_runs.append(
            {
                "run_id": run.id,
                "request_id": request.id,
                "tenant_key": request.tenant_key,
                "definition_key": request.definition_key or "classic",
                "heartbeat_at": heartbeat,
                "age_seconds": age,
                "reason": "scheduler heartbeat is stale",
            }
        )
    return {
        "threshold_minutes": stuck_minutes,
        "tenant_key": tenant_key,
        "stuck_count": len(stuck_runs),
        "stuck_runs": stuck_runs,
    }


# ---------------------------------------------------------------------------
# Run + SLA summary
# ---------------------------------------------------------------------------


def summary(
    session: Session,
    *,
    days: int = DEFAULT_DAYS,
    tenant_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Aggregate run outcomes, durations, approvals, and SLA figures."""
    request_ids = _request_ids(session, days=days, tenant_key=tenant_key, now=now)
    requests = {
        row.id: row
        for row in session.execute(
            select(db.OnboardingRequestRecord).where(
                db.OnboardingRequestRecord.id.in_(request_ids)
            )
        ).scalars()
    }
    runs = (
        session.execute(
            select(db.WorkflowRunRecord).where(
                db.WorkflowRunRecord.request_id.in_(request_ids)
            )
        ).scalars().all()
        if request_ids
        else []
    )

    runs_by_status: dict[str, int] = {}
    durations: list[float] = []
    active_ages: list[float] = []
    definition_stats: dict[str, dict[str, Any]] = {}
    at = _at(now)
    for run in runs:
        runs_by_status[run.status] = runs_by_status.get(run.status, 0) + 1
        duration = _seconds(run.started_at, run.completed_at)
        if run.status == "completed" and duration is not None:
            durations.append(duration)
        if run.status == "running":
            age = _seconds(run.started_at or run.created_at, at)
            if age is not None:
                active_ages.append(age)
        request = requests.get(run.request_id)
        key = _label(
            request.definition_key if request else None,
            "classic",
        )
        bucket = definition_stats.setdefault(
            key,
            {
                "definition_key": key,
                "request_type": request.request_type if request else "",
                "runs": 0,
                "completed": 0,
                "failed": 0,
                "durations": [],
            },
        )
        bucket["runs"] += 1
        if run.status == "completed":
            bucket["completed"] += 1
            if duration is not None:
                bucket["durations"].append(duration)
        elif run.status == "failed":
            bucket["failed"] += 1

    by_definition = []
    for bucket in sorted(
        definition_stats.values(), key=lambda item: -item["runs"]
    ):
        by_definition.append(
            {
                "definition_key": bucket["definition_key"],
                "request_type": bucket["request_type"],
                "runs": bucket["runs"],
                "completed": bucket["completed"],
                "failed": bucket["failed"],
                "median_seconds": percentile(bucket["durations"], 50),
                "p95_seconds": percentile(bucket["durations"], 95),
            }
        )

    tenant_stats: dict[str, int] = {}
    for request in requests.values():
        tenant = _label(request.tenant_key, "(no tenant)")
        tenant_stats[tenant] = tenant_stats.get(tenant, 0) + 1
    by_tenant = [
        {"tenant_key": key, "requests": count}
        for key, count in sorted(
            tenant_stats.items(), key=lambda item: -item[1]
        )[:5]
    ]

    items = (
        session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id.in_(request_ids),
                db.ChecklistItemRecord.requires_approval.is_(True),
            )
        ).scalars()
        if request_ids
        else []
    )
    approvals: dict[str, int] = {}
    for item in items:
        approvals[item.status] = approvals.get(item.status, 0) + 1

    timers = (
        session.execute(
            select(db.WorkflowTimerRecord).where(
                db.WorkflowTimerRecord.request_id.in_(request_ids)
            )
        ).scalars()
        if request_ids
        else []
    )
    sla_by_status: dict[str, int] = {}
    overruns: list[float] = []
    overdue_pending = 0
    for timer in timers:
        sla_by_status[timer.status] = sla_by_status.get(timer.status, 0) + 1
        overrun = _seconds(timer.due_at, timer.fired_at)
        if timer.fired_at is not None and overrun is not None and overrun > 0:
            overruns.append(overrun)
        if timer.status == "pending" and (_seconds(timer.due_at, at) or 0) > 0:
            overdue_pending += 1
    total_timers = sum(sla_by_status.values())

    total_runs = len(runs)
    completed = runs_by_status.get("completed", 0)
    return {
        "window": {"days": days, "tenant_key": tenant_key},
        "runs": {
            "total": total_runs,
            "by_status": runs_by_status,
            "completion_rate": round(completed / total_runs, 3) if total_runs else None,
            "failure_rate": round(
                runs_by_status.get("failed", 0) / total_runs, 3
            )
            if total_runs
            else None,
            "active": runs_by_status.get("running", 0),
            "oldest_active_seconds": max(active_ages, default=None),
            "median_duration_seconds": percentile(durations, 50),
            "p95_duration_seconds": percentile(durations, 95),
        },
        "by_definition": by_definition,
        "by_tenant": by_tenant,
        "approvals": {
            "total": sum(approvals.values()),
            "by_status": approvals,
        },
        "sla": {
            "total": total_timers,
            "by_status": sla_by_status,
            "breached": len(overruns),
            "overdue_pending": overdue_pending,
            "breach_rate": round(len(overruns) / total_timers, 3)
            if total_timers
            else None,
            "median_overrun_seconds": percentile(overruns, 50),
        },
    }


# ---------------------------------------------------------------------------
# Step leaderboard
# ---------------------------------------------------------------------------


def steps(
    session: Session,
    *,
    days: int = DEFAULT_DAYS,
    tenant_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Per-step duration and failure stats, slowest median first."""
    request_ids = _request_ids(session, days=days, tenant_key=tenant_key, now=now)
    if not request_ids:
        return []
    step_runs = session.execute(
        select(db.WorkflowStepRunRecord).where(
            db.WorkflowStepRunRecord.status.in_(("succeeded", "failed")),
            db.WorkflowStepRunRecord.checklist_item_id.in_(
                select(db.ChecklistItemRecord.id).where(
                    db.ChecklistItemRecord.request_id.in_(request_ids)
                )
            ),
        )
    ).scalars()
    item_names = {
        row.id: row.step
        for row in session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id.in_(request_ids)
            )
        ).scalars()
    }

    buckets: dict[str, dict[str, Any]] = {}
    for step_run in step_runs:
        name = item_names.get(step_run.checklist_item_id, f"item #{step_run.checklist_item_id}")
        bucket = buckets.setdefault(
            name, {"step": name, "durations": [], "failures": 0, "succeeded": 0}
        )
        if step_run.status == "failed":
            bucket["failures"] += 1
            continue
        bucket["succeeded"] += 1
        duration = _seconds(step_run.started_at, step_run.completed_at)
        if duration is not None:
            bucket["durations"].append(duration)

    rows = []
    for bucket in buckets.values():
        sample = len(bucket["durations"])
        rows.append(
            {
                "step": bucket["step"],
                "succeeded": bucket["succeeded"],
                "failed": bucket["failures"],
                "attempts": bucket["succeeded"] + bucket["failures"],
                "failure_rate": round(
                    bucket["failures"] / (bucket["succeeded"] + bucket["failures"]), 3
                )
                if (bucket["succeeded"] + bucket["failures"])
                else None,
                "median_seconds": percentile(bucket["durations"], 50),
                "p95_seconds": percentile(bucket["durations"], 95),
                "sample": sample,
                "ranked": sample >= MIN_STEP_SAMPLE,
            }
        )
    rows.sort(
        key=lambda row: (
            not row["ranked"],
            -(row["median_seconds"] or 0.0),
        )
    )
    return rows[:TOP_STEPS]
