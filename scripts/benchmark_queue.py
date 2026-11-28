"""Synthetic PostgreSQL queue contention and recovery evidence (disposable DB only)."""
import json
import os
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from src import db, jobs

# Each phase adds a burst at an explicit logical arrival time. Workers perform
# a fixed number of claim attempts; later phases advance the logical clock.
PHASES = (
    ("steady", 12, 16, 4),
    ("overload", 48, 16, 8),
    ("sustained", 24, 24, 8),
    ("drain", 0, 120, 8),
)
RESOURCE_TYPE = "synthetic-queue-benchmark"


def main():
    if os.environ.get("FLOWOPS_DISPOSABLE_BENCHMARK") != "1":
        raise RuntimeError("Queue benchmark requires an explicitly disposable database.")
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development") != "development":
        raise RuntimeError("Queue benchmark requires development mode.")
    if db.engine.dialect.name != "postgresql":
        raise RuntimeError("Queue benchmark requires PostgreSQL.")
    for adapter in ("ACTION", "AI", "AUTOMATION"):
        if os.environ.get(f"FLOWOPS_{adapter}_ADAPTER", "simulated") != "simulated":
            raise RuntimeError("Queue benchmark requires simulated adapters.")

    with db.SessionLocal() as session:
        if session.execute(select(db.WorkflowJobRecord.id).limit(1)).first():
            raise RuntimeError("Queue benchmark requires an empty job table.")

    start = db.utcnow() + timedelta(seconds=1)
    report = {"schema_version": 1, "git_sha": os.environ.get("GITHUB_SHA"),
              "environment": "disposable_postgresql_logical_time_queue",
              "acceptance": {"lost_jobs": 0, "duplicate_finalizations": 0, "unexpected_errors": 0,
                             "final_backlog": 0, "max_attempts": 3},
              "phases": []}
    ids = set()
    crashes = {}
    finalized = set()
    outcomes = Counter()
    failures = []

    def work(args):
        worker_index, now, phase = args
        started = time.perf_counter()
        with db.SessionLocal() as session:
            job = jobs.claim_job(session, f"benchmark-{worker_index}", now=now, lease_seconds=1)
            if job is None:
                return ("empty", None, None, (time.perf_counter() - started) * 1000)
            job_id, token, sequence = job.id, job.lease_owner_token, json.loads(job.payload_json)["sequence"]
            if job.resource_type != RESOURCE_TYPE or not token:
                raise RuntimeError("Unexpected job claimed in disposable benchmark.")
            if sequence % 11 == 0 and job.attempts == 1 and phase != "drain":
                return ("crash", job_id, token, (time.perf_counter() - started) * 1000)
            if sequence % 7 == 0 and job.attempts == 1 and phase != "drain":
                status = jobs.fail_job(session, job_id, token, "synthetic retry", now=now, delay_seconds=1)
                if status != "retry_scheduled":
                    raise RuntimeError("Retry transition was not accepted.")
                outcome = "retry"
            elif sequence % 13 == 0:
                status = jobs.fail_job(session, job_id, token, "synthetic terminal error", retryable=False, now=now)
                if status != "failed":
                    raise RuntimeError("Terminal failure transition was not accepted.")
                outcome = "failed"
            else:
                if not jobs.complete_job(session, job_id, token, result={"synthetic": True}, now=now):
                    raise RuntimeError("Completion fence rejected a current owner.")
                outcome = "succeeded"
            session.commit()
            return (outcome, job_id, token, (time.perf_counter() - started) * 1000)

    try:
        sequence = 0
        for phase_index, (name, arrivals, budget, concurrency) in enumerate(PHASES):
            now = start + timedelta(seconds=10 * phase_index)
            with db.SessionLocal() as session:
                for _ in range(arrivals):
                    sequence += 1
                    row = jobs.enqueue_job(session, job_type=jobs.JOB_ONBOARDING_ITEM,
                                           payload={"sequence": sequence}, resource_type=RESOURCE_TYPE,
                                           idempotency_key=f"queue-benchmark-{sequence}", max_attempts=3)
                    row.available_at = now
                    ids.add(row.id)
                session.commit()
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                rows = list(pool.map(work, [(i, now, name) for i in range(budget)]))
            elapsed = time.perf_counter() - started
            counts = Counter(row[0] for row in rows)
            outcomes.update(counts)
            for outcome, job_id, token, _ in rows:
                if outcome == "crash":
                    crashes[job_id] = token
                if outcome in ("succeeded", "failed"):
                    if job_id in finalized:
                        failures.append("duplicate_finalization")
                    finalized.add(job_id)
            durations = sorted(row[3] for row in rows if row[0] != "empty")
            with db.SessionLocal() as session:
                records = session.execute(select(db.WorkflowJobRecord)).scalars().all()
                state = Counter(row.status for row in records)
            terminal = state["succeeded"] + state["failed"]
            result = {"phase": name, "logical_arrivals": arrivals, "logical_time_seconds": phase_index * 10,
                      "concurrency": concurrency, "claim_attempt_budget": budget, "outcomes": dict(counts),
                      "backlog": len(records) - terminal, "terminal_jobs": terminal,
                      "elapsed_seconds": round(elapsed, 4),
                      "terminal_jobs_per_second": round((counts["succeeded"] + counts["failed"]) / elapsed, 2),
                      "operation_p50_ms": round(statistics.median(durations), 2) if durations else None,
                      "operation_p95_ms": round(durations[(95 * len(durations) + 99) // 100 - 1], 2) if durations else None,
                      "operation_p99_ms": round(durations[(99 * len(durations) + 99) // 100 - 1], 2) if durations else None}
            report["phases"].append(result)
            print("FLOWOPS_QUEUE_BENCHMARK " + json.dumps(result), flush=True)

        with db.SessionLocal() as session:
            rows = session.execute(select(db.WorkflowJobRecord)).scalars().all()
            if {row.id for row in rows} != ids or finalized != ids:
                failures.append("lost_or_unfinished_jobs")
            if any(row.status not in ("succeeded", "failed") or row.attempts > row.max_attempts for row in rows):
                failures.append("invalid_final_state")
            if not crashes or not outcomes["retry"] or not outcomes["failed"]:
                failures.append("missing_failure_injection")
            if report["phases"][1]["backlog"] <= report["phases"][0]["backlog"]:
                failures.append("overload_did_not_grow_backlog")
            for job_id, token in crashes.items():
                if jobs.complete_job(session, job_id, token, result={"stale": True}):
                    failures.append("stale_owner_finalized")
            session.rollback()
        report["total_jobs"] = len(ids)
        report["outcomes"] = dict(outcomes)
        report["expired_owner_rejections"] = len(crashes)
        if failures:
            raise RuntimeError("Queue benchmark invariant failure: " + ", ".join(failures))
    except Exception as error:
        report["error_class"] = type(error).__name__
        raise
    finally:
        report["invariant_failures"] = failures
        Path("benchmark-queue.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
