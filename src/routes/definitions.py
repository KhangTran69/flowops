"""Workflow definition CRUD/versioning routes (v1.3, split out of onboarding).

Admin surface for versioned workflow definitions: create, add a version,
publish, list, diff, get, and the authoritative validate preview. Route
bodies, response models, status codes, and idempotency semantics are
unchanged; ``scripts/dump_openapi.py`` + the committed
``tests/openapi_snapshot.json`` keep the OpenAPI surface byte-identical.

Idempotency-protocol helpers follow the established ``src.api`` late-import
pattern: api.py defines those names before registering routers.
"""

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import db, definitions, idempotency
from src.api import (  # noqa: E402
    _complete_idempotency_key,
    _release_idempotency_key,
    _reserve_idempotency_key,
    _validate_idempotency_key,
)
from src.auth import Actor
from src.deps import _global_admin_actor, _new_run_id
from src.schemas import (
    DefinitionCreateIn,
    DefinitionPublishIn,
    DefinitionVersionIn,
    DefinitionVersionOut,
    WorkflowDefinitionOut,
)

router = APIRouter()

def _definition_out(
    session: Session, definition: db.WorkflowDefinitionRecord
) -> WorkflowDefinitionOut:
    versions = definitions.list_versions(session, definition.key)
    return WorkflowDefinitionOut(
        key=definition.key,
        name=definition.name,
        request_type=definition.request_type,
        status=definition.status,
        current_version=definition.current_version,
        versions=[
            DefinitionVersionOut(
                version=row.version,
                checksum=row.checksum,
                steps=json.loads(row.steps_json),
                created_by=row.created_by,
                created_at=row.created_at,
                published_at=row.published_at,
            )
            for row in versions
        ],
    )

def _definition_mutation(
    session: Session, response: Response, actor: Actor, operation: str,
    definition_key: str, payload: BaseModel, idempotency_key: str | None,
) -> WorkflowDefinitionOut:
    """Use the existing fenced transaction for all definition mutations."""
    _validate_idempotency_key(idempotency_key)
    scope = idempotency.SCOPE_DEFINITION
    key = ("definition:" + hashlib.sha256(json.dumps(
        [actor.subject, idempotency_key], separators=(",", ":")
    ).encode()).hexdigest()) if idempotency_key else None
    fingerprint = hashlib.sha256(json.dumps(
        [operation, definition_key, payload.model_dump(mode="json")],
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    owner, replay = _reserve_idempotency_key(
        session, response, key, fingerprint, scope, definition_key, actor_subject=actor.subject
    )
    if replay is not None:
        return WorkflowDefinitionOut.model_validate(replay["definition"])
    try:
        if operation == "create":
            definition = definitions.create_definition(
                session, key=payload.key, name=payload.name,
                request_type=payload.request_type, steps=payload.steps,
                actor=actor.subject,
            )
        elif operation == "version":
            definitions.create_version(session, definition_key, payload.steps, actor=actor.subject)
            definition = definitions.get_definition(session, definition_key)
        else:
            definition = definitions.publish_version(
                session, definition_key, payload.version, actor=actor.subject
            )
        if definition is None:
            raise HTTPException(404, "Definition not found.")
        result = _definition_out(session, definition)
        db.log_execution(
            session, run_id=_new_run_id(), action=f"workflow.definition.{operation}",
            status="success", detail=f"definition={definition.key}",
            actor_subject=actor.subject,
        )
        if key and owner:
            if not _complete_idempotency_key(
                session, key, owner, fingerprint, scope, definition.key,
                {"definition": result.model_dump(mode="json")},
            ):
                raise HTTPException(409, "Definition reservation changed; retry.")
        else:
            session.commit()
        return result
    except Exception as error:
        session.rollback()
        if key and owner:
            _release_idempotency_key(session, key, owner)
        if isinstance(error, definitions.DefinitionError):
            raise HTTPException(422, str(error)) from error
        if isinstance(error, IntegrityError):
            raise HTTPException(409, "Concurrent definition change; refresh and retry.") from error
        raise

class DefinitionValidationIn(BaseModel):
    request_type: Literal["onboarding", "offboarding"]
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class DefinitionValidationOut(BaseModel):
    steps: list[dict[str, Any]]
    checksum: str


@router.post("/workflow-definitions/validate", response_model=DefinitionValidationOut)
def validate_workflow_definition(
    payload: DefinitionValidationIn,
    actor: Actor = Depends(_global_admin_actor),
) -> DefinitionValidationOut:
    """Preview the authoritative DAG validator without writing any records."""
    try:
        steps = definitions.validate_steps(payload.steps, payload.request_type)
    except definitions.DefinitionError as error:
        raise HTTPException(422, str(error)) from error
    return DefinitionValidationOut(steps=steps, checksum=definitions.checksum_for(steps))


@router.post("/workflow-definitions", response_model=WorkflowDefinitionOut)
def create_workflow_definition(
    payload: DefinitionCreateIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "create", payload.key, payload, idempotency_key)


@router.put("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def create_workflow_definition_version(
    key: str,
    payload: DefinitionVersionIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "version", key, payload, idempotency_key)


@router.post("/workflow-definitions/{key}/publish", response_model=WorkflowDefinitionOut)
def publish_workflow_definition(
    key: str,
    payload: DefinitionPublishIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "publish", key, payload, idempotency_key)


@router.get("/workflow-definitions", response_model=list[WorkflowDefinitionOut])
def list_workflow_definitions(
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[WorkflowDefinitionOut]:
    return [
        _definition_out(session, definition)
        for definition in definitions.list_definitions(session)
    ]


@router.get("/workflow-definitions/{key}/diff")
def compare_workflow_definition_versions(
    key: str,
    from_version: int = Query(alias="from", ge=1),
    to_version: int = Query(alias="to", ge=1),
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    previous = definitions.get_version(session, key, from_version)
    current = definitions.get_version(session, key, to_version)
    if previous is None or current is None:
        raise HTTPException(404, "Definition version not found.")
    before = {step["key"]: step for step in json.loads(previous.steps_json)}
    after = {step["key"]: step for step in json.loads(current.steps_json)}
    return {
        "from_version": from_version,
        "to_version": to_version,
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


@router.get("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def get_workflow_definition(
    key: str,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowDefinitionOut:
    definition = definitions.get_definition(session, key)
    if definition is None:
        raise HTTPException(404, "Definition not found.")
    return _definition_out(session, definition)
