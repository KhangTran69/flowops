"""Microsoft Graph client for FlowOps actions.

Covers the parts of Graph an MSP automation actually touches:

* OAuth2 client-credentials tokens per tenant (works for GDAP / multi-tenant
  app registrations: the same app id requests a token from each customer
  tenant's authority), cached until shortly before expiry.
* Throttling: 429/503/504 are retried, honouring ``Retry-After``.
* Paging: ``@odata.nextLink`` is followed until exhausted.
* OData ``$filter`` values are quoted safely (single quotes doubled).
* Errors are raised as :class:`GraphError` with Graph's error code and
  ``request-id`` so failures are diagnosable instead of swallowed.

``http_client`` and ``sleep`` are injectable so the test suite runs offline
with ``httpx.MockTransport``.
"""

from __future__ import annotations

import secrets
import string
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Protocol

import httpx

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
LOGIN_AUTHORITY = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
RETRYABLE_STATUS = frozenset({429, 503, 504})
USER_SELECT = "id,displayName,userPrincipalName,accountEnabled,department,usageLocation,assignedLicenses"


class GraphError(RuntimeError):
    """A non-successful Graph or token-endpoint response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        detail = message
        if code:
            detail = f"{code}: {detail}"
        if status_code is not None:
            detail = f"HTTP {status_code} {detail}"
        if request_id:
            detail = f"{detail} (request-id {request_id})"
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code in RETRYABLE_STATUS or (
            self.status_code >= 500
        )


def odata_quote(value: str) -> str:
    """Quote a string literal for an OData ``$filter`` expression."""
    return "'" + str(value).replace("'", "''") + "'"


def odata_eq(field: str, value: str) -> str:
    return f"{field} eq {odata_quote(value)}"


class TokenProvider(Protocol):
    def token(self) -> str:
        """Return a bearer token valid for at least the next request."""


@dataclass
class StaticToken:
    """Pre-issued access token (useful for local experiments)."""

    access_token: str

    def token(self) -> str:
        return self.access_token


class ClientCredentialsToken:
    """OAuth2 client-credentials flow against one Entra ID tenant."""

    refresh_margin_seconds = 300

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        authority: str = LOGIN_AUTHORITY,
        scope: str = GRAPH_SCOPE,
        http_client: Optional[httpx.Client] = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (tenant_id and client_id and client_secret):
            raise ValueError("tenant_id, client_id and client_secret are required")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret = client_secret
        self.token_url = f"{authority.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"
        self.scope = scope
        self.http_client = http_client
        self.timeout = timeout
        self.clock = clock
        self._access_token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never print the secret
        return f"ClientCredentialsToken(tenant_id={self.tenant_id!r}, client_id={self.client_id!r})"

    def token(self) -> str:
        with self._lock:
            if self._access_token and self.clock() < self._expires_at:
                return self._access_token
            client = self.http_client or httpx.Client(timeout=self.timeout)
            try:
                response = client.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self._client_secret,
                        "scope": self.scope,
                    },
                )
            except httpx.HTTPError as error:
                raise GraphError(f"token request failed: {error}") from error
            finally:
                if self.http_client is None:
                    client.close()
            body = _json_or_empty(response)
            if response.status_code != 200 or "access_token" not in body:
                raise GraphError(
                    str(body.get("error_description") or "token request rejected").splitlines()[0],
                    status_code=response.status_code,
                    code=body.get("error"),
                )
            lifetime = int(body.get("expires_in", 3600))
            self._access_token = str(body["access_token"])
            self._expires_at = self.clock() + max(lifetime - self.refresh_margin_seconds, 30)
            return self._access_token


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    return min(2.0 ** attempt, 30.0)


def temporary_password(length: int = 20) -> str:
    """Random password satisfying Entra complexity rules."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(not c.isalnum() for c in candidate)
        ):
            return candidate


