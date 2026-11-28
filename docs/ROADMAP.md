# FlowOps — Long-Term Improvement Plan

This is the working roadmap for evolving FlowOps from a feature-complete local
prototype into a deployable, portfolio-grade automation platform. It extends
the merged `improvement/roadmap-*` PR series (#51, #52) and the open release
validation line (#53); it does not replace them.

**Status of this document:** proposed 2026-11-27 against `main` @ `057c745`.
Review it whenever a release gate opens or closes; update the checkboxes as
work lands. It is the source of truth for *what is next*; the
[README](../README.md) documents *what exists*, and
[architecture](architecture.md) documents *how it works*.

---

## Where the project stands (verified 2026-11-27)

- `main` is green across all six CI jobs (Python 3.10–3.13 matrix,
  Docker/Postgres, PostgreSQL reliability, command-center build, Chromium
  browser smoke, monitoring-rule tests). Local suite: **459 passed, 21
  skipped** (the skips are PostgreSQL-only tests).
- Milestones v0.1 → v1.5 are done at simulated-integration level: tickets,
  routing, onboarding/offboarding, versioned DAGs with predicates/joins/SLA
  timers, durable leased jobs, multi-tenant (GDAP-style) fencing, event
  triggers + connectors, workforce module, operational analytics, an
  observability stack, and a React **command center** behind an allowlist
  proxy.
- Alembic owns the schema (16 migrations); Dependabot groups weekly bumps;
  security hardening has been continuous (#34–#46: tenant fencing, webhook
  SSRF controls, secret redaction, trigger scheduler leases).
- PR #53 (release validation: durable session revocation, concurrency,
  accessibility, delivery visibility) is green and mergeable at the time of
  writing.

### The honest gaps

Evidence-based; each one is addressed by a phase below.

| # | Gap | Evidence |
|---|-----|----------|
| 1 | **Five release gates are open** and block any real deployment: production identity provider/gateway, workforce browser sign-in, backup/restore rehearsal, full WCAG audit, live (credentialed) connectors. Staging latency + migration validation are also pending. | PR #51–#53 bodies; `docs/console-monitoring.md`; `command-center/README.md` ("No production workforce browser release is authorized") |
| 2 | **Two parallel operator UIs are drifting.** 23 Jinja templates vs 11 React pages. Analytics, triggers, workflow jobs, notifications, knowledge base, and the whole workforce module exist only in Jinja; the Jinja console is kept "while validating React parity" with no parity checklist. | `src/templates/` vs `command-center/src/pages/`; `command-center/README.md` |
| 3 | **`src/api.py` is a 3,158-line monolith** with a ~1,430-line onboarding/offboarding section. Router extraction stopped at tickets/AI/monitoring. It is the highest-conflict file in the repo for parallel bot/IDE work. | `grep -c "@app." src/api.py` → 53 app-level routes; section banners at lines 289–2860 |
| 4 | ~~Offboarding DAG parity~~ **resolved during planning**: the ledger already consults the graph resolver (`workflow.missing_prerequisites` + skipped guard) and `offboarding-dag` is selectable; only the docs and edge-order tests were missing (now added). | `tests/test_offboarding.py::test_dag_ledger_rejects_out_of_order_completion` |
| 5 | **Docs actively mislead.** README claims "321 passed" (real: 459), its project-structure section omits 16 shipped modules (`analytics.py`, `connectors.py`, `triggers.py`, `idempotency.py`, `workforce_worker.py`, …), and `docs/dev-notes/HANDOFF_PROMPT.md` still says "no migration framework yet" and "the API has no auth yet". Anyone (human or IDE agent) starting from the handoff prompt gets a wrong model of the system. | `grep` counts vs suite output; `docs/dev-notes/HANDOFF_PROMPT.md` |
| 6 | **Documented reliability limits have no scheduled owner**: at-least-once webhook delivery outside the reservation transaction, wall-clock lease staleness, in-process rate limiting that is not multi-process safe. | `docs/dev-notes/HANDOFF_PROMPT.md` "Known reliability limits"; README auth section |
| 7 | **Monitoring exists but is not operated.** Alert rules ship with provisional thresholds and are "not installed or activated"; alert thresholds have never seen a representative workload. | `docs/console-monitoring.md`; `monitoring/flowops-alerts.yml` |
| 8 | **Connector polling state is shallow.** Per-connector cursors store a batch count, not a real incremental cursor, and there are no per-source setup runbooks. | `src/connectors.py` (`cursor_json` = `{"last_batch": N}`) |

---

## Guiding principles (unchanged from the repo's own guardrails)

1. Offline, deterministic tests; no test ever touches the network.
2. Additive schema only; Alembic owns migrations; old databases keep opening.
3. Existing API contracts are sacred; new fields are additive and nullable.
4. Fail closed on unknown identifiers, tenants, and credentials.
5. Sensitive actions stay behind explicit human approval gates.
6. No new frameworks without an explicit decision recorded in this file.
7. Keep it interview-readable: small modules, plain-English runbooks.

---

## Phase 0 — Stabilize the baseline (immediate, ≤ 1 week)

Small, unglamorous, unblocks everything else.

- [x] Merge PR #53 once its consolidated CI is green (it is the current
      release-validation head; later phases assume its session-revocation and
      accessibility work). — merged 2026-11-27.
