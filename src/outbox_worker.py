"""Durable notification-outbox dispatcher.

Run once:

    python -m src.outbox_worker --once

Or as a polling daemon:

    python -m src.outbox_worker --poll-interval 2
"""

import argparse
import json
import signal
import time
import uuid

from src import db, idempotency, outbox, rate_limit
from src.notify import deliver_webhook
from src.services.connectors import deliver_outbox_notification


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def sweep_idempotency_keys() -> int:
    """Delete idempotency records past the retention window.

    Returns the number of rows removed; failures are logged but never crash
    the dispatcher loop (the next poll retries).
    """
    session = db.SessionLocal()
    try:
        return idempotency.sweep_expired(session)
    except Exception as exc:  # pragma: no cover - defensive for daemon loop
        from src import logging_config

        logging_config.configure()
        logging_config.log_exception("idempotency sweep failed", exc)
        session.rollback()
        return 0
    finally:
        session.close()


def run_once(
    *,
    worker_id: str | None = None,
    lease_seconds: int = outbox.DEFAULT_LEASE_SECONDS,
) -> db.NotificationOutboxRecord | None:
    """Claim and deliver at most one pending outbox row."""
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        record = outbox.claim_delivery(
            session, worker_id, lease_seconds=lease_seconds
        )
        if record is None:
            return None
        payload = json.loads(record.payload_json)
        started = time.perf_counter()
        if payload.get("event") == "trigger.fired" and payload.get("tenant"):
            delivery = deliver_outbox_notification(
                record.url, payload, session, max_attempts=1
            )
        else:
            delivery = deliver_webhook(record.url, payload, max_attempts=1)
        duration_ms = int((time.perf_counter() - started) * 1000)
        resulting = outbox.finalize_delivery(
            session,
            record.id,
            record.lease_owner_token or "",
            success=delivery.success,
            attempt_delta=delivery.attempts or 1,
            error=delivery.last_error,
            max_attempts=outbox.max_attempts_from_environment(),
        )
        if resulting is None:
            session.rollback()
        else:
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action="notification.dispatched",
                status="success" if delivery.success else "failure",
                ticket_id=record.ticket_id,
                duration_ms=duration_ms,
                detail=f"outbox #{record.id}: now '{resulting}'",
            )
            if resulting == "failed":
                db.log_execution(
                    session,
                    run_id=_new_run_id(),
                    action="notification.dead_lettered",
                    status="failure",
                    ticket_id=record.ticket_id,
                    duration_ms=0,
                    detail=f"outbox #{record.id} exhausted by dispatcher",
                )
            session.commit()
        session.expire_all()
        return session.get(db.NotificationOutboxRecord, record.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="deliver at most one row")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=outbox.DEFAULT_LEASE_SECONDS)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument(
        "--sweep-interval",
        type=float,
        default=3600.0,
        help="seconds between idempotency retention sweeps (0 disables)",
    )
    args = parser.parse_args(argv)
    worker_id = args.worker_id or f"outbox-{uuid.uuid4().hex[:8]}"
    stopping = False
    last_sweep = 0.0

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        record = run_once(worker_id=worker_id, lease_seconds=args.lease_seconds)
        if record is not None:
            print(f"{record.id} {record.status}", flush=True)
            if args.once:
                return 0
        elif args.once:
            return 0
        if args.sweep_interval > 0 and (time.monotonic() - last_sweep) >= args.sweep_interval:
            last_sweep = time.monotonic()
            removed = sweep_idempotency_keys()
            try:
                rate_limit.sweep_expired()
            except Exception as error:  # retention must not interrupt dispatch
                import logging

                logging.getLogger(__name__).warning("Rate limit retention failed: %s", type(error).__name__)
            if removed:
                print(f"idempotency sweep removed {removed} expired keys", flush=True)
        if not args.once:
            time.sleep(max(0.1, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
