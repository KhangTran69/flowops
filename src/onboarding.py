"""Employee onboarding/offboarding checklist templates and the DAG engine.

v0.5 shipped flat checklists. v1.1 turns them into an event-driven directed
acyclic graph: every step may declare ``depends_on`` prerequisites, and the
pure helpers in this module decide which steps are ready, which are blocked,
and which cascade when a prerequisite fails terminally.

The graph rules themselves stay pure (no database access); the only web
coupling is that :class:`WorkflowGraphError` is an ``HTTPException`` carrying
status 422, so a rejected template never surfaces as an unhandled 500.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, TypedDict

from src import expressions

from fastapi import HTTPException

VALID_REQUEST_TYPES = ("onboarding", "offboarding")

# --- Step statuses ---------------------------------------------------------
# ``blocked``         waiting on prerequisites (v1.1)
# ``cascade_blocked`` an upstream prerequisite failed terminally (v1.1)
# ``rejected``        a human rejected the step via ChatOps (v1.1)
STATUS_PENDING = "pending"
STATUS_BLOCKED = "blocked"
STATUS_APPROVED = "approved"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"
STATUS_CASCADE_BLOCKED = "cascade_blocked"

#: Statuses that make every dependent step unreachable.
TERMINAL_FAILURE_STATUSES = frozenset(
    {STATUS_FAILED, STATUS_CANCELLED, STATUS_REJECTED, STATUS_SKIPPED, STATUS_CASCADE_BLOCKED}
)
#: Statuses a step may still move out of.
ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_BLOCKED, STATUS_APPROVED})
#: Statuses that count as progress for the request summary.
DONE_STATUSES = frozenset({STATUS_COMPLETED})


class WorkflowGraphError(HTTPException, ValueError):
    """Raised when a checklist template is not a valid dependency graph.

    It doubles as an ``HTTPException`` with status 422 so a bad ``template``
    value in a create request is reported as client error instead of an
    unhandled server error, while callers that treat graph problems as data
    errors can keep catching ``ValueError``.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=422, detail=str(detail))

    def __str__(self) -> str:
        return str(self.detail)


class _RequiredStep(TypedDict):
    step: str
    key: str  # stable identifier used by the ledger and dependency references
    queue: str
    requires_approval: bool
    action: str  # which automation runs when this step is completed


class ChecklistStep(_RequiredStep, total=False):
    """One checklist template step; ``depends_on`` is the v1.1 addition."""

    depends_on: list[str]


@dataclass(frozen=True)
class StepState:
    """An immutable view of one step's status inside the graph."""

    key: str
    status: str
    requires_approval: bool = False
    depends_on: tuple[str, ...] = ()
    join_mode: str = "all"
    when: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepTransition:
    """One resolved status change, returned by :func:`resolve_blocked_steps`."""

    key: str
    from_status: str
    to_status: str
    reason: str = ""


ONBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Create user account", "key": "create_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none"},
    {"step": "Provision laptop and peripherals", "key": "provision_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none"},
    {"step": "Grant software licenses", "key": "grant_licenses", "queue": "it-software", "requires_approval": True, "action": "none"},
    {"step": "Configure email mailbox", "key": "configure_mailbox", "queue": "it-messaging", "requires_approval": False, "action": "none"},
)

OFFBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Disable user account", "key": "disable_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none"},
    {"step": "Recover laptop and peripherals", "key": "recover_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none"},
    {"step": "Revoke software licenses", "key": "revoke_licenses", "queue": "it-software", "requires_approval": False, "action": "none"},
    {"step": "Archive email mailbox", "key": "archive_mailbox", "queue": "it-messaging", "requires_approval": True, "action": "none"},
)

