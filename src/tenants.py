"""Multi-tenant (MSP / GDAP) support.

One partner app registration acts across many customer tenants: a token is
requested from each customer's Entra authority with the same client id.
Each tenant can differ in licence SKU, usage location, PSA company and an
expected configuration *baseline*; :func:`run_drift_check` compares live
Graph state against that baseline for every tenant and keeps going when one
tenant fails, so a single broken consent never hides drift elsewhere.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from src import db
from src.graph import (
    ClientCredentialsToken,
    GraphClient,
    GraphError,
    StaticToken,
    TokenProvider,
    available_units,
)

DEFAULT_TENANT_KEY = "default"
DEFAULT_BASELINE: dict[str, Any] = {
    "required_skus": [],
    "min_free_licences": 1,
    "security_defaults": True,
    "max_unlicensed_users": 0,
}


@dataclass(frozen=True)
class TenantSettings:
    key: str
    name: str
    entra_tenant_id: str | None = None
    default_domain: str | None = None
    licence_sku: str | None = None
    usage_location: str | None = None
    psa_company_id: str | None = None
    credential_ref: str | None = None
    baseline: dict[str, Any] = field(default_factory=dict)
    active: bool = True

    @classmethod
    def from_record(cls, record: db.TenantRecord) -> TenantSettings:
        try:
            baseline = json.loads(record.baseline_json) if record.baseline_json else {}
        except ValueError:
            baseline = {}
        return cls(
            key=record.key,
            name=record.name,
            entra_tenant_id=record.entra_tenant_id,
            default_domain=record.default_domain,
            licence_sku=record.licence_sku,
            usage_location=record.usage_location,
            psa_company_id=record.psa_company_id,
            credential_ref=record.credential_ref,
            baseline=baseline if isinstance(baseline, dict) else {},
            active=bool(record.active),
        )

    def effective_baseline(self) -> dict[str, Any]:
        return {**DEFAULT_BASELINE, **self.baseline}


def default_settings(environ: Mapping[str, str] | None = None) -> TenantSettings:
    """Settings for single-tenant deployments, read from the environment."""
    values = environ if environ is not None else os.environ
    return TenantSettings(
        key=DEFAULT_TENANT_KEY,
        name="Default tenant",
        entra_tenant_id=values.get("FLOWOPS_GRAPH_TENANT_ID") or None,
        default_domain=values.get("FLOWOPS_GRAPH_DEFAULT_DOMAIN") or None,
        licence_sku=values.get("FLOWOPS_GRAPH_DEFAULT_SKU") or None,
        usage_location=values.get("FLOWOPS_GRAPH_USAGE_LOCATION") or None,
    )


def load_tenant(key: str, session: db.Session | None = None) -> TenantSettings | None:
    if session is not None:
        record = session.get(db.TenantRecord, key)
        return TenantSettings.from_record(record) if record else None
    db._ensure_tables()
    with db.SessionLocal() as own_session:
        record = own_session.get(db.TenantRecord, key)
        return TenantSettings.from_record(record) if record else None


def list_tenants(session: db.Session, *, active_only: bool = False) -> list[TenantSettings]:
    query = select(db.TenantRecord).order_by(db.TenantRecord.key)
    if active_only:
        query = query.where(db.TenantRecord.active.is_(True))
    return [TenantSettings.from_record(r) for r in session.execute(query).scalars()]


def credentials_for(
    settings: TenantSettings, environ: Mapping[str, str] | None = None
) -> tuple[str | None, str | None]:
    """Resolve (client_id, client_secret): per-tenant override, else partner app."""
    values = environ if environ is not None else os.environ
    client_id = values.get("FLOWOPS_GRAPH_CLIENT_ID")
    client_secret = values.get("FLOWOPS_GRAPH_CLIENT_SECRET")
    if settings.credential_ref:
        prefix = f"FLOWOPS_TENANT_{settings.credential_ref.upper()}_"
        client_id = values.get(prefix + "GRAPH_CLIENT_ID") or client_id
        client_secret = values.get(prefix + "GRAPH_CLIENT_SECRET") or client_secret
    return client_id or None, client_secret or None


GraphResolver = Callable[[str | None], tuple[GraphClient, TenantSettings]]


class GraphClientFactory:
    """Build a :class:`GraphClient` for a tenant key (``None`` = default)."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        simulated: bool = False,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        loader: Callable[[str], TenantSettings | None] = load_tenant,
    ) -> None:
        self.environ = dict(environ if environ is not None else os.environ)
        self.simulated = simulated
        self.http_client = http_client
        self.sleep = sleep
        self.loader = loader
        self._tokens: dict[tuple[str, str], TokenProvider] = {}
        self._simulated_clients: dict[str, GraphClient] = {}
        self._lock = threading.Lock()

    def settings_for(self, tenant_key: str | None) -> TenantSettings:
        if not tenant_key or tenant_key == DEFAULT_TENANT_KEY:
            stored = self.loader(DEFAULT_TENANT_KEY) if tenant_key else None
            if stored is not None and not stored.active:
                raise GraphError(
                    f"tenant {DEFAULT_TENANT_KEY!r} is inactive",
                    code="TenantInactive",
                    status_code=409,
                )
            return stored or default_settings(self.environ)
        settings = self.loader(tenant_key)
        if settings is None:
            raise GraphError(f"tenant {tenant_key!r} is not registered", code="TenantNotFound", status_code=404)
        if not settings.active:
            raise GraphError(f"tenant {tenant_key!r} is inactive", code="TenantInactive", status_code=409)
        return settings

    def __call__(self, tenant_key: str | None) -> tuple[GraphClient, TenantSettings]:
        settings = self.settings_for(tenant_key)
        kwargs: dict[str, Any] = {"base_url": self.environ.get("FLOWOPS_GRAPH_URL") or "https://graph.microsoft.com/v1.0"}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        if self.simulated:
            from src.graph_simulator import simulated_graph_client

            with self._lock:
                client = self._simulated_clients.get(settings.key)
                if client is None:
                    client = simulated_graph_client(settings)
                    self._simulated_clients[settings.key] = client
            return client, settings
        return GraphClient(self._token_provider(settings), http_client=self.http_client, **kwargs), settings

    def _token_provider(self, settings: TenantSettings) -> TokenProvider:
        static = self.environ.get("FLOWOPS_GRAPH_ACCESS_TOKEN")
        if static and settings.key == DEFAULT_TENANT_KEY:
            return StaticToken(static)
        client_id, client_secret = credentials_for(settings, self.environ)
        if not (settings.entra_tenant_id and client_id and client_secret):
            raise GraphError(
                f"tenant {settings.key!r} needs entra_tenant_id plus FLOWOPS_GRAPH_CLIENT_ID/"
                "FLOWOPS_GRAPH_CLIENT_SECRET (or a per-tenant credential_ref)",
                code="CredentialsMissing",
                status_code=401,
            )
        cache_key = (settings.entra_tenant_id, client_id)
        with self._lock:
            provider = self._tokens.get(cache_key)
            if provider is None:
                provider = ClientCredentialsToken(
                    settings.entra_tenant_id,
                    client_id,
                    client_secret,
                    authority=self.environ.get("FLOWOPS_GRAPH_AUTHORITY") or "https://login.microsoftonline.com",
                    http_client=self.http_client,
                )
                self._tokens[cache_key] = provider
            return provider


