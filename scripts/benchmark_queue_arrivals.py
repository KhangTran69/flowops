"""Paced, wall-clock synthetic queue workload on a disposable PostgreSQL database.

This intentionally exercises queue mechanics, not provider effects or staging SLOs.
"""
import json
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from src import db, jobs

PHASES = (("steady", 4, 2), ("burst", 48, 2), ("sustained", 12, 4))
WORKERS = 4
SERVICE_SECONDS = 0.15
DRAIN_DEADLINE = 40
RESOURCE = "arrival-benchmark"
P95_LIMIT_MS = 25000


def percentiles(samples):
    values = sorted(samples)
    return {f"p{p}_ms": round(values[math.ceil(p * len(values) / 100) - 1], 2) if values else None
            for p in (50, 95, 99)}


def main():
    if (os.environ.get("FLOWOPS_DISPOSABLE_BENCHMARK") != "1"
            or os.environ.get("FLOWOPS_ENVIRONMENT", "development") != "development"
            or db.engine.dialect.name != "postgresql"):
        raise RuntimeError("Arrival benchmark requires disposable development PostgreSQL.")
    for adapter in ("ACTION", "AI", "AUTOMATION"):
        if os.environ.get(f"FLOWOPS_{adapter}_ADAPTER", "simulated") != "simulated":
            raise RuntimeError("Arrival benchmark requires simulated adapters.")
    with db.SessionLocal() as session:
        table = db.WorkflowJobRecord
        if session.execute(select(table.id).where(
                (table.resource_type == RESOURCE) | (~table.status.in_(("succeeded", "failed", "cancelled")))
        ).limit(1)).first():
            raise RuntimeError("Arrival benchmark requires no active or previously seeded arrival jobs.")

    report = {"schema_version": 1, "git_sha": os.environ.get("GITHUB_SHA"),
              "environment": "disposable_postgresql_wall_clock_queue",
              "workers": WORKERS, "synthetic_service_seconds": SERVICE_SECONDS,
              "thresholds": {"unexpected_errors": 0, "duplicate_terminal_results": 0,
                             "final_backlog": 0, "max_attempts": 3,
                             "drain_seconds": DRAIN_DEADLINE, "end_to_end_p95_ms": P95_LIMIT_MS},
              "phases": []}
    stop = threading.Event()
    lock = threading.Lock()
    arrivals = {}
    final = {}
    errors = []
    counters = Counter()
    operation_samples = []
    abandoned = {}
    started = time.monotonic()

    def consumer(index):
        while not stop.is_set():
            try:
                op_start = time.monotonic()
                with db.SessionLocal() as session:
                    job = jobs.claim_job(session, f"arrival-worker-{index}", lease_seconds=2)
                    if job is None:
                        stop.wait(0.02)
                        continue
                    job_id, token = job.id, job.lease_owner_token
                    sequence = json.loads(job.payload_json)["sequence"]
                    if job.resource_type != RESOURCE or not token:
                        raise RuntimeError("Unexpected job claimed.")
                    if sequence % 17 == 0 and job.attempts == 1:
                        with lock:
                            abandoned[job_id] = token
                            counters["abandoned"] += 1
                        continue
                    stop.wait(SERVICE_SECONDS)
                    if sequence % 7 == 0 and job.attempts == 1:
                        state = jobs.fail_job(session, job_id, token, "synthetic retry",
                                              delay_seconds=1)
                        if state != "retry_scheduled":
                            raise RuntimeError("Unexpected retry transition.")
                        outcome = "retry"
                    elif sequence % 19 == 0:
                        state = jobs.fail_job(session, job_id, token, "synthetic terminal failure",
                                              retryable=False)
                        if state != "failed":
                            raise RuntimeError("Unexpected failure transition.")
                        outcome = "failed"
                    else:
                        if not jobs.complete_job(session, job_id, token, result={"synthetic": True}):
                            raise RuntimeError("Unexpected completion rejection.")
                        outcome = "succeeded"
                    session.commit()
                    finished = time.monotonic()
                    with lock:
                        counters[outcome] += 1
                        operation_samples.append((finished - op_start) * 1000)
                        if outcome != "retry":
                            if sequence in final:
                                errors.append("duplicate_terminal_result")
                            final[sequence] = finished
            except Exception as error:
                with lock:
                    errors.append(type(error).__name__)
                stop.set()
                return

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(consumer, index) for index in range(WORKERS)]
            try:
                sequence = 0
                for name, rate, duration in PHASES:
                    phase_start = time.monotonic()
                    count_before = sequence
                    for offset in range(rate * duration):
                        if stop.is_set():
                            raise RuntimeError("Consumer failed.")
                        stop.wait(max(0, phase_start + offset / rate - time.monotonic()))
                        sequence += 1
                        with lock:
                            arrivals[sequence] = time.monotonic()
                        with db.SessionLocal() as session:
                            jobs.enqueue_job(session, job_type=jobs.JOB_ONBOARDING_ITEM,
                                             payload={"sequence": sequence}, resource_type=RESOURCE,
                                             idempotency_key=f"{RESOURCE}-{sequence}", max_attempts=3)
                            session.commit()
                    stop.wait(max(0, phase_start + duration - time.monotonic()))
                    elapsed = time.monotonic() - phase_start
                    with lock:
                        result = {"phase": name, "target_arrivals_per_second": rate,
                                  "arrivals": sequence - count_before,
                                  "observed_arrivals_per_second": round((sequence-count_before)/elapsed, 2),
                                  "elapsed_seconds": round(elapsed, 3),
                                  "backlog": len(arrivals) - len(final),
                                  "terminal": len(final), "outcomes": dict(counters)}
                    report["phases"].append(result)
                    print("FLOWOPS_ARRIVAL_BENCHMARK " + json.dumps(result), flush=True)
                drain_started = time.monotonic()
                while time.monotonic() - drain_started < DRAIN_DEADLINE and not stop.is_set():
                    with lock:
                        if len(final) == len(arrivals):
                            break
                    stop.wait(0.05)
                report["drain_seconds"] = round(time.monotonic() - drain_started, 3)
            finally:
                stop.set()
                for future in futures:
                    future.result()

        with db.SessionLocal() as session:
            rows = session.execute(select(db.WorkflowJobRecord).where(
                db.WorkflowJobRecord.resource_type == RESOURCE)).scalars().all()
            if len(rows) != len(arrivals) or any(row.status not in ("succeeded", "failed")
                                                 or row.attempts > row.max_attempts for row in rows):
                errors.append("invalid_or_unfinished_database_state")
            for job_id, token in abandoned.items():
                if jobs.complete_job(session, job_id, token, result={"stale": True}):
                    errors.append("stale_owner_finalized")
            session.rollback()
        elapsed = time.monotonic() - started
        report.update({"arrivals": len(arrivals), "terminal_jobs": len(final),
                       "final_backlog": len(arrivals) - len(final), "outcomes": dict(counters),
                       "terminal_jobs_per_second": round(len(final) / elapsed, 2),
                       "end_to_end_latency": percentiles([(end-arrivals[seq])*1000 for seq,end in final.items()]),
                       "claim_and_finalize_latency": percentiles(operation_samples),
                       "stale_owner_rejections": len(abandoned)})
        if set(final) != set(arrivals):
            errors.append("lost_or_unfinished_jobs")
        if report["phases"][1]["backlog"] <= report["phases"][0]["backlog"]:
            errors.append("burst_did_not_grow_backlog")
        if not all(counters[key] for key in ("retry", "abandoned", "failed")):
            errors.append("missing_failure_injection")
        if report["end_to_end_latency"]["p95_ms"] is None or report["end_to_end_latency"]["p95_ms"] > P95_LIMIT_MS:
            errors.append("synthetic_latency_threshold_exceeded")
        if report["drain_seconds"] >= DRAIN_DEADLINE:
            errors.append("drain_deadline_exceeded")
        print("FLOWOPS_ARRIVAL_RESULT " + json.dumps(report), flush=True)
        if errors:
            raise RuntimeError("Arrival benchmark invariant failure: " + ", ".join(errors))
    except Exception as error:
        report["error_class"] = type(error).__name__
        raise
    finally:
        report["invariant_failures"] = errors
        Path("benchmark-queue-arrivals.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