- [x] **Docs debt sweep** (closes gap 5):
  - Refresh README "Running the tests" count policy: stop pinning counts in
    prose — link the CI badge and say "the suite is fully offline; PostgreSQL
    tests opt in via `FLOWOPS_TEST_POSTGRES_URL`".
  - Add the 16 missing modules to the README project-structure tree.
  - Rewrite `docs/dev-notes/HANDOFF_PROMPT.md` against current reality
    (Alembic exists, auth exists, 459+ tests, dual UI, workforce module,
    command center). It is the entry point for IDE agents; a wrong handoff
    prompt causes duplicate/conflicting work — this repo has already paid
    that cost (PR #12 was closed as a superseded parallel rewrite).
  - Extend `AGENTS.md` to describe both run modes (Base44 sandbox and local
    venv) and point contributors at this roadmap.
- [x] **Environment refresh procedure**: add a one-liner to the README dev
      setup — after pulling, reinstall pinned deps
      (`pip install -r requirements-dev.txt`). Rationale: a stale local venv
      (`mcp` 1.30 vs pinned 2.3.0) currently breaks test collection for two
      files while CI stays green, which looks like a repo bug but isn't.
- [x] Adopt a lightweight **contribution protocol** for the concurrent IDE/bot
      workflows that already exist here: one PR per concern, rebase on `main`
      before opening, roadmap checkbox updates belong in the PR body. — now in
      `AGENTS.md`.

**Definition of done:** PR #53 merged; every doc claim about tests, auth, and
migrations matches the code; a fresh clone + README instructions produce a
green suite on the first try.

---

## Phase 1 — Consolidate the platform (1–3 weeks)

One API, one workflow engine, one operator console trajectory.

### 1a. Finish the API decomposition (closes gap 3)

- [x] Extract `src/routes/monitoring.py` (#56), `src/routes/ai.py` (#57), and
      `src/routes/onboarding.py` (#58) from `src/api.py`, following the
      existing router pattern. `src/api.py`: 3,158 -> 1,177 lines.
- [x] Guardrail: byte-identical OpenAPI. `scripts/dump_openapi.py` dumps the
      spec; CI diffs it against the committed `tests/openapi_snapshot.json`
      on every matrix leg. The command-center `contracts.ts` and runtime
      validators (#51/#52 work) prove the frontend still agrees.
      Intentional API changes regenerate the snapshot in the same PR.
- [x] Extract the remaining tickets/intake section as `src/routes/tickets.py`
      (#60), consolidating the tenant access/scoping helpers into
      `src/deps.py` and moving `AI_ADAPTER` beside its only consumer.
      `src/api.py`: 3,158 -> 605 lines — app assembly, middleware,
      idempotency aliases, service routes, and the durable-jobs section.
      Lesson from #56/#58/#60, applied going forward: after moving code,
      tests that monkeypatch module attributes must repoint at the module
      that now owns the callable — including `original = api.X` attribute
      reads and PostgreSQL-only variants that only CI exercises (the
      postgres outbox test missed exactly this until CI caught it).

### 1b. Offboarding DAG parity (closes gap 4) — verified, docs corrected

Found already implemented during execution: the ledger consults
`workflow.missing_prerequisites` and rejects skipped steps, and
`offboarding-dag` is registered in `TEMPLATES`. What was missing is now done:

- [x] Edge-order tests on the ledger: out-of-order completion returns 409
      naming the blocker and leaves no ledger row; a skipped step returns 409
      (`tests/test_offboarding.py`).
- [x] Stale "flat template on purpose" caveat removed from
      `docs/architecture.md` and the `src/onboarding.py` comment; flat
      templates remain the defaults for backward compatibility.
- [ ] Optional deepening: ledger replay after a runtime definition migration,
      and PostgreSQL variants of the two new tests.

### 1c. One operator console (closes gap 2, sets up Phase 2)

Decide the trajectory explicitly: **the React command center is the operator
console; the Jinja console is frozen and then retired.**

- [ ] Write the parity checklist into `command-center/README.md` — pages
      missing today: analytics, triggers (+ connector webhook surface),
      workflow jobs, notifications/outbox, knowledge base, workforce module.
      Each port = typed contract in `contracts.ts` + runtime response
      validation + a browser-smoke journey step (the patterns #51–#53 already
      established).
- [ ] Port in that order (operator value × frequency): workflow jobs →
      notifications → analytics → triggers → knowledge base → workforce.
- [ ] Keep `/dashboard` (public monitoring) and `/` (landing) server-rendered;
      they are not operator surfaces.
- [ ] Only after parity + browser tests pass: hide Jinja `/ui/*` routes behind
      a flag for one release, then remove them. Never two live consoles.

**Definition of done:** OpenAPI diff green in CI; offboarding DAG selectable
with parity tests; command-center covers every operator page with typed
contracts; a documented flag flips the Jinja console off.

---

## Phase 2 — Open the release gates (2–4 weeks, partly external)

These are the project's *own* declared gates (PR #51–#53, console-monitoring).
"Deployment-ready" means every box here is checked with evidence, not vibes.

### 2a. Production identity (gate 1, the hardest)

- [ ] Configure a real Entra app registration (interactive browser sign-in or
      gateway-issued per-user tokens; the console deliberately does not
      implement its own login flow).
- [ ] Verify the gateway strips client-supplied identity headers
      (`X-User-Role`, `X-User-Email`, `X-Actor-Email`) — there is already a
      security regression suite in `command-center/server/*.test.mjs`; extend
      it to prove strip-before-forward with a fake upstream.
- [ ] Lift the workforce browser release gate (`docs/console-monitoring.md`)
      only after an independently tested sign-in journey exists in the
      browser-smoke suite against a protected backend.

### 2b. Data safety rehearsal (gate 2)

- [ ] Script a backup/restore rehearsal: `pg_dump`/restore into a clean
      stack, `alembic upgrade head` replay, row-count + spot-check
      reconciliation, measure the window. Document as
      `docs/runbooks/backup-restore.md` with the actual commands and timings.
- [ ] Run the manual-ticket migration (0016) on a restored copy before ever
      running it on "real" data — the command-center README already requires
      this; make the rehearsal the proof.

### 2c. Accessibility audit (gate 3)

- [ ] CI already runs axe-core checks; add a manual pass (keyboard-only,
      screen reader) over the top five command-center flows, file findings as
      issues, fix, and record the audit date + tool versions in
      `docs/console-monitoring.md`.

### 2d. Representative performance + migration validation (gate 4)

- [ ] Use `python -m src.bench` and `scripts/benchmark_console.py` against a
      Postgres-backed staging stack (not SQLite) with the acceptance workload
      (#52: 250+ tickets, 50 runs). Record real p95 numbers and set alert
      thresholds from them — this is what `docs/console-monitoring.md` says
      the provisional thresholds are waiting for.
- [ ] Rehearse the full migration chain (0001 → head) on a
      representative-size database; record duration.

### 2e. Live connectors (gate 5, optional by design)

- [ ] Keep simulated adapters as the default. Wire one real integration
      end-to-end (Microsoft Graph is the natural first: credentials +
      tenant + one onboarding action) behind `.env`, with the tenant-scoped
      webhook keys already supported.
- [ ] Write per-source setup runbooks (`docs/runbooks/connectors-<source>.md`)
      and add real incremental cursors where the source supports them
      (closes gap 8: Jira `updated>=`, ServiceNow `sys_updated_on`, Slack
      `ts` high-water marks — replacing the batch-count cursor).

### 2f. Operate the monitoring (closes gap 7)

- [ ] Install the Prometheus + Grafana stack from `docker/` with the shipped
      alert rules; replace provisional thresholds with the staging-measured
      ones from 2d.
- [ ] Add one synthetic canary (workflow run per hour) so "silence" is
      distinguishable from "healthy".

**Definition of done:** each gate has a dated evidence artifact (test output,
rehearsal log, audit report, measured thresholds) linked from
`docs/console-monitoring.md`, and the "not authorized" caveats in
`command-center/README.md` are updated by the PR that lifts each gate.

---

## Phase 3 — Reliability depth (ongoing, behind the gates)

Scheduled owners for the documented limits (gap 6), in priority order:

- [ ] **Multi-process rate limiting — decision recorded 2026-11-28: deferred
      until a multi-replica deployment exists.** The limiter is off by
      default (`FLOWOPS_RATE_LIMIT_PER_MINUTE` unset) and single-process by
      design; Phase 2's staging deployment is the earliest point where the
      limitation becomes real. Implement then: a DB-backed token bucket
      (no new infra — SQLAlchemy + conditional updates, same fencing pattern
      as jobs), keeping the in-process path for single-process dev.
- [ ] **Delivery reconciliation ledger.** At-least-once webhook delivery is
      fine, but make duplicates *visible*: record delivery attempts keyed by
      event fingerprint (the `events.fingerprint` column already exists) so
      operators can query "what did we actually send?" and receivers get a
      documented dedupe key per event type. Sequencing note: land this with
      the Phase 2e live-connector work, where real delivery traffic first
      makes duplicate visibility matter.
- [ ] **Clock-skew-tolerant leases.** Documented limit: wall-clock takeover.
      Evaluate DB-time (`now()`) lease comparisons for Postgres and
      monotonic-heartbeat staleness for the worker; if adopted, note it as
      the one place SQLite/Postgres semantics intentionally differ.
- [x] **Chaos drills.** `tests/test_chaos_drills.py`: takeover storm (eight
      concurrent claimers, exactly one winner, recovery completes at
      attempts == 2), scheduler killed mid-tick (lease takeover enqueues each
      ready step exactly once), outbox worker killed mid-delivery (claimed
      row is skipped until its lease expires, then delivered exactly once).
      Gate held: a simulated crash never loses a committed side effect and
      never double-fires a lease.
- [x] **Coverage instrumentation.** `pytest-cov` added to
      `requirements-dev.in`; floor measured from the real baseline and
      enforced by the CI `coverage` job plus `scripts/coverage.sh`:
      **83%** (baseline 83.6% on 2026-11-28). Raise the floor as coverage
      improves; never lower it to make a PR pass. The 14 `pragma: no cover`
      markers are still unaudited — review them when touching those files.

---

## Phase 4 — Product surface (portfolio value; overlaps Phase 2)

The README's last unchecked roadmap item is "v1.0 — Public deployment and
screenshots". This phase is that item, made concrete:

- [ ] **Deploy a public demo**: small VPS/Fly/Render instance, Postgres,
      `FLOWOPS_AUTH_REQUIRED=true`, seeded demo tenant, read-only demo
      credentials, simulated adapters. HTTPS + the gateway pattern from 2a.
      *Decision recorded 2026-11-28: Fly.io is the primary target (one app
      with `[processes]` groups fits the worker/scheduler/timers/outbox
      topology; `deploy/fly.toml` + `deploy/README.md`), Render is the
      fallback. Follow-up decision: Fly runs with container-start migrations
      disabled (six machines would race alembic) — moving "migrations as
      release step" into the release pipeline properly is still open.*
- [ ] **Screenshot pack** for the README/portfolio: landing, dashboard,
      command center (runs/definitions/analytics), an approval journey, and
      the analytics page — generated from the deployed demo, stored in-tree
      or linked.
- [ ] **Guided demo script**: extend `scripts/demo_smoke.sh` into a
      "day in the life" offline journey — ticket in → trigger fires →
      workflow runs → approval gate → SLA timer escalates → analytics shows
      the result — with narration matching `docs/portfolio-demo.md`.
- [ ] **MCP surface expansion**: cover workflow definitions, runs, and
      trigger read paths in `src/mcp_server.py` (read-only first,
      `FLOWOPS_MCP_ALLOW_WRITES` already gates the rest), so "AI agent
      operates the platform" is demonstrable live, not just documented.
- [ ] **Automation-library growth**: one new exported flow per bridge
      (n8n / Power Automate / Rewst) that exercises triggers → workflow,
      proving the "move workflows between tools" pitch in the README.

---

## Explicit non-goals

- No Celery/Kafka/Redis/Terraform/Kubernetes — the durable-jobs + leases +
  outbox pattern is the architecture; new infrastructure needs a written
  decision here first.
- No rewrite of the workflow engine, expressions evaluator, or scheduler —
  they are tested, hardened, and documented.
- No production tenant data, no real PII in seeds, no credentialed CI.
- No second workflow engine in the React console; it is a client for the
  FastAPI boundary, full stop.

## Risk register

| Risk | Mitigation |
|------|------------|
| Concurrent IDE/bot work produces parallel rewrites (it already happened once) | This file is the single plan of record; PR bodies must tick its boxes; handoff prompt kept accurate (Phase 0) |
| `src/api.py` monolith causes merge conflicts between parallel efforts | Phase 1a decomposition first; OpenAPI-diff CI guard |
| Jinja/React consoles drift silently | Parity checklist lives in `command-center/README.md`; one console trajectory decided in 1c |
| Local venv drift masquerading as repo bugs | Phase 0 pinned-install refresh note; CI remains the authority |
| Release gates lifted without evidence | Each gate needs a dated artifact linked from `docs/console-monitoring.md` |
| Docs rot (already at 459-vs-321) | Counts removed from prose; structure section reviewed at each phase boundary |

## Sequencing summary

```
Phase 0  ──►  Phase 1a (API split) ──►  Phase 1b/1c (engine parity, one console)
   │                                            │
   └──►  docs debt (same week)                 ▼
                                    Phase 2 gates (identity → data safety →
                                    a11y → performance → connectors → monitoring)
                                                 │
                                                 ▼
                              Phase 3 reliability depth (ongoing)
                                                 │
                                                 ▼
                              Phase 4 public demo + screenshots (after 2a/2b)
```

Phases 3 and 4 can interleave with Phase 2 once 1c lands; nothing in Phase 4
ships publicly before the identity and backup gates (2a, 2b) are closed.
