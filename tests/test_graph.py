"""Offline tests for the Microsoft Graph client (httpx.MockTransport)."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.graph import (
    ClientCredentialsToken,
    GraphClient,
    GraphError,
    StaticToken,
    available_units,
    odata_eq,
    odata_quote,
)


def test_odata_quote_escapes_single_quotes():
    assert odata_quote("o'brien@contoso.com") == "'o''brien@contoso.com'"
    assert odata_eq("userPrincipalName", "a'b") == "userPrincipalName eq 'a''b'"


def test_client_credentials_token_is_cached_until_expiry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 3600})

    now = [1000.0]
    provider = ClientCredentialsToken(
        "tenant-guid",
        "client-id",
        "s3cret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    assert provider.token() == "tok-1"
    assert provider.token() == "tok-1"
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["https://graph.microsoft.com/.default"]
    assert form["client_id"] == ["client-id"]
    assert "s3cret" not in repr(provider)
    now[0] += 3600  # past expiry (minus refresh skew)
    assert provider.token() == "tok-2"


def test_token_endpoint_error_is_raised_not_swallowed():
    def handler(request):
        return httpx.Response(
            401,
            json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"},
        )

    provider = ClientCredentialsToken(
        "t", "c", "bad", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(GraphError, match="AADSTS7000215"):
        provider.token()


def _client(handler, sleeps=None):
    return GraphClient(
        StaticToken("abc"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def test_paginate_follows_odata_next_link_and_sends_filter():
    seen = []

    def handler(request):
        seen.append(request)
        query = parse_qs(urlparse(str(request.url)).query)
        if "$skiptoken" not in query:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "1"}, {"id": "2"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=X",
                },
            )
        return httpx.Response(200, json={"value": [{"id": "3"}]})

    client = _client(handler)
    items = list(client.paginate("users", params={"$filter": "accountEnabled eq true"}))
    assert [i["id"] for i in items] == ["1", "2", "3"]
    assert parse_qs(urlparse(str(seen[0].url)).query)["$filter"] == ["accountEnabled eq true"]
    assert seen[0].headers["Authorization"] == "Bearer abc"
    assert "$filter" not in str(seen[1].url)  # nextLink already has the query


def test_429_honours_retry_after_then_succeeds():
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}),
        httpx.Response(503),
        httpx.Response(200, json={"value": []}),
    ]
    sleeps: list[float] = []
    client = _client(lambda request: responses.pop(0), sleeps)
    assert client.request("GET", "users") == {"value": []}
    assert sleeps[0] == 7.0
    assert len(sleeps) == 2


def test_retries_are_bounded_and_error_details_surface():
    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "1", "request-id": "req-123"},
            json={"error": {"code": "TooManyRequests", "message": "Throttled"}},
        )

    client = _client(handler)
    client.max_retries = 2
    with pytest.raises(GraphError) as info:
        client.request("GET", "users")
    assert info.value.status_code == 429
    assert info.value.code == "TooManyRequests"
    assert info.value.request_id == "req-123"
    assert info.value.retryable is True


def test_client_errors_are_not_retryable():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})

    with pytest.raises(GraphError) as info:
        _client(handler).request("GET", "users")
    assert len(calls) == 1
    assert info.value.retryable is False
    assert "Authorization_RequestDenied" in str(info.value)


def test_create_user_and_assign_license_payloads():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(201, json={"id": "u1"})
        return httpx.Response(200, json={})

    client = _client(handler)
    user = client.create_user(
        display_name="Jane Doe", user_principal_name="jane@contoso.com", department="Sales", usage_location="GB"
    )
    client.assign_licenses(user["id"], add=["sku-1"], remove=["sku-0"])
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/v1.0/users")
    assert body["mailNickname"] == "jane"
    assert body["usageLocation"] == "GB"
    assert body["passwordProfile"]["forceChangePasswordNextSignIn"] is True
    assert seen[1][1] == "/v1.0/users/u1/assignLicense"
    assert seen[1][2] == {"addLicenses": [{"skuId": "sku-1", "disabledPlans": []}], "removeLicenses": ["sku-0"]}


def test_available_units_handles_missing_keys():
    assert available_units({"prepaidUnits": {"enabled": 5}, "consumedUnits": 3}) == 2
    assert available_units({}) == 0
