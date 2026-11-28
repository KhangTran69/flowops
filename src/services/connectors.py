"""FlowOps v1.4 — Event-Driven Triggers & Connector Framework (service layer).

Owns the connector/trigger business logic: connector adapters and config
validation, trigger-rule evaluation and firing, the ingestion pipeline, and
the polling loop body. ``python -m src.connectors`` remains the CLI
entrypoint (thin wrapper in ``src/connectors.py``).

Two capabilities complete the v1.4 roadmap:

* **Connector framework** — a ``SourceConnector`` interface with polling and
  webhook adapters (Jira, ServiceNow, Slack, Microsoft Teams, generic JSON).
  Connectors normalize external events into one canonical shape; downstream
  routing, persistence, notifications, and monitoring are unchanged. Sources
  are configured in the database (``/connectors``), never by code changes.
  Credentials use server-managed, connector-and-tenant-specific environment
  variables; stored configs cannot select arbitrary variables or contain raw
  secret values. Polling hosts must match a server-side allowlist.

* **Event-driven triggers** — ``TriggerRuleRecord`` rows evaluated whenever an
  event is ingested (from any connector or ``POST /events``). A rule matches
  on source/event-type plus a safe predicate expression (the v1.3
  ``src.expressions`` engine) over the event context, then fires exactly one
  action: create+start an onboarding/offboarding workflow run (reusing the
  v0.5 checklist builder and the v1.2/v1.3 orchestrator) or enqueue an outbox
  notification (at-least-once delivery). Rule evaluation failures are logged
  and swallowed so a bad rule can never break ingestion.

Reliability invariants carried over from earlier milestones:

* At-least-once ingestion with dedupe: ``events.fingerprint`` hashes tenant,
  connector, source, event type, and external ID; replays from pollers or
  retried webhooks return ``duplicate: true`` and fire nothing.
* Trigger firing is idempotent per ``(rule, event)`` pair via a deterministic
  execution-log key ``trigger:event:<rule_id>:<event_id>``, checked under a
  row lock (no-op on SQLite) before any side effect.
* Multi-tenancy (GDAP): events and rules carry ``tenant_key``; a global rule
  (``tenant_key IS NULL``) may only act on tenants listed in its
  ``allowed_tenants`` static input.
* Offline-first: everything runs against SQLite with no network access; live
  HTTP calls happen only for connectors configured with allowlisted HTTPS URLs.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import db, expressions, scheduler
from src.deps import _new_run_id
from src.schemas import OnboardingRequestIn

# ---------------------------------------------------------------------------
# Canonical event model
# ---------------------------------------------------------------------------

MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
MAX_POLL_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_POLL_RECORDS = 50


def _read_poll_page(response: requests.Response, field: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Accept only bounded successful JSON provider pages, not redirects."""
    with response:
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise ValueError("Connector response was not successful.")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(data) + len(chunk) > MAX_POLL_RESPONSE_BYTES:
                raise ValueError("Connector response exceeds the configured size limit.")
            data.extend(chunk)
        page = json.loads(data)
        if not isinstance(page, dict) or not isinstance(page.get(field), list):
            raise ValueError("Connector response has an invalid page shape.")
        records = page[field]
        if len(records) > MAX_POLL_RECORDS or not all(isinstance(row, dict) for row in records):
            raise ValueError("Connector response has invalid or excessive records.")
        return records, page


def _read_poll_records(response: requests.Response, field: str) -> list[dict[str, Any]]:
    return _read_poll_page(response, field)[0]


