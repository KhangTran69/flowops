"""FlowOps v0.1 — Ticket Intake Processor.

Fetches a simulated ticket from JSONPlaceholder, validates it,
normalizes it into FlowOps' internal ticket format, and saves it as JSON.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

SAMPLE_API_URL = os.environ.get(
    "FLOWOPS_SAMPLE_API_URL", "https://jsonplaceholder.typicode.com/todos/1"
)
DEFAULT_OUTPUT_PATH = Path("data") / "ticket.json"
REQUEST_TIMEOUT_SECONDS = 10

REQUIRED_FIELDS = {
    "userId": int,
    "id": int,
    "title": str,
    "completed": bool,
}


class TicketValidationError(ValueError):
    """Raised when the API response is not a valid ticket."""


def _is_exact_type(value: Any, expected: type) -> bool:
    """Return True if value is of the expected type, excluding bool-as-int."""
    if expected is int and isinstance(value, bool):
        return False
    return type(value) is expected


def validate_ticket(payload: Any) -> dict:
    """Validate a raw API payload and return it unchanged if valid.

    Raises TicketValidationError for unexpected structures, missing
    fields, wrong types, or empty titles.
    """
    if not isinstance(payload, dict):
        raise TicketValidationError(
            f"Expected a JSON object at the top level, got {type(payload).__name__}."
        )

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload:
            raise TicketValidationError(f"Missing required field: '{field}'.")
        value = payload[field]
        if not _is_exact_type(value, expected_type):
            raise TicketValidationError(
                f"Field '{field}' must be of type {expected_type.__name__}, "
                f"got {type(value).__name__} ({value!r})."
            )

    if not payload["title"].strip():
        raise TicketValidationError("Field 'title' must be a non-empty string.")

    return payload


def normalize_ticket(payload: dict) -> dict:
    """Transform a validated raw payload into the internal ticket format."""
    return {
        "ticket_id": str(payload["id"]),
        "requester_id": payload["userId"],
        "title": payload["title"],
        "status": "closed" if payload["completed"] else "open",
        "priority": "normal",
    }


def fetch_ticket(api_url: str = SAMPLE_API_URL) -> dict:
    """Fetch a sample ticket from the API and validate it.

    Raises requests.RequestException for network/HTTP failures and
    TicketValidationError for invalid response data.
    """
    response = requests.get(api_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    return validate_ticket(payload)


def save_ticket(ticket: dict, output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    """Save a normalized ticket as indented JSON. Returns the output path."""
    validate_normalized_ticket(ticket)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(ticket, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return output_path


def validate_normalized_ticket(ticket: Any) -> None:
    """Ensure a normalized ticket has the expected keys and value types."""
    expected = {
        "ticket_id": str,
        "requester_id": int,
        "title": str,
        "status": str,
        "priority": str,
    }
    if not isinstance(ticket, dict):
        raise TicketValidationError(
            f"Ticket must be a dict, got {type(ticket).__name__}."
        )
    for field, expected_type in expected.items():
        if field not in ticket:
            raise TicketValidationError(f"Ticket is missing field: '{field}'.")
        if not _is_exact_type(ticket[field], expected_type):
            raise TicketValidationError(
                f"Ticket field '{field}' must be {expected_type.__name__}."
            )


def main() -> int:
    """Fetch, validate, normalize, and save one sample ticket."""
    try:
        raw = fetch_ticket()
        ticket = normalize_ticket(raw)
        output_path = save_ticket(ticket)
    except requests.RequestException as error:
        print(f"Error: could not reach the sample API: {error}", file=sys.stderr)
        return 1
    except TicketValidationError as error:
        print(f"Error: the API returned invalid ticket data: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Error: could not save the ticket file: {error}", file=sys.stderr)
        return 1

    print("Ticket processed successfully.")
    print(f"Saved to: {output_path}")
    print(json.dumps(ticket, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
