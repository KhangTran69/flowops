"""Idempotency-Key reservation, fencing, and replay logic.

Extracted from ``src/api.py`` so the ticket and onboarding routes can share
one implementation (via the ``idempotent_create`` decorator) instead of
duplicating reserve/complete/release blocks in each handler.

Storage model: one row per key in ``idempotency_keys`` holding a JSON payload
with the request fingerprint hash, a state (``processing``/``completed``),
the resource type/scope, and a fencing ``owner_token``. A processing
reservation whose lease (``PROCESSING_TIMEOUT``) has expired may be taken
over atomically; every finalize/release is conditional on still owning the
token, so a stale request can never commit or delete a takeover's result.

Retention sweep: records older than ``COMPLETED_RETENTION`` (7 days by
default, override with ``FLOWOPS_IDEMPOTENCY_RETENTION_HOURS``) can no
longer serve a correct replay and are deleted by ``sweep_expired``, which
runs periodically inside the outbox worker loop. Abandoned *processing*
reservations within the retention window are not swept — they are
reclaimed safely by the five-minute takeover lease described above.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Optional

from fastapi import Header, HTTPException, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import and_, delete, or_, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src import db, logging_config
from src.deps import _new_run_id

# Recovery lease for in-flight idempotency reservations (README: abandoned
# requests can be reclaimed after five minutes). Used by the claim/takeover
# logic below and by _reservation_is_stale().
PROCESSING_TIMEOUT = timedelta(minutes=5)
SCOPE_TICKET = "ticket"
SCOPE_ONBOARDING = "onboarding"
REPLAY_ACTIONS = {
    SCOPE_TICKET: "ticket.idempotent_replay",
    SCOPE_ONBOARDING: "onboarding.request.idempotent_replay",
}

# Lifecycle states, stored inside the ``response_json`` payload under the
# ``state`` key (the table has no dedicated status column; legacy rows are
# matched via a JSON-shape LIKE, so these literals must stay compact-stable).
STATE_PROCESSING = "processing"
STATE_COMPLETED = "completed"


def _retention_hours() -> float:
    raw = os.environ.get("FLOWOPS_IDEMPOTENCY_RETENTION_HOURS", "168")
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 168.0


COMPLETED_RETENTION = timedelta(hours=_retention_hours())


def fingerprint_payload(request: BaseModel) -> dict:
    """Payload used for Idempotency-Key binding.

    Optional tenant fields are left out when unset so keys stored before
    multi-tenancy keep matching the same request.
    """
    payload = request.model_dump()
    for field in ("tenant", "tenant_key"):
        if payload.get(field) is None:
            payload.pop(field, None)
    return payload


def request_fingerprint(request: BaseModel) -> str:
    """Return a stable SHA-256 of a validated request body."""
    return hashlib.sha256(
        json.dumps(
            fingerprint_payload(request), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def validate_idempotency_key(key: Optional[str]) -> None:
    if key is not None and len(key) > 128:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key must be at most 128 characters."
        )


def _stored_resource_type(payload: dict[str, Any]) -> str:
    """Legacy records predate resource scoping and are ticket reservations."""
    return payload.get("resource_type", SCOPE_TICKET)


def claim_key(
    session: Session,
    key: str,
    resource_id: str,
    request_hash: str,
    resource_type: str,
) -> Optional[str]:
    """Atomically claim a key or take over an expired processing reservation.

    Returns a fresh fencing token when this request owns the reservation, or
    ``None`` when another request owns it. A takeover replaces the token and
    refreshes the lease timestamp in the same upsert, so the previous owner
    loses the ability to finalize or remove the reservation.
    """
    table = db.IdempotencyRecord.__table__
    now = db.utcnow()
    stale_before = now - PROCESSING_TIMEOUT
    owner_token = uuid.uuid4().hex
    values = {
        "key": key,
        "ticket_id": resource_id,
        "response_json": json.dumps(
            {
                "request_hash": request_hash,
                "state": "processing",
                "resource_type": resource_type,
            },
            separators=(",", ":"),
        ),
        "owner_token": owner_token,
        "resource_type": resource_type,
        "created_at": now,
    }
    scope_matches = table.c.resource_type == resource_type
    if resource_type == SCOPE_TICKET:
        # Reservations created before scoping existed are ticket reservations.
        scope_matches = or_(scope_matches, table.c.resource_type.is_(None))
    takeover = and_(
        table.c.created_at < stale_before,
        table.c.response_json.like('%"state":"processing"%'),
        scope_matches,
    )
    set_values = {
        "ticket_id": resource_id,
        "response_json": values["response_json"],
        "owner_token": owner_token,
        "resource_type": resource_type,
        "created_at": now,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_update(
            index_elements=[table.c.key],
            set_=set_values,
            where=takeover,
        )
        # SQLite reports 1 for both the insert and the takeover update.
        claimed = session.execute(statement).rowcount == 1
    elif dialect == "postgresql":
        statement = (
            postgres_insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[table.c.key],
                set_=set_values,
                where=takeover,
            )
            .returning(table.c.owner_token)
        )
        # psycopg reports -1 for this upsert, so confirm the claim through the
        # RETURNING row instead of relying on a driver-specific rowcount.
        claimed = session.execute(statement).scalar_one_or_none() == owner_token
    else:
        raise RuntimeError(
            f"Idempotency reservations do not support database dialect {dialect!r}."
        )

    session.commit()
    return owner_token if claimed else None


def complete_key(
    session: Session,
    key: str,
    owner_token: str,
    request_hash: str,
    resource_type: str,
    resource_id: str,
    completion: dict[str, Any],
) -> bool:
    """Store the completed response only if this request still owns the key.

    The conditional update only lands while this request still owns the
    fencing token, so a request that lost its lease to a takeover can never
    overwrite the takeover's reservation. On success the update is committed,
    making the stored replay response durable; callers that lose ownership
    afterwards must roll back their own resource writes.
    """
    table = db.IdempotencyRecord.__table__
    payload = {
        "request_hash": request_hash,
        "state": "completed",
        "resource_type": resource_type,
        **completion,
    }
    result = session.execute(
        update(table)
        .where(
            and_(
                table.c.key == key,
                table.c.owner_token == owner_token,
                table.c.resource_type == resource_type,
                table.c.response_json.like('%"state":"processing"%'),
            )
        )
        .values(
            ticket_id=resource_id,
            response_json=json.dumps(payload, separators=(",", ":")),
        )
    )
    completed = result.rowcount == 1
    if completed:
        session.commit()
    return completed


def release_key(session: Session, key: str, owner_token: str) -> bool:
    """Delete a failed request's reservation, but only while it still owns it."""
    table = db.IdempotencyRecord.__table__
    result = session.execute(
        delete(table).where(
            and_(
                table.c.key == key,
                table.c.owner_token == owner_token,
                table.c.response_json.like('%"state":"processing"%'),
            )
        )
    )
    released = result.rowcount == 1
    if released:
        # Commit only when this call actually deleted a row; rolling back on
        # a no-op would discard unrelated uncommitted work in the session.
        session.commit()
    return released


