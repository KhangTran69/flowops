"""Workflow graph model: step specs, validation, and the join resolver.

This module is the single place that decides *when a checklist step may run*.
Both the API (onboarding + offboarding) and the worker use it so the rules
can never drift apart between entry points.

Key concepts for beginners:

* A **step spec** describes one node of the DAG: its stable ``key``, the
  human-readable label, which queue owns it, what it depends on, how to join
  those dependencies ("all" or "any"), an optional condition predicate
  (see src/expressions.py), and an optional SLA in minutes.

* The **resolver** looks at the current status of every step plus the stored
  outputs and returns, for each step, either READY / BLOCKED / SKIPPED with a
  human-readable reason.  It is pure: no database access, no side effects.

Join semantics
--------------
* ``all``  — every prerequisite must be completed.
* ``any``  — ready as soon as at least one prerequisite is completed;
             terminally blocked only when EVERY prerequisite ended failed or
             skipped (skipped prerequisites can never deadlock an ``any``).

Condition semantics
-------------------
* A false predicate makes the step terminal ``skipped`` (never pending).
* Unknown identifiers fail closed: the step is skipped with an audit-visible
  reason naming the identifiers — we never return a 500 for bad data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src import expressions

# Output keys that look secret-ish are redacted from audit rows (v1.3).
_SENSITIVE_KEY_MARKERS = ("password", "secret", "token", "credential")


def _redact_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``output`` with password-like values replaced by '***'."""
    safe: Dict[str, Any] = {}
    for name, value in output.items():
        lowered = str(name).lower()
        if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
            safe[name] = "***"
        else:
            safe[name] = value
    return safe

# Statuses used by checklist items (kept as plain strings, matching db.py).
PENDING = "pending"
IN_PROGRESS = "in_progress"
APPROVAL_REQUIRED = "approval_required"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"          # new terminal status (v1.3 conditional edges)

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, SKIPPED})
JOIN_MODES = ("all", "any")


