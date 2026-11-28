"""Seed a plausible demo dataset for the FlowOps portfolio demo.

Populates the demo tenant with employees, ~30 tickets across queues, two
onboarding runs (one mid-flight, one paused awaiting approval), one failed
job, and one dead-letter notification so the dashboards have signal. Every
row is written through the normal SQLAlchemy models and session utilities
(the same pattern as src/workforce_demo.py) — no schema bypass, no HTTP
calls, works on SQLite and PostgreSQL.

Usage:
    python -m scripts.seed_demo          # seed when empty, no-op otherwise
    python -m scripts.seed_demo --reset  # clear all data rows and reseed

``--reset`` deletes rows from every application table (leaving the schema and
Alembic's ``alembic_version`` untouched) and reseeds, so it is safe to run
against a migrated PostgreSQL demo stack. All adapters stay simulated.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import timedelta
from pathlib import Path

# Allow "python scripts/seed_demo.py" as well as "python -m scripts.seed_demo".
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src import db  # noqa: E402
from src import jobs as jobs_mod  # noqa: E402
from src import onboarding as workflow  # noqa: E402
from src.definitions import checksum_for  # noqa: E402
from src.routing import route_ticket  # noqa: E402

TENANT = "demo"
ACTOR = "demo-seed"

#: (title, requester_id, closed) — routing is derived with the real
#: ``route_ticket`` rules so categories, priorities, and queues match what
#: live intake would produce. Covers every queue and priority band.
TICKET_SPECS: tuple[tuple[str, int, bool], ...] = (
    ("VPN outage - all users cannot work", 101, False),
    ("Office wi-fi drops every few minutes", 102, False),
    ("Cannot reach the internet after network upgrade", 103, True),
    ("Laptop will not start", 104, False),
    ("Second monitor not detected on docking station", 105, False),
    ("Printer on 3rd floor jams constantly", 106, True),
    ("Keyboard and mouse for new desk", 107, True),
    ("Headset microphone not working in Teams", 108, False),
    ("Install Adobe Acrobat for contracts team", 109, True),
    ("Excel crashes when opening large workbook", 110, False),
    ("Request license for statistical software", 111, False),
    ("Teams recording missing after meeting", 112, True),
    ("Software update stuck at 50%", 113, False),
    ("Password reset - locked out after vacation", 114, False),
    ("Cannot sign in to payroll portal", 115, False),
    ("MFA device replacement for new phone", 116, True),
    ("Phishing email reported - security review", 117, False),
    ("Urgent: security breach alert on file server", 118, False),
    ("Access request: shared drive for Finance folder", 119, False),
    ("Permission error opening SharePoint site", 120, True),
    ("New mailbox setup for support alias", 121, True),
    ("Outlook mailbox full - cannot send email", 122, False),
    ("Distribution list membership update", 123, True),
    ("Email delivery delayed for external recipients", 124, False),
    ("Add user to engineering group", 125, True),
    ("Screen flickering on external display", 126, False),
    ("Docking station ethernet port unreliable", 127, False),
    ("General question about expense tool", 128, True),
    ("Onboarding question: what to expect first week", 129, True),
    ("VPN connectivity intermittent from home office", 130, False),
)

EMPLOYEE_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    # (employee_number, full_name, department, job_title, email local part)
    ("DEMO-1001", "Avery Santos", "Finance", "Operations Analyst", "avery.santos"),
    ("DEMO-1002", "Priya Raman", "Engineering", "Platform Engineer", "priya.raman"),
    ("DEMO-1003", "Marcus Webb", "Sales", "Account Executive", "marcus.webb"),
    ("DEMO-1004", "Lena Fischer", "People Ops", "HR Business Partner", "lena.fischer"),
    ("DEMO-1005", "Tom Okafor", "Support", "Service Desk Lead", "tom.okafor"),
    ("DEMO-1006", "Ines Duarte", "Marketing", "Content Manager", "ines.duarte"),
)


def _delete_all_rows(session: Session) -> int:
    """Delete every row from every application table, parents after children."""
    deleted = 0
    for table in reversed(db.Base.metadata.sorted_tables):
        deleted += session.execute(table.delete()).rowcount or 0
    return deleted


def _seed_tenant(session: Session) -> None:
    session.add(db.TenantRecord(
        key=TENANT, name="FlowOps Demo Tenant", default_domain="example.test",
        usage_location="US", active=True,
    ))


def _seed_employees(session: Session) -> list[db.EmployeeRecord]:
    employees: list[db.EmployeeRecord] = []
    for index, (number, name, department, title, local) in enumerate(EMPLOYEE_SPECS):
        employee = db.EmployeeRecord(
            employee_number=number, full_name=name,
            work_email=f"{local}@example.test", department=department,
            job_title=title,
            start_date=(db.utcnow() + timedelta(days=7 + index * 3)).date().isoformat(),
            manager_subject="manager-demo", tenant_key=TENANT, status="ACTIVE",
        )
        session.add(employee)
        employees.append(employee)
    session.flush()
    # One staff onboarding request with a pending IT setup task, so the
    # workforce pages have a lifecycle to show.
    session.add(db.StaffOnboardingRecord(
        employee_id=employees[0].id, tenant_key=TENANT, status="PENDING_APPROVAL",
        created_by=ACTOR,
    ))
    session.flush()
    session.add(db.ITTaskRecord(
        request_id=session.query(db.StaffOnboardingRecord).filter_by(
            employee_id=employees[0].id).one().id,
        tenant_key=TENANT, task_key="provision_laptop",
        title="Provision laptop for new hire",
        description="Prepare and ship the standard demo laptop image.",
        assignee_subject="it-admin-demo", status="IN_PROGRESS",
    ))
    for tag_index, employee in enumerate(employees[:3]):
        session.add(db.ITAssetRecord(
            asset_tag=f"DEMO-LAPTOP-{tag_index + 1:03d}",
            serial_number=f"DEMO-SN-{tag_index + 1:04d}",
            category="Laptop", brand_model="Example Business 14",
            condition="GOOD", status="ASSIGNED", employee_id=employee.id,
            tenant_key=TENANT,
        ))
    return employees


def _seed_tickets(session: Session) -> int:
    created = 0
    for index, (title, requester_id, closed) in enumerate(TICKET_SPECS, start=1):
        decision = route_ticket(title)
        now = db.utcnow() - timedelta(hours=index * 3)
        ticket = db.TicketRecord(
            ticket_id=f"DEMO-{index:04d}", requester_id=requester_id, title=title,
            status="closed" if closed else "open", priority=decision.priority,
            category=decision.category, queue=decision.queue, tenant_key=TENANT,
            requester_email=f"requester{requester_id}@example.test",
            description=f"Demo ticket: {title}.",
            resolution_note="Resolved by service desk (demo data)." if closed else None,
            closed_at=now + timedelta(hours=2) if closed else None,
            created_at=now, updated_at=now,
        )
        session.add(ticket)
        session.add(db.ExecutionLogRecord(
            run_id=f"demo-{index:04d}", action="classify_route", status="success",
            ticket_id=ticket.ticket_id, duration_ms=40 + index,
            detail=f"{decision.category}/{decision.priority} -> {decision.queue}",
            actor_subject="intake", correlation_id=f"demo-corr-{index:04d}",
            created_at=now,
        ))
        # A minority of failures so failure-rate panels have signal.
        if index % 9 == 0:
            session.add(db.ExecutionLogRecord(
                run_id=f"demo-{index:04d}", action="notify_webhook", status="failure",
                ticket_id=ticket.ticket_id, duration_ms=1500,
                detail="Simulated webhook timeout (demo data)",
                actor_subject="outbox", correlation_id=f"demo-corr-{index:04d}",
                created_at=now + timedelta(seconds=30),
            ))
        created += 1
    return created


def _definition_steps(name: str) -> tuple[str, list[dict]]:
    request_type, steps = workflow.TEMPLATES[name]
    payload = [dict(step) for step in steps]
    return request_type, payload


def _seed_definitions(session: Session) -> int:
    created = 0
    for name, display in (("onboarding-dag", "Onboarding (DAG)"),
                          ("offboarding-dag", "Offboarding (DAG)")):
        request_type, steps = _definition_steps(name)
        session.add(db.WorkflowDefinitionRecord(
            key=name, name=display, request_type=request_type, status="published",
            current_version=1,
        ))
        session.add(db.WorkflowDefinitionVersionRecord(
            definition_key=name, version=1,
            steps_json=json.dumps(steps, separators=(",", ":")),
            checksum=checksum_for(steps), created_by=ACTOR,
            published_at=db.utcnow(),
        ))
        created += 1
    return created


def _checklist(session: Session, request: db.OnboardingRequestRecord,
               states: dict[str, str]) -> dict[str, db.ChecklistItemRecord]:
    items: dict[str, db.ChecklistItemRecord] = {}
    for spec in workflow.ONBOARDING_DAG_STEPS:
        item = db.ChecklistItemRecord(
            request_id=request.id, step=spec["step"], step_key=spec["key"],
            queue=spec["queue"], requires_approval=spec["requires_approval"],
            status=states.get(spec["key"], "pending"), action=spec["action"],
            depends_on_json=json.dumps(spec.get("depends_on", [])),
            join_mode="all",
        )
        session.add(item)
        items[spec["key"]] = item
    session.flush()
    return items


def _seed_onboarding_runs(session: Session) -> dict[str, int]:
    counts = {"requests": 0, "items": 0, "runs": 0, "step_runs": 0, "jobs": 0, "events": 0}

    def request_for(name: str, email_local: str, department: str) -> db.OnboardingRequestRecord:
        record = db.OnboardingRequestRecord(
            request_type="onboarding", employee_name=name,
            employee_email=f"{email_local}@example.test", department=department,
            requester_id=201, start_date=(db.utcnow() + timedelta(days=7)).date().isoformat(),
            status="in_progress", definition_key="onboarding-dag", definition_version=1,
            tenant_key=TENANT,
        )
        session.add(record)
        session.flush()
        counts["requests"] += 1
        return record

    # Run A: mid-flight. Two steps done, one approved and queued, one pending.
    run_a_request = request_for("Priya Raman", "priya.raman", "Engineering")
    items_a = _checklist(session, run_a_request, {
        "create_user_account": "completed", "provision_laptop": "completed",
        "grant_licenses": "approved", "configure_mailbox": "pending",
    })
    for key, result in (("create_user_account", '{"account": "priya.raman@example.test", "status": "active"}'),
                        ("provision_laptop", '{"asset_tag": "DEMO-LAPTOP-002", "shipped": true}')):
        items_a[key].result = result
    counts["items"] += len(items_a)
    run_a = db.WorkflowRunRecord(request_id=run_a_request.id, status="running",
                                 started_at=db.utcnow() - timedelta(hours=2),
                                 last_tick_at=db.utcnow() - timedelta(minutes=1))
    session.add(run_a)
    session.flush()
    counts["runs"] += 1
    for key in ("create_user_account", "provision_laptop"):
        session.add(db.WorkflowStepRunRecord(
            run_id=run_a.id, checklist_item_id=items_a[key].id, attempt=1,
            status="succeeded", started_at=db.utcnow() - timedelta(hours=2),
            completed_at=db.utcnow() - timedelta(hours=2) + timedelta(minutes=4),
        ))
        counts["step_runs"] += 1
    license_spec = next(s for s in workflow.ONBOARDING_DAG_STEPS if s["key"] == "grant_licenses")
    license_step_run = db.WorkflowStepRunRecord(
        run_id=run_a.id, checklist_item_id=items_a["grant_licenses"].id,
        attempt=1, status="scheduled", scheduled_at=db.utcnow() - timedelta(minutes=1),
    )
    session.add(license_step_run)
    session.flush()
    counts["step_runs"] += 1
    job = jobs_mod.enqueue_job(
        session, job_type=jobs_mod.JOB_ONBOARDING_ITEM,
        payload={"run_id": run_a.id, "step_run_id": license_step_run.id,
                 "request_id": run_a_request.id, "item_id": items_a["grant_licenses"].id,
                 "action": license_spec["action"] or "none"},
        idempotency_key=f"dag:{run_a.id}:{items_a['grant_licenses'].id}:1",
        resource_type="onboarding", resource_id=str(run_a_request.id),
        label=f"{run_a_request.employee_name}: {license_spec['step']}",
    )
    license_step_run.job_id = job.id
    counts["jobs"] += 1
    session.add(db.WorkflowEventRecord(
        run_id=run_a.id, event_type="workflow.run.started",
        payload_json=json.dumps({"definition": "onboarding-dag", "version": 1}),
    ))
    counts["events"] += 1

    # Run B: paused awaiting approval. The gated first step is still pending;
    # an operator resumes, approves, and the DAG takes over from there. Its
    # SLA timer comes due ~90 s after seeding, so the timers service in a
    # running stack escalates it live during the guided demo journey.
    run_b_request = request_for("Marcus Webb", "marcus.webb", "Sales")
    items_b = _checklist(session, run_b_request, {
        "create_user_account": "pending", "provision_laptop": "completed",
        "grant_licenses": "blocked", "configure_mailbox": "blocked",
    })
    items_b["provision_laptop"].result = '{"asset_tag": "DEMO-LAPTOP-003", "shipped": true}'
    items_b["create_user_account"].sla_minutes = 60
    items_b["create_user_account"].escalation_queue = "it-management"
    counts["items"] += len(items_b)
    run_b = db.WorkflowRunRecord(request_id=run_b_request.id, status="paused",
                                 started_at=db.utcnow() - timedelta(days=1),
                                 last_tick_at=db.utcnow() - timedelta(minutes=5))
    session.add(run_b)
    session.flush()
    counts["runs"] += 1
    session.add(db.WorkflowStepRunRecord(
        run_id=run_b.id, checklist_item_id=items_b["provision_laptop"].id,
        attempt=1, status="succeeded", started_at=db.utcnow() - timedelta(days=1),
        completed_at=db.utcnow() - timedelta(days=1) + timedelta(minutes=6),
    ))
    counts["step_runs"] += 1
    session.add(db.WorkflowTimerRecord(
        request_id=run_b_request.id, item_id=items_b["create_user_account"].id,
        kind="step_sla", due_at=db.utcnow() + timedelta(seconds=90), status="pending",
    ))
    for event_type, payload in (
        ("workflow.run.started", {"definition": "onboarding-dag", "version": 1}),
        ("workflow.run.paused", {}),
    ):
        session.add(db.WorkflowEventRecord(
            run_id=run_b.id, event_type=event_type,
            payload_json=json.dumps(payload),
        ))
        counts["events"] += 1
    return counts


def _seed_triggers(session: Session) -> int:
    """One HMAC-signed webhook source and trigger for the /ui/triggers page.

    ``secret_generated`` mirrors what POST /event-sources/{key}/rotate-secret
    returns, so webhook triggers stay executable and the guided journey can
    rotate the secret live to sign a real delivery.
    """
    session.add(db.EventSourceRecord(
        key="demo-source", name="Demo HR system", tenant_key=TENANT,
        secret=secrets.token_urlsafe(32), secret_generated=True, active=True,
    ))
    session.add(db.TriggerRecord(
        key="demo-new-hire", name="New hire from HR system",
        source_key="demo-source", event_type="hr.new_hire",
        target_type="onboarding_request", definition_key="onboarding-dag",
        mapping_json=json.dumps({
            "employee_name": "$event.name",
            "employee_email": "$event.email",
            "department": "$event.department",
            "requester_id": "$event.requester_id",
        }, separators=(",", ":")),
        schedule_kind="webhook", active=True,
    ))
    return 1


def _seed_failure_signal(session: Session) -> int:
    """One exhausted job and one dead-letter notification for the dashboards."""
    request = session.execute(
        select(db.OnboardingRequestRecord).where(
            db.OnboardingRequestRecord.tenant_key == TENANT
        ).order_by(db.OnboardingRequestRecord.id.asc())
    ).scalars().first()
    item = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == request.id
        ).order_by(db.ChecklistItemRecord.id.asc())
    ).scalars().first()
    failed_job = jobs_mod.enqueue_job(
        session, job_type=jobs_mod.JOB_ONBOARDING_ITEM,
        payload={"run_id": 0, "step_run_id": 0, "request_id": request.id,
                 "item_id": item.id, "action": "m365_provision"},
        idempotency_key="demo:failed:m365-provision",
        resource_type="onboarding", resource_id=str(request.id),
        label=f"{request.employee_name}: simulated exhausted retry",
    )
    failed_job.status = "failed"
    failed_job.attempts = failed_job.max_attempts
    failed_job.last_error = "SimulatedActionError: adapter timeout after 5 attempts (demo data)"
    failed_job.completed_at = db.utcnow()

    session.add(db.NotificationOutboxRecord(
        url="http://webhook-sink:8080/webhook",
        payload_json=json.dumps({
            "event": "ticket.created", "ticket_id": "DEMO-0001",
            "title": "VPN outage - all users cannot work", "priority": "urgent",
            "tenant_key": TENANT,
        }, separators=(",", ":")),
        ticket_id="DEMO-0001", status="failed", attempts=5,
        last_error="Connection refused after 5 attempts (demo data)",
        next_attempt_at=None,
    ))
    return 2


def seed(*, reset: bool = False) -> dict[str, int]:
    """Seed the demo dataset; idempotent unless ``reset`` clears it first."""
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        if not reset:
            existing = session.scalar(
                select(db.TicketRecord.ticket_id).where(
                    db.TicketRecord.tenant_key == TENANT
                ).limit(1)
            )
            if existing is not None:
                return {"skipped": 1}
        counts: dict[str, int] = {}
        if reset:
            counts["deleted_rows"] = _delete_all_rows(session)
            session.commit()
        _seed_tenant(session)
        counts["employees"] = len(_seed_employees(session))
        counts["tickets"] = _seed_tickets(session)
        counts["definitions"] = _seed_definitions(session)
        counts.update(_seed_onboarding_runs(session))
        counts["triggers"] = _seed_triggers(session)
        counts["failure_signal"] = _seed_failure_signal(session)
        session.add(db.WorkforceAuditRecord(
            actor_subject=ACTOR, action="demo.dataset_seeded", tenant_key=TENANT,
            entity_type="tenant", entity_id=TENANT,
            detail="Portfolio demo dataset seeded by scripts/seed_demo.py.",
        ))
        session.commit()
        return counts
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true",
                        help="delete all existing rows, then reseed the demo dataset")
    args = parser.parse_args(argv)
    counts = seed(reset=args.reset)
    if counts.get("skipped"):
        print("Demo dataset already present; use --reset to reseed from scratch.")
        return 0
    print("Demo dataset seeded:")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    print("All adapters are simulated; no external system was contacted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
