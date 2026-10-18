"""End-to-end workflow benchmark: ``python -m src.bench``.

Exercises the real engine — definition resolution, DAG scheduling, durable
jobs, and the worker — against a throwaway SQLite database, then reports
throughput and per-request latency percentiles.

It answers a question the unit tests cannot: *how fast is a workflow, and does
it stay correct at volume?* Everything runs in-process with the simulated
action adapter, so the numbers reflect engine overhead rather than network
time. No external service is contacted.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from src import api, db

# Ungated steps with a diamond shape: two independent roots, two dependents.
STEPS: list[dict[str, Any]] = [
    {
        "key": "create_user_account",
        "step": "Create user account",
        "queue": "it-access-management",
        "requires_approval": False,
        "action": "create_user_account",
        "depends_on": [],
        "join": "all",
        "when": [],
    },
    {
        "key": "provision_laptop",
        "step": "Provision laptop and peripherals",
        "queue": "it-hardware",
        "requires_approval": False,
        "action": "provision_hardware",
        "depends_on": [],
        "join": "all",
        "when": [],
    },
    {
        "key": "grant_licenses",
        "step": "Grant software licenses",
        "queue": "it-software",
        "requires_approval": False,
        "action": "grant_software_licenses",
        "depends_on": ["create_user_account"],
        "join": "all",
        "when": [],
    },
    {
        "key": "configure_mailbox",
        "step": "Configure email mailbox",
        "queue": "it-messaging",
        "requires_approval": False,
        "action": "configure_email_mailbox",
        "depends_on": ["create_user_account"],
        "join": "all",
        "when": [],
    },
]


@dataclass
class BenchmarkReport:
    requests: int
    engine_parallelism: int
    steps_per_request: int
    completed: int
    failed: int
    wall_seconds: float
    requests_per_second: float
    steps_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    iterations: int
    latencies_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Report without the raw sample list, for logs and JSON output."""
        data = asdict(self)
        data.pop("latencies_ms")
        return data


def _percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile; deterministic and dependency-free."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    # Nearest-rank: ceil(p * N) - 1, so p50 of four samples is the second.
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _as_utc(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def run_benchmark(
    *,
    requests: int = 25,
    database_url: Optional[str] = None,
    iteration_cap: int = 20_000,
) -> BenchmarkReport:
    """Drive ``requests`` workflows to completion and measure the run."""
    if requests < 1:
        raise ValueError("requests must be >= 1")

    from src import scheduler, worker

    if database_url is None:
        root = Path(tempfile.mkdtemp(prefix="flowops-bench-"))
        database_url = f"sqlite:///{root / 'bench.db'}"
    db.use_database(database_url)
    db._ensure_tables()

    client = TestClient(api.app)
    definition_key = f"bench-{int(time.time() * 1000)}"
    created = client.post(
        "/workflow-definitions",
        json={
            "key": definition_key,
            "name": "Benchmark workflow",
            "request_type": "onboarding",
            "steps": STEPS,
        },
    )
    if created.status_code != 200:
        raise RuntimeError(
            f"definition create failed: {created.status_code} {created.text}"
        )
    published = client.post(
        f"/workflow-definitions/{definition_key}/publish", json={"version": 1}
    )
    if published.status_code != 200:
        raise RuntimeError(
            f"publish failed: {published.status_code} {published.text}"
        )

    request_ids: list[int] = []
    for index in range(requests):
        response = client.post(
            "/onboarding-requests",
            json={
                "request_type": "onboarding",
                "employee_name": f"Benchmark User {index}",
                "employee_email": f"bench{index}@example.com",
                "department": "Benchmark",
                "requester_id": 1,
                "definition_key": definition_key,
            },
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"request create failed: {response.status_code} {response.text}"
            )
        request_ids.append(response.json()["id"])

    for request_id in request_ids:
        started = client.post(f"/onboarding-requests/{request_id}/run")
        if started.status_code != 200:
            raise RuntimeError(f"run start failed: {started.status_code} {started.text}")

    def run_statuses() -> dict[int, str]:
        statuses: dict[int, str] = {}
        for request_id in request_ids:
            payload = client.get(f"/onboarding-requests/{request_id}/run").json()
            statuses[request_id] = payload.get("status") or payload["run"]["status"]
        return statuses

    terminal = {"completed", "failed", "cancelled"}
    wall_start = time.perf_counter()
    iterations = 0
    while iterations < iteration_cap:
        iterations += 1
        worked = False
        while True:
            job = worker.run_once(worker_id="bench-worker")
            if job is None:
                break
            worked = True
        session = db.SessionLocal()
        try:
            scheduler.tick_all(worker_id=f"bench-scheduler-{iterations % 3}")
        finally:
            session.close()
        if all(status in terminal for status in run_statuses().values()):
            break
        if not worked:
            time.sleep(0.002)
    wall_seconds = time.perf_counter() - wall_start

    statuses = run_statuses()
    completed = sum(1 for status in statuses.values() if status == "completed")
    failed = sum(1 for status in statuses.values() if status == "failed")

    latencies: list[float] = []
    session = db.SessionLocal()
    try:
        for request_id in request_ids:
            record = session.get(db.OnboardingRequestRecord, request_id)
            run = scheduler.get_run_for_request(session, request_id)
            if record is not None and run is not None and run.completed_at is not None:
                elapsed = _as_utc(run.completed_at) - _as_utc(record.created_at)
                latencies.append(elapsed.total_seconds() * 1000)
    finally:
        session.close()

    executed_steps = completed * len(STEPS)
    return BenchmarkReport(
        requests=requests,
        engine_parallelism=scheduler.max_parallel_default(),
        steps_per_request=len(STEPS),
        completed=completed,
        failed=failed,
        wall_seconds=round(wall_seconds, 4),
        requests_per_second=round(completed / wall_seconds, 2) if wall_seconds else 0.0,
        steps_per_second=round(executed_steps / wall_seconds, 2) if wall_seconds else 0.0,
        p50_ms=round(_percentile(latencies, 0.50), 1),
        p95_ms=round(_percentile(latencies, 0.95), 1),
        p99_ms=round(_percentile(latencies, 0.99), 1),
        min_ms=round(min(latencies), 1) if latencies else 0.0,
        max_ms=round(max(latencies), 1) if latencies else 0.0,
        iterations=iterations,
        latencies_ms=[round(value, 1) for value in latencies],
    )


def format_report(report: BenchmarkReport) -> str:
    return "\n".join(
        [
            f"requests        {report.requests}",
            f"steps/request   {report.steps_per_request}",
            f"engine slots    {report.engine_parallelism} (single worker drains them)",
            f"completed       {report.completed}",
            f"failed          {report.failed}",
            f"wall            {report.wall_seconds:.2f}s",
            f"throughput      {report.requests_per_second} workflows/s "
            f"({report.steps_per_second} steps/s)",
            f"latency p50     {report.p50_ms} ms",
            f"latency p95     {report.p95_ms} ms",
            f"latency p99     {report.p99_ms} ms",
            f"latency range   {report.min_ms}-{report.max_ms} ms",
            f"scheduler ticks {report.iterations}",
        ]
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps end-to-end workflow benchmark")
    parser.add_argument("--requests", type=int, default=25)
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    report = run_benchmark(requests=args.requests)
    print(format_report(report))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report.summary(), indent=2) + "\n")
        print(f"wrote {args.json_path}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
