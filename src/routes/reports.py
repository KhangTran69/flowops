"""Read-only KPI endpoints backed by the SQL views in src/reports.py."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src import db, reports

router = APIRouter(prefix="/reports", tags=["reports"])

_REPORTS = {
    "automation-daily": lambda conn, days: reports.automation_daily(conn, days=days),
    "action-reliability": lambda conn, days: reports.action_reliability(conn),
    "ticket-backlog": lambda conn, days: reports.ticket_backlog(conn),
    "time-saved": lambda conn, days: reports.time_saved(conn),
    "tenant-kpis": lambda conn, days: reports.tenant_kpis(conn, days=days),
}


@router.get("/summary")
def report_summary(
    days: int = Query(default=30, ge=1, le=365), session: Session = Depends(db.get_session)
) -> dict[str, Any]:
    return reports.summary(session.connection(), days=days)


@router.get("/{name}")
def report(
    name: str, days: int = Query(default=30, ge=1, le=365), session: Session = Depends(db.get_session)
) -> list[dict[str, Any]]:
    if name not in _REPORTS:
        raise HTTPException(404, f"Unknown report. Available: {', '.join(sorted(_REPORTS))}")
    return _REPORTS[name](session.connection(), days)
