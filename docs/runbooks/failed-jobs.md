# Runbook: failed (dead-letter) jobs

FlowOps retries a temporary failure (timeouts, throttling, Microsoft outages)
up to 5 times with growing waits. A job is marked **failed** when:
- it ran out of retries, or
- the error can never fix itself by waiting (bad input, missing licence, no permission).
  These fail on the first attempt on purpose, so you hear about them straight away.

## What to do
1. Open **Jobs** and filter on *failed*. The morning Teams digest lists them too.
2. Read *Last error* and look it up in [onboarding-failure.md](onboarding-failure.md).
3. Fix the cause, then press **Retry** once.
4. If the work is no longer needed (for example the starter withdrew), press **Cancel** (admin only) and note why on the ticket.

## Escalate when
More than 5 jobs fail with the same error in an hour. That usually means a
platform problem (expired secret, Microsoft outage), not a one-off.
Check https://status.cloud.microsoft first.
