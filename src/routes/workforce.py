"""Employee onboarding, IT task and asset-management API."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import db
from src.auth import Actor
from src.deps import _actor_from_header
from src import web

router = APIRouter(prefix="/workforce", tags=["employee onboarding and IT assets"])

TASKS = (
    ("prepare_laptop", "Prepare laptop", "Prepare and verify a suitable laptop."),
    ("assign_equipment", "Assign equipment", "Record the employee's issued IT equipment."),
    ("provision_account", "Simulate account provisioning", "Provision the employee account through the configured automation adapter."),
    ("software_setup", "Confirm software setup", "Confirm required software and access are available."),
    ("it_orientation", "Complete IT orientation checklist", "Complete security and IT orientation."),
)
STATUSES = {"DRAFT", "PENDING_APPROVAL", "APPROVED", "IN_PROGRESS", "COMPLETED", "REJECTED", "CANCELLED"}
TASK_STATUSES = {"TODO", "IN_PROGRESS", "BLOCKED", "DONE"}


def role_actor(*required: str):
    allowed = {item.upper() for item in required}

    def dependency(actor: Actor = Depends(_actor_from_header)) -> Actor:
        roles = {role.upper() for role in actor.roles}
        # Existing FlowOps operators keep access during the incremental rollout.
        if "AUTOMATION.ADMIN" in roles or roles.intersection(allowed):
            return actor
        raise HTTPException(403, "One of these roles is required: " + ", ".join(sorted(allowed)))

    return dependency


def _audit(session: Session, actor: Actor, action: str, entity: str, entity_id: int, detail: str = "") -> None:
    session.add(db.WorkforceAuditRecord(actor_subject=actor.subject, action=action,
        entity_type=entity, entity_id=str(entity_id), detail=detail[:1000] or None))


def _redact_detail(value: str) -> str:
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    return re.sub(r"(?i)(authorization|password|secret|token|api[_-]?key)(\s*[=:]\s*|\s+)[^\s,;]+",
                  r"\1\2[REDACTED]", value)[:1000]


def _is_before(value: Optional[datetime], reference: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return value < reference


class EmployeeIn(BaseModel):
    employee_number: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=200)
    work_email: str = Field(min_length=3, max_length=320)
    department: str = Field(min_length=1, max_length=120)
    job_title: str = Field(min_length=1, max_length=160)
    start_date: date
    manager_subject: str = Field(min_length=1, max_length=256)

    @field_validator("work_email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid work email address.")
        return value


class RequestIn(BaseModel):
    employee_id: int = Field(gt=0)


class DraftUpdateIn(BaseModel):
    employee_id: int = Field(gt=0)


class DecisionIn(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


class RejectIn(DecisionIn):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A rejection reason is required.")
        return value


class TaskUpdateIn(BaseModel):
    status: str
    assignee_subject: Optional[str] = Field(default=None, max_length=256)
    due_at: Optional[datetime] = None
    completion_notes: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("status")
    @classmethod
    def task_status(cls, value: str) -> str:
        value = value.upper()
        if value not in TASK_STATUSES:
            raise ValueError("Unknown IT task status.")
        return value

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class AssetIn(BaseModel):
    asset_tag: str = Field(min_length=1, max_length=64)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    brand_model: str = Field(min_length=1, max_length=200)
    condition: str = "GOOD"

    @field_validator("condition")
    @classmethod
    def valid_condition(cls, value):
        value = value.upper()
        if value not in {"GOOD", "FAIR", "POOR", "DAMAGED"}:
            raise ValueError("Invalid asset condition.")
        return value


class EmployeeStatusIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def employee_status(cls, value):
        value = value.upper()
        if value not in {"ACTIVE", "LEAVE", "TERMINATED"}:
            raise ValueError("Invalid employment status.")
        return value


class AssetStatusIn(BaseModel):
    status: str
    condition: Optional[str] = Field(default=None, max_length=24)

    @field_validator("status")
    @classmethod
    def asset_status(cls, value):
        value = value.upper()
        if value not in {"AVAILABLE", "MAINTENANCE", "RETIRED"}:
            raise ValueError("Status can be AVAILABLE, MAINTENANCE, or RETIRED. Use assign to set ASSIGNED.")
        return value

    @field_validator("condition")
    @classmethod
    def valid_condition(cls, value):
        if value is None:
            return value
        value = value.upper()
        if value not in {"GOOD", "FAIR", "POOR", "DAMAGED"}:
            raise ValueError("Invalid asset condition.")
        return value


class AssignIn(BaseModel):
    employee_id: int = Field(gt=0)


class ReturnIn(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=1000)


def _request(session: Session, request_id: int, actor: Actor) -> tuple[db.StaffOnboardingRecord, db.EmployeeRecord]:
    item = session.get(db.StaffOnboardingRecord, request_id)
    if item is None:
        raise HTTPException(404, "Onboarding request not found.")
    employee = session.get(db.EmployeeRecord, item.employee_id)
    if "MANAGER" in {x.upper() for x in actor.roles} and actor.subject != employee.manager_subject:
        raise HTTPException(404, "Onboarding request not found.")
    return item, employee


@router.get("/employees")
def employees(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
              actor: Actor = Depends(role_actor("HR", "IT_ADMIN")), session: Session = Depends(db.get_session)):
    rows = session.execute(select(db.EmployeeRecord).order_by(db.EmployeeRecord.id).offset(offset).limit(limit)).scalars()
    return [{"id": x.id, "employee_number": x.employee_number, "full_name": x.full_name,
             "work_email": x.work_email, "department": x.department, "job_title": x.job_title,
             "start_date": x.start_date, "manager_subject": x.manager_subject, "status": x.status} for x in rows]


@router.post("/employees", status_code=201)
def create_employee(payload: EmployeeIn, actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    row = db.EmployeeRecord(**payload.model_dump(mode="json"))
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Employee number or work email already exists.") from exc
    _audit(session, actor, "employee.created", "employee", row.id)
    session.flush()
    return {"id": row.id, "employee_number": row.employee_number, "full_name": row.full_name,
            "work_email": row.work_email, "department": row.department, "job_title": row.job_title,
            "start_date": row.start_date, "manager_subject": row.manager_subject, "status": row.status}


@router.patch("/employees/{employee_id}")
def update_employee(employee_id: int, payload: EmployeeIn,
    actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    employee = session.get(db.EmployeeRecord, employee_id)
    if employee is None:
        raise HTTPException(404, "Employee not found.")
    for key, value in payload.model_dump(mode="json").items():
        setattr(employee, key, value)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Employee number or work email already exists.") from exc
    _audit(session, actor, "employee.updated", "employee", employee.id)
    return {"id": employee.id, "employee_number": employee.employee_number, "full_name": employee.full_name,
            "work_email": employee.work_email, "department": employee.department, "job_title": employee.job_title,
            "start_date": employee.start_date, "manager_subject": employee.manager_subject, "status": employee.status}


@router.patch("/employees/{employee_id}/status")
def update_employee_status(employee_id: int, payload: EmployeeStatusIn,
    actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    employee = session.get(db.EmployeeRecord, employee_id)
    if employee is None:
        raise HTTPException(404, "Employee not found.")
    if payload.status == "TERMINATED":
        active = session.execute(select(db.StaffOnboardingRecord).where(
            db.StaffOnboardingRecord.employee_id == employee.id,
            db.StaffOnboardingRecord.status.in_(("DRAFT", "PENDING_APPROVAL", "APPROVED", "IN_PROGRESS"))
        )).scalars().all()
        for request in active:
            request.status = "CANCELLED"
            request.updated_at = db.utcnow()
            for event in session.execute(select(db.WorkforceAutomationEventRecord).where(
                db.WorkforceAutomationEventRecord.request_id == request.id,
                db.WorkforceAutomationEventRecord.delivery_status == "PENDING")).scalars():
                event.delivery_status = "CANCELLED"
            _audit(session, actor, "onboarding.cancelled_on_termination", "onboarding_request", request.id)
    employee.status = payload.status
    _audit(session, actor, "employee.status_changed", "employee", employee.id, payload.status)
    return {"id": employee.id, "status": employee.status}


@router.post("/onboarding-requests", status_code=201)
def create_request(payload: RequestIn, actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    employee = session.get(db.EmployeeRecord, payload.employee_id)
    if not employee or employee.status != "ACTIVE":
        raise HTTPException(404, "Active employee not found.")
    existing = session.execute(select(db.StaffOnboardingRecord.id).where(
        db.StaffOnboardingRecord.employee_id == employee.id,
        db.StaffOnboardingRecord.status.in_(("DRAFT", "PENDING_APPROVAL", "APPROVED", "IN_PROGRESS")))).first()
    if existing:
        raise HTTPException(409, "Employee already has an active onboarding request.")
    row = db.StaffOnboardingRecord(employee_id=employee.id, created_by=actor.subject, status="DRAFT")
    session.add(row)
    session.flush()
    _audit(session, actor, "onboarding.draft_created", "onboarding_request", row.id)
    session.flush()
    return _request_view(row, employee)


def _request_view(row, employee):
    return {"id": row.id, "employee_id": employee.id, "employee_name": employee.full_name,
            "employee_email": employee.work_email, "department": employee.department,
            "start_date": employee.start_date, "manager_subject": employee.manager_subject,
            "status": row.status, "created_by": row.created_by,
            "submitted_at": row.submitted_at, "completed_at": row.completed_at,
            "rejection_reason": row.rejection_reason}


@router.get("/onboarding-requests")
def requests(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
             status: Optional[str] = Query(None, max_length=24),
             actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")), session: Session = Depends(db.get_session)):
    query = select(db.StaffOnboardingRecord, db.EmployeeRecord).join(
        db.EmployeeRecord, db.EmployeeRecord.id == db.StaffOnboardingRecord.employee_id)
    roles = {x.upper() for x in actor.roles}
    if "MANAGER" in roles and not ("HR" in roles or "IT_ADMIN" in roles or "AUTOMATION.ADMIN" in roles):
        query = query.where(db.EmployeeRecord.manager_subject == actor.subject)
    if status:
        value = status.upper()
        if value not in STATUSES:
            raise HTTPException(422, "Invalid onboarding status.")
        query = query.where(db.StaffOnboardingRecord.status == value)
    rows = session.execute(query.order_by(db.StaffOnboardingRecord.created_at.desc()).offset(offset).limit(limit)).all()
    return [_request_view(item, employee) for item, employee in rows]


@router.get("/onboarding-requests/{request_id}")
def request_detail(request_id: int, actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")),
                   session: Session = Depends(db.get_session)):
    row, employee = _request(session, request_id, actor)
    task_rows = session.execute(select(db.ITTaskRecord).where(db.ITTaskRecord.request_id == row.id)
                                .order_by(db.ITTaskRecord.id)).scalars()
    decision = session.execute(select(db.WorkforceApprovalRecord).where(
        db.WorkforceApprovalRecord.request_id == row.id)).scalar_one_or_none()
    return {**_request_view(row, employee), "tasks": [_task_view(t) for t in task_rows],
            "approval": None if decision is None else {"decision": decision.decision,
                "actor_subject": decision.actor_subject, "comment": decision.comment, "decided_at": decision.decided_at}}


@router.patch("/onboarding-requests/{request_id}")
def update_draft(request_id: int, payload: DraftUpdateIn,
                 actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    row, _ = _request(session, request_id, actor)
    if row.status != "DRAFT":
        raise HTTPException(409, "Only a draft can be edited.")
    employee = session.get(db.EmployeeRecord, payload.employee_id)
    if employee is None or employee.status != "ACTIVE":
        raise HTTPException(404, "Active employee not found.")
    if session.execute(select(db.StaffOnboardingRecord.id).where(
        db.StaffOnboardingRecord.employee_id == employee.id,
        db.StaffOnboardingRecord.id != row.id,
        db.StaffOnboardingRecord.status.in_(("DRAFT", "PENDING_APPROVAL", "APPROVED", "IN_PROGRESS")))).first():
        raise HTTPException(409, "Employee already has another active onboarding request.")
    row.employee_id = employee.id
    _audit(session, actor, "onboarding.draft_updated", "onboarding_request", row.id)
    return _request_view(row, employee)


@router.post("/onboarding-requests/{request_id}/submit")
def submit_request(request_id: int, actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    row, employee = _request(session, request_id, actor)
    if row.status != "DRAFT":
        raise HTTPException(409, "Only a draft can be submitted.")
    row.status = "PENDING_APPROVAL"
    row.submitted_at = db.utcnow()
    _audit(session, actor, "onboarding.submitted", "onboarding_request", row.id)
    return _request_view(row, employee)


def _decide(request_id: int, actor: Actor, session: Session, decision: str, comment: Optional[str], reason: Optional[str] = None):
    row = session.execute(select(db.StaffOnboardingRecord).where(
        db.StaffOnboardingRecord.id == request_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Onboarding request not found.")
    employee = session.get(db.EmployeeRecord, row.employee_id)
    if employee.manager_subject != actor.subject:
        raise HTTPException(404, "Onboarding request not found.")
    if row.status != "PENDING_APPROVAL":
        raise HTTPException(409, "Only pending requests can be decided.")
    row.status = "APPROVED" if decision == "APPROVED" else "REJECTED"
    row.rejection_reason = reason
    row.updated_at = db.utcnow()
    session.add(db.WorkforceApprovalRecord(request_id=row.id, actor_subject=actor.subject, decision=decision,
                                           comment=reason or comment))
    _audit(session, actor, "onboarding." + decision.lower(), "onboarding_request", row.id, reason or comment or "")
    if decision == "APPROVED":
        row.status = "IN_PROGRESS"
        due = datetime.combine(date.fromisoformat(employee.start_date), datetime.min.time(), tzinfo=timezone.utc)
        due -= timedelta(days=1)
        for key, title, description in TASKS:
            session.add(db.ITTaskRecord(request_id=row.id, task_key=key, title=title, description=description,
                                        due_at=due, status="TODO", required=True))
        event_id = uuid.uuid4().hex
        payload = {"eventId": event_id, "eventType": "workforce.onboarding.approved", "schemaVersion": "1.0",
                   "occurredAt": db.utcnow().isoformat(), "correlationId": uuid.uuid4().hex,
                   "data": {"requestId": row.id, "employeeId": employee.id, "employeeNumber": employee.employee_number,
                            "workEmail": employee.work_email, "fullName": employee.full_name, "startDate": employee.start_date}}
        session.add(db.WorkforceAutomationEventRecord(event_id=event_id, event_type=payload["eventType"],
                   request_id=row.id, payload_json=json.dumps(payload), delivery_status="PENDING"))
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "This onboarding request already has a decision.") from exc
    return _request_view(row, employee)


@router.post("/onboarding-requests/{request_id}/approve")
def approve_request(request_id: int, payload: DecisionIn, actor: Actor = Depends(role_actor("MANAGER")), session: Session = Depends(db.get_session)):
    return _decide(request_id, actor, session, "APPROVED", payload.comment)


@router.post("/onboarding-requests/{request_id}/reject")
def reject_request(request_id: int, payload: RejectIn, actor: Actor = Depends(role_actor("MANAGER")), session: Session = Depends(db.get_session)):
    return _decide(request_id, actor, session, "REJECTED", payload.comment, payload.reason)


@router.post("/onboarding-requests/{request_id}/cancel")
def cancel_request(request_id: int, actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    row, _ = _request(session, request_id, actor)
    if row.status not in {"DRAFT", "PENDING_APPROVAL", "APPROVED", "IN_PROGRESS"}:
        raise HTTPException(409, "This request cannot be cancelled from its current state.")
    row.status = "CANCELLED"
    for event in session.execute(select(db.WorkforceAutomationEventRecord).where(
            db.WorkforceAutomationEventRecord.request_id == row.id,
            db.WorkforceAutomationEventRecord.delivery_status == "PENDING")).scalars():
        event.delivery_status = "CANCELLED"
    _audit(session, actor, "onboarding.cancelled", "onboarding_request", row.id)
    return {"id": row.id, "status": row.status}


@router.post("/onboarding-requests/{request_id}/copy-rejected", status_code=201)
def copy_rejected(request_id: int, actor: Actor = Depends(role_actor("HR")), session: Session = Depends(db.get_session)):
    source, employee = _request(session, request_id, actor)
    if source.status != "REJECTED":
        raise HTTPException(409, "Only a rejected request can be copied to a new draft.")
    row = db.StaffOnboardingRecord(employee_id=employee.id, created_by=actor.subject, status="DRAFT")
    session.add(row)
    session.flush()
    _audit(session, actor, "onboarding.draft_copied", "onboarding_request", row.id, f"source={source.id}")
    return _request_view(row, employee)


@router.get("/tasks")
def tasks(status: Optional[str] = Query(None, max_length=24), assignee: Optional[str] = Query(None, max_length=256),
          overdue: bool = False, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
          actor: Actor = Depends(role_actor("IT_ADMIN", "HR")), session: Session = Depends(db.get_session)):
    query = select(db.ITTaskRecord)
    if status:
        if status.upper() not in TASK_STATUSES:
            raise HTTPException(422, "Invalid task status.")
        query = query.where(db.ITTaskRecord.status == status.upper())
    if assignee:
        query = query.where(db.ITTaskRecord.assignee_subject == assignee)
    if overdue:
        query = query.where(db.ITTaskRecord.due_at < db.utcnow(), db.ITTaskRecord.status != "DONE")
    rows = session.execute(query.order_by(db.ITTaskRecord.due_at, db.ITTaskRecord.id).offset(offset).limit(limit)).scalars()
    return [_task_view(x) for x in rows]


def _task_view(x):
    return {"id": x.id, "request_id": x.request_id, "task_key": x.task_key, "title": x.title,
            "description": x.description, "assignee_subject": x.assignee_subject, "due_at": x.due_at,
            "required": x.required, "status": x.status, "completion_notes": x.completion_notes,
            "completed_at": x.completed_at}


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdateIn, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    task = session.get(db.ITTaskRecord, task_id)
    if task is None:
        raise HTTPException(404, "IT task not found.")
    request = session.execute(select(db.StaffOnboardingRecord).where(
        db.StaffOnboardingRecord.id == task.request_id).with_for_update()).scalar_one()
    if request.status in {"CANCELLED", "REJECTED", "COMPLETED"}:
        raise HTTPException(409, "Tasks for a terminal onboarding request cannot be changed.")
    if task.status == "DONE" and payload.status != "DONE":
        raise HTTPException(409, "Completed tasks cannot be reopened.")
    task.status = payload.status
    if "assignee_subject" in payload.model_fields_set:
        task.assignee_subject = payload.assignee_subject
    if "due_at" in payload.model_fields_set:
        task.due_at = payload.due_at
    if "completion_notes" in payload.model_fields_set:
        task.completion_notes = payload.completion_notes
    task.completed_at = db.utcnow() if payload.status == "DONE" else None
    _audit(session, actor, "it_task." + payload.status.lower(), "it_task", task.id)
    _update_completion(session, request, actor)
    return _task_view(task)


def _update_completion(session: Session, request: db.StaffOnboardingRecord, actor: Actor):
    if request.status not in {"CANCELLED", "REJECTED"}:
        remaining = session.execute(select(db.ITTaskRecord.id).where(
            db.ITTaskRecord.request_id == request.id, db.ITTaskRecord.required.is_(True),
            db.ITTaskRecord.status != "DONE").limit(1)).first()
        if remaining is None:
            request.status = "COMPLETED"
            request.completed_at = db.utcnow()
            _audit(session, actor, "onboarding.completed", "onboarding_request", request.id)


@router.get("/assets")
def assets(status: Optional[str] = Query(None, max_length=24), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
           actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    query = select(db.ITAssetRecord)
    if status:
        if status.upper() not in {"AVAILABLE", "ASSIGNED", "MAINTENANCE", "RETIRED"}:
            raise HTTPException(422, "Invalid asset status.")
        query = query.where(db.ITAssetRecord.status == status.upper())
    return [_asset_view(x) for x in session.execute(query.order_by(db.ITAssetRecord.asset_tag).offset(offset).limit(limit)).scalars()]


def _asset_view(x):
    return {"id": x.id, "asset_tag": x.asset_tag, "serial_number": x.serial_number,
            "category": x.category, "brand_model": x.brand_model, "condition": x.condition,
            "status": x.status, "employee_id": x.employee_id}


@router.post("/assets", status_code=201)
def create_asset(payload: AssetIn, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    asset = db.ITAssetRecord(**payload.model_dump(), status="AVAILABLE")
    session.add(asset)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Asset tag or serial number already exists.") from exc
    _audit(session, actor, "asset.created", "asset", asset.id)
    return _asset_view(asset)


@router.patch("/assets/{asset_id}/status")
def update_asset_status(asset_id: int, payload: AssetStatusIn,
    actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    asset = session.get(db.ITAssetRecord, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found.")
    if asset.status == "ASSIGNED":
        raise HTTPException(409, "Return the assigned asset before changing its inventory status.")
    asset.status = payload.status
    if payload.condition:
        asset.condition = payload.condition.upper()
    _audit(session, actor, "asset.status_changed", "asset", asset.id, payload.status)
    return _asset_view(asset)


@router.post("/assets/{asset_id}/assign")
def assign_asset(asset_id: int, payload: AssignIn, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    employee = session.get(db.EmployeeRecord, payload.employee_id)
    if employee is None or employee.status != "ACTIVE":
        raise HTTPException(404, "Active employee not found.")
    result = session.execute(update(db.ITAssetRecord).where(
        db.ITAssetRecord.id == asset_id, db.ITAssetRecord.status == "AVAILABLE",
        db.ITAssetRecord.employee_id.is_(None)).values(status="ASSIGNED", employee_id=employee.id))
    if result.rowcount != 1:
        raise HTTPException(409, "Asset is unavailable or already assigned.")
    assignment = db.AssetAssignmentRecord(asset_id=asset_id, employee_id=employee.id, assigned_by=actor.subject)
    session.add(assignment)
    session.flush()
    _audit(session, actor, "asset.assigned", "asset", asset_id, f"employee_id={employee.id}")
    session.flush()
    return {"assignment_id": assignment.id, "asset_id": asset_id, "employee_id": employee.id,
            "assigned_at": assignment.assigned_at}


@router.post("/assets/{asset_id}/return")
def return_asset(asset_id: int, payload: ReturnIn, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    asset = session.get(db.ITAssetRecord, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found.")
    if asset.status != "ASSIGNED" or asset.employee_id is None:
        raise HTTPException(409, "Asset is not currently assigned.")
    assignment = session.execute(select(db.AssetAssignmentRecord).where(
        db.AssetAssignmentRecord.asset_id == asset.id,
        db.AssetAssignmentRecord.returned_at.is_(None)).with_for_update()).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(409, "Current assignment record is missing.")
    assignment.returned_at = db.utcnow()
    assignment.return_notes = payload.notes
    asset.status, asset.employee_id = "AVAILABLE", None
    _audit(session, actor, "asset.returned", "asset", asset_id)
    return _asset_view(asset)


@router.get("/assets/{asset_id}/history")
def asset_history(asset_id: int, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    if session.get(db.ITAssetRecord, asset_id) is None:
        raise HTTPException(404, "Asset not found.")
    return [{"id": x.id, "employee_id": x.employee_id, "assigned_by": x.assigned_by,
             "assigned_at": x.assigned_at, "returned_at": x.returned_at, "return_notes": x.return_notes}
            for x in session.execute(select(db.AssetAssignmentRecord).where(
                db.AssetAssignmentRecord.asset_id == asset_id).order_by(db.AssetAssignmentRecord.assigned_at.desc())).scalars()]


def _complete_provisioning(session: Session, event: db.WorkforceAutomationEventRecord, actor_subject: str, outcome: str, detail: str = ""):
    request = session.execute(select(db.StaffOnboardingRecord).where(
        db.StaffOnboardingRecord.id == event.request_id).with_for_update()).scalar_one()
    if request.status == "CANCELLED":
        raise HTTPException(409, "Cancelled onboarding cannot accept automation callbacks.")
    task = session.execute(select(db.ITTaskRecord).where(
        db.ITTaskRecord.request_id == request.id, db.ITTaskRecord.task_key == "provision_account")).scalar_one_or_none()
    if task is None:
        raise HTTPException(409, "Provisioning task does not exist.")
    event.workflow_status = "SUCCEEDED" if outcome == "success" else "FAILED"
    if outcome == "success":
        if task.status != "DONE":
            task.status, task.completed_at = "DONE", db.utcnow()
            task.completion_notes = "Simulated account provisioning confirmed by automation callback."
            _audit(session, Actor(actor_subject, ()), "automation.provisioning_succeeded", "it_task", task.id)
        _update_completion(session, request, Actor(actor_subject, ()))
    else:
        event.last_error = _redact_detail(detail) or "Provisioning workflow reported failure."
        _audit(session, Actor(actor_subject, ()), "automation.provisioning_failed", "onboarding_request", request.id)


class CallbackIn(BaseModel):
    eventId: str = Field(min_length=1, max_length=64)
    outcome: str
    detail: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("outcome")
    @classmethod
    def valid_outcome(cls, value):
        value = value.lower()
        if value not in {"success", "failure"}:
            raise ValueError("outcome must be success or failure")
        return value


@router.post("/automation/callbacks/provisioning")
def provisioning_callback(payload: CallbackIn,
    x_flowops_automation_key: Optional[str] = Header(None, alias="X-FlowOps-Automation-Key"),
    session: Session = Depends(db.get_session)):
    secret = os.environ.get("FLOWOPS_AUTOMATION_CALLBACK_SECRET", "")
    if not secret or not hmac.compare_digest(x_flowops_automation_key or "", secret):
        raise HTTPException(401, "Invalid automation callback credential.")
    event = session.execute(select(db.WorkforceAutomationEventRecord).where(
        db.WorkforceAutomationEventRecord.event_id == payload.eventId).with_for_update()).scalar_one_or_none()
    if event is None:
        raise HTTPException(404, "Automation event not found.")
    if event.workflow_status in {"SUCCEEDED", "FAILED"}:
        return {"eventId": event.event_id, "workflowStatus": event.workflow_status, "duplicate": True}
    _complete_provisioning(session, event, "n8n", payload.outcome, payload.detail or "")
    return {"eventId": event.event_id, "workflowStatus": event.workflow_status, "duplicate": False}


@router.post("/automation/events/{event_id}/simulate")
def simulate_provisioning(event_id: str, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development":
        raise HTTPException(404, "Mock automation is available only in development.")
    event = session.execute(select(db.WorkforceAutomationEventRecord).where(
        db.WorkforceAutomationEventRecord.event_id == event_id).with_for_update()).scalar_one_or_none()
    if event is None:
        raise HTTPException(404, "Automation event not found.")
    if event.workflow_status in {"SUCCEEDED", "FAILED"}:
        return {"eventId": event.event_id, "workflowStatus": event.workflow_status, "duplicate": True}
    _complete_provisioning(session, event, actor.subject, "success")
    event.delivery_status = "DELIVERED"
    event.delivered_at = db.utcnow()
    return {"eventId": event.event_id, "workflowStatus": event.workflow_status, "simulated": True}


@router.get("/automation/events")
def automation_events(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    rows = session.execute(select(db.WorkforceAutomationEventRecord).order_by(
        db.WorkforceAutomationEventRecord.created_at.desc()).offset(offset).limit(limit)).scalars()
    return [{"event_id": x.event_id, "event_type": x.event_type, "request_id": x.request_id,
             "delivery_status": x.delivery_status, "workflow_status": x.workflow_status,
             "attempts": x.attempts, "next_attempt_at": x.next_attempt_at, "last_error": x.last_error,
             "created_at": x.created_at, "delivered_at": x.delivered_at} for x in rows]


@router.get("/automation/reminders")
def reminder_history(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    rows = session.execute(select(db.WorkforceReminderRecord).order_by(
        db.WorkforceReminderRecord.created_at.desc()).offset(offset).limit(limit)).scalars()
    return [{"event_id": x.event_id, "task_id": x.task_id, "outcome": x.outcome,
             "detail": x.detail, "created_at": x.created_at} for x in rows]


@router.post("/automation/events/{event_id}/retry")
def retry_event(event_id: str, actor: Actor = Depends(role_actor("IT_ADMIN")), session: Session = Depends(db.get_session)):
    event = session.execute(select(db.WorkforceAutomationEventRecord).where(
        db.WorkforceAutomationEventRecord.event_id == event_id).with_for_update()).scalar_one_or_none()
    if event is None:
        raise HTTPException(404, "Automation event not found.")
    request = session.get(db.StaffOnboardingRecord, event.request_id)
    if request.status == "CANCELLED":
        raise HTTPException(409, "Cancelled onboarding automation cannot be retried.")
    if event.delivery_status != "FAILED" and event.workflow_status != "FAILED":
        raise HTTPException(409, "Only failed delivery or workflow events can be retried.")
    event.delivery_status, event.attempts, event.next_attempt_at = "PENDING", 0, db.utcnow()
    event.workflow_status = "WAITING"
    event.delivered_at = None
    event.last_error = None
    _audit(session, actor, "automation.delivery_retried", "automation_event", event.id)
    return {"event_id": event.event_id, "delivery_status": event.delivery_status}


@router.get("/audit")
def audit(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
          actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")), session: Session = Depends(db.get_session)):
    query = select(db.WorkforceAuditRecord)
    roles = {x.upper() for x in actor.roles}
    if "MANAGER" in roles and not (roles & {"HR", "IT_ADMIN", "AUTOMATION.ADMIN"}):
        employee_ids = select(db.EmployeeRecord.id).where(db.EmployeeRecord.manager_subject == actor.subject)
        request_ids = select(db.StaffOnboardingRecord.id).where(db.StaffOnboardingRecord.employee_id.in_(employee_ids))
        task_ids = select(db.ITTaskRecord.id).where(db.ITTaskRecord.request_id.in_(request_ids))
        query = query.where(
            ((db.WorkforceAuditRecord.entity_type == "onboarding_request") & db.WorkforceAuditRecord.entity_id.in_(request_ids)) |
            ((db.WorkforceAuditRecord.entity_type == "employee") & db.WorkforceAuditRecord.entity_id.in_(employee_ids)) |
            ((db.WorkforceAuditRecord.entity_type == "it_task") & db.WorkforceAuditRecord.entity_id.in_(task_ids))
        )
    rows = session.execute(query.order_by(db.WorkforceAuditRecord.created_at.desc()).offset(offset).limit(limit)).scalars()
    return [{"id": x.id, "actor_subject": x.actor_subject, "action": x.action, "entity_type": x.entity_type,
             "entity_id": x.entity_id, "detail": x.detail, "created_at": x.created_at} for x in rows]


@router.get("/dashboard")
def workforce_dashboard(actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")), session: Session = Depends(db.get_session)):
    rows_query = select(db.StaffOnboardingRecord)
    roles = {x.upper() for x in actor.roles}
    if "MANAGER" in roles and not (roles & {"HR", "IT_ADMIN", "AUTOMATION.ADMIN"}):
        employee_ids = select(db.EmployeeRecord.id).where(db.EmployeeRecord.manager_subject == actor.subject)
        rows_query = rows_query.where(db.StaffOnboardingRecord.employee_id.in_(employee_ids))
    rows = session.execute(rows_query).scalars().all()
    request_ids = [row.id for row in rows]
    employee_ids = [row.employee_id for row in rows]
    tasks = session.execute(select(db.ITTaskRecord).where(db.ITTaskRecord.request_id.in_(request_ids or [-1]))).scalars().all()
    assets = session.execute(select(db.ITAssetRecord).where(db.ITAssetRecord.employee_id.in_(employee_ids or [-1]))).scalars().all()
    events = session.execute(select(db.WorkforceAutomationEventRecord).where(db.WorkforceAutomationEventRecord.request_id.in_(request_ids or [-1]))).scalars().all()
    manager_only = "MANAGER" in roles and not (roles & {"HR", "IT_ADMIN", "AUTOMATION.ADMIN"})
    error_count_query = select(db.WorkforceAuditRecord.id).where(db.WorkforceAuditRecord.action == "automation.error")
    error_count = 0 if manager_only else len(session.execute(error_count_query).all())
    return {"pending_approvals": sum(r.status == "PENDING_APPROVAL" for r in rows),
            "in_progress": sum(r.status == "IN_PROGRESS" for r in rows),
            "completed": sum(r.status == "COMPLETED" for r in rows),
            "overdue_tasks": sum(t.status != "DONE" and _is_before(t.due_at, db.utcnow()) for t in tasks),
            "available_assets": sum(a.status == "AVAILABLE" for a in assets),
            "assigned_assets": sum(a.status == "ASSIGNED" for a in assets),
            "failed_automation_deliveries": sum(e.delivery_status == "FAILED" or e.workflow_status == "FAILED" for e in events) + error_count}


def _integration_key(supplied: Optional[str]) -> None:
    expected = os.environ.get("FLOWOPS_N8N_API_KEY", "")
    if not expected or not hmac.compare_digest(supplied or "", expected):
        raise HTTPException(401, "Invalid automation API credential.")


@router.get("/integration/overdue-tasks")
def integration_overdue_tasks(x_flowops_automation_key: Optional[str] = Header(None, alias="X-FlowOps-Automation-Key"),
    limit: int = Query(100, ge=1, le=100), session: Session = Depends(db.get_session)):
    _integration_key(x_flowops_automation_key)
    now = db.utcnow()
    rows = session.execute(select(db.ITTaskRecord, db.StaffOnboardingRecord, db.EmployeeRecord)
        .join(db.StaffOnboardingRecord, db.StaffOnboardingRecord.id == db.ITTaskRecord.request_id)
        .join(db.EmployeeRecord, db.EmployeeRecord.id == db.StaffOnboardingRecord.employee_id)
        .where(db.ITTaskRecord.status != "DONE", db.ITTaskRecord.due_at < now,
               db.StaffOnboardingRecord.status.not_in(("CANCELLED", "REJECTED", "COMPLETED")))
        .order_by(db.ITTaskRecord.due_at).limit(limit)).all()
    return [{"taskId": task.id, "requestId": request.id, "employeeId": employee.id,
             "employeeName": employee.full_name, "workEmail": employee.work_email,
             "title": task.title, "dueAt": task.due_at.isoformat()} for task, request, employee in rows]


class ReminderCallbackIn(BaseModel):
    eventId: str = Field(min_length=1, max_length=128)
    taskId: int = Field(gt=0)
    outcome: str = Field(min_length=1, max_length=24)
    detail: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("outcome")
    @classmethod
    def reminder_outcome(cls, value):
        value = value.upper()
        if value not in {"SIMULATED", "SENT", "FAILED", "SKIPPED"}:
            raise ValueError("Invalid reminder outcome.")
        return value


@router.post("/integration/reminders", status_code=202)
def record_reminder(payload: ReminderCallbackIn,
    x_flowops_automation_key: Optional[str] = Header(None, alias="X-FlowOps-Automation-Key"),
    session: Session = Depends(db.get_session)):
    _integration_key(x_flowops_automation_key)
    existing = session.execute(select(db.WorkforceReminderRecord).where(
        db.WorkforceReminderRecord.event_id == payload.eventId)).scalar_one_or_none()
    if existing:
        if existing.task_id != payload.taskId:
            raise HTTPException(409, "Event ID was already used for a different task.")
        return {"eventId": existing.event_id, "outcome": existing.outcome, "duplicate": True}
    task = session.get(db.ITTaskRecord, payload.taskId)
    if task is None:
        raise HTTPException(404, "IT task not found.")
    request = session.get(db.StaffOnboardingRecord, task.request_id)
    if request.status in {"CANCELLED", "REJECTED", "COMPLETED"}:
        raise HTTPException(409, "Reminder outcome cannot be recorded for a terminal request.")
    record = db.WorkforceReminderRecord(event_id=payload.eventId, task_id=task.id,
        outcome=payload.outcome, detail=payload.detail)
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.execute(select(db.WorkforceReminderRecord).where(
            db.WorkforceReminderRecord.event_id == payload.eventId)).scalar_one_or_none()
        if existing and existing.task_id == payload.taskId:
            return {"eventId": existing.event_id, "outcome": existing.outcome, "duplicate": True}
        raise HTTPException(409, "Event ID has already been recorded.")
    _audit(session, Actor("n8n", ()), "task.reminder." + payload.outcome.lower(), "it_task", task.id)
    return {"eventId": payload.eventId, "outcome": payload.outcome, "duplicate": False}


class AutomationErrorIn(BaseModel):
    eventId: str = Field(min_length=1, max_length=128)
    workflowName: str = Field(min_length=1, max_length=120)
    error: str = Field(min_length=1, max_length=1000)


@router.post("/integration/errors", status_code=202)
def record_automation_error(payload: AutomationErrorIn,
    x_flowops_automation_key: Optional[str] = Header(None, alias="X-FlowOps-Automation-Key"),
    session: Session = Depends(db.get_session)):
    _integration_key(x_flowops_automation_key)
    existing = session.execute(select(db.WorkforceAuditRecord).where(
        db.WorkforceAuditRecord.action == "automation.error",
        db.WorkforceAuditRecord.entity_id == payload.eventId)).scalar_one_or_none()
    if existing:
        return {"eventId": payload.eventId, "recorded": True, "duplicate": True}
    session.add(db.WorkforceAuditRecord(actor_subject="n8n", action="automation.error",
        entity_type="automation_event", entity_id=payload.eventId,
        detail=_redact_detail(f"workflow={payload.workflowName}; error={payload.error}")))
    return {"eventId": payload.eventId, "recorded": True, "duplicate": False}


def _page(request: Request, title: str, description: str, columns: list[str], rows: list[dict],
          active: str, form: str = ""):
    return web.render(request, "workforce.html", {"title": title, "description": description,
        "columns": columns, "rows": rows, "active": active, "form": form})


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_home(request: Request, actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")),
             session: Session = Depends(db.get_session)):
    counts = workforce_dashboard(actor, session)
    return web.render(request, "workforce_dashboard.html", {"active": "workforce", "counts": counts})


@router.get("/ui/employees", response_class=HTMLResponse, include_in_schema=False)
def ui_employees(request: Request, actor: Actor = Depends(role_actor("HR", "IT_ADMIN")),
                 session: Session = Depends(db.get_session)):
    rows = employees(0, 100, actor, session)
    return _page(request, "Employees", "HR directory · employee records do not have login accounts.",
        ["ID", "Employee", "Work email", "Department", "Title", "Start date", "Manager", "Status"],
        [{"_id": x["id"], "ID": x["id"], "Employee": x["full_name"], "Work email": x["work_email"], "Department": x["department"],
          "Title": x["job_title"], "Start date": x["start_date"], "Manager": x["manager_subject"], "Status": x["status"]} for x in rows],
        "employees", "employee")


@router.get("/ui/onboarding", response_class=HTMLResponse, include_in_schema=False)
def ui_onboarding(request: Request, actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")),
                  session: Session = Depends(db.get_session)):
    rows = requests(0, 100, None, actor, session)
    return _page(request, "Onboarding requests", "Approval state and progress for new starters.",
        ["Request", "Employee", "Department", "Manager", "Start date", "Status"],
        [{"_id": x["id"], "Request": x["id"], "Employee": x["employee_name"], "Department": x["department"],
          "Manager": x["manager_subject"], "Start date": x["start_date"], "Status": x["status"]} for x in rows],
        "onboarding", "onboarding")


@router.get("/ui/onboarding/{request_id}", response_class=HTMLResponse, include_in_schema=False)
def ui_request_detail(request: Request, request_id: int,
                      actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")),
                      session: Session = Depends(db.get_session)):
    data = request_detail(request_id, actor, session)
    return web.render(request, "workforce_request.html", {"active": "onboarding", "item": data})


@router.get("/ui/approvals", response_class=HTMLResponse, include_in_schema=False)
def ui_approvals(request: Request, actor: Actor = Depends(role_actor("MANAGER")),
                 session: Session = Depends(db.get_session)):
    rows = requests(0, 100, "PENDING_APPROVAL", actor, session)
    return _page(request, "Manager approvals", "Review only requests for employees assigned to your account.",
        ["Request", "Employee", "Department", "Start date", "Status"],
        [{"_id": x["id"], "Request": x["id"], "Employee": x["employee_name"], "Department": x["department"],
          "Start date": x["start_date"], "Status": x["status"]} for x in rows], "approvals", "approvals")


@router.get("/ui/tasks", response_class=HTMLResponse, include_in_schema=False)
def ui_tasks(request: Request, actor: Actor = Depends(role_actor("IT_ADMIN", "HR")),
              session: Session = Depends(db.get_session)):
    rows = tasks(None, None, False, 0, 100, actor, session)
    return _page(request, "IT setup tasks", "Work created after manager approval. Simulated provisioning is labeled in task notes.",
        ["Task", "Request", "Due", "Assignee", "Required", "Status"],
        [{"_id": x["id"], "Task": x["title"], "Request": x["request_id"], "Due": x["due_at"] or "—",
          "Assignee": x["assignee_subject"] or "Unassigned", "Required": "Yes" if x["required"] else "No", "Status": x["status"]} for x in rows],
        "tasks", "tasks")


@router.get("/ui/assets", response_class=HTMLResponse, include_in_schema=False)
def ui_assets(request: Request, actor: Actor = Depends(role_actor("IT_ADMIN")),
              session: Session = Depends(db.get_session)):
    rows = assets(None, 0, 100, actor, session)
    return _page(request, "Asset inventory", "Assign available devices and retain their return history.",
        ["Asset tag", "Serial", "Category", "Model", "Condition", "Status", "Employee"],
        [{"_id": x["id"], "Asset tag": x["asset_tag"], "Serial": x["serial_number"] or "—", "Category": x["category"],
          "Model": x["brand_model"], "Condition": x["condition"], "Status": x["status"], "Employee": x["employee_id"] or "—"} for x in rows],
        "assets", "assets")


@router.get("/ui/automation", response_class=HTMLResponse, include_in_schema=False)
def ui_automation(request: Request, actor: Actor = Depends(role_actor("IT_ADMIN")),
                   session: Session = Depends(db.get_session)):
    rows = automation_events(0, 100, actor, session)
    rows = [{"_id": x["event_id"], "Event": x["event_id"], "Request": x["request_id"],
             "Delivery": x["delivery_status"], "Workflow": x["workflow_status"],
             "Attempts": x["attempts"], "Last error": x["last_error"] or "—"} for x in rows]
    reminders = reminder_history(0, 100, actor, session)
    rows.extend({"_id": "", "Event": x["event_id"], "Request": f"task {x['task_id']}",
                 "Delivery": "RECORDED", "Workflow": x["outcome"], "Attempts": "—",
                 "Last error": x["detail"] or "—"} for x in reminders)
    errors = [x for x in audit(0, 100, actor, session) if x["action"] == "automation.error"]
    rows.extend({"_id": "", "Event": x["entity_id"], "Request": "—", "Delivery": "ERROR",
                 "Workflow": "FAILED", "Attempts": "—", "Last error": x["detail"] or "—"} for x in errors)
    rows.sort(key=lambda r: str(r["Event"]), reverse=True)
    return _page(request, "Automation deliveries", "Webhook delivery and workflow outcome are tracked independently.",
        ["Event", "Request", "Delivery", "Workflow", "Attempts", "Last error"],
        rows,
        "automation", "automation")


@router.get("/ui/audit", response_class=HTMLResponse, include_in_schema=False)
def ui_audit(request: Request, actor: Actor = Depends(role_actor("HR", "MANAGER", "IT_ADMIN")),
              session: Session = Depends(db.get_session)):
    rows = audit(0, 100, actor, session)
    return _page(request, "Workforce audit", "Append-only record of meaningful onboarding and asset changes.",
        ["Time", "Actor", "Action", "Entity", "ID", "Detail"],
        [{"Time": x["created_at"], "Actor": x["actor_subject"], "Action": x["action"],
          "Entity": x["entity_type"], "ID": x["entity_id"], "Detail": x["detail"] or "—"} for x in rows], "audit")
