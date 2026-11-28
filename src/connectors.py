"""CLI entrypoint for the FlowOps connector poller (``python -m src.connectors``).

The connector/trigger business logic — adapters, config validation, trigger
evaluation, ingestion, and the poll loop body — lives in
``src.services.connectors`` (service-layer extraction, T3.1). This module
keeps only the blocking poll loop and argument handling.
"""

from __future__ import annotations

import json
import time

from src.services.connectors import poll_all_connectors


def run_forever(stop_event=None) -> None:
    """Blocking poll loop used by ``python -m src.connectors``."""
    from src.services import connectors as service

    while True:
        try:
            poll_all_connectors()
        except Exception:  # pragma: no cover - defensive daemon guard
            pass
        if stop_event is not None:
            if stop_event.wait(service.POLL_INTERVAL_SECONDS):
                return
        else:
            time.sleep(service.POLL_INTERVAL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from src.services import connectors as service

    parser = argparse.ArgumentParser(description="FlowOps connector poller")
    parser.add_argument(
        "--once", action="store_true", help="Poll all connectors once and exit."
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="Seconds between polls (daemon mode).",
    )
    args = parser.parse_args(argv)
    if args.poll_interval is not None:
        service.POLL_INTERVAL_SECONDS = max(5, args.poll_interval)
    if args.once:
        print(json.dumps({"ingested": poll_all_connectors()}))
        return 0
    run_forever()
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