#: v1.1 example DAG from the milestone brief::
#:
#:   [Create user account (gated)] ---+--> [Configure mailbox] --+
#:                                    +--> [Grant licenses (gated)] +--> request complete
#:   [Provision hardware (independent)] ---------------------------+
ONBOARDING_DAG_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Create user account", "key": "create_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none", "depends_on": []},
    {"step": "Provision laptop and peripherals", "key": "provision_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none", "depends_on": []},
    {"step": "Grant software licenses", "key": "grant_licenses", "queue": "it-software", "requires_approval": True, "action": "none", "depends_on": ["create_user_account"]},
    {"step": "Configure email mailbox", "key": "configure_mailbox", "queue": "it-messaging", "requires_approval": False, "action": "none", "depends_on": ["create_user_account"]},
)

#: Offboarding keeps the flat v0.5 template on purpose. Its completion path
#: goes through the idempotent ``offboarding_completions`` ledger, which does
#: not consult the dependency graph yet; offering a DAG template there would
#: advertise prerequisites that the ledger endpoint cannot enforce. The graph
#: shape is kept here for the follow-up that teaches the ledger about edges.
OFFBOARDING_DAG_SHAPE: tuple[ChecklistStep, ...] = (
    {"step": "Disable user account", "key": "disable_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none", "depends_on": []},
    {"step": "Recover laptop and peripherals", "key": "recover_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none", "depends_on": []},
    {"step": "Revoke software licenses", "key": "revoke_licenses", "queue": "it-software", "requires_approval": False, "action": "none", "depends_on": ["disable_user_account"]},
    {"step": "Archive email mailbox", "key": "archive_mailbox", "queue": "it-messaging", "requires_approval": True, "action": "none", "depends_on": ["disable_user_account"]},
)

# v0.6 placeholder: an extra onboarding step that provisions a *simulated*
# Microsoft 365 account (src/m365_simulator.py). Added only when the request
# carries a mail address; no real credentials are used anywhere.
M365_PROVISION_STEP: ChecklistStep = {
    "step": "Provision Microsoft 365 account (simulated)",
    "key": "m365_provision",
    "queue": "it-access-management",
    "requires_approval": False,
    "action": "m365_provision",
    "depends_on": ["create_user_account"],
}

#: Named templates a client may select with ``template=`` on the request.
#: Every built-in default stays flat, so v0.5 clients keep their behaviour.
TEMPLATES: dict[str, tuple[str, tuple[ChecklistStep, ...]]] = {
    "onboarding-standard": ("onboarding", ONBOARDING_STEPS),
    "onboarding-dag": ("onboarding", ONBOARDING_DAG_STEPS),
    "offboarding-standard": ("offboarding", OFFBOARDING_STEPS),
    "offboarding-dag": ("offboarding", OFFBOARDING_DAG_SHAPE),
}


# ---------------------------------------------------------------------------
# Pure graph evaluation


def step_dependencies(step: Mapping[str, Any]) -> list[str]:
    """Return the declared prerequisite keys of a template step.

    Legacy templates (and v0.5 API clients) omit ``depends_on`` entirely; a
    missing or null value means "no prerequisites", i.e. a flat list.
    """
    value = step.get("depends_on")
    if not value:
        return []
    if isinstance(value, str):  # tolerate a single key from hand-written JSON
        value = [value]
    return [str(key) for key in value]


def initial_status_for(step: Mapping[str, Any]) -> str:
    """Status a freshly created step starts in.

    Steps without prerequisites are immediately actionable; steps with
    prerequisites wait in ``blocked`` until the graph resolver promotes them.
    """
    return STATUS_BLOCKED if step_dependencies(step) else STATUS_PENDING


def unmet_prerequisites(
    step: StepState, states: Mapping[str, StepState]
) -> list[str]:
    """Prerequisite keys that have not reached ``completed`` yet."""
    return [
        key
        for key in step.depends_on
        if key not in states or states[key].status != STATUS_COMPLETED
    ]


def is_step_ready(step: StepState, states: Mapping[str, StepState]) -> bool:
    """True when every prerequisite step is ``completed``.

    Unknown prerequisites are treated as unsatisfied (fail closed): a graph
    referencing a missing node must never accidentally unblock work.
    """
    return not unmet_prerequisites(step, states)


