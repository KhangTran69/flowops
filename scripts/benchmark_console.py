"""Repeatable, isolated API read benchmark; run only on disposable test data."""
import json
import math
import os
import statistics
import time

from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient

from src import api, db


def main():
    if os.environ.get("FLOWOPS_DISPOSABLE_BENCHMARK") != "1":
        raise RuntimeError("Benchmark requires explicitly disposable database.")
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development") != "development":
        raise RuntimeError("Benchmark must run in development with simulated adapters.")
    tenant = "benchmark-fixture"
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
        for path, params in [
            ("/tickets", {"tenant": tenant, "search": "VPN", "limit": 50}),
            ("/workflow-runs", {"tenant": tenant, "limit": 50}),
            ("/command-center/summary", {}),
        ]:
            assert client.get(path, params=params).status_code == 200
            def sample(_):
                started = time.perf_counter()
                response = client.get(path, params=params)
                assert response.status_code == 200, response.text
                return (time.perf_counter() - started)*1000
            for concurrency in (1, 4):
                start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    durations = sorted(executor.map(sample, range(40)))
                elapsed = time.perf_counter() - start
                result = {"endpoint": path, "requests": len(durations),
                          "concurrency": concurrency,
                          "p50_ms": round(statistics.median(durations), 2),
                          "p95_ms": round(durations[math.ceil(.95*len(durations))-1], 2),
                          "requests_per_second": round(len(durations)/elapsed, 2)}
                print("FLOWOPS_BENCHMARK " + json.dumps(result), flush=True)
        # Database is disposable; no production data is used or deleted.


if __name__ == "__main__":
    main()
