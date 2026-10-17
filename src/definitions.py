"""Versioned workflow definitions (v1.3)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db
from src.expressions import ExpressionError, compile_predicate
from src.onboarding import WorkflowGraphError, validate_graph

VALID_REQUEST_TYPES = ("onboarding", "offboarding")
VALID_JOINS = ("all", "any")


class DefinitionError(ValueError):
    """Invalid definition, version, or migration request."""


def canonical_steps(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in steps:
        key = str(raw.get("key", "")).strip()
        when = raw.get("when") or []
        if isinstance(when, str):
            when = [when]
        step = {
            "key": key,
            "step": str(raw.get("step", "")).strip(),
            "queue": str(raw.get("queue", "")).strip(),
            "requires_approval": bool(raw.get("requires_approval", False)),
            "action": str(raw.get("action", "none")),
            "depends_on": [str(value) for value in raw.get("depends_on") or []],
            "join": str(raw.get("join", "all")).lower(),
            "when": [str(value) for value in when],
            "sla_minutes": raw.get("sla_minutes"),
            "escalation_queue": raw.get("escalation_queue") or None,
        }
        normalized.append(step)
    return normalized


def checksum_for(steps: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(canonical_steps(steps), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_steps(steps: Sequence[Mapping[str, Any]], request_type: str) -> list[dict[str, Any]]:
    normalized = canonical_steps(steps)
    if not normalized:
        raise DefinitionError("A workflow definition needs at least one step.")
    for step in normalized:
        if step["join"] not in VALID_JOINS:
            raise DefinitionError(f"Unsupported join mode: {step['join']!r}.")
        sla = step.get("sla_minutes")
        if sla is not None:
            if not isinstance(sla, int) or sla <= 0:
                raise DefinitionError("sla_minutes must be a positive integer.")
        for predicate in step["when"]:
            try:
                compile_predicate(predicate)
            except ExpressionError as error:
                raise DefinitionError(f"Invalid predicate {predicate!r}: {error}") from error
    try:
        validate_graph(normalized)
    except WorkflowGraphError as error:
        raise DefinitionError(str(error)) from error
    return normalized


def get_definition(session: Session, key: str) -> Optional[db.WorkflowDefinitionRecord]:
    return session.get(db.WorkflowDefinitionRecord, key)


def list_definitions(session: Session) -> list[db.WorkflowDefinitionRecord]:
    return list(
        session.execute(
            select(db.WorkflowDefinitionRecord).order_by(db.WorkflowDefinitionRecord.key)
        ).scalars()
    )


def list_versions(session: Session, key: str) -> list[db.WorkflowDefinitionVersionRecord]:
    return list(
        session.execute(
            select(db.WorkflowDefinitionVersionRecord)
            .where(db.WorkflowDefinitionVersionRecord.definition_key == key)
            .order_by(db.WorkflowDefinitionVersionRecord.version)
        ).scalars()
    )


def get_version(
    session: Session, key: str, version: int
) -> Optional[db.WorkflowDefinitionVersionRecord]:
    return session.execute(
        select(db.WorkflowDefinitionVersionRecord).where(
            db.WorkflowDefinitionVersionRecord.definition_key == key,
            db.WorkflowDefinitionVersionRecord.version == version,
        )
    ).scalar_one_or_none()


def published_version(
    session: Session, key: str
) -> tuple[db.WorkflowDefinitionRecord, db.WorkflowDefinitionVersionRecord]:
    definition = get_definition(session, key)
    if definition is None or definition.status != "published" or definition.current_version is None:
        raise DefinitionError(f"Definition {key!r} has no published version.")
    version = get_version(session, key, definition.current_version)
    if version is None:
        raise DefinitionError(f"Definition {key!r} current version is missing.")
    return definition, version


def create_definition(
    session: Session,
    *,
    key: str,
    name: str,
    request_type: str,
    steps: Sequence[Mapping[str, Any]],
    actor: Optional[str] = None,
) -> db.WorkflowDefinitionRecord:
    key = key.strip().lower()
    if not key:
        raise DefinitionError("Definition key is required.")
    if request_type not in VALID_REQUEST_TYPES:
        raise DefinitionError("request_type must be onboarding or offboarding.")
    if get_definition(session, key) is not None:
        raise DefinitionError(f"Definition {key!r} already exists.")
    normalized = validate_steps(steps, request_type)
    definition = db.WorkflowDefinitionRecord(
        key=key,
        name=name.strip() or key,
        request_type=request_type,
        status="draft",
        current_version=1,
    )
    session.add(definition)
    session.add(
        db.WorkflowDefinitionVersionRecord(
            definition_key=key,
            version=1,
            steps_json=json.dumps(normalized, sort_keys=True, separators=(",", ":")),
            checksum=checksum_for(normalized),
            created_by=actor,
        )
    )
    session.flush()
    return definition


def create_version(
    session: Session,
    key: str,
    steps: Sequence[Mapping[str, Any]],
    *,
    actor: Optional[str] = None,
) -> db.WorkflowDefinitionVersionRecord:
    definition = get_definition(session, key)
    if definition is None:
        raise DefinitionError(f"Definition {key!r} not found.")
    normalized = validate_steps(steps, definition.request_type)
    versions = list_versions(session, key)
    next_version = (max((row.version for row in versions), default=0)) + 1
    row = db.WorkflowDefinitionVersionRecord(
        definition_key=key,
        version=next_version,
        steps_json=json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        checksum=checksum_for(normalized),
        created_by=actor,
    )
    session.add(row)
    # Publishing is the only operation that moves current_version.
    session.flush()
    return row


def publish_version(
    session: Session, key: str, version: int, *, actor: Optional[str] = None
) -> db.WorkflowDefinitionRecord:
    definition = get_definition(session, key)
    row = get_version(session, key, version)
    if definition is None or row is None:
        raise DefinitionError("Definition or version not found.")
    definition.status = "published"
    definition.current_version = version
    if row.published_at is None:
        row.published_at = db.utcnow()
    session.flush()
    return definition