def resolve_blocked_steps(
    states: Mapping[str, StepState],
    *,
    pre_approved: Iterable[str] = (),
    scope: Optional[Mapping[str, Any]] = None,
) -> list[StepTransition]:
    """Promote or cascade every ``blocked`` step and return the transitions.

    Rules (v1.3): ``all`` requires every prerequisite completed; ``any``
    requires one completed and cascades only when every prerequisite is
    terminal. Predicates are evaluated only after the join is ready, so a
    downstream ``output.user_id`` predicate cannot skip before its upstream
    action has produced the output. False predicates become ``skipped``.
    """
    approved = set(pre_approved)
    scope = scope or {}
    current = {key: state.status for key, state in states.items()}
    transitions: list[StepTransition] = []
    failed_states = {
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_REJECTED,
        STATUS_CASCADE_BLOCKED,
    }
    changed = True
    while changed:
        changed = False
        for key, step in states.items():
            if current[key] != STATUS_BLOCKED:
                continue
            dependency_statuses = [
                current.get(dependency) for dependency in step.depends_on
            ]
            missing = any(status is None for status in dependency_statuses)
            if not step.depends_on:
                ready, cascade = True, False
            elif step.join_mode == "any":
                if missing:
                    continue
                if any(status == STATUS_COMPLETED for status in dependency_statuses):
                    ready, cascade = True, False
                elif all(
                    status in failed_states or status == STATUS_SKIPPED
                    for status in dependency_statuses
                ):
                    ready, cascade = False, True
                else:
                    continue
            else:
                if missing or any(
                    status in failed_states for status in dependency_statuses
                ):
                    ready, cascade = False, True
                elif all(
                    status == STATUS_COMPLETED for status in dependency_statuses
                ):
                    ready, cascade = True, False
                else:
                    continue
            if cascade:
                new_status = STATUS_CASCADE_BLOCKED
                reason = "prerequisite failed or missing: " + ", ".join(
                    dependency
                    for dependency in step.depends_on
                    if current.get(dependency) in failed_states
                    or current.get(dependency) is None
                )
            elif ready and step.when and not expressions.evaluate_all(step.when, scope):
                new_status = STATUS_SKIPPED
                reason = "predicate false: " + expressions.describe(step.when)
            elif ready:
                new_status = (
                    STATUS_APPROVED
                    if step.requires_approval and key in approved
                    else STATUS_PENDING
                )
                reason = "prerequisites satisfied"
            else:
                continue
            current[key] = new_status
            transitions.append(
                StepTransition(
                    key=key,
                    from_status=STATUS_BLOCKED,
                    to_status=new_status,
                    reason=reason,
                )
            )
            changed = True
    return transitions


