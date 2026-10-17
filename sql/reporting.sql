-- FlowOps reporting queries. PostgreSQL syntax; most also work on SQLite.

-- 1. Ticket volume by queue, priority and state.
SELECT queue, priority, status, COUNT(*) AS ticket_count
FROM tickets
GROUP BY queue, priority, status
ORDER BY ticket_count DESC;

-- 2. Open tickets by queue with the oldest first.
SELECT ticket_id, queue, priority, title, created_at
FROM tickets
WHERE status NOT IN ('closed', 'completed', 'cancelled')
ORDER BY created_at ASC;

-- 3. Priority mix by category.
SELECT COALESCE(category, 'uncategorized') AS category,
       priority,
       COUNT(*) AS ticket_count
FROM tickets
GROUP BY COALESCE(category, 'uncategorized'), priority
ORDER BY category, priority;

-- 4. Recent failure/retry activity from the execution ledger.
SELECT action, status, COUNT(*) AS occurrences,
       ROUND(AVG(duration_ms)) AS avg_duration_ms
FROM execution_logs
WHERE status IN ('failure', 'retrying')
GROUP BY action, status
ORDER BY occurrences DESC;

-- 5. The reporting function created by migration 0009 (PostgreSQL).
-- SQLite uses the equivalent view.
SELECT * FROM flowops_ticket_queue_summary()
ORDER BY ticket_count DESC;
