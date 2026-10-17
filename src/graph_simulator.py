"""Stateful fake Microsoft Graph served through ``httpx.MockTransport``.

The real :class:`src.graph.GraphClient` talks to it, so token handling,
OData filters, paging, throttling and error parsing are all exercised
without a Microsoft 365 tenant. Each tenant gets a deterministic licence
profile so the multi-tenant drift check has something to find.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from src.graph import GraphClient, StaticToken

PAGE_SIZE = 2
PROFILES = ("healthy", "licence_exhausted", "security_defaults_off")
SKU_IDS = {
    "O365_BUSINESS_PREMIUM": "f245ecc8-75af-4f8e-b61f-27d8114de5f3",
    "EXCHANGESTANDARD": "4b9405b0-7788-4568-add1-99614e613b69",
    "SPE_E3": "05e9a617-0261-4cee-bb44-138d3ef5d965",
}


def profile_for(tenant_key: str, override: str | None = None) -> str:
    if override in PROFILES:
        return str(override)
    digest = hashlib.sha256(tenant_key.encode()).digest()
    return PROFILES[digest[0] % len(PROFILES)]


class SimulatedTenant:
    def __init__(self, tenant_key: str, domain: str, profile: str) -> None:
        self.tenant_key = tenant_key
        self.domain = domain
        self.profile = profile
        premium_enabled = 5
        premium_consumed = 5 if profile == "licence_exhausted" else 2
        self.skus = [
            {
                "skuId": SKU_IDS["O365_BUSINESS_PREMIUM"],
                "skuPartNumber": "O365_BUSINESS_PREMIUM",
                "prepaidUnits": {"enabled": premium_enabled},
                "consumedUnits": premium_consumed,
            },
            {
                "skuId": SKU_IDS["EXCHANGESTANDARD"],
                "skuPartNumber": "EXCHANGESTANDARD",
                "prepaidUnits": {"enabled": 10},
                "consumedUnits": 1,
            },
        ]
        self.security_defaults = profile != "security_defaults_off"
        self.users: dict[str, dict[str, Any]] = {}
        licensed = [{"skuId": SKU_IDS["O365_BUSINESS_PREMIUM"], "disabledPlans": []}]
        for name, licences in (("admin", licensed), ("reception", licensed), ("scanner", [])):
            self._add_user(f"{name}@{domain}", name.title(), licences)

    def _add_user(self, upn: str, display_name: str, licences: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        user = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.tenant_key}:{upn.lower()}")),
            "displayName": display_name,
            "userPrincipalName": upn,
            "accountEnabled": True,
            "assignedLicenses": licences,
            "usageLocation": extra.pop("usageLocation", None),
            "department": extra.pop("department", None),
            "mailboxSettings": None,
        }
        self.users[user["id"]] = user
        return user

    # -- request handling --------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _error(401, "InvalidAuthenticationToken", "Access token is empty.")
        url = urlparse(str(request.url))
        path = re.sub(r"^/v1\.0/", "", url.path).strip("/")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = json.loads(request.content) if request.content else {}
        method = request.method

        if path == "subscribedSkus" and method == "GET":
            return httpx.Response(200, json={"value": self.skus})
        if path == "policies/identitySecurityDefaultsEnforcementPolicy" and method == "GET":
            return httpx.Response(200, json={"isEnabled": self.security_defaults})
        if path == "users" and method == "GET":
            return self._list_users(str(request.url).split("?")[0], query)
        if path == "users" and method == "POST":
            upn = body["userPrincipalName"]
            if any(u["userPrincipalName"].lower() == upn.lower() for u in self.users.values()):
                return _error(400, "Request_BadRequest", "Another object with the same value for property userPrincipalName already exists.")
            user = self._add_user(
                upn, body["displayName"], [],
                usageLocation=body.get("usageLocation"), department=body.get("department"),
            )
            return httpx.Response(201, json=_public(user))
        match = re.fullmatch(r"users/([^/]+)(?:/(.+))?", path)
        if match:
            user = self.users.get(match.group(1))
            if user is None:
                return _error(404, "Request_ResourceNotFound", f"Resource '{match.group(1)}' does not exist.")
            sub = match.group(2)
            if sub is None and method == "PATCH":
                user.update({k: v for k, v in body.items() if k in ("accountEnabled", "usageLocation", "department", "displayName")})
                return httpx.Response(204)
            if sub == "revokeSignInSessions" and method == "POST":
                return httpx.Response(200, json={"value": True})
            if sub == "mailboxSettings" and method == "PATCH":
                if not user["assignedLicenses"]:
                    return _error(404, "MailboxNotEnabledForRESTAPI", "The mailbox is either inactive, soft-deleted, or is hosted on-premise.")
                user["mailboxSettings"] = body
                return httpx.Response(200, json=body)
            if sub == "assignLicense" and method == "POST":
                if not user.get("usageLocation"):
                    return _error(400, "Request_BadRequest", "License assignment cannot be done for user with invalid usage location.")
                for remove in body.get("removeLicenses", []):
                    user["assignedLicenses"] = [l for l in user["assignedLicenses"] if l["skuId"] != remove]
                    self._sku_by_id(remove)["consumedUnits"] -= 1
                for add in body.get("addLicenses", []):
                    sku = self._sku_by_id(add["skuId"])
                    if sku["consumedUnits"] >= sku["prepaidUnits"]["enabled"]:
                        return _error(400, "Request_BadRequest", "Subscription has no available licenses.")
                    sku["consumedUnits"] += 1
                    user["assignedLicenses"].append(add)
                return httpx.Response(200, json=_public(user))
        return _error(400, "BadRequest", f"Unsupported simulated request {method} /{path}")

    def _sku_by_id(self, sku_id: str) -> dict[str, Any]:
        return next(s for s in self.skus if s["skuId"] == sku_id)

    def _list_users(self, base: str, query: dict[str, str]) -> httpx.Response:
        users = sorted(self.users.values(), key=lambda u: u["userPrincipalName"])
        flt = query.get("$filter", "")
        upn = re.fullmatch(r"userPrincipalName eq '((?:[^']|'')*)'", flt)
        if upn:
            wanted = upn.group(1).replace("''", "'").lower()
            users = [u for u in users if u["userPrincipalName"].lower() == wanted]
        elif flt == "accountEnabled eq true":
            users = [u for u in users if u["accountEnabled"]]
        elif flt:
            return _error(400, "Request_UnsupportedQuery", f"Unsupported query: {flt}")
        skip = int(query.get("$skiptoken", "0"))
        page = users[skip: skip + PAGE_SIZE]
        payload: dict[str, Any] = {"value": [_public(u) for u in page]}
        if skip + PAGE_SIZE < len(users):
            next_query = {k: v for k, v in query.items() if k != "$skiptoken"}
            next_query["$skiptoken"] = str(skip + PAGE_SIZE)
            payload["@odata.nextLink"] = f"{base}?{urlencode(next_query)}"
        return httpx.Response(200, json=payload)


def _public(user: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user.items() if k != "mailboxSettings"}


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": message}},
        headers={"request-id": str(uuid.uuid4())},
    )


def simulated_graph_client(settings: Any) -> GraphClient:
    """GraphClient wired to an in-memory tenant built from TenantSettings."""
    key = getattr(settings, "key", "default")
    domain = getattr(settings, "default_domain", None) or f"{key}.onmicrosoft.com"
    baseline = getattr(settings, "baseline", {}) or {}
    tenant = SimulatedTenant(key, domain, profile_for(key, baseline.get("simulated_profile")))
    client = httpx.Client(transport=httpx.MockTransport(tenant.handle))
    graph = GraphClient(StaticToken("simulated"), http_client=client, sleep=lambda _s: None)
    graph.simulated_tenant = tenant  # type: ignore[attr-defined]
    return graph
