-- FlowOps hand-written reporting queries.
-- Run with psql (PostgreSQL) or sqlite3 against data/flowops.db.
-- The v_* views come from migration 0009 (src/reports.py).

-- 1. Automation success rate per action over the last 7 days.
SELECT action,
       SUM(runs)                                            AS runs,
       SUM(failed)                                          AS failed,
       ROUND(100.0 * SUM(succeeded) / NULLIF(SUM(runs), 0), 1) AS success_pct,
       ROUND(AVG(avg_duration_ms), 0)                       AS avg_ms
FROM v_automation_daily
WHERE day >= date('now', '-7 day')          -- PostgreSQL: CURRENT_DATE - 7
GROUP BY action
ORDER BY failed DESC, runs DESC;

-- 2. Which adapters need the most retries? (reliability / manual intervention)
SELECT adapter, action, attempts, retries, failures,
       ROUND(100.0 * first_try_successes / NULLIF(attempts, 0), 1) AS first_try_pct
FROM v_action_reliability
ORDER BY failures DESC, retries DESC;

-- 3. Open-ticket backlog per client tenant and queue, oldest first.
SELECT tenant_key, queue, priority, open_tickets, oldest_created_at
FROM v_ticket_backlog
ORDER BY oldest_created_at;

-- 4. Hours of manual work saved per tenant (business KPI for stakeholders).
SELECT tenant_key,
       SUM(automated_steps)               AS automated_steps,
       ROUND(SUM(minutes_saved) / 60.0, 1) AS hours_saved
FROM v_time_saved
GROUP BY tenant_key
ORDER BY hours_saved DESC;

-- 5. Dead-letter jobs that still need an operator (joins jobs to their request).
SELECT j.id, j.label, j.attempts, j.last_error, r.employee_email, r.tenant_key
FROM workflow_jobs AS j
LEFT JOIN onboarding_requests AS r
       ON j.resource_type = 'onboarding' AND CAST(r.id AS TEXT) = j.resource_id
WHERE j.status = 'failed'
ORDER BY j.created_at DESC;

-- 6. Onboarding lead time: request created -> last checklist step finished.
SELECT r.tenant_key, r.request_type, r.id, r.created_at, MAX(c.updated_at) AS finished_at
FROM onboarding_requests AS r
JOIN checklist_items AS c ON c.request_id = r.id
WHERE r.status = 'completed'
GROUP BY r.tenant_key, r.request_type, r.id, r.created_at
ORDER BY finished_at DESC;

-- 7. PostgreSQL only: the same per-tenant KPI roll-up as a stored function.
-- SELECT * FROM flowops_tenant_kpis(now() - interval '30 days');