@dataclass(frozen=True)
class StepSpec:
    """One node of a workflow definition."""

    key: str
    label: str
    queue: str = "it-general"
    requires_approval: bool = False
    action: Optional[str] = None
    depends_on: Tuple[str, ...] = ()
    join: str = "all"
    when: Tuple[str, ...] = ()
    sla_minutes: Optional[int] = None
    escalation_queue: Optional[str] = None

    @staticmethod
    def from_obj(obj: Any) -> "StepSpec":
        """Build a spec from a mapping/dict (as stored in steps_json)."""
        if isinstance(obj, StepSpec):
            return obj
        if not isinstance(obj, dict):
            raise ValueError(f"step must be a mapping, got {type(obj).__name__}")
        raw_deps = obj.get("depends_on") or []
        if isinstance(raw_deps, str):
            raw_deps = [raw_deps]
        raw_when = obj.get("when") or []
        if isinstance(raw_when, str):
            raw_when = [raw_when]
        return StepSpec(
            key=str(obj.get("key", "")),
            label=str(obj.get("step") or obj.get("label") or obj.get("key", "")),
            queue=str(obj.get("queue") or "it-general"),
            requires_approval=bool(obj.get("requires_approval", False)),
            action=obj.get("action"),
            depends_on=tuple(str(d) for d in raw_deps),
            join=str(obj.get("join") or "all"),
            when=tuple(str(w) for w in raw_when),
            sla_minutes=(None if obj.get("sla_minutes") in (None, "") else int(obj["sla_minutes"])),
            escalation_queue=obj.get("escalation_queue"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "step": self.label,
            "queue": self.queue,
            "requires_approval": self.requires_approval,
            "action": self.action,
            "depends_on": list(self.depends_on),
            "join": self.join,
            "when": list(self.when),
            "sla_minutes": self.sla_minutes,
            "escalation_queue": self.escalation_queue,
        }


def canonical_steps_json(steps: Sequence[Any]) -> str:
    """Stable JSON text for a list of step objects (used for checksums)."""
    normalized = [StepSpec.from_obj(s).to_dict() for s in steps]
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def steps_checksum(steps: Sequence[Any]) -> str:
    """SHA-256 over the canonical JSON — same content, same checksum."""
    import hashlib

    return hashlib.sha256(canonical_steps_json(steps).encode("utf-8")).hexdigest()


class GraphValidationError(ValueError):
    """Raised by validate_graph(); message lists every problem found."""


def validate_graph(steps: Sequence[Any], request_type: Optional[str] = None) -> List[StepSpec]:
    """Parse and validate a step list; returns normalized StepSpecs.

    Rejects: empty graphs, blank/duplicate keys, unknown dependencies,
    self-dependencies, cycles, unknown join modes, unparseable/empty
    predicates, non-positive SLAs, and (optionally) request-type mismatches
    via the caller checking returned specs.
    """
    problems: List[str] = []
    specs: List[StepSpec] = []
    seen_keys: set[str] = set()

    if not steps:
        raise GraphValidationError("workflow must contain at least one step")

    for index, raw in enumerate(steps):
        try:
            spec = StepSpec.from_obj(raw)
        except (TypeError, ValueError) as exc:
            problems.append(f"step #{index}: {exc}")
            continue
        if not spec.key.strip():
            problems.append(f"step #{index}: key must not be blank")
            continue
        if spec.key in seen_keys:
            problems.append(f"duplicate step key: {spec.key}")
            continue
        seen_keys.add(spec.key)
        if spec.join not in JOIN_MODES:
            problems.append(f"step {spec.key}: unknown join mode {spec.join!r} (use all|any)")
        if spec.sla_minutes is not None and spec.sla_minutes <= 0:
            problems.append(f"step {spec.key}: sla_minutes must be > 0")
        for predicate in spec.when:
            if not predicate.strip():
                problems.append(f"step {spec.key}: empty predicate in 'when'")
            else:
                try:
                    expressions.parse(predicate)
                except expressions.ExpressionSyntaxError as exc:
                    problems.append(f"step {spec.key}: bad predicate {predicate!r} ({exc})")
        specs.append(spec)

    by_key = {spec.key: spec for spec in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep == spec.key:
                problems.append(f"step {spec.key} depends on itself")
            elif dep not in by_key:
                problems.append(f"step {spec.key} has unknown dependency {dep!r}")

    if not problems:
        _detect_cycle(by_key, problems)

    if problems:
        raise GraphValidationError("; ".join(problems))
    return specs


def _detect_cycle(by_key: Dict[str, StepSpec], problems: List[str]) -> None:
    """Iterative DFS cycle detection so bad definitions are rejected early."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {key: WHITE for key in by_key}
    for start in by_key:
        if color[start] != WHITE:
            continue
        stack: List[Tuple[str, bool]] = [(start, False)]
        while stack:
            key, exiting = stack.pop()
            if exiting:
                color[key] = BLACK
                continue
            if color[key] == GRAY:
                problems.append(f"dependency cycle involving step {key!r}")
                color[key] = BLACK
                continue
            color[key] = GRAY
            stack.append((key, True))
            for dep in by_key[key].depends_on:
                if dep in by_key and color[dep] == GRAY:
                    problems.append(f"dependency cycle involving step {dep!r}")
                elif dep in by_key and color[dep] == WHITE:
                    stack.append((dep, False))


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
@dataclass
class StepState:
    """Current runtime state of one step, fed into the resolver."""

    key: str
    status: str = PENDING
    output: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Resolution:
    """What the resolver decided about one step."""

    key: str
    decision: str               # "ready" | "blocked" | "skipped" | "terminal"
    reason: str = ""            # human-readable; audit-worthy
    unknown_identifiers: Tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.decision == "ready"


def build_context(states: Dict[str, StepState]) -> Dict[str, Any]:
    """Predicate context: outputs of every step under output.* and steps.*.

    ``output.<field>`` resolves to the field of the FIRST completed step that
    produced it (keys sorted for determinism); ``steps.<key>.status`` and
    ``steps.<key>.<field>`` address individual steps explicitly.
    """
    merged_outputs: Dict[str, Any] = {}
    steps_ctx: Dict[str, Any] = {}
    for key in sorted(states):
        state = states[key]
        steps_ctx[key] = {"status": state.status, **state.output}
        if state.status == COMPLETED:
            for name, value in state.output.items():
                merged_outputs.setdefault(name, value)
    return {"output": merged_outputs, "steps": steps_ctx}


def resolve_step(
    spec: StepSpec,
    states: Dict[str, StepState],
    context: Optional[Dict[str, Any]] = None,
) -> Resolution:
    """Decide whether ``spec`` may run given the current step states."""
    state = states.get(spec.key)
    if state is not None and state.status in TERMINAL_STATUSES:
        return Resolution(spec.key, "terminal", f"already {state.status}")

    deps = list(spec.depends_on)
    dep_states = {d: states.get(d) for d in deps}

    # --- join evaluation ----------------------------------------------------
    if deps:
        completed = [d for d in deps if dep_states[d] and dep_states[d].status == COMPLETED]
        if spec.join == "all":
            unmet = [d for d in deps if d not in completed]
            if unmet:
                detail = ", ".join(
                    f"{d} ({dep_states[d].status if dep_states[d] else 'missing'})"
                    for d in unmet
                )
                return Resolution(spec.key, "blocked", f"waiting on: {detail}")
        else:  # any
            if not completed:
                # Terminally blocked only when ALL deps are failed/skipped.
                dead = [
                    d for d in deps
                    if dep_states[d] and dep_states[d].status in (FAILED, SKIPPED)
                ]
                if len(dead) == len(deps):
                    return Resolution(
                        spec.key, "skipped",
                        f"cascade: every 'any' prerequisite ended without completing ({', '.join(sorted(dead))})",
                    )
                return Resolution(spec.key, "blocked", f"waiting on any of: {', '.join(deps)}")

    # --- predicate evaluation ----------------------------------------------
    if spec.when:
        ctx = context if context is not None else build_context(states)
        outcome = expressions.evaluate_all(list(spec.when), ctx)
        if not outcome.value:
            reason = outcome.detail or "condition not met"
            return Resolution(
                spec.key, "skipped",
                f"condition false ({'; '.join(spec.when)}): {reason}",
                unknown_identifiers=outcome.unknown_identifiers,
            )

    return Resolution(spec.key, "ready")


def resolve_graph(
    specs: Sequence[StepSpec],
    states: Dict[str, StepState],
) -> Dict[str, Resolution]:
    """Resolve every step once against the CURRENT snapshot (no auto-cascade).

    Callers apply ``skipped`` decisions by writing the status and then calling
    again, so cascade skips propagate transitively through the normal status
    machinery rather than hidden recursion.
    """
    context = build_context(states)
    return {spec.key: resolve_step(spec, states, context) for spec in specs}


def specs_from_records(items: Sequence[Any]) -> List[StepSpec]:
    """Adapt ChecklistItemRecord-like rows (duck-typed) into StepSpecs.

    Legacy rows have no join/when/sla columns; they behave exactly like v1.1
    (join=all, unconditional) so old checklists keep working.
    """
    specs: List[StepSpec] = []
    for item in items:
        depends = getattr(item, "depends_on", None)
        if isinstance(depends, str):
            try:
                depends = json.loads(depends)
            except json.JSONDecodeError:
                depends = []
        when = getattr(item, "when_json", None)
        if isinstance(when, str):
            try:
                when = json.loads(when)
            except json.JSONDecodeError:
                when = []
        specs.append(
            StepSpec(
                key=getattr(item, "step_key", None) or f"legacy_{item.id}",
                label=item.step,
                queue=item.queue,
                requires_approval=bool(item.requires_approval),
                action=getattr(item, "action", None),
                depends_on=tuple(depends or []),
                join=getattr(item, "join_mode", None) or "all",
                when=tuple(when or []),
                sla_minutes=getattr(item, "sla_minutes", None),
                escalation_queue=getattr(item, "escalation_queue", None),
            )
        )
    return specs


def states_from_records(items: Sequence[Any]) -> Dict[str, StepState]:
    """Snapshot checklist rows into resolver states (output = parsed result)."""
    states: Dict[str, StepState] = {}
    for item in items:
        output: Dict[str, Any] = {}
        raw = getattr(item, "result", None) or getattr(item, "output_json", None)
        if isinstance(raw, str) and raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    output = loaded
            except json.JSONDecodeError:
                output = {}
        elif isinstance(raw, dict):
            output = raw
        key = getattr(item, "step_key", None) or f"legacy_{item.id}"
        states[key] = StepState(key=key, status=item.status, output=output)
    return states


# ---------------------------------------------------------------------------
# Record helpers: adapt ChecklistItemRecord rows for callers that need more
# than specs/states (dependency lists, blockers, stored outputs).  All of
# these accept either ORM rows or plain dicts so tests can stay lightweight.
# ---------------------------------------------------------------------------
def _as_dict(obj: Any) -> Dict[str, Any]:
    """ORM row -> dict; dict -> dict(copy)."""
    if isinstance(obj, dict):
        return dict(obj)
    keys = (
        "id", "request_id", "step", "step_key", "queue", "requires_approval",
        "status", "action", "result", "depends_on", "join_mode", "when_json",
        "sla_minutes", "escalation_queue", "skip_reason",
    )
    return {key: getattr(obj, key, None) for key in keys}


def _parsed_list(raw: Any) -> List[Any]:
    """Parse a JSON-list column ('' / None / bad JSON all mean [])."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return loaded if isinstance(loaded, list) else []


def _parsed_dict(raw: Any) -> Dict[str, Any]:
    """Parse a JSON-dict column into a dict (never raises)."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def state_key(item: Any) -> str:
    """Stable graph key for one checklist row (legacy rows fall back to id)."""
    data = item if isinstance(item, dict) else _as_dict(item)
    return data.get("step_key") or f"legacy_{data.get('id')}"


def dependencies_of(item: Any) -> List[str]:
    """Prerequisite step keys recorded on this row."""
    data = item if isinstance(item, dict) else _as_dict(item)
    return [str(dep) for dep in _parsed_list(data.get("depends_on"))]


def step_output(item: Any) -> Dict[str, Any]:
    """Stored action output of one row (password-like values redacted)."""
    data = item if isinstance(item, dict) else _as_dict(item)
    return _redact_output(_parsed_dict(data.get("result")))


def record_output(item: Any, output: Optional[Dict[str, Any]]) -> None:
    """Persist an action's output dict onto the row (call before flush)."""
    payload = json.dumps(output or {}, sort_keys=True, default=str)
    if isinstance(item, dict):
        item["result"] = payload
    else:
        item.result = payload


def missing_prerequisites(item: Any, items: Sequence[Any]) -> List[str]:
    """Keys of prerequisites that are not completed yet ([] when unmet-free).

    'any'-join steps are satisfied as soon as ONE prerequisite completed;
    'all' joins require every prerequisite to be completed.
    """
    data = item if isinstance(item, dict) else _as_dict(item)
    deps = [str(dep) for dep in _parsed_list(data.get("depends_on"))]
    if not deps:
        return []
    states = {state_key(other): (other if isinstance(other, dict) else _as_dict(other))
              for other in items}
    completed = [dep for dep in deps if states.get(dep, {}).get("status") == COMPLETED]
    join = data.get("join_mode") or "all"
    if join == "any":
        return [] if completed else list(deps)
    return [dep for dep in deps if dep not in completed]


def blocked_status_detail(item: Any, missing: Sequence[str]) -> str:
    """Human-readable 409 detail naming the unmet prerequisites."""
    data = item if isinstance(item, dict) else _as_dict(item)
    names = ", ".join(str(dep) for dep in missing)
    return (
        f"Step '{data.get('step')}' cannot run yet: unmet prerequisites: {names}. "
        "Complete them first."
    )


def action_context_outputs(item: Any, items: Sequence[Any]) -> Dict[str, Any]:
    """Outputs of completed prerequisite steps, merged for adapter contexts.

    Earlier prerequisites (by row id) lose to later ones on key collisions;
    password-like keys are always redacted before they reach a context.
    """
    ordered = sorted(items, key=lambda entry: _as_dict(entry).get("id") or 0)
    merged: Dict[str, Any] = {}
    for dep_key in dependencies_of(item):
        for other in ordered:
            other_data = other if isinstance(other, dict) else _as_dict(other)
            if state_key(other) == dep_key and other_data.get("status") == COMPLETED:
                merged.update(_redact_output(step_output(other)))
    return merged


# ---------------------------------------------------------------------------
# Shared transition engine (used by API, worker, scheduler, and ChatOps)
# ---------------------------------------------------------------------------
def apply_graph_transitions(
    session: Any,
    items: Sequence[Any],
    *,
    request_id: Optional[int] = None,
    actor_subject: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[Any] = None,
) -> List[str]:
    """Re-resolve the DAG against current statuses and persist outcomes.

    * false predicates and 'any' joins whose prerequisites ALL ended
      failed/skipped cascade to terminal ``skipped`` (transitively);
    * every skip writes an audit-visible ``onboarding.item.skipped`` log row
      naming the predicate/reason with password-like output values redacted;
    * SLA timers are armed when a step enters pending (promotion time starts
      the clock) and cancelled the moment it leaves pending;
    * a request whose steps are ALL completed-or-skipped becomes completed.

    Returns the step keys newly skipped this call.  Safe to call repeatedly;
    it never marks completed work pending again.
    """
    from src import db  # local import keeps workflow.py DB-library-agnostic
    from src import timers as timers_module

    records = [item for item in items if not isinstance(item, dict)]
    if request_id is None and records:
        request_id = getattr(records[0], "request_id", None)

    def _snapshot() -> Dict[str, StepState]:
        states: Dict[str, StepState] = {}
        for item in records:
            key = state_key(item)
            states[key] = StepState(
                key=key,
                status=item.status,
                output=_parsed_dict(getattr(item, "result", None)),
            )
        return states

    specs = specs_from_records(records)
    spec_by_key = {spec.key: spec for spec in specs}
    newly_skipped: List[str] = []
    changed_any = False

    for _round in range(len(specs) + 1):  # bounded: at most one skip per round
        states = _snapshot()
        decisions = resolve_graph(specs, states)
        changed = False
        for item in records:
            key = state_key(item)
            resolution = decisions.get(key)
            if resolution is None:
                continue
            if (
                resolution.decision == "skipped"
                and item.status not in TERMINAL_STATUSES
            ):
                item.status = SKIPPED
                item.skip_reason = resolution.reason
                timers_module.cancel_step_timer(session, item.id)
                detail = f"step '{item.step}' skipped: {resolution.reason}"
                if reason:
                    detail += f"; trigger: {reason}"
                db.log_execution(
                    session,
                    run_id=f"graph-{key}",
                    action="onboarding.item.skipped",
                    status="success",
                    ticket_id=str(request_id) if request_id is not None else None,
                    duration_ms=0,
                    detail=detail,
                    actor_subject=actor_subject,
                )
                newly_skipped.append(key)
                changed = True
                changed_any = True
            elif item.status == PENDING and resolution.decision == "ready":
                if getattr(item, "sla_minutes", None):
                    if timers_module.ensure_step_timer(session, item, now=now):
                        changed = True  # re-armed on promotion
        if not changed:
            break
        session.flush()

    # Cancel timers for any step that left pending by other means too.
    for item in records:
        if item.status != PENDING:
            timers_module.cancel_step_timer(session, item.id)

    if records:
        done = all(item.status in (COMPLETED, SKIPPED) for item in records)
        if done and request_id is not None:
            record = session.get(db.OnboardingRequestRecord, request_id)
            if record is not None and record.status != "completed":
                record.status = "completed"
                db.log_execution(
                    session,
                    run_id=f"finalize-{request_id}",
                    action="onboarding.request.completed",
                    status="success",
                    ticket_id=str(request_id),
                    duration_ms=0,
                    detail=f"all checklist steps done for request #{record.id}",
                    actor_subject=actor_subject,
                )
    _ = changed_any
    return newly_skipped