class GraphClient:
    """Small, explicit Microsoft Graph v1.0 client."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = GRAPH_BASE_URL,
        http_client: Optional[httpx.Client] = None,
        timeout: float = 15.0,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    # -- transport ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[dict[str, Any]]:
        url = path if path.startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"
        client = self.http_client or httpx.Client(timeout=self.timeout)
        try:
            for attempt in range(self.max_retries + 1):
                request_headers = {
                    "Authorization": f"Bearer {self.token_provider.token()}",
                    "Accept": "application/json",
                    **(headers or {}),
                }
                try:
                    response = client.request(
                        method, url, params=params, json=json, headers=request_headers
                    )
                except httpx.TransportError as error:
                    if attempt >= self.max_retries:
                        raise GraphError(f"{method} {url} failed: {error}") from error
                    self.sleep(min(2.0 ** attempt, 30.0))
                    continue
                if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    self.sleep(_retry_after_seconds(response, attempt))
                    continue
                if response.status_code == 204 or not response.content:
                    if response.is_success:
                        return None
                body = _json_or_empty(response)
                if not response.is_success:
                    error = body.get("error") if isinstance(body.get("error"), dict) else {}
                    raise GraphError(
                        str(error.get("message") or response.reason_phrase or "request failed"),
                        status_code=response.status_code,
                        code=error.get("code"),
                        request_id=response.headers.get("request-id"),
                    )
                return body
            raise GraphError(f"{method} {url} exhausted retries")  # pragma: no cover
        finally:
            if self.http_client is None:
                client.close()

    def paginate(
        self, path: str, *, params: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a collection, following ``@odata.nextLink``."""
        next_path: Optional[str] = path
        next_params = params
        while next_path:
            page = self.request("GET", next_path, params=next_params, headers=headers) or {}
            yield from page.get("value", [])
            next_path = page.get("@odata.nextLink")
            next_params = None  # nextLink already carries the query string

    # -- users -------------------------------------------------------------

    def find_user(self, user_principal_name: str) -> Optional[dict[str, Any]]:
        matches = list(
            self.paginate(
                "users",
                params={
                    "$filter": odata_eq("userPrincipalName", user_principal_name),
                    "$select": USER_SELECT,
                },
            )
        )
        return matches[0] if matches else None

    def require_user(self, user_principal_name: str) -> dict[str, Any]:
        user = self.find_user(user_principal_name)
        if user is None:
            raise GraphError(
                f"user {user_principal_name} not found", status_code=404, code="Request_ResourceNotFound"
            )
        return user

    def create_user(
        self,
        *,
        display_name: str,
        user_principal_name: str,
        department: Optional[str] = None,
        usage_location: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "accountEnabled": True,
            "displayName": display_name,
            "mailNickname": user_principal_name.split("@", 1)[0],
            "userPrincipalName": user_principal_name,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": temporary_password(),
            },
        }
        if department:
            body["department"] = department
        if usage_location:
            body["usageLocation"] = usage_location
        return self.request("POST", "users", json=body) or {}

    def update_user(self, user_id: str, changes: dict[str, Any]) -> None:
        self.request("PATCH", f"users/{user_id}", json=changes)

    def revoke_sign_in_sessions(self, user_id: str) -> None:
        self.request("POST", f"users/{user_id}/revokeSignInSessions")

    def assign_licenses(
        self, user_id: str, *, add: list[str] = (), remove: list[str] = ()  # type: ignore[assignment]
    ) -> None:
        self.request(
            "POST",
            f"users/{user_id}/assignLicense",
            json={
                "addLicenses": [{"skuId": sku, "disabledPlans": []} for sku in add],
                "removeLicenses": list(remove),
            },
        )

    def update_mailbox_settings(self, user_id: str, settings: dict[str, Any]) -> None:
        self.request("PATCH", f"users/{user_id}/mailboxSettings", json=settings)

    # -- tenant ------------------------------------------------------------

    def subscribed_skus(self) -> list[dict[str, Any]]:
        return list(self.paginate("subscribedSkus"))

    def sku(self, part_number: str) -> dict[str, Any]:
        for sku in self.subscribed_skus():
            if str(sku.get("skuPartNumber", "")).lower() == part_number.lower():
                return sku
        raise GraphError(f"licence SKU {part_number} is not subscribed in this tenant", code="SkuNotFound")


def available_units(sku: dict[str, Any]) -> int:
    enabled = int((sku.get("prepaidUnits") or {}).get("enabled") or 0)
    return enabled - int(sku.get("consumedUnits") or 0)
