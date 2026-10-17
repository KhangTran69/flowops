"""Offline tests for Graph, ConnectWise, and Exchange adapters."""

from src.connectwise import ConnectWiseMockAdapter
from src.graph import GraphClient
from src.powershell import PowerShellScriptAdapter


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_graph_client_credentials_filter_paging_and_license():
    session = FakeSession([
        FakeResponse(body={"access_token": "token-1", "expires_in": 3600}),
        FakeResponse(body={"value": [{"id": "u1"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc"}),
        FakeResponse(body={"value": [{"id": "u2"}]}),
        FakeResponse(body={"id": "u1", "licenses": []}),
    ])
    client = GraphClient(tenant_id="tenant", client_id="client", client_secret="secret", session=session, sleep=lambda _: None)
    users = client.list_users("department eq 'Engineering'")
    assert [u["id"] for u in users] == ["u1", "u2"]
    assert session.calls[1][2]["params"]["$filter"] == "department eq 'Engineering'"
    assert session.calls[2][2]["params"] is None  # nextLink carries query params
    client.assign_license("u1", "sku-1", ["plan-a"])
    assert session.calls[-1][2]["json"]["addLicenses"][0]["skuId"] == "sku-1"


def test_graph_retries_429_with_retry_after():
    sleeps = []
    session = FakeSession([
        FakeResponse(body={"access_token": "token-1", "expires_in": 3600}),
        FakeResponse(status=429, headers={"Retry-After": "3"}),
        FakeResponse(body={"value": []}),
    ])
    client = GraphClient(tenant_id="t", client_id="c", client_secret="s", session=session, sleep=sleeps.append)
    assert client.list_users("id eq 'x'") == []
    assert sleeps == [3.0]


def test_connectwise_conditions_patch_and_time_entry_payloads():
    session = FakeSession([
        FakeResponse(body=[{"id": 10}]),
        FakeResponse(body={"id": 10}),
        FakeResponse(body={"id": 99}),
    ])
    cw = ConnectWiseMockAdapter(session=session)
    assert cw.get_tickets("status/name = 'Open'") == [{"id": 10}]
    assert session.calls[0][2]["params"]["conditions"] == "status/name = 'Open'"
    cw.patch_ticket(10, cw.ticket_patch("it-network", "urgent"))
    assert session.calls[1][0] == "PATCH"
    assert session.calls[1][2]["json"][0]["op"] == "replace"
    entry = cw.time_entry(10, 7, 1.5, "VPN triage")
    cw.create_time_entry(entry)
    assert session.calls[2][2]["json"]["actualHours"] == 1.5


def test_exchange_adapter_dry_run():
    adapter = PowerShellScriptAdapter(dry_run=True)
    result = adapter.execute("configure_mailbox", {"employee_email": "dana@example.com"})
    assert result.output["userPrincipalName"] == "dana@example.com"


def test_graph_action_adapter_and_factory():
    from src.actions import GraphClientActionAdapter, adapter_from_environment

    class FakeGraph:
        def __init__(self):
            self.assigned = []
        def create_user(self, payload):
            return {"id": "u-1", "userPrincipalName": payload["userPrincipalName"]}
        def list_users(self, query):
            return [{"id": "u-1"}]
        def assign_license(self, user_id, sku):
            self.assigned.append((user_id, sku))
            return {}
        def request(self, method, path, json=None):
            return {"ok": True}

    fake = FakeGraph()
    adapter = GraphClientActionAdapter(fake, sku_id="sku-1")
    created = adapter.execute("create_user_account", {"employee_email": "dana@example.com", "temporary_password": "Temp-123"})
    assert created.output["id"] == "u-1"
    adapter.execute("grant_software_licenses", {"employee_email": "dana@example.com"})
    assert fake.assigned == [("u-1", "sku-1")]

    built = adapter_from_environment({
        "FLOWOPS_ACTION_ADAPTER": "graph",
        "FLOWOPS_GRAPH_TENANT_ID": "t",
        "FLOWOPS_GRAPH_CLIENT_ID": "c",
        "FLOWOPS_GRAPH_CLIENT_SECRET": "s",
    })
    assert isinstance(built, GraphClientActionAdapter)
