"""Outbound webhook notifications with retries and exponential backoff.

Used to notify automation platforms (n8n, Power Automate) and chat tools
about ticket events. Failures are reported, never raised, so a flaky
webhook cannot fail ticket intake.
"""

import time
from dataclasses import dataclass
from typing import Callable

import requests


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    attempts: int
    last_error: str | None


def deliver_webhook(
    url: str,
    payload: dict,
    *,
    timeout: float = 5.0,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> DeliveryResult:
    """POST the payload to the webhook, retrying with exponential backoff.

    Returns a DeliveryResult instead of raising so callers can log the
    outcome and continue.
    """
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            return DeliveryResult(success=True, attempts=attempt, last_error=None)
        except requests.RequestException as error:
            last_error = str(error)
            if attempt < max_attempts:
                sleep(backoff_seconds * (2 ** (attempt - 1)))
    return DeliveryResult(success=False, attempts=max_attempts, last_error=last_error)
