#!/usr/bin/env python3
"""Small concurrent smoke test for ticket intake and idempotent replay.

This is not a benchmark. It checks that concurrent requests remain correct.

    python scripts/load_smoke.py --base-url http://127.0.0.1:8000 --requests 20
"""

import argparse
import concurrent.futures
import time
import uuid

import httpx


def one_round(base_url: str, index: int) -> tuple[bool, str, float]:
    ticket_id = 80_000 + index
    payload = {
        "userId": 7,
        "id": ticket_id,
        "title": "Load smoke VPN outage - all users cannot work",
        "completed": False,
    }
    headers = {"Idempotency-Key": f"load-{ticket_id}-{uuid.uuid4().hex[:8]}"}
    started = time.perf_counter()
    with httpx.Client(base_url=base_url, timeout=10) as client:
        first = client.post("/tickets", json=payload, headers=headers)
        second = client.post("/tickets", json=payload, headers=headers)
    elapsed = time.perf_counter() - started
    if first.status_code != 201:
        return False, f"create {first.status_code}", elapsed
    if second.status_code != 200:
        return False, f"replay {second.status_code}", elapsed
    if second.headers.get("Idempotency-Replayed") != "true":
        return False, "missing replay header", elapsed
    return True, "ok", elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    started = time.perf_counter()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(one_round, args.base_url, index)
            for index in range(args.requests)
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    failures = [result for result in results if not result[0]]
    latencies = sorted(result[2] for result in results)
    print(f"requests: {len(results)}  failures: {len(failures)}  elapsed: {elapsed:.2f}s")
    if latencies:
        print(
            f"latency p50: {latencies[len(latencies) // 2]:.3f}s  "
            f"max: {latencies[-1]:.3f}s"
        )
    for _, message, _ in failures[:10]:
        print(f"  failure: {message}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
