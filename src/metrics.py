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
    key = (method, route, status_code)
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + 1


def snapshot_http() -> dict[tuple[str, str, int], int]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def snapshot_ai() -> tuple[dict, dict, dict]:
    with _AI_METRICS_LOCK:
        return dict(_AI_TRIAGE), dict(_AI_APPLY), dict(_AI_PROVIDER_ERRORS)


def uptime_seconds() -> float:
    return time.time() - _METRICS_STARTED
