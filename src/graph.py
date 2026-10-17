"""Microsoft Graph client-credentials client (v2.0 integration).

The default token is obtained with the OAuth2 client-credentials flow. Tests
inject a fake session and sleep function, so no network or real tenant is
needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class GraphError(RuntimeError):
    """Raised when Graph returns a non-retryable error."""


@dataclass
class GraphToken:
    access_token: str
    expires_at: float


class GraphClient:
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        base_url: str = GRAPH,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 20.0,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.timeout = timeout
        self._token: Optional[GraphToken] = None

    def token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 30:
            return self._token.access_token
        response = self.session.post(
            TOKEN_URL.format(tenant=self.tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        self._token = GraphToken(
            access_token=body["access_token"],
            expires_at=now + int(body.get("expires_in", 3600)),
        )
        return self._token.access_token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        retries: int = 4,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(retries + 1):
            response = self.session.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self.token()}"},
                timeout=self.timeout,
            )
            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(30, 2 ** attempt)
                self.sleep(delay)
                continue
            if response.status_code >= 400:
                try:
                    detail = response.json()
                except ValueError:
                    detail = response.text
                raise GraphError(f"Graph {method} {url} failed: {response.status_code} {detail}")
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        raise GraphError("Graph request exhausted retries.")

    def list_users(self, filter_query: str, *, top: int = 100) -> list[dict[str, Any]]:
        """Follow @odata.nextLink until every page is collected."""
        users: list[dict[str, Any]] = []
        params: Optional[dict[str, Any]] = {"$filter": filter_query, "$top": top}
        url: Optional[str] = f"{self.base_url}/users"
        while url:
            body = self.request("GET", url, params=params)
            users.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            params = None  # nextLink already contains query parameters
        return users

    def create_user(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/users", json=payload)

    def assign_license(self, user_id: str, sku_id: str, disabled_plans: Optional[list[str]] = None) -> Any:
        body: dict[str, Any] = {
            "addLicenses": [
                {"skuId": sku_id, "disabledPlans": disabled_plans or []}
            ],
            "removeLicenses": [],
        }
        return self.request("POST", f"/users/{user_id}/assignLicense", json=body)