def validate_graph(steps: Sequence[Mapping[str, Any]]) -> None:
    """Validate keys, references, and acyclicity of a checklist template.

    Raises :class:`WorkflowGraphError` with an actionable message. Called on
    every custom template before it can be persisted, so a bad graph never
    reaches the database.
    """
    keys: list[str] = []
    for index, step in enumerate(steps):
        key = str(step.get("key") or "").strip()
        if not key:
            raise WorkflowGraphError(f"Checklist step #{index + 1} is missing a key.")
        if key in keys:
            raise WorkflowGraphError(f"Duplicate checklist step key: {key!r}.")
        keys.append(key)

    known = set(keys)
    for step in steps:
        key = str(step["key"])
        join = str(step.get("join", "all")).lower()
        if join not in ("all", "any"):
            raise WorkflowGraphError(f"Step {key!r} has unsupported join mode {join!r}.")
        for predicate in step.get("when") or []:
            if not str(predicate).strip():
                raise WorkflowGraphError(f"Step {key!r} has an empty predicate.")
            try:
                expressions.compile_predicate(str(predicate))
            except expressions.ExpressionError as error:
                raise WorkflowGraphError(
                    f"Step {key!r} has an invalid predicate: {error}"
                ) from error
        sla = step.get("sla_minutes")
        if sla is not None and (not isinstance(sla, int) or sla <= 0):
            raise WorkflowGraphError(f"Step {key!r} has invalid sla_minutes.")
        for dependency in step_dependencies(step):
            if dependency == key:
                raise WorkflowGraphError(f"Step {key!r} cannot depend on itself.")
            if dependency not in known:
                raise WorkflowGraphError(
                    f"Step {key!r} depends on unknown step {dependency!r}."
                )

    # Kahn's algorithm: if not every node can be emitted, a cycle exists.
    indegree = {str(step["key"]): len(step_dependencies(step)) for step in steps}
    dependents: dict[str, list[str]] = {key: [] for key in keys}
    for step in steps:
        for dependency in step_dependencies(step):
            dependents[dependency].append(str(step["key"]))
    ready = [key for key, degree in indegree.items() if degree == 0]
    emitted = 0
    while ready:
        key = ready.pop()
        emitted += 1
        for dependent in dependents[key]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if emitted != len(keys):
        cyclic = sorted(key for key, degree in indegree.items() if degree > 0)
        raise WorkflowGraphError(
            "Checklist dependencies contain a cycle involving: "
            + ", ".join(cyclic)
        )


def topological_order(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return step keys with every prerequisite before its dependents."""
    remaining = {str(step["key"]): set(step_dependencies(step)) for step in steps}
    ordered: list[str] = []
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:  # cyclic graph; callers validate first
            ordered.extend(sorted(remaining))
            break
        for key in ready:
            ordered.append(key)
            remaining.pop(key)
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


# ---------------------------------------------------------------------------
# Template selection


def _copy_step(step: Mapping[str, Any]) -> ChecklistStep:
    """Copy a template step, normalising ``depends_on`` into a fresh list."""
    copied: dict[str, Any] = {
        key: step[key] for key in ("step", "key", "queue", "requires_approval", "action")
    }
    copied["depends_on"] = step_dependencies(step)
    return copied  # type: ignore[return-value]


def template_for(name: str, request_type: str) -> list[ChecklistStep]:
    """Return a named template, rejecting unknown names or type mismatches."""
    entry = TEMPLATES.get(name)
    if entry is None:
        known = ", ".join(sorted(TEMPLATES))
        raise WorkflowGraphError(
            f"Unknown workflow template: {name!r}. Known templates: {known}."
        )
    template_type, steps = entry
    if template_type != request_type:
        raise WorkflowGraphError(
            f"Template {name!r} is for {template_type!r} requests, "
            f"not {request_type!r}."
        )
    return [_copy_step(step) for step in steps]


def checklist_for(
    request_type: str,
    include_m365: bool = False,
    template: Optional[str] = None,
) -> list[ChecklistStep]:
    """Return the checklist template for a request type.

    ``template`` selects one of :data:`TEMPLATES`; when omitted the v0.5
    defaults are used, which are flat (no ``depends_on``). ``include_m365``
    adds the simulated M365 provisioning step, but only for onboarding —
    offboarding never creates accounts.
    """
    if template is not None:
        steps = template_for(template, request_type)
        if include_m365 and request_type == "onboarding":
            steps.append(_copy_step(M365_PROVISION_STEP))
        validate_graph(steps)
        return steps
    if request_type == "onboarding":
        steps = [_copy_step(step) for step in ONBOARDING_STEPS]
        if include_m365:
            steps.append(_copy_step(M365_PROVISION_STEP))
        return steps
    if request_type == "offboarding":
        return [_copy_step(step) for step in OFFBOARDING_STEPS]
    raise ValueError(
        f"Unknown request type: {request_type!r}. Expected one of {VALID_REQUEST_TYPES}."
    )
