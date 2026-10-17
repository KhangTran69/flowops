"""ConnectWise Manage (PSA) REST client and ticket sync.

* Auth: HTTP Basic ``{company}+{public_key}:{private_key}`` plus the
  ``clientId`` header ConnectWise requires for every integration.
* Queries use ConnectWise ``conditions`` syntax: strings in double quotes,
  dates in ``[brackets]``, booleans/numbers bare, joined with ``and``/``or``.
* Updates use JSON-Patch style operations (``op``/``path``/``value``).
* 429/503 responses are retried honouring ``Retry-After``; other errors
  raise :class:`ConnectWiseError` with ConnectWise's error code/message.
* :func:`sync_ticket` is idempotent: an ``external_links`` row maps each
  FlowOps ticket to its ConnectWise ticket so re-running patches instead of
  creating a duplicate.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db

API_PATH = "/v4_6_release/apis/3.0"
ACCEPT = "application/vnd.connectwise.com+json; version=2022.1"
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
SYSTEM = "connectwise"

PRIORITY_MAP = {
    "critical": "Priority 1 - Emergency Response",
    "high": "Priority 2 - Quick Response",
    "normal": "Priority 3 - Normal Response",
    "low": "Priority 4 - Schedule Maintenance",
}
DEFAULT_BOARD = "Service Desk"


class ConnectWiseError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None) -> None:
        prefix = f"HTTP {status_code} " if status_code else ""
        super().__init__(f"{prefix}{code + ': ' if code else ''}{message}")
        self.status_code = status_code
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code in RETRYABLE_STATUS


def cw_value(value: Any) -> str:
    """Render one value in ConnectWise conditions syntax."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        moment = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return f"[{moment.strftime('%Y-%m-%dT%H:%M:%SZ')}]"
    if isinstance(value, date):
        return f"[{value.isoformat()}]"
    if value is None:
        return "null"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def condition(field: str, op: str, value: Any) -> str:
    allowed = {"=", "!=", "<", "<=", ">", ">=", "contains", "like", "in", "not in"}
    if op not in allowed:
        raise ValueError(f"unsupported conditions operator {op!r}")
    if op in ("in", "not in"):
        rendered = "(" + ",".join(cw_value(v) for v in value) + ")"
        return f"{field} {op} {rendered}"
    return f"{field} {op} {cw_value(value)}"


def all_of(*parts: str) -> str:
    return " and ".join(p if " or " not in p else f"({p})" for p in parts if p)


def any_of(*parts: str) -> str:
    return " or ".join(p for p in parts if p)