def _finding(check: str, ok: bool, expected: Any, actual: Any, detail: str = "") -> dict[str, Any]:
    return {
        "check": check,
        "status": "ok" if ok else "drift",
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def check_drift(settings: TenantSettings, client: GraphClient) -> dict[str, Any]:
    """Compare one tenant's live Graph state with its baseline."""
    baseline = settings.effective_baseline()
    findings: list[dict[str, Any]] = []
    skus = {str(s.get("skuPartNumber")).upper(): s for s in client.subscribed_skus()}
    required = [str(s).upper() for s in baseline.get("required_skus") or []]
    if settings.licence_sku and settings.licence_sku.upper() not in required:
        required.append(settings.licence_sku.upper())
    min_free = int(baseline.get("min_free_licences") or 0)
    for part in required:
        sku = skus.get(part)
        if sku is None:
            findings.append(_finding(f"sku:{part}", False, "subscribed", "missing"))
            continue
        free = available_units(sku)
        findings.append(
            _finding(f"sku:{part}:free_units", free >= min_free, f">= {min_free}", free)
        )
    if "security_defaults" in baseline and baseline["security_defaults"] is not None:
        policy = client.request("GET", "policies/identitySecurityDefaultsEnforcementPolicy") or {}
        enabled = bool(policy.get("isEnabled"))
        findings.append(
            _finding("security_defaults", enabled == bool(baseline["security_defaults"]), baseline["security_defaults"], enabled)
        )
    unlicensed = [
        u.get("userPrincipalName")
        for u in client.paginate(
            "users",
            params={
                "$filter": "accountEnabled eq true",
                "$select": "id,userPrincipalName,assignedLicenses",
                "$top": "100",
            },
        )
        if not u.get("assignedLicenses")
    ]
    limit = int(baseline.get("max_unlicensed_users") or 0)
    findings.append(
        _finding(
            "unlicensed_enabled_users",
            len(unlicensed) <= limit,
            f"<= {limit}",
            len(unlicensed),
            ", ".join(sorted(filter(None, unlicensed))[:10]),
        )
    )
    drifted = [f for f in findings if f["status"] == "drift"]
    return {
        "tenant": settings.key,
        "name": settings.name,
        "status": "drift" if drifted else "compliant",
        "drift_count": len(drifted),
        "findings": findings,
        "error": None,
    }


def run_drift_check(
    tenants: list[TenantSettings], resolve: GraphResolver
) -> list[dict[str, Any]]:
    """Check every tenant; one failing tenant is reported, not fatal."""
    results = []
    for settings in tenants:
        try:
            client, resolved = resolve(settings.key)
            results.append(check_drift(resolved, client))
        except Exception as error:  # surfaced per tenant, never swallowed
            results.append(
                {
                    "tenant": settings.key,
                    "name": settings.name,
                    "status": "error",
                    "drift_count": 0,
                    "findings": [],
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return results
