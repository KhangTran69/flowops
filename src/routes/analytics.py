"""Read-only run analytics endpoints + operator page (v1.5)."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import analytics, db, web

router = APIRouter(tags=["analytics"])


@router.get("/analytics/summary")
def analytics_summary(
    days: int = Query(default=analytics.DEFAULT_DAYS, ge=1, le=analytics.MAX_DAYS),
    tenant: Optional[str] = Query(default=None, max_length=64),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    return analytics.summary(session, days=days, tenant_key=tenant)


@router.get("/analytics/steps")
def analytics_steps(
    days: int = Query(default=analytics.DEFAULT_DAYS, ge=1, le=analytics.MAX_DAYS),
    tenant: Optional[str] = Query(default=None, max_length=64),
    session: Session = Depends(db.get_session),
) -> list[dict[str, Any]]:
    return analytics.steps(session, days=days, tenant_key=tenant)


@router.get("/analytics/health")
def analytics_health(
    stuck_minutes: int = Query(
        default=analytics.DEFAULT_STUCK_MINUTES,
        ge=1,
        le=analytics.MAX_STUCK_MINUTES,
    ),
    tenant: Optional[str] = Query(default=None, max_length=64),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    return analytics.health(session, stuck_minutes=stuck_minutes, tenant_key=tenant)


def _bar_width(value: Optional[float], ceiling: float) -> int:
    """Percent width for a CSS bar; 0 when the value is unknown."""
    if not value or ceiling <= 0:
        return 0
    return max(2, min(100, round(100.0 * value / ceiling)))


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@router.get("/ui/analytics", response_class=HTMLResponse, include_in_schema=False)
def ui_analytics(
    request: Request,
    days: int = Query(default=analytics.DEFAULT_DAYS, ge=1, le=analytics.MAX_DAYS),
    stuck_minutes: int = Query(
        default=analytics.DEFAULT_STUCK_MINUTES,
        ge=1,
        le=analytics.MAX_STUCK_MINUTES,
    ),
    tenant: Optional[str] = Query(default=None, max_length=64),
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    data = analytics.summary(session, days=days, tenant_key=tenant)
    health = analytics.health(
        session, stuck_minutes=stuck_minutes, tenant_key=tenant
    )
    step_rows = analytics.steps(session, days=days, tenant_key=tenant)

    status_rows = [
        {"status": status, "count": count}
        for status, count in sorted(
            data["runs"]["by_status"].items(), key=lambda item: -item[1]
        )
    ]
    max_status = max((row["count"] for row in status_rows), default=0)
    for row in status_rows:
        row["bar"] = _bar_width(row["count"], max_status)

    max_step = max(
        (row["median_seconds"] or 0.0 for row in step_rows if row["median_seconds"]),
        default=0.0,
    )
    for row in step_rows:
        row["duration_label"] = _fmt_duration(row["median_seconds"])
        row["p95_label"] = _fmt_duration(row["p95_seconds"])
        row["bar"] = _bar_width(row["median_seconds"], max_step)

    for row in data["by_definition"]:
        row["median_label"] = _fmt_duration(row["median_seconds"])
        row["p95_label"] = _fmt_duration(row["p95_seconds"])

    for row in health["stuck_runs"]:
        heartbeat = row["heartbeat_at"]
        row["heartbeat_label"] = heartbeat.strftime("%Y-%m-%d %H:%M:%S UTC")
        row["age_label"] = _fmt_duration(row["age_seconds"])

    tenants = session.execute(
        select(db.TenantRecord.key).order_by(db.TenantRecord.key)
    ).scalars().all()

    return web.render(
        request,
        "analytics.html",
        {
            "days": days,
            "tenant": tenant,
            "tenants": tenants,
            "summary": data,
            "health": health,
            "stuck_minutes": stuck_minutes,
            "status_rows": status_rows,
            "step_rows": step_rows,
            "median_duration_label": _fmt_duration(
                data["runs"]["median_duration_seconds"]
            ),
            "p95_duration_label": _fmt_duration(data["runs"]["p95_duration_seconds"]),
            "sla_overrun_label": _fmt_duration(
                data["sla"]["median_overrun_seconds"]
            ),
            "oldest_active_label": _fmt_duration(
                data["runs"]["oldest_active_seconds"]
            ),
        },
    )
