"""Mock ConnectWise Manage adapter.

The real API uses a conditions query string, PATCH operations, and separate
ticket/time-entry payloads. This module keeps those shapes without requiring a
ConnectWise tenant; tests use a recording session.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import requests


class ConnectWiseError(RuntimeError):
    pass


class ConnectWiseMockAdapter:
    def __init__(self, *, base_url: str = "https://api.connectwise.com", session: Optional[requests.Session] = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None, json: Any = None) -> Any:
        response = self.session.request(method, f"{self.base_url}{path}", params=params, json=json, timeout=self.timeout)
        if response.status_code >= 400:
            raise ConnectWiseError(f"ConnectWise {method} {path} failed: {response.status_code} {response.text}")
        return response.json() if response.content else None

    def get_tickets(self, conditions: str) -> list[dict[str, Any]]:
        body = self._request("GET", "/v4_6_release/apis/3.0/service/tickets", params={"conditions": conditions})
        return body if isinstance(body, list) else body.get("value", [])

    def patch_ticket(self, ticket_id: int, operations: list[dict[str, Any]]) -> Any:
        return self._request("PATCH", f"/v4_6_release/apis/3.0/service/tickets/{ticket_id}", json=operations)

    def create_time_entry(self, payload: Mapping[str, Any]) -> Any:
        return self._request("POST", "/v4_6_release/apis/3.0/time/entries", json=dict(payload))

    @staticmethod
    def ticket_patch(queue: str, priority: str) -> list[dict[str, Any]]:
        return [
            {"op": "replace", "path": "/queue/id", "value": queue},
            {"op": "replace", "path": "/priority/id", "value": priority},
        ]

    @staticmethod
    def time_entry(ticket_id: int, member_id: int, hours: float, notes: str) -> dict[str, Any]:
        return {"ticketId": ticket_id, "memberId": member_id, "actualHours": hours, "notes": notes}
