# Runbook: an onboarding run failed

1. Open **Workflow runs** and find the run; note the failing step and error.
2. Check the step's **Attempts** and **Output** panels. A 4xx usually means bad
   input, not a transient failure.
3. Verify the request's pinned definition version and its prerequisites.
4. If the failure is an adapter outage, fix the adapter/credential and retry the
   failed job from **Runs**. Do not mark the step completed manually.
5. If a prerequisite is wrong, correct the request or migrate it to a new
   definition version; never edit completed steps in place.
6. Record the incident in the audit note and notify the requester.
7. Confirm dependent steps are `cascade_blocked`, not silently skipped.
