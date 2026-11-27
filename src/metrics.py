"""In-process counters for HTTP, AI, and uptime metrics.

Kept as plain dictionaries guarded by locks so tests can reset or inspect
them directly. See ``docs/architecture.md`` (Observability) for the planned
Prometheus push-gateway migration for multi-replica deployments.
"""

import threading
import time

_METRICS_LOCK = threading.Lock()
_METRICS: dict[tuple[str, str, int], int] = {}
_METRICS_STARTED = time.time()
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_HTTP_LATENCY: dict[tuple[str, str], tuple[list[int], int, float]] = {}
_AI_METRICS_LOCK = threading.Lock()
_AI_TRIAGE: dict[tuple[str, str], int] = {}
_AI_APPLY: dict[str, int] = {}
_AI_PROVIDER_ERRORS: dict[str, int] = {}


def record_ai_triage(adapter: str, outcome: str) -> None:
    with _AI_METRICS_LOCK:
        key = (adapter, outcome)
        _AI_TRIAGE[key] = _AI_TRIAGE.get(key, 0) + 1


def record_ai_apply(field: str) -> None:
    with _AI_METRICS_LOCK:
        _AI_APPLY[field] = _AI_APPLY.get(field, 0) + 1


def record_ai_provider_error(provider: str) -> None:
    with _AI_METRICS_LOCK:
        _AI_PROVIDER_ERRORS[provider] = _AI_PROVIDER_ERRORS.get(provider, 0) + 1


def record_request(method: str, route: str, status_code: int, duration: float) -> None:
    method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
    key = (method, route, status_code)
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + 1
        counts, total, seconds = _HTTP_LATENCY.get((method, route), ([0] * len(LATENCY_BUCKETS), 0, 0.0))
        for index, upper in enumerate(LATENCY_BUCKETS):
            if duration <= upper:
                counts[index] += 1
        _HTTP_LATENCY[(method, route)] = (counts, total + 1, seconds + duration)


def snapshot_http() -> dict[tuple[str, str, int], int]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def snapshot_latency() -> dict[tuple[str, str], tuple[list[int], int, float]]:
    with _METRICS_LOCK:
        return {key: (list(counts), total, seconds) for key, (counts, total, seconds) in _HTTP_LATENCY.items()}


def snapshot_ai() -> tuple[dict, dict, dict]:
    with _AI_METRICS_LOCK:
        return dict(_AI_TRIAGE), dict(_AI_APPLY), dict(_AI_PROVIDER_ERRORS)


def uptime_seconds() -> float:
    return time.time() - _METRICS_STARTED
