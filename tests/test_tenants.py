"""Multi-tenant Graph adapter, credentials and config-drift tests (offline)."""

from urllib.parse import parse_qs

import httpx
import pytest

from src.actions import MicrosoftGraphAdapter, ActionResult
from src.graph import GraphError
from src.tenants import (
    GraphClientFactory,
    TenantSettings,
    credentials_for,
    run_drift_check,
)

TENANTS = {
    "acme": TenantSettings(
        key="acme", name="Acme Ltd", usage_location="GB", licence_sku="O365_BUSINESS_PREMIUM",
        baseline={"simulated_profile": "healthy", "max_unlicensed_users": 1},
    ),
    "globex": TenantSettings(
        key="globex", name="Globex", usage_location="US", licence_sku="O365_BUSINESS_PREMIUM",
        baseline={"simulated_profile": "licence_exhausted"},
    ),
    "initech": TenantSettings(
        key="initech", name="Initech", usage_location="US",
        baseline={"simulated_profile": "security_defaults_off", "required_skus": ["SPE_E3"]},
    ),
    "paused": TenantSettings(key="paused", name="Paused", active=False),
}


class RecordingFallback:
    def __init__(self):
        self.calls = []

    def execute(self, action, context):
        self.calls.append((action, context))
        return ActionResult(action=action, detail="planned 2 step(s)", output={"converted": True})


@pytest.fixture
def factory():
    return GraphClientFactory({}, simulated=True, loader=TENANTS.get)


def test_onboarding_and_offboarding_are_idempotent_per_tenant(factory):
    fallback = RecordingFallback()
    adapter = MicrosoftGraphAdapter(factory, exchange_fallback=fallback)
    ctx = {"employee_email": "Jane@acme.com", "employee_name": "Jane", "department": "Sales", "tenant_key": "acme"}

    first = adapter.execute("create_user_account", ctx)
    again = adapter.execute("create_user_account", ctx)
    assert first.output["created"] is True and again.output["created"] is False
    assert first.output["user_id"] == again.output["user_id"]
    assert first.detail.startswith("[acme]")

    granted = adapter.execute("grant_software_licenses", ctx)
    assert granted.output["assigned"] is True
    assert adapter.execute("grant_software_licenses", ctx).output["assigned"] is False

    adapter.execute("configure_email_mailbox", ctx)
    adapter.execute("disable_user_account", ctx)
    revoked = adapter.execute("revoke_software_licenses", ctx)
    assert len(revoked.output["removed_sku_ids"]) == 1

    archived = adapter.execute("archive_email_mailbox", ctx)
    assert "Exchange Online PowerShell" in archived.detail
    assert fallback.calls[0][0] == "archive_email_mailbox"

    tenant = factory("acme")[0].simulated_tenant
    jane = next(u for u in tenant.users.values() if u["userPrincipalName"] == "jane@acme.com")
    assert jane["accountEnabled"] is False and jane["assignedLicenses"] == []
    assert jane["usageLocation"] == "GB"


def test_same_user_is_isolated_between_tenants(factory):
    adapter = MicrosoftGraphAdapter(factory)
    adapter.execute("create_user_account", {"employee_email": "sam@x.com", "tenant_key": "acme"})
    other = adapter.execute("create_user_account", {"employee_email": "sam@x.com", "tenant_key": "globex"})
    assert other.output["created"] is True


def test_licence_exhausted_is_a_permanent_error(factory):
    adapter = MicrosoftGraphAdapter(factory)
    ctx = {"employee_email": "new@globex.com", "tenant_key": "globex"}
    adapter.execute("create_user_account", ctx)
    with pytest.raises(GraphError) as info:
        adapter.execute("grant_software_licenses", ctx)
    assert info.value.code == "LicenceExhausted"
    assert info.value.retryable is False
    assert "globex" in str(info.value)


def test_unknown_inactive_tenant_and_missing_fallback(factory):
    adapter = MicrosoftGraphAdapter(factory)
    with pytest.raises(GraphError, match="not registered"):
        adapter.execute("create_user_account", {"employee_email": "a@b.c", "tenant_key": "nope"})
    with pytest.raises(GraphError, match="inactive"):
        adapter.execute("create_user_account", {"employee_email": "a@b.c", "tenant_key": "paused"})
    with pytest.raises(GraphError, match="Exchange Online PowerShell") as info:
        adapter.execute("archive_email_mailbox", {"employee_email": "a@b.c"})
    assert info.value.retryable is True  # 501 is a config gap; operator can fix and retry
    assert "PSA/RMM" in adapter.execute("provision_hardware", {"employee_email": "a@b.c"}).detail


def test_drift_check_reports_each_tenant_and_continues_after_errors(factory):
    def resolve(key):
        if key == "paused":
            raise GraphError("consent revoked", code="AADSTS65001", status_code=401)
        return factory(key)

    results = {r["tenant"]: r for r in run_drift_check(list(TENANTS.values()), resolve)}
    assert results["acme"]["status"] == "compliant"
    assert results["globex"]["status"] == "drift"
    assert any(f["check"].endswith("free_units") and f["status"] == "drift" for f in results["globex"]["findings"])
    initech = {f["check"]: f for f in results["initech"]["findings"]}
    assert initech["security_defaults"]["status"] == "drift"
    assert initech["sku:SPE_E3"]["actual"] == "missing"
    assert results["paused"]["status"] == "error"
    assert "AADSTS65001" in results["paused"]["error"]


def test_per_tenant_credentials_override_partner_app():
    env = {
        "FLOWOPS_GRAPH_CLIENT_ID": "partner-id",
        "FLOWOPS_GRAPH_CLIENT_SECRET": "partner-secret",
        "FLOWOPS_TENANT_GLOBEX_GRAPH_CLIENT_ID": "globex-id",
        "FLOWOPS_TENANT_GLOBEX_GRAPH_CLIENT_SECRET": "globex-secret",
    }
    assert credentials_for(TenantSettings(key="a", name="A"), env) == ("partner-id", "partner-secret")
    assert credentials_for(TenantSettings(key="g", name="G", credential_ref="globex"), env) == (
        "globex-id",
        "globex-secret",
    )


def test_factory_requests_token_from_each_customer_authority():
    token_urls = []

    def handler(request):
        if "oauth2" in request.url.path:
            token_urls.append(str(request.url))
            assert parse_qs(request.content.decode())["client_id"] == ["partner-id"]
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(200, json={"value": []})

    tenants = {
        "a": TenantSettings(key="a", name="A", entra_tenant_id="guid-a"),
        "b": TenantSettings(key="b", name="B", entra_tenant_id="guid-b"),
        "c": TenantSettings(key="c", name="C"),
    }
    factory = GraphClientFactory(
        {"FLOWOPS_GRAPH_CLIENT_ID": "partner-id", "FLOWOPS_GRAPH_CLIENT_SECRET": "partner-secret"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        loader=tenants.get,
    )
    for key in ("a", "b", "a"):
        client, _ = factory(key)
        assert client.find_user("x@y.z") is None
    assert token_urls == [
        "https://login.microsoftonline.com/guid-a/oauth2/v2.0/token",
        "https://login.microsoftonline.com/guid-b/oauth2/v2.0/token",
    ]
    with pytest.raises(GraphError, match="entra_tenant_id") as info:
        factory("c")
    assert info.value.code == "CredentialsMissing"
