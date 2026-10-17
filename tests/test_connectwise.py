"""ConnectWise Manage client and idempotent ticket sync (offline)."""

import base64
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src import connectwise as cw
from src import db


def make_client(handler, sleeps=None, page_size=2):
    return cw.ConnectWiseClient(
        "api-na.myconnectwise.net", "msp", "pub", "priv", "client-guid",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        page_size=page_size,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def test_conditions_syntax():
    expr = cw.all_of(
        cw.condition("board/name", "=", 'Service "Desk"'),
        cw.condition("closedFlag", "=", False),
        cw.condition("dateEntered", ">", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        cw.any_of(cw.condition("priority/id", "in", [1, 2]), cw.condition("summary", "contains", "VPN")),
    )
    assert expr == (
        'board/name = "Service \\"Desk\\"" and closedFlag = false and '
        'dateEntered > [2026-01-02T03:04:05Z] and (priority/id in (1,2) or summary contains "VPN")'
    )
    assert cw.cw_value(date(2026, 5, 1)) == "[2026-05-01]"
    with pytest.raises(ValueError):
        cw.condition("id", "DROP", 1)


def test_auth_headers_and_paging():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        return httpx.Response(200, json=[{"id": n} for n in ([1, 2] if page == 1 else [3])])

    tickets = make_client(handler).open_tickets_for_company(250)
    assert [t["id"] for t in tickets] == [1, 2, 3]
    first = seen[0]
    assert first.url.path == "/v4_6_release/apis/3.0/service/tickets"
    assert first.headers["clientId"] == "client-guid"
    assert first.headers["Authorization"] == "Basic " + base64.b64encode(b"msp+pub:priv").decode()
    assert parse_qs(urlparse(str(first.url)).query)["conditions"] == [
        'company/id = 250 and board/name = "Service Desk" and closedFlag = false'
    ]


def test_retry_after_and_error_details():
    responses = [httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json={"id": 9})]
    sleeps = []
    assert make_client(lambda r: responses.pop(0), sleeps).patch_ticket(9, [cw.replace("summary", "x")]) == {"id": 9}
    assert sleeps == [4.0]

    def bad(request):
        return httpx.Response(400, json={"code": "InvalidObject", "message": "ticket object is invalid",
                                         "errors": [{"field": "board", "message": "board not found"}]})

    with pytest.raises(cw.ConnectWiseError, match="board not found") as info:
        make_client(bad).create_ticket(summary="x", company_id=1)
    assert info.value.retryable is False and info.value.code == "InvalidObject"


def test_time_entry_payload():
    seen = []
    client = make_client(lambda r: seen.append(json.loads(r.content)) or httpx.Response(201, json={"id": 1}))
    start = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    client.add_time_entry(77, member_identifier="ktran", time_start=start,
                          time_end=start.replace(hour=9, minute=30), notes="Automated onboarding")
    assert seen[0]["chargeToType"] == "ServiceTicket" and seen[0]["chargeToId"] == 77
    assert seen[0]["timeStart"] == "2026-03-01T09:00:00Z"
    with pytest.raises(ValueError):
        client.add_time_entry(77, member_identifier="k", time_start=start, time_end=start, notes="")


def test_sync_ticket_creates_once_then_patches(tmp_path):
    db.use_database(f"sqlite:///{tmp_path / 'cw.db'}")
    db._ensure_tables()
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path.rsplit("/apis/3.0/", 1)[1], body))
        return httpx.Response(201 if request.method == "POST" else 200, json={"id": 555})

    client = make_client(handler)
    with db.SessionLocal() as session:
        session.add(db.TenantRecord(key="acme", name="Acme", psa_company_id="250"))
        ticket = db.TicketRecord(ticket_id="42", requester_id=1, title="VPN down for sales",
                                 status="open", priority="high", queue="network", tenant_key="acme")
        session.add(ticket)
        session.flush()
        first = cw.sync_ticket(session, ticket, client)
        ticket.status, ticket.resolution_note = "closed", "Reset VPN profile"
        second = cw.sync_ticket(session, ticket, client)
        session.commit()
    assert first["action"] == "created" and second["action"] == "updated"
    method, path, body = calls[0]
    assert (method, path) == ("POST", "service/tickets")
    assert body["company"] == {"id": 250} and body["externalXRef"] == "flowops:42"
    assert body["priority"] == {"name": "Priority 2 - Quick Response"}
    assert calls[1][:2] == ("PATCH", "service/tickets/555")
    assert {"op": "replace", "path": "status", "value": {"name": "Closed (resolved)"}} in calls[1][2]
    assert calls[2][1] == "service/tickets/555/notes"
    assert sum(1 for c in calls if c[0] == "POST" and c[1] == "service/tickets") == 1


def test_client_requires_credentials():
    with pytest.raises(ValueError):
        cw.client_from_environment({})
    assert "priv" not in repr(make_client(lambda r: httpx.Response(200)))
