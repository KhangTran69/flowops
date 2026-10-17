# TASK: Implement FlowOps Milestone v1.2 — Versioned Workflow Definitions, Conditional Branching, and SLA Timers

## 1. Operating Identity & Mandate

You are acting as a Principal Systems Architect and Lead Automation Engineer.

FlowOps just shipped **v1.1**: a hard-coded DAG engine (`src/onboarding.py`), a
graph-to-row bridge (`src/workflow.py`), structured output piping, and signed
single-use ChatOps approvals (`src/chatops.py`). That engine has two limits you
are now hired to remove:

1. **Graphs are code.** Templates live in a Python tuple, cannot be edited by an
   operator, and have no version. Editing one would silently change workflows
   that are already half-executed.
2. **Edges are unconditional and static.** A step runs if its prerequisites
   completed, full stop. There is no "only if the account was created", no
   "any one of these is enough", and no way to notice a step that has been
   sitting for three days.

Your mandate is to turn that into a **versioned, data-driven workflow
definition store with conditional edges, join semantics, and SLA timers**, and
to close the offboarding enforcement gap left open in v1.1.

Execute autonomously across all 6 phases. Do not claim completion before the
full offline suite is green and you have run the live end-to-end script.

---

## 2. Baseline you must establish before writing code

- `.venv/bin/pytest -q` currently reports **249 passed, 13 skipped**. Any
  regression below that is a defect you introduced.
- Read, in this order: `src/onboarding.py`, `src/workflow.py`, `src/db.py`,
  `src/api.py` (onboarding + webhook sections), `src/worker.py`,
  `src/chatops.py`, `src/templates/onboarding_detail.html`,
  `tests/test_workflow_dag.py`.
- Known v1.1 gaps you are expected to close (do not re-discover them the hard
  way):
  - `OFFBOARDING_DAG_SHAPE` exists in `src/onboarding.py` but is **not
    selectable**, because `POST /offboarding-requests/{id}/steps/{key}/complete`
    authorises by approval state only. A non-gated `blocked` step can be
    completed out of order.
  - The built-in `SimulatedMicrosoftAdapter` returns an empty `output`, so a
    demo run persists `{}`. Your new evaluator tests must inject adapter
    doubles rather than relying on the simulator.

---

## 3. Deliverables

### Deliverable 1: Workflow definitions as versioned data (`src/definitions.py`, `src/db.py`)

- New tables, both additive and nullable-compatible:
  - `workflow_definitions`: `key` (PK, slug), `name`, `request_type`
    (`onboarding|offboarding`), `status` (`draft|published|archived`),
    `current_version`, timestamps.
  - `workflow_definition_versions`: `(definition_key, version)` composite PK,
    `steps_json`, `checksum` (SHA-256 of canonical JSON), `created_by`,
    `created_at`, `published_at`.
- `steps_json` is a list of step objects: `key`, `step`, `queue`,
  `requires_approval`, `action`, `depends_on`, `join` (`all|any`), `when`
  (list of predicate strings), `sla_minutes` (optional int).
- Endpoints (operator role, CSRF-aware where a browser session is used):
  - `POST /workflow-definitions` — create draft v1.
  - `PUT /workflow-definitions/{key}` — create a **new version** from a payload;
    never mutate an existing version row.
  - `POST /workflow-definitions/{key}/publish` — publish a specific version.
  - `GET /workflow-definitions`, `GET /workflow-definitions/{key}` (latest
    published + version list).
- **Version pinning is the architectural core.** `OnboardingRequestRecord` gains
  nullable `definition_key` + `definition_version`. Creating a request resolves
  the template **once** into concrete `ChecklistItemRecord` rows. Editing or
  publishing a definition must never alter an in-flight request.
- Validation reuses `validate_graph()` and adds: unknown join mode, empty
  predicate, duplicate step key, `sla_minutes <= 0`, and a definition whose
  `request_type` does not match the request.

### Deliverable 2: Safe predicate evaluation and join semantics (`src/expressions.py`)

- Implement a **hand-written** predicate evaluator. `eval`, `exec`, `compile`,
  `ast.literal_eval`, and `__import__` are forbidden — including for "just the
  simple cases". Parse a tiny grammar:
  - operands: dotted paths into `context` and `output` (`output.user_id`,
    `context.department`, `step.<key>.status`), string/number/boolean/null
    literals;
  - operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `is null`,
    `is not null`, `and`, `or`, `not`, parentheses.
- Unknown identifiers must fail **closed** (the step is skipped), never raise a
  500, and must produce an audit-visible reason string.
