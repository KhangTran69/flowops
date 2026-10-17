# FlowOps v1.3 — Versioned Workflow Definitions, Conditional Edges, and SLA Timers

You are a senior Python backend engineer joining an existing repository. Read this
whole brief before touching code; it assumes no knowledge of prior sessions.

## 1. Repository and baseline

- Project root: the FlowOps repository (`src/`, `tests/`, `migrations/`, `docs/`).
- Stack: Python 3.10+ (3.13 in the venv), FastAPI, Pydantic v2, SQLAlchemy 2.0,
  Alembic, Jinja2, pytest + httpx. SQLite by default; PostgreSQL via
  `FLOWOPS_DATABASE_URL`.
- Commands:
  - `.venv/bin/pytest -q` → **must start at 287 passed, 14 skipped**.
  - `.venv/bin/alembic upgrade head`
  - `.venv/bin/uvicorn src.api:app --reload`
  - `.venv/bin/python -m src.worker --once`, `python -m src.scheduler --once`
- The suite is **fully offline**. Mock HTTP, use `tmp_path` and a per-test SQLite
  URL via `db.use_database(...)`. Never add a test that needs the network.

## 2. What already exists (do not rebuild it)

| Area | Where | Notes |
|---|---|---|
| DAG resolver | `src/onboarding.py` | `ChecklistStep.depends_on`, `StepState`, `StepTransition`, `is_step_ready`, `resolve_blocked_steps`, `validate_graph`, `topological_order`, `TEMPLATES` (`onboarding-standard`, `onboarding-dag`, `offboarding-standard`) |
| Graph ↔ rows glue | `src/workflow.py` | `step_states`, `missing_prerequisites`, `is_ready`, `step_output`, `record_output`, `prerequisite_outputs`, `flatten_outputs`, `action_context_outputs`, `apply_graph_transitions`, `blocked_status_detail` |
| ChatOps signing | `src/chatops.py` | HMAC-SHA256 links bound to request/item/action, 24 h TTL, `jti` replay ledger |
| Safe predicates | `src/expressions.py` | **New and complete**: `compile_predicate`, `evaluate`, `evaluate_all`, `describe`, `ExpressionError`, `MISSING`. 28 tests in `tests/test_expressions.py`. **It is not wired into the resolver yet — that is your job.** |
| Run orchestrator (v1.2) | `src/scheduler.py` | `workflow_runs`, `workflow_step_runs`, `workflow_events`; leased `claim_run`, pure `plan_ready_steps`, `tick`, `advance_from_job`, pause/resume/cancel |
| Schema | `src/db.py` | `ChecklistItemRecord` has `depends_on_json`, `output_json`; `OnboardingRequestRecord` has no definition columns yet |
| Migrations | `migrations/versions/` | `0006_workflow_dag_chatops`, `0007_workflow_orchestrator` |
| UI | `src/templates/`, `src/web.py` | `onboarding_detail.html` already renders "Depends on", blocker text, step output, ChatOps buttons |

Known defects/gaps you inherit (fix or account for them):

1. `OFFBOARDING_DAG_SHAPE` in `src/onboarding.py` is deliberately **not selectable**
   because `POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete`
   authorises by approval state only — a non-gated `blocked` step can complete out
   of order. Deliverable 4 closes this.
2. `README.md` still advertises `offboarding-dag` as selectable. It is not. Fix the
   README when you enable it (Deliverable 4), not before.
3. The built-in `SimulatedMicrosoftAdapter` returns an empty `output`, so demo runs
   persist `{}`. Your piping tests must inject an adapter double.
4. `resolve_blocked_steps` currently implements `all` joins only, and has no notion
   of a predicate that makes a step irrelevant.

## 3. Mission

Turn the graph from **code** into **versioned data with conditional edges and
service-level timers**, without breaking any v1.1/v1.2 contract.

### Deliverable 1 — Versioned definition store

New tables (additive, nullable-compatible, shipped as Alembic `0008`):

- `workflow_definitions`: `key` (PK, slug), `name`, `request_type`
  (`onboarding|offboarding`), `status` (`draft|published|archived`),
  `current_version`, `created_at`, `updated_at`.
- `workflow_definition_versions`: composite PK `(definition_key, version)`,
  `steps_json` (canonical JSON), `checksum` (SHA-256 of the canonical bytes),
  `created_by`, `created_at`, `published_at`.
- `onboarding_requests` gains nullable `definition_key` and `definition_version`.
- `checklist_items` gains nullable `join_mode` (default `all`), `when_json`, and
  `sla_minutes`.

A step object in `steps_json`:

