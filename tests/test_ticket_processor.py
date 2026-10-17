"""Tests for FlowOps v0.1 ticket processing. All HTTP calls are mocked."""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from src.ticket_processor import (
    TicketValidationError,
    fetch_ticket,
    normalize_ticket,
    save_ticket,
    validate_ticket,
)

VALID_PAYLOAD = {
    "userId": 1,
    "id": 1,
    "title": "delectus aut autem",
    "completed": False,
}


@pytest.fixture
def mock_response():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = dict(VALID_PAYLOAD)
    return response


# --- Normalization ---


def test_valid_response_is_normalized_correctly(mock_response):
    with patch("src.ticket_processor.requests.get", return_value=mock_response):
        raw = fetch_ticket()
    ticket = normalize_ticket(raw)
    assert ticket == {
        "ticket_id": "1",
        "requester_id": 1,
        "title": "delectus aut autem",
        "status": "open",
        "priority": "normal",
    }


def test_completed_item_maps_to_closed():
    ticket = normalize_ticket({**VALID_PAYLOAD, "completed": True})
    assert ticket["status"] == "closed"


def test_pending_item_maps_to_open():
    ticket = normalize_ticket({**VALID_PAYLOAD, "completed": False})
    assert ticket["status"] == "open"


# --- Validation ---


@pytest.mark.parametrize("missing_field", ["userId", "id", "title", "completed"])
def test_missing_required_fields_rejected(missing_field):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != missing_field}
    with pytest.raises(TicketValidationError, match=missing_field):
        validate_ticket(payload)


def test_invalid_field_type_rejected():
    with pytest.raises(TicketValidationError, match="userId"):
        validate_ticket({**VALID_PAYLOAD, "userId": "one"})


def test_boolean_integer_confusion_rejected():
    with pytest.raises(TicketValidationError):
        validate_ticket({**VALID_PAYLOAD, "id": True})
    with pytest.raises(TicketValidationError):
        validate_ticket({**VALID_PAYLOAD, "completed": 1})


def test_empty_title_rejected():
    with pytest.raises(TicketValidationError, match="title"):
        validate_ticket({**VALID_PAYLOAD, "title": "   "})


def test_unexpected_top_level_structure_rejected():
    with pytest.raises(TicketValidationError, match="JSON object"):
        validate_ticket([VALID_PAYLOAD])


# --- Network error handling ---


def test_http_error_handled(mock_response):
    mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    with patch("src.ticket_processor.requests.get", return_value=mock_response):
        with pytest.raises(requests.HTTPError):
            fetch_ticket()


def test_network_error_handled():
    with patch(
        "src.ticket_processor.requests.get",
        side_effect=requests.ConnectionError("connection refused"),
    ):
        with pytest.raises(requests.ConnectionError):
            fetch_ticket()


def test_timeout_handled():
    with patch(
        "src.ticket_processor.requests.get",
        side_effect=requests.Timeout("timed out"),
    ):
        with pytest.raises(requests.Timeout):
            fetch_ticket()


# --- Persistence ---


def test_ticket_saved_as_valid_json(tmp_path):
    ticket = normalize_ticket(VALID_PAYLOAD)
    output = save_ticket(ticket, tmp_path / "out" / "ticket.json")
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == ticket


def test_output_directory_created_when_absent(tmp_path):
    target = tmp_path / "new_dir" / "sub_dir" / "ticket.json"
    save_ticket(normalize_ticket(VALID_PAYLOAD), target)
    assert target.exists()


def test_invalid_ticket_not_saved(tmp_path):
    target = tmp_path / "ticket.json"
    with pytest.raises(TicketValidationError):
        save_ticket({"ticket_id": 1}, target)
    with pytest.raises(TicketValidationError):
        save_ticket("not a dict", target)
    assert not target.exists()
