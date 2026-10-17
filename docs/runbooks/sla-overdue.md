# Runbook: SLA timer fired / step overdue

1. Open **Approvals** and filter for overdue steps.
2. Check the queue and escalation queue; if the timer service reassigned it,
   confirm the new owner in the step detail.
3. Contact the queue owner with the request ID, step, and elapsed minutes.
4. If the step is blocked by a failed prerequisite, resolve or cancel that
   prerequisite; the dependent step remains blocked until then.
5. Do not disable timers to hide the alert. Fix the underlying queue capacity.
6. Record the outcome in the audit log.