def _reservation_is_stale(created_at: datetime) -> bool:
    """Return whether a processing reservation has exceeded its recovery lease."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < db.utcnow() - PROCESSING_TIMEOUT


def sweep_expired(session: Session) -> int:
    """Delete idempotency records that can no longer serve a correct replay.

    Removes *completed* records older than ``COMPLETED_RETENTION`` and
    *processing* reservations abandoned beyond the retention window (those
    within the five-minute lease stay in place so the takeover path can
    reclaim them safely). Returns the number of rows deleted.
    """
    table = db.IdempotencyRecord.__table__
    cutoff = db.utcnow() - COMPLETED_RETENTION
    result = session.execute(
        delete(table).where(table.c.created_at < cutoff)
    )
    session.commit()
    return result.rowcount


def replay_stored_response(
    stored: db.IdempotencyRecord,
    payload: dict[str, Any],
    request_hash: str,
    resource_type: str,
    response: Response,
    session: Session,
) -> dict[str, Any]:
    """Validate and replay a stored completed response, or return a 409."""
    if _stored_resource_type(payload) != resource_type:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different resource type.",
        )
    if payload.get("request_hash") != request_hash:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request.",
        )
    if payload.get("state") == "processing":
        raise HTTPException(
            status_code=409,
            detail="A request with this Idempotency-Key is still processing.",
        )
    response.status_code = http_status.HTTP_200_OK
    response.headers["Idempotency-Replayed"] = "true"
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=REPLAY_ACTIONS[resource_type],
        status="success",
        ticket_id=stored.ticket_id,
        detail="replayed stored response without re-executing",
    )
    return payload


def reserve_key(
    session: Session,
    response: Response,
    key: Optional[str],
    request_hash: str,
    resource_type: str,
    resource_id: str,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Claim a reservation or return a completed stored response.

    Returns ``(owner_token, None)`` for a new claim and ``(None, payload)`` for
    a replay. Active, changed-payload, and cross-resource requests raise 409.
    """
    if not key:
        return None, None
    stored = session.get(db.IdempotencyRecord, key)
    if stored is not None:
        payload = json.loads(stored.response_json)
        if _stored_resource_type(payload) != resource_type:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for a different resource type.",
            )
        if payload.get("state") == "processing":
            if payload.get("request_hash") != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different request.",
                )
            if not _reservation_is_stale(stored.created_at):
                raise HTTPException(
                    status_code=409,
                    detail="A request with this Idempotency-Key is still processing.",
                )
            owner_token = claim_key(
                session, key, resource_id, request_hash, resource_type
            )
            if owner_token is None:
                # Another request won the takeover race; a processing record
                # holds no replayable response, so report a retryable conflict.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Another request took over this Idempotency-Key while "
                        "it was processing; retry the request."
                    ),
                )
        else:
            return None, replay_stored_response(
                stored, payload, request_hash, resource_type, response, session
            )
    else:
        owner_token = claim_key(
            session, key, resource_id, request_hash, resource_type
        )
    if owner_token is None:
        session.expire_all()
        stored = session.get(db.IdempotencyRecord, key)
        if stored is None:
            raise HTTPException(
                409, "Unable to claim this Idempotency-Key; retry the request."
            )
        payload = json.loads(stored.response_json)
        return None, replay_stored_response(
            stored, payload, request_hash, resource_type, response, session
        )
    return owner_token, None