def replace(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


class ConnectWiseClient:
    def __init__(
        self,
        site: str,
        company_id: str,
        public_key: str,
        private_key: str,
        client_id: str,
        *,
        http_client: Optional[httpx.Client] = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        page_size: int = 100,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not all((site, company_id, public_key, private_key, client_id)):
            raise ValueError("ConnectWise site, company, public/private key and clientId are required")
        host = site if site.startswith("http") else f"https://{site}"
        self.base_url = host.rstrip("/") + API_PATH
        raw = f"{company_id}+{public_key}:{private_key}".encode()
        self._headers = {
            "Authorization": "Basic " + base64.b64encode(raw).decode(),
            "clientId": client_id,
            "Accept": ACCEPT,
            "Content-Type": "application/json",
        }
        self.http_client = http_client
        self.timeout = timeout
        self.max_retries = max_retries
        self.page_size = page_size
        self.sleep = sleep

    def __repr__(self) -> str:
        return f"ConnectWiseClient(base_url={self.base_url!r})"

    def request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None, json: Any = None) -> Any:
        client = self.http_client or httpx.Client(timeout=self.timeout)
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.request(method, url, params=params, json=json, headers=self._headers)
                except httpx.TransportError as error:
                    if attempt >= self.max_retries:
                        raise ConnectWiseError(f"{method} {path} failed: {error}") from error
                    self.sleep(min(2.0 ** attempt, 30.0))
                    continue
                if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After", "")
                    self.sleep(float(retry_after) if retry_after.isdigit() else min(2.0 ** attempt, 30.0))
                    continue
                if response.is_success:
                    return response.json() if response.content else None
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                errors = body.get("errors") or []
                detail = body.get("message") or response.reason_phrase
                if errors and isinstance(errors[0], dict):
                    detail = f"{detail} ({errors[0].get('field', '')}: {errors[0].get('message', '')})"
                raise ConnectWiseError(str(detail), status_code=response.status_code, code=body.get("code"))
            raise ConnectWiseError(f"{method} {path} exhausted retries")  # pragma: no cover
        finally:
            if self.http_client is None:
                client.close()

    def paginate(self, path: str, *, conditions: str = "", fields: str = "", order_by: str = "") -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": self.page_size}
            if conditions:
                params["conditions"] = conditions
            if fields:
                params["fields"] = fields
            if order_by:
                params["orderBy"] = order_by
            batch = self.request("GET", path, params=params) or []
            yield from batch
            if len(batch) < self.page_size:
                return
            page += 1

    # -- tickets / notes / time ---------------------------------------------

    def find_tickets(self, conditions: str, *, order_by: str = "id desc") -> list[dict[str, Any]]:
        return list(self.paginate("service/tickets", conditions=conditions, order_by=order_by))

    def open_tickets_for_company(self, company_id: int, board: str = DEFAULT_BOARD) -> list[dict[str, Any]]:
        return self.find_tickets(
            all_of(
                condition("company/id", "=", company_id),
                condition("board/name", "=", board),
                condition("closedFlag", "=", False),
            )
        )

    def create_ticket(
        self,
        *,
        summary: str,
        company_id: int,
        board: str = DEFAULT_BOARD,
        priority: Optional[str] = None,
        description: str = "",
        external_reference: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary[:100],
            "company": {"id": company_id},
            "board": {"name": board},
            "initialDescription": description,
        }
        if priority:
            body["priority"] = {"name": priority}
        if external_reference:
            body["externalXRef"] = external_reference[:100]
        return self.request("POST", "service/tickets", json=body)

    def patch_ticket(self, ticket_id: int, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("PATCH", f"service/tickets/{ticket_id}", json=operations)

    def add_note(self, ticket_id: int, text: str, *, internal: bool = True) -> dict[str, Any]:
        return self.request(
            "POST",
            f"service/tickets/{ticket_id}/notes",
            json={"text": text, "internalAnalysisFlag": internal, "detailDescriptionFlag": not internal},
        )

    def add_time_entry(
        self,
        ticket_id: int,
        *,
        member_identifier: str,
        time_start: datetime,
        time_end: datetime,
        notes: str,
        work_type: str = "Remote Support",
        billable: bool = True,
    ) -> dict[str, Any]:
        if time_end <= time_start:
            raise ValueError("time_end must be after time_start")
        return self.request(
            "POST",
            "time/entries",
            json={
                "chargeToId": ticket_id,
                "chargeToType": "ServiceTicket",
                "member": {"identifier": member_identifier},
                "timeStart": cw_value(time_start).strip("[]"),
                "timeEnd": cw_value(time_end).strip("[]"),
                "workType": {"name": work_type},
                "billableOption": "Billable" if billable else "DoNotBill",
                "notes": notes,
            },
        )


def client_from_environment(environ: Optional[Mapping[str, str]] = None, **kwargs: Any) -> ConnectWiseClient:
    values = environ if environ is not None else os.environ
    return ConnectWiseClient(
        values.get("FLOWOPS_CW_SITE", ""),
        values.get("FLOWOPS_CW_COMPANY_ID", ""),
        values.get("FLOWOPS_CW_PUBLIC_KEY", ""),
        values.get("FLOWOPS_CW_PRIVATE_KEY", ""),
        values.get("FLOWOPS_CW_CLIENT_ID", ""),
        **kwargs,
    )


def _link(session: Session, ticket_id: str) -> Optional[db.ExternalLinkRecord]:
    return session.execute(
        select(db.ExternalLinkRecord).where(
            db.ExternalLinkRecord.system == SYSTEM,
            db.ExternalLinkRecord.local_type == "ticket",
            db.ExternalLinkRecord.local_id == ticket_id,
        )
    ).scalar_one_or_none()


def sync_ticket(
    session: Session,
    ticket: db.TicketRecord,
    client: ConnectWiseClient,
    *,
    company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Create or update the PSA ticket that mirrors a FlowOps ticket."""
    link = _link(session, ticket.ticket_id)
    priority = PRIORITY_MAP.get(ticket.priority, PRIORITY_MAP["normal"])
    board = DEFAULT_BOARD if not ticket.queue else ticket.queue.replace("-", " ").title()
    if link is None:
        if company_id is None and ticket.tenant_key:
            tenant = session.get(db.TenantRecord, ticket.tenant_key)
            if tenant and tenant.psa_company_id:
                company_id = int(tenant.psa_company_id)
        if company_id is None:
            raise ConnectWiseError(
                f"ticket {ticket.ticket_id} has no PSA company (set tenants.psa_company_id)",
                status_code=422,
                code="CompanyMissing",
            )
        created = client.create_ticket(
            summary=ticket.title,
            company_id=company_id,
            board=board,
            priority=priority,
            description=f"Created by FlowOps from ticket {ticket.ticket_id}.",
            external_reference=f"flowops:{ticket.ticket_id}",
        )
        session.add(
            db.ExternalLinkRecord(
                system=SYSTEM,
                local_type="ticket",
                local_id=ticket.ticket_id,
                external_id=str(created["id"]),
                tenant_key=ticket.tenant_key,
            )
        )
        session.flush()
        return {"action": "created", "connectwise_id": created["id"], "board": board, "priority": priority}
    operations = [replace("priority", {"name": priority})]
    if ticket.status == "closed":
        operations.append(replace("status", {"name": "Closed (resolved)"}))
    client.patch_ticket(int(link.external_id), operations)
    if ticket.resolution_note:
        client.add_note(int(link.external_id), ticket.resolution_note, internal=False)
    link.last_synced_at = db.utcnow()
    session.flush()
    return {"action": "updated", "connectwise_id": int(link.external_id), "operations": operations}
