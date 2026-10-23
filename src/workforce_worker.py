"""Deliver the workforce transactional outbox to n8n or the local simulator."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import timedelta
from typing import Optional

import requests
from sqlalchemy import or_, select, update

from src import db

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


def _safe_error(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(authorization|password|secret|token|api[_-]?key)(\s*[=:]\s*|\s+)[^\s,;]+",
                   r"\1\2[REDACTED]", value)
    return value[:1000]


def _claim(session):
    now = db.utcnow()
    candidate = session.execute(select(db.WorkforceAutomationEventRecord).where(
        db.WorkforceAutomationEventRecord.delivery_status == "PENDING",
        db.WorkforceAutomationEventRecord.next_attempt_at <= now,
        or_(db.WorkforceAutomationEventRecord.lease_until.is_(None),
            db.WorkforceAutomationEventRecord.lease_until < now),
    ).order_by(db.WorkforceAutomationEventRecord.created_at).limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
    if candidate is None:
        return None
    lease_token = uuid.uuid4().hex
    result = session.execute(update(db.WorkforceAutomationEventRecord).where(
        db.WorkforceAutomationEventRecord.id == candidate.id,
        db.WorkforceAutomationEventRecord.delivery_status == "PENDING",
        or_(db.WorkforceAutomationEventRecord.lease_until.is_(None),
            db.WorkforceAutomationEventRecord.lease_until < now),
    ).values(lease_until=now + timedelta(seconds=LEASE_SECONDS), lease_token=lease_token)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return candidate.id, candidate.event_id, candidate.payload_json, lease_token, candidate.attempts + 1


def run_once(*, timeout: float = 10.0) -> Optional[dict]:
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        claimed = _claim(session)
    finally:
        session.close()
    if claimed is None:
        return None
    event_row_id, event_id, payload_json, lease_token, attempt_number = claimed
    mode = os.environ.get("FLOWOPS_AUTOMATION_ADAPTER", "simulated").lower()
    # Cancellation committed after enqueue but before dispatch must suppress
    # queued work. The callback also rejects a cancellation that races with
    # an already-started external execution.
    session = db.SessionLocal()
    try:
        event = session.get(db.WorkforceAutomationEventRecord, event_row_id)
        request = session.get(db.StaffOnboardingRecord, event.request_id) if event else None
        if event is None:
            return {"event_id": event_id, "status": "lease_lost"}
        if event.delivery_status == "CANCELLED":
            return {"event_id": event_id, "status": "CANCELLED"}
        if event.lease_token != lease_token:
            return {"event_id": event_id, "status": "lease_lost"}
        if request is None or request.tenant_key != event.tenant_key:
            event.delivery_status = "FAILED"
            event.lease_until = None
            event.lease_token = None
            event.last_error = "Onboarding request is missing or tenant ownership does not match."
            session.commit()
            return {"event_id": event_id, "status": "FAILED"}
        if request.status in {"CANCELLED", "REJECTED", "COMPLETED"}:
            event.delivery_status = "CANCELLED"
            event.workflow_status = "CANCELLED"
            event.lease_until = None
            event.lease_token = None
            session.commit()
            return {"event_id": event_id, "status": "CANCELLED"}
        if event.tenant_key:
            tenant = session.get(db.TenantRecord, event.tenant_key)
            if tenant is None or not tenant.active:
                event.delivery_status = "FAILED"
                event.lease_until = None
                event.lease_token = None
                event.last_error = "Tenant is missing or inactive; automation was not sent."
                session.commit()
                return {"event_id": event_id, "status": "FAILED"}
        elif mode == "n8n":
            event.delivery_status = "FAILED"
            event.lease_until = None
            event.lease_token = None
            event.last_error = "Tenant ownership is unknown; map this legacy record before n8n delivery."
            session.commit()
            return {"event_id": event_id, "status": "FAILED"}
    finally:
        session.close()
    success, error = False, None
    if mode == "simulated":
        success = True
    elif mode == "n8n":
        url = os.environ.get("FLOWOPS_N8N_WEBHOOK_URL", "")
        secret = os.environ.get("FLOWOPS_N8N_WEBHOOK_SECRET", "")
        if not url or not secret:
            error = "n8n webhook URL and secret are required."
        else:
            signature = hmac.new(secret.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
            try:
                response = requests.post(url, data=payload_json.encode(), headers={
                    "Content-Type": "application/json", "X-FlowOps-Event-ID": event_id,
                    "X-FlowOps-Signature": "sha256=" + signature,
                    "X-FlowOps-Automation-Key": os.environ.get("FLOWOPS_N8N_API_KEY", ""),
                }, timeout=timeout)
                response.raise_for_status()
                success = True  # Workflow outcome still requires an authenticated callback.
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: n8n webhook delivery failed."
    else:
        error = "Unknown FLOWOPS_AUTOMATION_ADAPTER; expected simulated or n8n."

    session = db.SessionLocal()
    try:
        now = db.utcnow()
        next_status = "DELIVERED" if success else ("FAILED" if attempt_number >= MAX_ATTEMPTS else "PENDING")
        values = {
            "attempts": attempt_number,
            "lease_until": None,
            "lease_token": None,
            "delivery_status": next_status,
        }
        if success:
            values["delivered_at"] = now
        else:
            values["last_error"] = _safe_error(error)
            if next_status == "PENDING":
                values["next_attempt_at"] = now + timedelta(seconds=min(300, 2 ** attempt_number))
        updated = session.execute(update(db.WorkforceAutomationEventRecord).where(
            db.WorkforceAutomationEventRecord.id == event_row_id,
            db.WorkforceAutomationEventRecord.delivery_status == "PENDING",
            db.WorkforceAutomationEventRecord.lease_token == lease_token,
            db.WorkforceAutomationEventRecord.lease_until > now,
        ).values(**values))
        if updated.rowcount != 1:
            session.rollback()
            return {"event_id": event_id, "status": "lease_lost"}
        if success and mode == "simulated":
            from src.routes.workforce import _complete_provisioning
            event = session.get(db.WorkforceAutomationEventRecord, event_row_id)
            try:
                _complete_provisioning(session, event, "mock-automation", "success")
            except Exception as exc:
                # Cancellation/termination may commit after dispatch starts. Keep
                # the lease-fenced delivery update and mark the workflow safely;
                # do not let an expected terminal-state race kill the worker.
                session.rollback()
                event = session.get(db.WorkforceAutomationEventRecord, event_row_id)
                request = session.get(db.StaffOnboardingRecord, event.request_id) if event else None
                if request and request.status in {"CANCELLED", "REJECTED", "COMPLETED"}:
                    event.delivery_status = "CANCELLED"
                    event.workflow_status = "CANCELLED"
                    event.lease_until = None
                    event.lease_token = None
                else:
                    raise exc
        session.commit()
        event = session.get(db.WorkforceAutomationEventRecord, event_row_id)
        return {"event_id": event_id, "status": event.delivery_status, "workflow_status": event.workflow_status,
                "attempts": event.attempts, "error": error}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    interval = max(0.1, float(os.environ.get("FLOWOPS_WORKFORCE_POLL_INTERVAL", "2")))
    while True:
        result = run_once()
        if result is None:
            time.sleep(interval)
        else:
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
