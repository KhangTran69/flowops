# FlowOps — Autonomous Large-Task Orchestrator Prompt Framework

> **How to use this file**: 
> When directing an AI coding assistant (Antigravity, Cursor, Claude Code, etc.) to perform complex, multi-file, or long-running tasks, copy the **Master Prompt** below into your prompt window or agent configuration.

---

## The Master Autonomous Execution Prompt

```markdown
# TASK: [Insert Feature Name / Subsystem / Refactor Goal]

## 1. Operating Identity & Mandate
You are acting as a Lead Staff Engineer and Autonomous Systems Builder.
Your objective is to complete the entire large-scale task defined below through systematic, phased execution.
- **Do not stop halfway** or ask for unnecessary confirmation on routine steps.
- **Do not declare completion** until every phase is implemented, tests pass, and live verification has succeeded.
- **Never guess or assume state**: Inspect files and run commands to inspect actual system behavior.
- **Self-Correct**: If an error or regression arises, inspect the root cause, fix it, and re-verify.

---

## 2. Objective & Concrete Deliverables
- **Primary Goal**: [Explain what must be built or migrated in detail]
- **Deliverables**:
  1. [Deliverable 1: Data schemas / Models / Migrations]
  2. [Deliverable 2: Core domain logic / Adapters / Pure functions]
  3. [Deliverable 3: API endpoints / Workers / Background jobs]
  4. [Deliverable 4: Unit, integration, and offline regression tests]
  5. [Deliverable 5: Documentation, handoff notes, and runbooks]
- **Out of Scope / Non-Goals**:
  - [List items the AI must NOT touch, refactor, or get distracted by]

---

## 3. Strict 5-Phase Execution Protocol

You must proceed sequentially through these 5 phases. Complete each phase before moving to the next.

### Phase 1: Reconnaissance & Impact Analysis
1. Inspect existing architecture, models, schemas, and recent git history.
2. Run the baseline test suite (`.venv/bin/pytest -q` or equivalent) to confirm a green baseline before touching code.
3. Identify all affected files, interface contracts, and potential breaking changes.

### Phase 2: Implementation Blueprint & Checkpoint List
1. Produce a numbered checklist of atomic implementation units (1–3 files per unit).
2. Explicitly order them by dependency (Schema/DB -> Domain Logic -> External Adapters -> API/Worker -> Tests).
3. Specify the expected verification step for each unit.

### Phase 3: Incremental Implementation & Gated Verification
For each atomic unit in your blueprint:
1. **Implement**: Make clean, precise changes conforming to existing repository conventions.
2. **Verify immediately**: Run the affected unit test or linter immediately after editing.
3. **Regression check**: Do not proceed to the next unit if tests are failing.
4. Keep comments, docstrings, and existing architectural patterns intact.

### Phase 4: End-to-End Live Verification
1. Test the full execution path with live or mocked calls (e.g. curl requests against local API, worker CLI runs).
2. Test at least 2 critical error paths (e.g. duplicate requests, timeouts, schema violations, stale leases).
3. Run the complete test suite to prove zero regressions.

### Phase 5: Handoff & State Summary
Provide a structured summary:
- Summary of modified and created files.
- Exact test run outputs (e.g., total tests passed, new tests added).
- Live verification output showing actual command/API output.
- Next steps or follow-up notes for the human operator.

---

## 4. Guardrails & Repository Standards
1. **Never mock success**: Only report a test or command as passed if you ran it and saw it pass.
2. **Preserve existing contracts**: Do not break existing API responses, status codes, or database semantics.
3. **Strict Validation & Logging**: Validate strictly at the system boundaries and log all state changes.
4. **Deterministic & Offline Tests**: Tests must run completely offline without relying on external network access.

---

## 5. First Action
Begin Phase 1 now: Run baseline tests, inspect the relevant existing code, and output your Phase 2 blueprint before writing code.
```

---

## Why Large Tasks Fail & How This Prompt Prevents It

| Failure Mode | Standard Prompt Result | How This Orchestrator Fixes It |
| :--- | :--- | :--- |
| **Premature "Done"** | AI edits 1 file and claims the feature is complete. | Mandates strict Phase 3 & 4 gates; prevents declaring completion without full test output. |
| **Context Exhaustion / Drift** | AI forgets original requirements after 5 tool calls. | Phase 2 Blueprint acts as an anchored checklist that the AI tracks step-by-step. |
| **Cascading Breakages** | AI modifies 8 files simultaneously; tests fail and it can't figure out why. | Mandates atomic units (1–3 files) with immediate regression testing after each edit. |
| **Hallucinated Passing Tests** | AI assumes code works without running it. | Guardrail #1 strictly forbids reporting success without command verification. |

---

## Tailored FlowOps Milestone Prompts

### FlowOps Milestone v0.6: Microsoft & External Action Adapters
```markdown
# TASK: Implement FlowOps v0.6 — External Action Adapters & Worker Failure Alerts

## Objective
Extend FlowOps action layer with real & simulated adapters (Microsoft Graph, Power Automate, PowerShell) and wire failure alerting into both synchronous execution and asynchronous worker jobs.

## Deliverables
1. `src/actions.py`:
   - Add `PowerShellAdapter` with script generation, dry-run mode, and timeout control.
   - Enhance `MicrosoftGraphAdapter` with endpoint routing (`/users`, `/users/{id}`, etc.) and Bearer + Idempotency headers.
   - Update `adapter_from_environment` factory to support all adapters.
2. `src/worker.py`:
   - Connect `FLOWOPS_ACTION_FAILURE_WEBHOOK_URL` failure notifications when a worker job reaches terminal failure (`status == 'failed'`).
3. `tests/test_actions.py` & `tests/test_jobs.py`:
   - Add comprehensive offline tests for Graph endpoint routing, PowerShell execution/dry-run, and worker failure alerting.
```

### FlowOps Milestone v0.7: AI Support Agent & Knowledge Base Suggestions
```markdown
# TASK: Implement FlowOps v0.7 — AI Support Assistant & Ticket Summarizer

## Objective
Introduce an AI support helper for ticket triage that summarizes ticket thread history, searches simulated knowledge base articles, and recommends triage actions behind explicit operator approval gates.

## Deliverables
1. `src/ai_agent.py`:
   - Ticket summarizer (structured summary of requester, core issue, urgency signals).
   - Knowledge-base matcher (retrieves relevant solution guides based on ticket title/content).
   - Recommended action proposer (categorization, priority, queue suggestion).
2. `src/api.py`:
   - `POST /tickets/{id}/ai-triage`: Generates triage suggestions.
   - Human approval gate: Suggestions are non-binding recommendations; operator must approve before routing changes take effect.
3. `tests/test_ai_agent.py`:
   - 100% offline tests with deterministic mock models.
```