```json
{
  "key": "configure_mailbox",
  "step": "Configure email mailbox",
  "queue": "it-messaging",
  "requires_approval": false,
  "action": "configure_email_mailbox",
  "depends_on": ["create_user_account"],
  "join": "all",
  "when": ["output.user_id is not null"],
  "sla_minutes": 120,
  "escalation_queue": "it-messaging-escalations"
}
```

Endpoints (operator role; keep the existing auth dependencies and CSRF rules):

- `POST /workflow-definitions` — create draft v1 (`{key, name, request_type, steps}`).
- `PUT /workflow-definitions/{key}` — append a new version; never mutate a version row.
- `POST /workflow-definitions/{key}/versions/{version}/publish`
- `GET /workflow-definitions`, `GET /workflow-definitions/{key}`
- `POST /onboarding-requests` accepts `definition: "key@version"` (published
  versions only) in addition to the existing `template` field.

**Version pinning is the architectural core**: resolution happens once at creation
and is persisted on the request. Publishing a new version must never change an
in-flight request. A draft edit is a new version row, never an update.

### Deliverable 2 — Conditional edges and join semantics

Wire `src/expressions.py` into the resolver.

Evaluation scope for a step predicate:

```python
{
  "context": {"request_id", "request_type", "employee_name", "department", ...},
  "output":  aggregated prerequisite outputs (flattened, first-wins),
  "step":    {step_key: {"status": ...}},   # for cross-step conditions
}
```

Extend `StepState` with `join` and `when`, and implement exactly this table:

| situation | `join: all` | `join: any` |
|---|---|---|
| every prerequisite `completed` | ready | ready |
| at least one `completed`, others pending/blocked | blocked | ready |
| some `completed`, one terminal failure | blocked | ready |
| no `completed`, any terminal failure | `cascade_blocked` | `cascade_blocked` |
| no `completed`, any prerequisite `skipped`, no failure | `skipped` | blocked |
| all prerequisites `skipped` | `skipped` | `skipped` |
| predicate false for this step, prerequisites ready | `skipped` | `skipped` |

Rules that must hold:

- `skipped` is terminal, never counts as `completed`, and never cascades a dependent.
- Predicate evaluation failure (bad data, unknown identifier) fails closed to `skipped`
  with an audit-visible reason — never a 500.
- `is_ready` must remain callable without a DB session (the scheduler calls it in a
  pure planning loop). Pass the resolved definition/template through the state, not
  through queries inside the predicate.
- Every skip/cascade writes an execution log naming the predicate source and the
  resolved operands, with password-like keys redacted.

### Deliverable 3 — SLA timers

- New table `workflow_timers`: `id`, `request_id`, `item_id`, `kind`
  (`step_sla|request_sla`), `due_at`, `status` (`pending|fired|cancelled`),
  `lease_owner_token`, `lease_expires_at`, `fired_at`, `attempts`, `created_at`.
- Create on transition **into** `pending` (promotion from `blocked` starts the clock,
  not request creation). Cancel when the step leaves `pending`.
- `src/timers.py` with `tick(now=None, worker_id=...)` and a CLI
  (`python -m src.timers --once`, `--poll-interval`). Firing is a conditional
  `UPDATE ... WHERE status='pending' AND due_at <= now` so two schedulers cannot
  double-fire; reuse the lease pattern from `src/jobs.py`.
- On fire: log `onboarding.step.overdue` with the elapsed minutes and actor
  `scheduler:<id>`, enqueue a notification through the existing outbox, and move the
  step to `escalation_queue` when the step declares one.
- Expose per-step `overdue` and `sla_remaining_minutes` on
  `GET /onboarding-requests/{id}` and in the detail template, computed from open
  timers — do not add a second query per item.

### Deliverable 4 — Offboarding ledger parity

Make `POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete` consult
the graph:

- unknown step key → 422;
- unmet prerequisites → 409 naming them;
- predicate-skipped step → 409 with the reason;
- keep admin-only, claim-before-execute, idempotent replay
  (`X-Offboarding-Replayed: true`), stale-claim takeover, and release-on-failure.

Only after parity tests pass: register `offboarding-dag` in `TEMPLATES`, and update
the README section that already mentions it.

### Deliverable 5 — Runtime definition migration

`POST /onboarding-requests/{id}/definition` with `{version, dry_run}`:

- `dry_run=true` returns a diff: steps added / removed / retargeted, join or predicate
  changes, and the projected status change per step.
- Applying maps by `step_key`, preserves `output_json` of completed steps, refuses
  (409) when a completed step disappears or a new prerequisite would invalidate
  completed work, recomputes statuses through the resolver, and logs
  `onboarding.request.definition_migrated` with the from/to versions.