- Add `join: "all" | "any"` to the resolver:
  - `all` — every prerequisite must be `completed` (current behaviour);
  - `any` — the step is ready when **at least one** prerequisite is `completed`,
    and cascades only when **every** prerequisite is terminally failed.
  - A prerequisite that evaluated to `skipped` must not deadlock an `any` join.
- Predicate false evaluates to the new terminal status `skipped`, which never
  counts as `completed` for an `all` join and never cascades a dependent.
- Every skip/cascade decision must write an audit row naming the predicate and
  the resolved values (redact anything matching a password-like step output).

### Deliverable 3: SLA timers and escalation (`src/timers.py`, `src/db.py`, `src/worker.py`)

- New table `workflow_timers`: `id`, `request_id`, `item_id`, `kind`
  (`step_sla|request_sla`), `due_at`, `status` (`pending|fired|cancelled`),
  `lease_owner_token`, `lease_expires_at`, `fired_at`, `attempts`.
  Reuse the lease/fencing pattern already proven in `src/jobs.py`.
- Timers are created when a step enters `pending` and cancelled when it leaves.
  A step that is promoted from `blocked` starts its clock at promotion time,
  not at request creation.
- `python -m src.timers --once` and a polling mode. Firing:
  - is conditional (`UPDATE ... WHERE status='pending'`), so two schedulers
    cannot double-fire;
  - writes `onboarding.step.overdue` with actor `scheduler:<pid>` and the
    elapsed minutes;
  - enqueues a notification through the existing outbox;
  - may reassign the step's queue when `escalation_queue` is configured on the
    definition.
- `GET /onboarding-requests/{id}` must expose `overdue: bool` and
  `sla_remaining_minutes` per step without a second round trip per item.

### Deliverable 4: Offboarding ledger parity (`src/api.py`, `src/offboarding.py`)

- `POST /offboarding-requests/{id}/steps/{key}/complete` must consult the same
  graph resolver as onboarding: unknown key → 422, unmet prerequisites → 409
  naming them, `skipped`-by-predicate steps → 409 with the reason.
- Keep every existing guarantee: admin-only, claim-before-execute, idempotent
  replay (`X-Offboarding-Replayed: true`), stale-claim takeover, release on
  executor failure.
- Only after parity tests pass, register `offboarding-dag` as a selectable
  definition/template and re-enable it in the README.

### Deliverable 5: Runtime definition migration (`src/api.py`, `src/workflow.py`)

- `POST /onboarding-requests/{id}/definition` with `{version, dry_run}`.
- `dry_run=true` returns a diff: steps added / removed / retargeted, join or
  predicate changes, and which steps would change status.
- Applying:
  - maps by `step_key`; preserves `output_json` of completed steps;
  - refuses (409) if a **completed** step disappears or if a new prerequisite
    would invalidate completed work;
  - recomputes statuses through the resolver, never marks completed work
    pending;
  - logs `onboarding.request.definition_migrated` with from/to versions.

### Deliverable 6: Operator UI and test suite (`src/templates/`, `tests/`)

- Definition pages: list, detail with version history, and a **version diff**
  rendered server-side. No CDN, no build step — follow `base.html` conventions.
- Render the live graph as inline SVG (topological columns, arrows, status
  colours). It must degrade to the existing table when a request has no
  definition.
- Detail page additions: `overdue` badge, SLA remaining, skip reason, and a
  migration preview form that calls the dry-run endpoint.
- New tests, all offline, using an injectable clock (no `sleep`):
  - `tests/test_expressions.py` — operator precedence, precedence between `and`
    and `or`, null comparisons, unknown identifier fails closed, and an
    adversarial corpus (`__class__`, `import os`, `()`, deeply nested
    parentheses) that must raise `ExpressionError` rather than execute.
  - `tests/test_definitions.py` — create/publish/version immutability,
    checksum stability, pinning of in-flight requests, invalid graphs → 422.
  - `tests/test_joins.py` — `all` vs `any` joins, skipped prerequisites, mixed
    cascade.
  - `tests/test_timers.py` — due/not-due with a fake clock, conditional firing,
    lease takeover, cancel-on-completion, outbox row, overdue flags in the API.
  - `tests/test_migration.py` — dry-run diff, apply, refusal cases, preserved
    outputs, audit row.
  - Extend `tests/test_offboarding.py` for the new 409 parity paths.
- Target: **at least 45 new tests**; total suite at or above **294 passed**.

---

## 4. Strict Execution Protocol

### Phase 1 — Baseline & schema impact
1. Run `.venv/bin/pytest -q`, record the exact counts.
2. Map every call site of `checklist_for`, `initial_status_for`,
   `resolve_blocked_steps`, `apply_graph_transitions`, and
   `missing_prerequisites`.
