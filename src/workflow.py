"""Workflow graph glue for the v1.1 DAG engine.

This module is the thin, database-aware layer between the pure graph rules in
:mod:`src.onboarding` and the HTTP/worker call sites. It keeps the three
concerns that must stay identical on both execution paths in one place:

* turning checklist rows into an immutable :class:`~src.onboarding.StepState`
  snapshot,
* aggregating the structured outputs of prerequisite steps so downstream
  actions receive them through ``context["prerequisite_outputs"]``,
* persisting resolved status changes (unblock / cascade) with audit rows.
"""

import json
import uuid
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from src import db
from src.onboarding import (
    STATUS_BLOCKED,
    STATUS_SKIPPED,
    STATUS_CASCADE_BLOCKED,
    STATUS_COMPLETED,
    StepState,
    StepTransition,
    is_step_ready,
    resolve_blocked_steps,
    unmet_prerequisites,
)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def state_key(item: db.ChecklistItemRecord) -> str:
    """Stable graph key for a checklist row.

    Rows written before v0.6.1 have no ``step_key``; they fall back to their
    primary key so a legacy checklist still forms a valid (flat) graph.
    """
    return item.step_key or f"item-{item.id}"


def dependencies_of(item: db.ChecklistItemRecord) -> list[str]:
    """Prerequisite keys stored on a checklist row (``[]`` for legacy rows)."""
    raw = getattr(item, "depends_on_json", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(key) for key in value]


def predicates_of(item: db.ChecklistItemRecord) -> tuple[str, ...]:
    raw = getattr(item, "when_json", None)
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        return ()
    return tuple(str(predicate) for predicate in value)


def step_state(item: db.ChecklistItemRecord) -> StepState:
    return StepState(
        key=state_key(item),
        status=item.status,
        requires_approval=bool(item.requires_approval),
        depends_on=tuple(dependencies_of(item)),
        join_mode=getattr(item, "join_mode", None) or "all",
        when=predicates_of(item),
    )


def step_states(
    items: Iterable[db.ChecklistItemRecord],
) -> dict[str, StepState]:
    """Snapshot every checklist row as an immutable graph state."""
    return {state_key(item): step_state(item) for item in items}


def missing_prerequisites(
    item: db.ChecklistItemRecord, items: Sequence[db.ChecklistItemRecord]
) -> list[str]:
    """Prerequisite keys of ``item`` that are not ``completed`` yet."""
    return unmet_prerequisites(step_state(item), step_states(items))


def is_ready(
    item: db.ChecklistItemRecord, items: Sequence[db.ChecklistItemRecord]
) -> bool:
    """True when every prerequisite step of ``item`` is ``completed``."""
    return is_step_ready(step_state(item), step_states(items))


def step_output(item: db.ChecklistItemRecord) -> dict[str, Any]:
    """Structured output returned by the action behind ``item``.

    ``output_json`` is the v1.1 column; ``result`` is read as a fallback so
    steps completed before the upgrade still feed downstream context.
    """
    raw = getattr(item, "output_json", None) or item.result
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"value": value}


def record_output(
    item: db.ChecklistItemRecord, output: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Persist an action's structured output, keeping ``result`` in sync."""
    payload = dict(output or {})
    serialised = json.dumps(payload, sort_keys=True, default=str)
    item.output_json = serialised
    item.result = serialised  # legacy column, still read by older clients
    return payload


def prerequisite_outputs(
    item: db.ChecklistItemRecord, items: Sequence[db.ChecklistItemRecord]
) -> dict[str, dict[str, Any]]:
    """Aggregate outputs of the direct prerequisites that completed."""
    by_key = {state_key(entry): entry for entry in items}
    aggregated: dict[str, dict[str, Any]] = {}
    for dependency in dependencies_of(item):
        upstream = by_key.get(dependency)
        if upstream is None or upstream.status != STATUS_COMPLETED:
            continue
        aggregated[dependency] = step_output(upstream)
    return aggregated


def flatten_outputs(outputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten prerequisite outputs into one namespace for convenience.

    Earlier (closer) prerequisites win on key collisions so a downstream
    action never sees a later step silently overwrite a ``user_id``.
    """
    flat: dict[str, Any] = {}
    for value in outputs.values():
        for key, item in value.items():
            flat.setdefault(str(key), item)
    return flat


def action_context_outputs(
    item: db.ChecklistItemRecord, items: Sequence[db.ChecklistItemRecord]
) -> dict[str, Any]:
    """The ``prerequisite_outputs`` / ``upstream_outputs`` context fragment."""
    outputs = prerequisite_outputs(item, items)
    return {
        "prerequisite_outputs": outputs,
        "upstream_outputs": flatten_outputs(outputs),
    }


def graph_scope(items: Sequence[db.ChecklistItemRecord]) -> dict[str, Any]:
    """Build the predicate scope from current step outputs/statuses."""
    step_scope = {
        state_key(item): {"status": item.status, "output": step_output(item)}
        for item in items
    }
    flat_output: dict[str, Any] = {}
    for item in items:
        for key, value in step_output(item).items():
            flat_output.setdefault(str(key), value)
    return {"step": step_scope, "output": flat_output}


def apply_graph_transitions(
    session: Session,
    items: Sequence[db.ChecklistItemRecord],
    *,
    request_id: int,
    actor_subject: Optional[str] = None,
    reason: Optional[str] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> list[StepTransition]:
    """Resolve the graph after a status change and persist the transitions.

    A step promoted to ``pending``/``approved`` is logged as
    ``onboarding.item.unblocked``; a step made unreachable by a failed
    prerequisite is logged as ``onboarding.item.cascade_blocked``. Both rows
    carry the actor so the audit page shows who caused the cascade.
    """
    scope = graph_scope(items)
    if context:
        scope["context"] = dict(context)
    transitions = resolve_blocked_steps(step_states(items), scope=scope)
    if not transitions:
        return []
    by_key = {state_key(item): item for item in items}
    for transition in transitions:
        item = by_key.get(transition.key)
        if item is None:  # pragma: no cover - defensive, keys come from items
            continue
        item.status = transition.to_status
        cascaded = transition.to_status == STATUS_CASCADE_BLOCKED
        skipped = transition.to_status == STATUS_SKIPPED
        detail = (
            f"step '{item.step}' of request #{request_id} "
            f"{'skipped' if skipped else ('cascade-blocked' if cascaded else 'unblocked')} "
            f"({transition.from_status} -> {transition.to_status})"
        )
        if transition.reason:
            detail = f"{detail}; {transition.reason}"
        if reason:
            detail = f"{detail}; {reason}"
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action=(
                "onboarding.item.skipped"
                if skipped
                else (
                    "onboarding.item.cascade_blocked"
                    if cascaded
                    else "onboarding.item.unblocked"
                )
            ),
            status="success",
            duration_ms=0,
            ticket_id=str(request_id),
            detail=detail,
            actor_subject=actor_subject,
        )
    return transitions


def blocked_status_detail(item: db.ChecklistItemRecord, missing: Sequence[str]) -> str:
    """Human-readable 409 message for an attempt to run a blocked step."""
    names = ", ".join(missing) if missing else "prerequisites"
    return (
        f"Step '{item.step}' is blocked by incomplete prerequisites: {names}. "
        "Complete the prerequisite step(s) first."
    )


__all__ = [
    "action_context_outputs",
    "apply_graph_transitions",
    "blocked_status_detail",
    "dependencies_of",
    "flatten_outputs",
    "is_ready",
    "missing_prerequisites",
    "prerequisite_outputs",
    "record_output",
    "state_key",
    "step_output",
    "step_state",
    "step_states",
]
