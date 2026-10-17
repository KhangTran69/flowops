"""Central logging configuration for FlowOps services.

Structured exception logging helper used by API routes and workers so that
production failures carry correlation IDs, run IDs, and ticket/request
identifiers instead of being swallowed by bare ``except Exception`` blocks.
"""

import logging
import os
from typing import Any, Optional


_CONFIGURED = False


def configure(level: Optional[str] = None) -> logging.Logger:
    """Idempotently configure the root ``flowops`` logger.

    Log level comes from ``FLOWOPS_LOG_LEVEL`` (default ``INFO``). Format is a
    single line with timestamp, level, logger name, and message so log
    aggregators can parse it without multi-line stack-trace handling issues.
    """
    global _CONFIGURED
    logger = logging.getLogger("flowops")
    if not _CONFIGURED:
        resolved = (level or os.environ.get("FLOWOPS_LOG_LEVEL", "INFO")).upper()
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(resolved)
        logger.propagate = False
        _CONFIGURED = True
    return logger


def log_exception(
    context: str,
    error: BaseException,
    *,
    run_id: Optional[str] = None,
    ticket_id: Optional[str] = None,
    request_id: Optional[Any] = None,
    correlation_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """Log a handled exception with identifiers before it is re-raised.

    Never raises: logging must not mask the original failure path.
    """
    try:
        parts = [context, f"{type(error).__name__}: {error}"]
        for name, value in (
            ("run_id", run_id),
            ("ticket_id", ticket_id),
            ("request_id", request_id),
            ("correlation_id", correlation_id),
        ):
            if value is not None:
                parts.append(f"{name}={value}")
        for name, value in fields.items():
            if value is not None:
                parts.append(f"{name}={value}")
        configure().error(" | ".join(str(part) for part in parts), exc_info=True)
    except Exception:  # pragma: no cover - logging must never fail a request
        pass