def idempotent_create(
    *,
    scope: str,
    request_param: str,
    resource_id_extractor: Callable[[BaseModel], str],
    response_field: str,
    response_model: type[BaseModel],
):
    """Wrap a create route so Idempotency-Key handling is shared, not copied.

    The decorated handler keeps business logic only; this wrapper performs the
    full protocol:

    1. Validate and hash the body, then reserve the key (or replay a stored
       response with ``Idempotency-Replayed`` and status 200).
    2. Run the handler; on success, finalize the reservation with the stored
       response in the same transaction as the handler's writes. If another
       request took over the expired lease meanwhile, roll everything back and
       report a retryable 409.
    3. On any failure, log the exception with identifiers, release the
       reservation so the client may retry, and re-raise unchanged.
    """

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        def wrapper(
            *args: Any,
            idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
            **kwargs: Any,
        ) -> Any:
            body: BaseModel = kwargs[request_param]
            response: Response = kwargs["response"]
            session: Session = kwargs["session"]
            validate_idempotency_key(idempotency_key)
            request_hash = request_fingerprint(body)
            resource_id = str(resource_id_extractor(body))
            owner_token, replay = reserve_key(
                session,
                response,
                idempotency_key,
                request_hash,
                scope,
                resource_id,
            )
            if replay is not None:
                return response_model.model_validate(replay[response_field])

            ownership_lost = False
            try:
                result = handler(*args, **kwargs)
                if idempotency_key and owner_token is not None:
                    if not complete_key(
                        session,
                        idempotency_key,
                        owner_token,
                        request_hash,
                        scope,
                        str(getattr(result, "id", resource_id)),
                        {response_field: result.model_dump(mode="json")},
                    ):
                        # Another request took over the expired lease. Roll
                        # back this request's writes before reporting a
                        # retryable conflict.
                        ownership_lost = True
                        session.rollback()
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Another request took over this Idempotency-Key "
                                "while it was processing; retry the request."
                            ),
                        )
                return result
            except Exception as error:
                logging_config.log_exception(
                    f"idempotent create failed (scope={scope})",
                    error,
                    ticket_id=resource_id if scope == SCOPE_TICKET else None,
                    request_id=resource_id if scope == SCOPE_ONBOARDING else None,
                    key_present=idempotency_key is not None,
                )
                if (
                    idempotency_key
                    and owner_token is not None
                    and not ownership_lost
                ):
                    session.rollback()
                    release_key(session, idempotency_key, owner_token)
                raise

        wrapper.__wrapped_handler__ = handler
        wrapper._idempotency_scope = scope
        return wrapper

    return decorator


def idempotency_key_header() -> Any:
    """Reusable Annotated-style default for the Idempotency-Key header."""
    return Header(default=None, alias="Idempotency-Key")