3. Write down the additive-only schema delta before touching a model.

### Phase 2 — Blueprint
Produce a numbered blueprint: tables, columns, endpoints, status transitions,
and the exact semantics table for `join` × prerequisite status (`completed`,
`failed`, `skipped`, `blocked`). Get the semantics on paper before coding — this
is where v1.1 skipped a case.

### Phase 3 — Incremental implementation
One unit at a time, in this order: schema → definitions store → expressions →
resolver join/skip → timers → offboarding parity → migration → UI. Run
`pytest tests/test_expressions.py` style focused slices after each unit, and a
full regression at every third unit.

### Phase 4 — Live end-to-end verification
Using `TestClient` against a temp SQLite file, prove and paste the output of:
1. create a definition v1 with a conditional edge and an `any` join;
2. publish it and create a request pinned to v1;
3. execute the gate with an adapter double that returns `user_id`;
4. show the conditional step skipping when the predicate is false, and running
   when it is true (two requests);
5. publish v2, edit v1's draft, and prove the in-flight request is unchanged;
6. migrate the in-flight request to v2 with a dry-run diff first;
7. force a timer due with the fake clock and show `onboarding.step.overdue`;
8. complete an offboarding step out of order and show the new 409.

### Phase 5 — Documentation
Update `README.md` (milestone table, new endpoints, test counts), `HANDOFF_PROMPT.md`
(counts + the v1.2 bullet), `docs/architecture.md` (definition lifecycle diagram
in text), and `.env.example` for any new setting. Keep the existing section
titles; add, never renumber.

### Phase 6 — Handoff
Close with: what changed, what is deliberately deferred, the exact test counts,
and the three riskiest assumptions you made.

---

## 5. Guardrails

1. **No breaking changes.** v1.1 template selection (`template="onboarding-dag"`),
   flat defaults, webhook contracts, response shapes, and every existing test
   must keep working. New response fields are additive and nullable.
2. **Version pinning is inviolable.** A published version row is immutable; a
   draft edit produces a new version. In-flight requests never observe a change.
3. **No dynamic code execution.** The evaluator is a parser you wrote. No
   `eval`/`exec`/`compile`, and no regex-based "good enough" filter.
4. **Deterministic and offline.** Inject the clock; never `sleep`; never call
   Slack, Teams, or Microsoft. All timers and leases must be testable with a
   fake `now`.
5. **Fail closed.** Unknown identifiers, unknown steps, missing prerequisites,
   and malformed predicates skip or reject — they never unblock work and never
   500.
6. **Additive schema only.** Every new column is nullable; every new table is
   created by Alembic `0007` and by `_add_missing_columns()` for SQLite. Existing
   `flowops.db` files must keep opening.
7. **No new dependencies.** Standard library plus what `requirements.txt`
   already pins.

---

## 6. Definition of done

- [ ] `pytest -q` reports ≥ 294 passed, 13 skipped, with no new warnings.
- [ ] `alembic upgrade head` on a fresh SQLite file creates the three new tables
      and both new request columns; an old DB upgrades in place.
- [ ] The Phase 4 script output is pasted in the final report.
- [ ] A published definition cannot be mutated through any endpoint (test).
- [ ] A request created before a version bump still reports its pinned version.
- [ ] `POST /offboarding-requests/{id}/steps/{key}/complete` returns 409 for an
      out-of-order step, and the ledger still replays idempotently.
- [ ] README, HANDOFF, architecture, and `.env.example` match the shipped code.

---

## 7. Environment notes (read before your first edit)

- Python lives at `.venv/bin/python`; run tests with `.venv/bin/pytest -q`.
- The suite is **fully offline**: `httpx`/`requests` are mocked, DB tests use
  `tmp_path` + per-test SQLite.
- This workspace has a write guard that scans the **whole resulting file** for
  credential-shaped assignments and rejects the write. Several pre-existing
  files trip it on lines such as `self.token = token`,
  `owner_token: Mapped[...] = mapped_column(...)`, and
  `authorization: Optional[str] = Header(...)`. If a write is denied with
  `artifact_secret_detected`, **report it to the user with the file and line and
  stop** — do not rename production credential handling, and do not reimplement
  the same edit through another tool to get past the scanner. Prefer placing new
  logic in new modules (`src/definitions.py`, `src/expressions.py`,
  `src/timers.py`, `src/offboarding.py`) so the bulk of the work does not depend
  on blocked files.

## 8. First action

Do not write code yet. Run the baseline, read the seven files listed in §2, and
post the Phase 2 blueprint — including the `join` × prerequisite-status table
and the additive schema delta — before the first edit.
