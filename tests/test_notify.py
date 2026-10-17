"""Tests for outbound webhook delivery with retries. All HTTP mocked."""

from unittest.mock import Mock, patch

import requests

from src.notify import deliver_webhook


def _ok_response():
    response = Mock()
    response.raise_for_status.return_value = None
    return response


def test_success_on_first_attempt():
    sleeps = []
    with patch("src.notify.requests.post", return_value=_ok_response()) as post:
        result = deliver_webhook(
            "https://hooks.example/t", {"event": "x"}, sleep=sleeps.append
        )
    assert result.success and result.attempts == 1 and result.last_error is None
    post.assert_called_once()
    assert sleeps == []


def test_retries_then_succeeds():
    sleeps = []
    with patch(
        "src.notify.requests.post",
        side_effect=[requests.ConnectionError("refused"), _ok_response()],
    ):
        result = deliver_webhook(
            "https://hooks.example/t", {"event": "x"}, sleep=sleeps.append
        )
    assert result.success and result.attempts == 2
    assert sleeps == [0.5]  # exponential backoff after the first failure


def test_exhausts_retries_with_exponential_backoff():
    sleeps = []
    with patch(
        "src.notify.requests.post",
        side_effect=requests.Timeout("timed out"),
    ):
        result = deliver_webhook(
            "https://hooks.example/t", {"event": "x"}, sleep=sleeps.append
        )
    assert not result.success and result.attempts == 3
    assert sleeps == [0.5, 1.0]
    assert "timed out" in result.last_error


def test_http_error_is_retried():
    error_response = Mock()
    error_response.raise_for_status.side_effect = requests.HTTPError("503")
    with patch("src.notify.requests.post", return_value=error_response):
        result = deliver_webhook(
            "https://hooks.example/t", {"event": "x"}, max_attempts=2, sleep=lambda _: None
        )
    assert not result.success and result.attempts == 2
