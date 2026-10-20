# AI agents and human approval

FlowOps uses AI as a bounded assistant, not an autonomous change engine.

- `POST /tickets/{id}/ai-triage` returns a summary, knowledge-base matches,
  urgency signals, and recommendations without mutating the ticket.
- `POST /tickets/{id}/ai-recommendations/apply` re-runs triage and applies only
  explicitly approved queue/priority recommendation IDs.
- MCP exposes read-oriented triage and KPI tools for Claude and other agents.

Before provider calls, ticket text is redacted and bounded. Model output is
validated against an allowlist; only supported queue and priority changes can
be applicable, and status is never changed by AI. Provider failures are
visible as 503 responses and audit/metrics entries.

Recommended operating pattern: let an agent inspect and explain, show the
proposed change to an operator, require explicit approval, then let FlowOps
perform the idempotent mutation and record the actor/correlation ID.