@dataclass(frozen=True)
class NormalizedEvent:
    """One normalized external event ready for ingestion."""

    source: str
    event_type: str
    external_id: str
    payload: dict[str, Any]
    tenant_key: str | None = None
    title: str = ""
    description: str = ""
    connector_id: int | None = None

    def fingerprint(self) -> str:
        identity = json.dumps(
            [
                2,
                self.tenant_key[:64] if self.tenant_key else self.tenant_key,
                self.connector_id,
                self.source[:40],
                self.event_type[:80],
                self.external_id[:255],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "v2:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def trigger_context(self) -> dict[str, Any]:
        """Scope for predicate evaluation: envelope fields + payload keys."""
        context = {
            "source": self.source,
            "event_type": self.event_type,
            "external_id": self.external_id,
            "title": self.title,
            "tenant": self.tenant_key,
        }
        if isinstance(self.payload, dict):
            # Envelope fields win; payload cannot spoof source/event_type.
            for key, value in self.payload.items():
                context.setdefault(key, value)
        return context


def _stable_int_id(external_id: str) -> int:
    """Map an arbitrary external id to a stable positive int for intake."""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"flowops-event:{external_id}")
    return digest.int % 1_000_000_000 + 1


# ---------------------------------------------------------------------------
# Source connectors
# ---------------------------------------------------------------------------


class SourceConnector:
    """Interface implemented by every event source adapter."""

    name: str = "generic"
    kind: str = "polling"  # "polling" | "webhook"

    def fetch_events(self, session: Session) -> list[NormalizedEvent]:
        """Return new events since the last poll. Webhook sources: []."""
        raise NotImplementedError

    def fetch_page(self, session: Session, cursor: str | None) -> tuple[list[NormalizedEvent], str | None]:
        """One resumable page; older adapters retain their single-page contract."""
        return self.fetch_events(session), None

    def verify_webhook(self, request, body: bytes) -> bool:
        """Authenticate a webhook delivery at the transport level."""
        return True  # route-level API-key auth is the baseline guarantee

    def parse_webhook(self, payload: Any) -> list[NormalizedEvent]:
        """Translate a verified webhook body into normalized events."""
        raise NotImplementedError


class JiraConnector(SourceConnector):
    """Polls the Jira REST search API using a saved JQL query."""

    name = "jira"

    def __init__(self, base_url: str, email: str, api_token: str, jql: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.jql = jql

    def fetch_events(self, session: Session) -> list[NormalizedEvent]:
        return self.fetch_page(session, None)[0]

    def fetch_page(self, session: Session, cursor: str | None) -> tuple[list[NormalizedEvent], str | None]:
        params: dict[str, str | int] = {
            "jql": self.jql, "maxResults": MAX_POLL_RECORDS,
            "fields": "summary,status,priority,assignee,description",
        }
        if cursor:
            params["nextPageToken"] = cursor
        response = requests.get(
            f"{self.base_url}/rest/api/3/search/jql",
            auth=(self.email, self.api_token),
            params=params,
            timeout=15,
            stream=True,
            allow_redirects=False,
        )
        events = []
        records, page = _read_poll_page(response, "issues")
        next_cursor = page.get("nextPageToken")
        if page.get("isLast") is True:
            next_cursor = None
        elif not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 8192 or next_cursor == cursor:
            raise ValueError("Jira returned an invalid continuation token.")
        for issue in records:
            if not issue.get("id"):
                raise ValueError("Jira record has no stable identity.")
            fields = issue.get("fields", {})
            events.append(
                NormalizedEvent(
                    source=self.name,
                    event_type="issue_updated",
                    external_id=str(issue["id"]),
                    payload={
                        "key": issue.get("key"),
                        "status": (fields.get("status") or {}).get("name"),
                        "priority": (fields.get("priority") or {}).get("name"),
                        "assignee": (fields.get("assignee") or {}).get("emailAddress"),
                    },
                    title=f"Jira {issue.get('key')}: {fields.get('summary', '')}".strip(),
                    description=(fields.get("description") if isinstance(fields.get("description"), str) else "") or "",
                )
            )
        return events, next_cursor


class ServiceNowConnector(SourceConnector):
    """Polls the ServiceNow Table API for recent incident updates."""

    name = "servicenow"

    def __init__(self, instance_url: str, username: str, password: str) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.username = username
        self.password = password

    def fetch_events(self, session: Session) -> list[NormalizedEvent]:
        return self.fetch_page(session, None)[0]

    def fetch_page(self, session: Session, cursor: str | None) -> tuple[list[NormalizedEvent], str | None]:
        # Keyset scan, not offset pagination: inserts/deletions cannot shift
        # the next page. Existing event identity remains the incident number.
        if cursor is not None and not re.fullmatch(r"[0-9a-f]{32}", cursor):
            raise ValueError("Invalid ServiceNow continuation key.")
        query = "ORDERBYsys_id" if cursor is None else f"sys_id>{cursor}^ORDERBYsys_id"
        response = requests.get(
            f"{self.instance_url}/api/now/table/incident",
            auth=(self.username, self.password),
            headers={"Accept": "application/json"},
            params={"sysparm_limit": MAX_POLL_RECORDS, "sysparm_query": query,
                    "sysparm_fields": "sys_id,number,state,severity,assigned_to,short_description,description"},
            timeout=15,
            stream=True,
            allow_redirects=False,
        )
        events = []
        records = _read_poll_records(response, "result")
        previous = cursor or ""
        for rec in records:
            key = rec.get("sys_id")
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or key <= previous:
                raise ValueError("ServiceNow returned an invalid or unordered continuation key.")
            previous = key
            if not (rec.get("number") or rec.get("sys_id")):
                raise ValueError("ServiceNow record has no stable identity.")
            events.append(
                NormalizedEvent(
                    source=self.name,
                    event_type="incident_updated",
                    external_id=str(rec.get("number") or rec.get("sys_id")),
                    payload={
                        "number": rec.get("number"),
                        "state": rec.get("state"),
                        "severity": rec.get("severity"),
                        "assigned_to": (rec.get("assigned_to") or {}).get("value"),
                    },
                    title=f"SNOW {rec.get('number')}: {rec.get('short_description', '')}".strip(),
                    description=rec.get("description") or "",
                )
            )
        return events, previous if len(records) == MAX_POLL_RECORDS else None


class SlackConnector(SourceConnector):
    """Webhook source for the Slack Events API (signed deliveries)."""

    name = "slack"
    kind = "webhook"

    def __init__(self, signing_secret: str = "") -> None:
        self.signing_secret = signing_secret

    def verify_webhook(self, request, body: bytes) -> bool:
        if not self.signing_secret:
            return False
        signature = request.headers.get("x-slack-signature", "")
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        if not signature or not timestamp:
            return False
        try:
            if abs(int(time.time()) - int(timestamp)) > 300:
                return False  # outside replay window
        except ValueError:
            return False
        import hashlib
        import hmac
        import secrets

        base = b"v0:" + timestamp.encode() + b":" + body
        expected = "v0=" + hmac.new(
            self.signing_secret.encode(), base, hashlib.sha256
        ).hexdigest()
        return secrets.compare_digest(signature.encode(), expected.encode())

    def parse_webhook(self, payload: Any) -> list[NormalizedEvent]:
        if not isinstance(payload, dict):
            return []
        body = payload.get("event") if payload.get("type") == "event_callback" else payload
        if not isinstance(body, dict):
            return []
        if payload.get("type") == "url_verification" or body.get("type") == "url_verification" or body.get("subtype") == "bot_message":
            return []
        identity = body.get("ts") or body.get("event_ts") or payload.get("event_id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 255:
            raise ValueError("Slack delivery requires a stable event identity.")
        if not isinstance(body.get("text") or "", str):
            raise ValueError("Slack message text must be a string.")
        return [
            NormalizedEvent(
                source=self.name,
                event_type="message_posted",
                external_id=identity,
                payload={
                    "channel": body.get("channel"),
                    "user": body.get("user"),
                    "team": body.get("team"),
                },
                title=f"Slack #{body.get('channel', 'unknown')}",
                description=(body.get("text") or "")[:4000],
            )
        ]


class MSTeamsConnector(SourceConnector):
    """Webhook source for Microsoft Teams incoming-webhook cards."""

    name = "msteams"
    kind = "webhook"

    def parse_webhook(self, payload: Any) -> list[NormalizedEvent]:
        if not isinstance(payload, dict):
            return []
        text = payload.get("text") or payload.get("message") or ""
        identity = payload.get("id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 255 or not isinstance(text, str):
            raise ValueError("Teams delivery requires a stable ID and text message.")
        return [
            NormalizedEvent(
                source=self.name,
                event_type="message_posted",
                external_id=identity,
                payload={
                    "summary": payload.get("summary"),
                    "themeColor": payload.get("themeColor"),
                },
                title=f"Teams: {payload.get('title') or 'message'}"[:200],
                description=text[:4000],
            )
        ]


class GenericJsonConnector(SourceConnector):
    """Webhook source accepting the FlowOps canonical event envelope.

    Accepts ``{"events": [{"source", "id", "type", "title", "description",
    "payload", "tenant"}]}``, a bare list of such objects, or a single object.
    """

    name = "generic"
    kind = "webhook"

    def parse_webhook(self, payload: Any) -> list[NormalizedEvent]:
        if isinstance(payload, dict) and "events" in payload:
            items = payload["events"]
        elif isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            return []
        if not isinstance(items, list):
            raise ValueError("Connector events must be a list.")
        events = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each connector event must be an object.")
            identity = item.get("id")
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 255:
                raise ValueError("Connector event requires a stable ID.")
            if not isinstance(item.get("payload", {}), dict):
                raise ValueError("Connector event payload must be an object.")
            events.append(
                NormalizedEvent(
                    source=str(item.get("source") or self.name)[:40],
                    event_type=str(item.get("type") or "event")[:80],
                    external_id=identity,
                    payload=item.get("payload") or {},
                    tenant_key=item.get("tenant"),
                    title=str(item.get("title") or "")[:200],
                    description=str(item.get("description") or ""),
                )
            )
        return events


CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {
    cls.name: cls
    for cls in (
        JiraConnector,
        ServiceNowConnector,
        SlackConnector,
        MSTeamsConnector,
        GenericJsonConnector,
    )
}

REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "jira": ("base_url", "email", "api_token_env", "jql"),
    "servicenow": ("instance_url", "username_env", "password_env"),
    "slack": ("signing_secret_env",),
    "msteams": (),
    "generic": (),
}


CONNECTOR_ENDPOINT_KEYS = {"jira": "base_url", "servicenow": "instance_url"}
CONNECTOR_SECRET_SUFFIXES: dict[str, dict[str, str]] = {
    "jira": {"api_token_env": "API_TOKEN"},
    "servicenow": {"username_env": "USERNAME", "password_env": "PASSWORD"},
    "slack": {"signing_secret_env": "SIGNING_SECRET"},
}


def _connector_secret_env_name(connector_type: str, key: str, tenant_key: str | None) -> str:
    suffix = CONNECTOR_SECRET_SUFFIXES[connector_type][key]
    if tenant_key is None:
        scope = "CONNECTOR"
    else:
        tenant = tenant_key.upper().replace("-", "_")
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", tenant):
            raise ValueError("Connector tenant key is invalid for credential lookup.")
        scope = f"TENANT_{tenant}_CONNECTOR"
    return f"FLOWOPS_{scope}_{connector_type.upper()}_{suffix}"


def _normalise_host(host: str) -> str:
    try:
        normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("Connector URL has an invalid host.") from error
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(normalized) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", normalized):
        raise ValueError("Connector URL must use a valid host.")
    return normalized


def _allowed_connector_hosts(
    tenant_key: str | None, environ: dict[str, str]
) -> set[str]:
    raw = environ.get("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON", "")
    try:
        configured = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON must be a JSON object.") from error
    if not isinstance(configured, dict):
        raise ValueError("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON must be a JSON object.")
    scope = tenant_key if tenant_key is not None else "*"
    hosts = configured.get(scope, [])
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ValueError(f"No valid connector host allowlist is configured for {scope!r}.")
    return {_normalise_host(host) for host in hosts}


def validate_connector_config(
    connector_type: str,
    config: dict[str, Any],
    *,
    tenant_key: str | None = None,
    environ: dict[str, str] | None = None,
) -> None:
    """Check server-controlled credential references and outbound host policy."""
    values = environ if environ is not None else os.environ
    for key in CONNECTOR_SECRET_SUFFIXES.get(connector_type, {}):
        expected = _connector_secret_env_name(connector_type, key, tenant_key)
        if config.get(key) != expected:
            raise ValueError(
                f"Connector credential reference {key!r} must use its server-managed variable."
            )

    endpoint_key = CONNECTOR_ENDPOINT_KEYS.get(connector_type)
    if endpoint_key is None:
        return
    raw_url = config.get(endpoint_key)
    if not isinstance(raw_url, str):
        raise ValueError("Connector endpoint must be an HTTPS URL.")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Connector endpoint must be a valid HTTPS URL.") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Connector endpoint must be an HTTPS URL without credentials, query, or fragment.")
    host = _normalise_host(parsed.hostname)
    if host not in _allowed_connector_hosts(tenant_key, values):
        raise ValueError("Connector endpoint host is not approved for this tenant.")


def build_connector(
    connector_type: str, config: dict[str, Any], *, tenant_key: str | None = None
) -> SourceConnector:
    """Instantiate a connector from its stored JSON configuration.

    Keys ending in ``_env`` name environment variables holding the secret;
    they are resolved here and never persisted in the database.
    """
    cls = CONNECTOR_REGISTRY.get(connector_type)
    if cls is None:
        raise ValueError(f"Unknown connector type: {connector_type!r}")
    required = REQUIRED_CONFIG_KEYS[connector_type]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(f"Connector {connector_type!r} missing config keys: {missing}")
    validate_connector_config(connector_type, config, tenant_key=tenant_key)
    kwargs: dict[str, Any] = {}
    for key in required:
        if key.endswith("_env"):
            value = os.environ.get(str(config[key]), "")
            if not value:
                raise ValueError(
                    f"Connector {connector_type!r}: env var {config[key]!r} is not set"
                )
            kwargs[key[: -len("_env")]] = value
        else:
            kwargs[key] = config[key]
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Trigger rules
# ---------------------------------------------------------------------------


@dataclass
class FiredAction:
    rule_id: int
    rule_name: str
    action: str
    detail: str
    ok: bool
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "action": self.action,
            "detail": self.detail,
            "ok": self.ok,
            "skipped": self.skipped,
        }


def validate_rule_shape(
    predicate: str,
    action_type: str,
    request_type: str | None,
    notify_url: str | None,
    static_inputs_json: str | None,
    *,
    tenant_key: str | None = None,
    session: Session | None = None,
) -> None:
    """Fail fast on rules that could never fire correctly (used by the API)."""
    try:
        expressions.compile_predicate(predicate)
    except Exception as error:
        raise HTTPException(422, f"Invalid trigger predicate: {error}") from error
    if action_type == "start_workflow":
        if request_type not in (None, "onboarding", "offboarding"):
            raise HTTPException(422, "request_type must be onboarding or offboarding.")
        if static_inputs_json:
            try:
                inputs = json.loads(static_inputs_json)
            except json.JSONDecodeError as error:
                raise HTTPException(422, "static_inputs must be valid JSON.") from error
            if not isinstance(inputs, dict):
                raise HTTPException(422, "static_inputs must be a JSON object.")
            employees = inputs.get("employee_emails")
            if employees is not None and (
                not isinstance(employees, list)
                or not all(isinstance(e, str) and "@" in e for e in employees)
            ):
                raise HTTPException(
                    422, "static_inputs.employee_emails must be a list of emails."
                )
    elif action_type == "notify":
        if not notify_url:
            raise HTTPException(422, "notify rules require a notify_url.")
        if tenant_key and session is not None:
            try:
                validate_notify_destination(notify_url, tenant_key, session)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
    else:
        raise HTTPException(422, f"Unknown action_type: {action_type!r}")


def validate_notify_destination(
    url: str, tenant_key: str | None, session: Session
) -> str:
    """Validate static destination policy and return its normalized hostname."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise ValueError("Notify URL is invalid.") from error
    if (
        parsed.scheme.lower() != "https"
        or not host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
    ):
        raise ValueError("Notify URL must use HTTPS on port 443 without credentials, query, or fragment.")
    if tenant_key:
        tenant = session.get(db.TenantRecord, tenant_key)
        if tenant is None or not tenant.active or not tenant.credential_ref:
            raise ValueError("Notify destinations require an active tenant credential reference.")
        reference = tenant.credential_ref
        if not reference.isascii() or not reference.replace("_", "").isalnum():
            raise ValueError("Tenant credential reference is invalid.")
        configured = os.environ.get(
            f"FLOWOPS_TENANT_{reference.upper()}_NOTIFY_ALLOWED_HOSTS", ""
        )
        allowed_hosts = {
            allowed.strip().lower().rstrip(".")
            for allowed in configured.split(",")
            if allowed.strip()
        }
        if not allowed_hosts:
            raise ValueError(
                f"No notify destination hosts are configured for tenant {tenant_key!r}."
            )
        if host not in allowed_hosts:
            raise ValueError("Notify URL host is not allowlisted for this tenant.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Private notify destinations are not allowed.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Private notify destinations are not allowed.")
    return host


def _resolve_public_notify_addresses(host: str) -> list[str]:
    """Resolve immediately before delivery and retain only public destinations."""
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("Notify destination host could not be resolved.") from error
    resolved_ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not resolved_ips or any(
        not ipaddress.ip_address(address).is_global for address in resolved_ips
    ):
        raise ValueError("Private notify destinations are not allowed.")
    return resolved_ips


def _rule_allows_tenant(
    rule: db.TriggerRuleRecord, event: NormalizedEvent, inputs: dict[str, Any]
) -> bool:
    if rule.tenant_key:
        return event.tenant_key == rule.tenant_key
    if not event.tenant_key:
        return True
    allowed = inputs.get("allowed_tenants")
    if not isinstance(allowed, list):
        return False
    return event.tenant_key in {str(tenant) for tenant in allowed}


def evaluate_triggers(
    session: Session, event: NormalizedEvent, event_row: db.EventRecord
) -> list[FiredAction]:
    """Run all enabled trigger rules matching this event. Never raises."""
    fired: list[FiredAction] = []
    stmt = (
        select(db.TriggerRuleRecord)
        .where(db.TriggerRuleRecord.enabled.is_(True))
        .where(
            or_(
                db.TriggerRuleRecord.event_type == event.event_type,
                db.TriggerRuleRecord.event_type == "*",
            )
        )
        .order_by(db.TriggerRuleRecord.id)
    )
    for rule in session.execute(stmt).scalars():
        if rule.source and rule.source != event.source:
            continue
        # GDAP scoping: a tenant-bound rule fires only for events carrying
        # the same tenant; unbound (global) rules fire for any event.
        if rule.tenant_key and rule.tenant_key != event.tenant_key:
            continue
        try:
            matched = bool(
                expressions.evaluate(rule.predicate, event.trigger_context())
            )
        except Exception as error:
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action="trigger.rule_error",
                status="failure",
                detail=f"rule={rule.id}; {type(error).__name__}: {error}",
            )
            continue
        if matched:
            fired.append(_fire_rule(session, rule, event, event_row))
    return fired


def _fire_rule(
    session: Session,
    rule: db.TriggerRuleRecord,
    event: NormalizedEvent,
    event_row: db.EventRecord,
) -> FiredAction:
    correlation = f"event:{rule.id}:{event_row.id}"
    idem_key = f"trigger:{correlation}"
    # Lock the rule row so concurrent firings of the same (rule, event)
    # serialize. On SQLite with_for_update is a no-op; the unique event
    # fingerprint already serializes ingestion there.
    locked = session.execute(
        select(db.TriggerRuleRecord)
        .where(db.TriggerRuleRecord.id == rule.id)
        .with_for_update()
    ).scalar_one()
    already = session.execute(
        select(db.ExecutionLogRecord.run_id).where(
            db.ExecutionLogRecord.run_id == idem_key,
            db.ExecutionLogRecord.status == "success",
        )
    ).scalar_one_or_none()
    if already is not None:
        return FiredAction(
            locked.id, locked.name, locked.action_type, "already fired", ok=True, skipped=True
        )
    try:
        inputs = json.loads(locked.static_inputs_json or "{}")
        if not isinstance(inputs, dict):
            inputs = {}
    except json.JSONDecodeError:
        inputs = {}
    if not _rule_allows_tenant(locked, event, inputs):
        # Roll back only the idempotency-log row added above so a blocked
        # firing can be retried later (e.g. after the tenant is allow-listed)
        # without poisoning the (rule, event) key or aborting the savepoint.
        session.flush()
        session.begin_nested().rollback()
        return FiredAction(
            locked.id, locked.name, locked.action_type,
            f"tenant {event.tenant_key!r} not allowed by rule", ok=False,
        )
    if locked.action_type == "start_workflow":
        result = _fire_start_workflow(session, locked, event, correlation, idem_key, inputs)
    elif locked.action_type == "notify":
        result = _fire_notify(session, locked, event, correlation, idem_key)
    else:
        result = FiredAction(
            locked.id, locked.name, locked.action_type, "unknown action type", ok=False
        )
    if result.ok and not result.skipped:
        locked.fired_count += 1
    return result


def _fire_start_workflow(
    session: Session,
    rule: db.TriggerRuleRecord,
    event: NormalizedEvent,
    correlation: str,
    idem_key: str,
    inputs: dict[str, Any],
) -> FiredAction:
    """Create onboarding request(s) from the rule template and start runs.

    Reuses ``routes.onboarding._create_onboarding_request`` so triggers get the same
    validation, checklist generation, definition pinning, timers, and audit
    trail as the operator console. Late import avoids the api->routes cycle.
    """
    from src.routes.onboarding import _create_onboarding_request

    request_type = rule.request_type or "onboarding"
    tenant_key = event.tenant_key or rule.tenant_key
    # Fail fast with a clean FiredAction if the tenant is unknown/inactive,
    # instead of letting _create_onboarding_request's HTTPException abort the
    # ingestion savepoint.
    if tenant_key is not None:
        tenant = session.get(db.TenantRecord, tenant_key)
        if tenant is None or not tenant.active:
            return FiredAction(
                rule.id, rule.name, rule.action_type,
                f"unknown or inactive tenant {tenant_key!r}", ok=False,
            )
    emails = inputs.get("employee_emails") or [
        f"{event.source}-trigger@flowops.local"
    ]
    started_runs: list[int] = []
    created_requests: list[int] = []
    for index, email in enumerate(emails):
        payload = OnboardingRequestIn(
            request_type=request_type,
            employee_name=str(
                inputs.get("employee_names", [None] * len(emails))[index]
                or f"{event.source} trigger {event.external_id}"
            )[:200],
            employee_email=str(email)[:255],
            department=str(inputs.get("department") or f"trigger:{rule.name}")[:120],
            requester_id=1,
            start_date=inputs.get("start_date"),
            definition_key=inputs.get("definition_key"),
            definition_version=inputs.get("definition_version"),
            template=inputs.get("template"),
            tenant_key=tenant_key,
        )
        record = _create_onboarding_request(
            payload, session, actor_subject=f"trigger:{rule.id}"
        )
        created_requests.append(record.id)
        run = scheduler.start_run(
            session, record.id, actor_subject=f"trigger:{rule.id}"
        )
        started_runs.append(run.id)
    db.log_execution(
        session,
        run_id=idem_key,
        action="trigger.started_workflow",
        status="success",
        detail=f"rule={rule.id}; requests={created_requests}; runs={started_runs}; "
        f"event={event.fingerprint()}",
        correlation_id=correlation,
    )
    return FiredAction(
        rule.id,
        rule.name,
        rule.action_type,
        f"started {len(started_runs)} run(s): {started_runs}",
        ok=True,
    )


def _fire_notify(
    session: Session,
    rule: db.TriggerRuleRecord,
    event: NormalizedEvent,
    correlation: str,
    idem_key: str,
) -> FiredAction:
    url = rule.notify_url or os.environ.get("FLOWOPS_TRIGGER_WEBHOOK_URL", "")
    if not url:
        db.log_execution(
            session,
            run_id=idem_key,
            action="trigger.notify_failed",
            status="failure",
            detail=f"rule={rule.id}: no notify_url configured",
            correlation_id=correlation,
        )
        return FiredAction(
            rule.id, rule.name, rule.action_type, "no notify_url configured", ok=False
        )
    if event.tenant_key is not None:
        try:
            validate_notify_destination(url, event.tenant_key, session)
        except ValueError as error:
            db.log_execution(
                session,
                run_id=idem_key,
                action="trigger.notify_failed",
                status="failure",
                detail=f"rule={rule.id}: {error}",
                correlation_id=correlation,
            )
            return FiredAction(rule.id, rule.name, rule.action_type, str(error), ok=False)
    payload = {
        "event": "trigger.fired",
        "rule_id": rule.id,
        "rule_name": rule.name,
        "source": event.source,
        "event_type": event.event_type,
        "external_id": event.external_id,
        "title": event.title,
        "tenant": event.tenant_key,
        "payload": event.payload,
    }
    session.add(
        db.NotificationOutboxRecord(
            url=url,
            payload_json=json.dumps(payload, separators=(",", ":")),
            ticket_id=None,
            status="pending",
            attempts=0,
            last_error=None,
        )
    )
    db.log_execution(
        session,
        run_id=idem_key,
        action="trigger.notified",
        status="success",
        detail=f"rule={rule.id}; event={event.fingerprint()}",
        correlation_id=correlation,
    )
    return FiredAction(
        rule.id, rule.name, rule.action_type, "queued outbox notification", ok=True
    )


def deliver_outbox_notification(
    url: str, payload: dict[str, Any], session: Session, *, max_attempts: int = 1
):
    """Revalidate trigger destinations before each durable outbox delivery."""
    if payload.get("event") == "trigger.fired" and payload.get("tenant"):
        from src.notify import DeliveryResult, deliver_webhook

        try:
            host = validate_notify_destination(url, payload.get("tenant"), session)
            resolved_ips = _resolve_public_notify_addresses(host)
        except ValueError as error:
            return DeliveryResult(success=False, attempts=1, last_error=str(error))
        return deliver_webhook(
            url,
            payload,
            max_attempts=max_attempts,
            pinned_ip=resolved_ips[0],
            allow_redirects=False,
        )
    from src.notify import deliver_webhook

    return deliver_webhook(url, payload, max_attempts=max_attempts)


# ---------------------------------------------------------------------------
# Ingestion pipeline (shared by poller loop and API routes)
# ---------------------------------------------------------------------------


def ingest_event(session: Session, event: NormalizedEvent) -> dict[str, Any]:
    """Persist the raw event and run trigger rules over it.

    The caller owns the outer transaction. Returns a summary dict used
    directly in API responses. Duplicate fingerprints are no-ops.
    """
    fingerprint = event.fingerprint()
    existing = session.execute(
        select(db.EventRecord).where(db.EventRecord.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "fingerprint": fingerprint,
            "event_id": existing.id,
            "duplicate": True,
            "triggers_fired": [],
        }

    payload_json = json.dumps(event.payload, separators=(",", ":"))
    if len(payload_json.encode()) > MAX_EVENT_PAYLOAD_BYTES:
        raise HTTPException(413, "Event payload exceeds the 64 KiB limit.")

    row = db.EventRecord(
        source=event.source[:40],
        event_type=event.event_type[:80],
        external_id=event.external_id[:255],
        fingerprint=fingerprint,
        payload_json=payload_json,
        tenant_key=event.tenant_key,
        connector_id=event.connector_id,
    )
    # A simultaneous poll/webhook may insert the same fingerprint between
    # our initial read and flush. Resolve that conflict in a savepoint, not
    # by rolling back unrelated work in the caller's transaction.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.execute(
            select(db.EventRecord).where(db.EventRecord.fingerprint == fingerprint)
        ).scalar_one_or_none()
        if existing is None:
            raise
        return {"fingerprint": fingerprint, "event_id": existing.id,
                "duplicate": True, "triggers_fired": []}

    fired = evaluate_triggers(session, event, row)

    row.processed_at = datetime.now(timezone.utc)
    return {
        "fingerprint": fingerprint,
        "event_id": row.id,
        "duplicate": False,
        "triggers_fired": [f.as_dict() for f in fired],
    }


def ingest_events(session: Session, events: list[NormalizedEvent]) -> list[dict[str, Any]]:
    """Ingest a batch; each event gets its own savepoint. Returns summaries.

    One failing event (bad rule side effect, oversized payload) rolls back
    only that event's writes and is reported in the summary — the rest of the
    batch still commits.
    """
    results = []
    for event in events:
        savepoint = session.begin_nested()
        try:
            results.append(ingest_event(session, event))
            savepoint.commit()
        except HTTPException:
            savepoint.rollback()
            raise
        except Exception as error:
            savepoint.rollback()
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action="connector.event_failed",
                status="failure",
                detail=f"{event.fingerprint()}: {type(error).__name__}",
            )
            results.append(
                {
                    "fingerprint": event.fingerprint(),
                    "event_id": None,
                    "duplicate": False,
                    "error": f"{type(error).__name__}: event processing failed.",
                    "triggers_fired": [],
                }
            )
    return results


# ---------------------------------------------------------------------------
# Polling worker
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = max(
    5, int(os.environ.get("FLOWOPS_CONNECTOR_POLL_INTERVAL", "60"))
)
#: Floor for a per-connector ``poll_interval`` config — protects external
#: systems from a scheduler that ticks every couple of seconds.
MIN_CONNECTOR_POLL_SECONDS = 30
#: Backoff multiplier cap: a failing source is retried at most 32x its base
#: interval (e.g. 5m base -> 2h40m between attempts) until it succeeds once.
MAX_BACKOFF_MULTIPLIER = 32


def _as_utc(value: datetime) -> datetime:
    """Normalize scheduler timestamps before comparing persisted values."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _poll_state(record: "db.ConnectorRecord") -> dict[str, Any]:
    try:
        state = json.loads(record.cursor_json or "{}")
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    return {
        "failures": max(0, _safe_int(state.get("failures"))),
        "last_batch": max(0, _safe_int(state.get("last_batch"))),
        "cursor": state.get("cursor") if isinstance(state.get("cursor"), str) else None,
        "config_digest": state.get("config_digest"),
    }


def _connector_base_interval(record: "db.ConnectorRecord") -> int:
    try:
        configured = json.loads(record.config_json or "{}").get("poll_interval")
    except (TypeError, ValueError):
        configured = None
    if configured is None:
        return POLL_INTERVAL_SECONDS
    try:
        return max(MIN_CONNECTOR_POLL_SECONDS, int(configured))
    except (TypeError, ValueError):
        return POLL_INTERVAL_SECONDS


def connector_due(record: "db.ConnectorRecord", now: datetime) -> bool:
    """True when this polling connector should be polled at ``now``.

    A connector is due when ``now - last_polled_at`` covers its effective
    interval: the base interval (config ``poll_interval``, else the global
    default) multiplied by exponential backoff for consecutive failures.
    """
    now = _as_utc(now)
    state = _poll_state(record)
    interval = _connector_base_interval(record) * min(
        2 ** min(state["failures"], 8), MAX_BACKOFF_MULTIPLIER
    )
    if record.last_polled_at is None:
        return True
    last = record.last_polled_at
    if last.tzinfo is None:  # SQLite returns naive datetimes
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= interval


def poll_all_connectors(
    now: datetime | None = None,
    *,
    only_due: bool = True,
    tenant_key: str | None = None,
) -> int:
    """Poll due connectors once. Returns events ingested.

    When supplied, ``tenant_key`` restricts polling to that tenant's connectors.
    Each connector runs in its own session/transaction so one failing source
    (bad credentials, network outage) never blocks the others; failures are
    recorded in the execution log for the operator console and push the
    connector's next attempt back exponentially.
    """
    at = _as_utc(now or datetime.now(timezone.utc))
    count = 0
    session = db.SessionLocal()
    try:
        db._ensure_tables()
        query = select(db.ConnectorRecord).where(
            db.ConnectorRecord.enabled.is_(True),
            db.ConnectorRecord.kind == "polling",
        )
        if tenant_key is not None:
            query = query.where(db.ConnectorRecord.tenant_key == tenant_key)
        records = session.execute(query).scalars().all()
        if only_due:
            records = [record for record in records if connector_due(record, at)]
    finally:
        session.close()

    for candidate in records:
        session = db.SessionLocal()
        try:
            # PostgreSQL locks one connector at a time; recheck due state
            # after claiming it so competing pollers do not fetch twice.
            query = select(db.ConnectorRecord).where(
                db.ConnectorRecord.id == candidate.id,
                db.ConnectorRecord.enabled.is_(True),
                db.ConnectorRecord.kind == "polling",
            )
            if tenant_key is not None:
                query = query.where(db.ConnectorRecord.tenant_key == tenant_key)
            if session.get_bind().dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            record = session.execute(query).scalar_one_or_none()
            if record is None or (only_due and not connector_due(record, at)):
                continue
            try:
                with session.begin_nested():
                    if record.tenant_key:
                        tenant = session.get(db.TenantRecord, record.tenant_key)
                        if tenant is None or not tenant.active:
                            raise ValueError("Connector tenant is missing or inactive.")
                    connector = build_connector(
                        record.connector_type,
                        json.loads(record.config_json or "{}"),
                        tenant_key=record.tenant_key,
                    )
                    state = _poll_state(record)
                    config_digest = hashlib.sha256((record.config_json or "{}").encode()).hexdigest()
                    cursor = state["cursor"] if state["config_digest"] == config_digest else None
                    fetch_page = getattr(connector, "fetch_page", None)
                    if fetch_page is None:
                        events, next_cursor = connector.fetch_events(session), None
                    else:
                        events, next_cursor = fetch_page(session, cursor)
                    events = [
                        NormalizedEvent(
                            source=event.source, event_type=event.event_type,
                            external_id=event.external_id, payload=event.payload,
                            tenant_key=record.tenant_key, connector_id=record.id,
                            title=event.title, description=event.description,
                        )
                        for event in events
                    ]
                    summaries = ingest_events(session, events)
                    ingested = sum(1 for s in summaries if not s["duplicate"] and "error" not in s)
                    failed = sum(1 for s in summaries if "error" in s)
                    # Successful rows commit even if a peer fails. In that case
                    # replay the same page and let event fingerprints dedupe.
                    state["cursor"] = cursor if failed else next_cursor
                    state["config_digest"] = config_digest
                    state["failures"] = min(32, state["failures"] + 1) if failed else 0
                    state["last_batch"] = len(events)
                    record.cursor_json = json.dumps(state)
                    record.last_polled_at = at
                session.commit()
                count += ingested
            except Exception as error:
                # Roll back only the savepoint; keep the claim until the
                # failure/backoff state has been persisted.
                state = _poll_state(record)
                state["failures"] = min(32, state["failures"] + 1)
                record.last_polled_at = at
                record.cursor_json = json.dumps(state)
                db.log_execution(
                    session, run_id=_new_run_id(), action="connector.poll_failed",
                    status="failure",
                    detail=f"connector={record.id} ({record.connector_type}): {type(error).__name__}",
                )
                session.commit()
        finally:
            session.close()
    return count