### Deliverable 6 — UI and tests

- Definition pages: list, detail with version history, server-rendered version diff.
  No CDN, no build step — follow `base.html` conventions.
- Detail page: overdue badge, SLA remaining, skip reason, migration preview form.
- Inline-SVG graph (topological columns, arrows, status colours) that degrades to the
  existing table when a request has no definition.
- New tests: `test_definitions.py`, `test_joins.py`, `test_timers.py`,
  `test_migration.py`; extend `test_expressions.py`, `test_workflow_dag.py`,
  `test_offboarding.py`, `test_orchestrator.py`.
- Inject the clock everywhere (`now=` parameters). No `sleep`.
- Target: **≥ 330 passed** (287 + at least 45 new).

## 4. Acceptance criteria

- [ ] `.venv/bin/pytest -q` ≥ 330 passed, 14 skipped, no new warnings.
- [ ] `alembic upgrade head` on a fresh SQLite file creates the three new tables and
      all new columns; an existing database upgrades in place.
- [ ] A published version row is immutable through every endpoint (tested).
- [ ] A request created before a publish still reports and executes its pinned version.
- [ ] `join: any` and predicate skips behave per the table in Deliverable 2 (tested
      cell by cell).
- [ ] A due timer fires exactly once under two concurrent `tick()` calls (tested with
      a fake clock and a real second connection).
- [ ] Offboarding completes out of order → 409; the ledger still replays idempotently.
- [ ] `pytest -q` is green with `FLOWOPS_DATABASE_URL` unset (SQLite path).
- [ ] README, `HANDOFF_PROMPT.md`, `docs/architecture.md` and `.env.example` match the
      shipped code.

## 5. Execution order

1. **Baseline** — run the suite, record the counts, read `src/onboarding.py`,
   `src/workflow.py`, `src/expressions.py`, `src/scheduler.py`, `src/db.py`,
   `src/api.py` (onboarding + offboarding sections), `tests/test_workflow_dag.py`.
2. **Schema first** — models + Alembic `0008` + SQLite auto-migration in
   `_add_missing_columns`; prove an old DB upgrades.
3. **Definitions store** — create/version/publish/pin, with checksum stability tests.
4. **Resolver upgrade** — join modes + predicates, keeping `is_ready` pure.
5. **Timers** — table, creation/cancellation in `apply_graph_transitions`, scanner.
6. **Offboarding parity**, then enable `offboarding-dag`.
7. **Migration endpoint**, then **UI**, then docs.
8. **Live verification** — run the eight-step script below and paste its output.

## 6. Live verification script (run before declaring done)

Against `TestClient` and a temp SQLite file:

1. create definition `hire-dag` v1 with a conditional edge and one `join: any` step;
2. publish it, create a request pinned to `hire-dag@1`;
3. execute the gate with an adapter double returning `{"user_id": "u-42"}`;
4. show the conditional step skipping when the predicate is false and running when true
   (two separate requests);
5. publish v2 and edit v1's draft — prove the in-flight request is unchanged;
6. migrate the in-flight request to v2, dry-run first;
7. force a timer due with the fake clock and show `onboarding.step.overdue`;
8. complete an offboarding step out of order and show the 409.

## 7. Guardrails

1. **No breaking changes.** Flat defaults, `template=` selection, ChatOps webhook
   contracts, run/pause/resume/cancel, and every existing test keep working. New
   response fields are additive and nullable.
2. **No dynamic code execution.** Predicates use `src/expressions.py`. Do not add
   `eval`, `exec`, `compile`, `literal_eval`, or a regex "filter".
3. **Fail closed.** Unknown identifiers, unknown steps, missing prerequisites and
   malformed predicates skip or reject; they never unblock work and never 500.
4. **Deterministic.** Inject `now`; never `sleep`; never call Slack, Teams, Microsoft.
5. **Additive schema only.** Nullable columns, new tables, Alembic `0008`, and the
   existing SQLite compatibility path.
6. **No new dependencies.** Standard library plus `requirements.txt`.
7. **Keep the two execution paths identical.** The synchronous executor, the durable
   worker and the scheduler must all resolve edges through the same
   `src/workflow.py` helpers — never duplicate the rules.

## 8. Definition of done

Report, with evidence:

- exact test counts before and after;
- the Alembic upgrade transcript;
- the eight-step live script output;
- what you deliberately deferred and why;
- the three riskiest assumptions you made.

If you cannot finish, stop at a green suite and list precisely what is left — a
half-applied schema with a red suite is worse than an honest partial.
