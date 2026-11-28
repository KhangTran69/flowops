"""Repeatable, isolated API read benchmark; run only on disposable test data."""
import json
import math
import os
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from src import api, db


CORE_READ_TARGET_MS = 500.0


def summarize(samples: list[tuple[float, str]], elapsed: float) -> dict:
    """Summarize all samples, including failures; latency covers successes only."""
    counts = Counter(status for _, status in samples)
    durations = sorted(duration for duration, status in samples if status == "200")

    def percentile(p: float) -> float | None:
        return round(durations[math.ceil(p * len(durations)) - 1], 2) if durations else None

    p95 = percentile(.95)
    return {
        "requests": len(samples),
        "successful": len(durations),
        "errors": len(samples) - len(durations),
        "status_counts": dict(sorted(counts.items())),
        "p50_ms": round(statistics.median(durations), 2) if durations else None,
        "p95_ms": p95,
        "p99_ms": percentile(.99),
        "max_ms": round(durations[-1], 2) if durations else None,
        "requests_per_second": round(len(samples) / elapsed, 2),
        "successful_requests_per_second": round(len(durations) / elapsed, 2),
        "within_provisional_p95_target": p95 is not None and p95 <= CORE_READ_TARGET_MS,
    }


def measure(client, requests: list[tuple[str, dict]], concurrency: int) -> tuple[list, float]:
    def sample(request):
        path, params = request
        started = time.perf_counter()
        try:
            response = client.get(path, params=params)
            status = str(response.status_code)
        except Exception as error:
            # Preserve the class for diagnosis, never a potentially sensitive body.
            status = "exception:" + type(error).__name__
        return path, (time.perf_counter() - started) * 1000, status

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        rows = list(executor.map(sample, requests))
    return rows, time.perf_counter() - started


def main():
    if os.environ.get("FLOWOPS_DISPOSABLE_BENCHMARK") != "1":
        raise RuntimeError("Benchmark requires explicitly disposable database.")
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development") != "development":
        raise RuntimeError("Benchmark must run in development with simulated adapters.")
    for adapter in ("ACTION", "AI", "AUTOMATION"):
        if os.environ.get(f"FLOWOPS_{adapter}_ADAPTER", "simulated") != "simulated":
            raise RuntimeError("Benchmark requires simulated adapters.")
    tenant = "benchmark-fixture"
    results = []
    with TestClient(api.app) as client:
        session = db.SessionLocal()
        try:
            session.add(db.TenantRecord(key=tenant, name='Synthetic benchmark tenant', active=True))
            session.add_all([
                db.TicketRecord(ticket_id=str(80000000+i), requester_id=7,
                    title="Benchmark VPN ticket", status="open", priority="normal",
                    category="network", queue="it-network", tenant_key=tenant)
                for i in range(1000)
            ])
            session.commit()
        finally:
            session.close()
        for i in range(50):
            created = client.post("/onboarding-requests", json={
                "request_type": "onboarding", "employee_name": f"Bench {i}",
                "employee_email": f"bench{i}@example.test", "department": "IT",
                "requester_id": 7, "start_date": "2026-11-27", "tenant_key": tenant,
            })
            assert created.status_code in (200, 201), created.text
            started = client.post(f"/onboarding-requests/{created.json()['id']}/run")
            assert started.status_code == 200, started.text
        endpoints = [
            ("/tickets", {"tenant": tenant, "search": "VPN", "limit": 50}),
            ("/workflow-runs", {"tenant": tenant, "limit": 50}),
            ("/command-center/summary", {}),
        ]
        for path, params in endpoints:
            assert client.get(path, params=params).status_code == 200
            for concurrency in (1, 4):
                rows, elapsed = measure(client, [(path, params)] * 40, concurrency)
                result = {
                    "scenario": "single_endpoint", "endpoint": path, "concurrency": concurrency,
                    **summarize([(ms, status) for _, ms, status in rows], elapsed),
                }
                results.append(result)
                print("FLOWOPS_BENCHMARK " + json.dumps(result), flush=True)
        # A balanced mix of read endpoints shares the same connection pool.
        # This is closed-loop concurrency, not an arrival-rate or queue-drain test.
        rows, elapsed = measure(client, endpoints * 40, 8)
        result = {
            "scenario": "mixed_read", "endpoint": "all", "concurrency": 8,
            **summarize([(ms, status) for _, ms, status in rows], elapsed),
        }
        results.append(result)
        print("FLOWOPS_BENCHMARK " + json.dumps(result), flush=True)
        for path, _ in endpoints:
            result = {
                "scenario": "mixed_read", "endpoint": path, "concurrency": 8,
                **summarize([(ms, status) for endpoint, ms, status in rows if endpoint == path], elapsed),
            }
            results.append(result)
            print("FLOWOPS_BENCHMARK " + json.dumps(result), flush=True)
        # Database is disposable; no production data is used or deleted.
    report_path = os.environ.get("FLOWOPS_BENCHMARK_REPORT")
    if report_path:
        report = {
            "schema_version": 1,
            "environment": "disposable_synthetic_testclient",
            "database": db.engine.dialect.name,
            "tickets": 1000,
            "workflow_runs": 50,
            "git_sha": os.environ.get("GITHUB_SHA"),
            "provisional_core_read_p95_ms": CORE_READ_TARGET_MS,
            "results": results,
        }
        Path(report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if any(result["errors"] for result in results):
        raise RuntimeError("Benchmark returned unsuccessful responses; inspect the status counts.")


if __name__ == "__main__":
    main()
