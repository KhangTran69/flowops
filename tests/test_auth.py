"""Offline tests for authenticated Microsoft action boundaries."""

import base64
import json
import pytest
from fastapi.testclient import TestClient

from src import api, db
from src.auth import AuthenticationError, authenticate_bearer
from src.actions import ActionResult, MicrosoftGraphAdapter, adapter_from_environment


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/auth.db")
    return TestClient(api.app)


def test_authentication_is_optional_for_local_development():
    actor = authenticate_bearer(None, environ={"FLOWOPS_AUTH_REQUIRED": "false"})
    assert actor.subject == "local-development"


def test_required_auth_rejects_missing_or_wrong_token():
    environ = {"FLOWOPS_AUTH_REQUIRED": "true", "FLOWOPS_DEV_BEARER_TOKEN": "dev-secret",
               "FLOWOPS_DEV_TENANT_KEY": "tenant-demo"}
    with pytest.raises(AuthenticationError):
        authenticate_bearer(None, environ=environ)
    with pytest.raises(AuthenticationError):
        authenticate_bearer("Bearer wrong", environ=environ)
    actor = authenticate_bearer("Bearer dev-secret", environ=environ)
    assert actor.roles == ("automation.operator",)
    assert actor.tenant_key == "tenant-demo"


def test_graph_adapter_uses_tenant_resolver():
    from src.tenants import GraphClientFactory, TenantSettings

    settings = TenantSettings(key="acme", name="Acme", usage_location="GB", baseline={"simulated_profile": "healthy"})
    factory = GraphClientFactory({}, simulated=True, loader=lambda key: settings)
    result = MicrosoftGraphAdapter(factory).execute(
        "create_user_account", {"employee_email": "jane@example.com", "tenant_key": "acme"}
    )
    assert result.action == "create_user_account"
    assert result.output["tenant"] == "acme"
    assert result.output["created"] is True


def test_graph_adapter_selection_requires_token():
    adapter = adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "graph", "FLOWOPS_GRAPH_ACCESS_TOKEN": "token"})
    assert isinstance(adapter, MicrosoftGraphAdapter)
    adapter = adapter_from_environment(
        {"FLOWOPS_ACTION_ADAPTER": "graph", "FLOWOPS_GRAPH_CLIENT_ID": "id", "FLOWOPS_GRAPH_CLIENT_SECRET": "secret"}
    )
    assert isinstance(adapter, MicrosoftGraphAdapter)
    with pytest.raises(ValueError, match="FLOWOPS_GRAPH_ACCESS_TOKEN"):
        adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "graph"})


def test_protected_mode_requires_entra_configuration():
    environ = {"FLOWOPS_AUTH_REQUIRED": "true", "FLOWOPS_ENVIRONMENT": "production", "FLOWOPS_DEV_BEARER_TOKEN": "dev-secret"}
    with pytest.raises(AuthenticationError, match="Entra authentication is required"):
        authenticate_bearer("Bearer dev-secret", environ=environ)


def test_entra_jwt_claims_and_jwks_cache(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "use": "sig"})
    payload = {"iss": "https://issuer", "aud": "client", "sub": "user-1", "exp": 4102444800,
               "roles": ["automation.operator"], "flowops_tenant_key": "tenant-demo"}
    token = __import__("jwt").encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})

    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"keys": [public_jwk]}

    monkeypatch.setattr("src.auth.requests.get", lambda *args, **kwargs: calls.append(args) or Response())
    environ = {"FLOWOPS_AUTH_REQUIRED": "true", "FLOWOPS_ENVIRONMENT": "production", "FLOWOPS_ENTRA_ISSUER": "https://issuer", "FLOWOPS_ENTRA_AUDIENCE": "client", "FLOWOPS_ENTRA_JWKS_URL": "https://issuer/keys"}
    actor = authenticate_bearer(f"Bearer {token}", environ=environ)
    authenticate_bearer(f"Bearer {token}", environ=environ)
    assert len(calls) == 1
    assert actor.tenant_key == "tenant-demo"


def test_execute_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    payload = {"request_type": "onboarding", "employee_name": "Jane Doe", "employee_email": "jane@example.com", "department": "Sales", "requester_id": 3}
    request = client.post("/onboarding-requests", json=payload, headers={"Authorization": "Bearer dev-secret"}).json()
    item = request["checklist"][1]
    assert client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute").status_code == 401
    class CapturingAdapter:
        def execute(self, action, context):
            assert context["actor_subject"] == "configured-bearer"
            return ActionResult(action, "captured")
    monkeypatch.setattr(api, "ACTION_ADAPTER", CapturingAdapter())
    assert client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute", headers={"Authorization": "Bearer dev-secret"}).status_code == 200


def test_mutating_ticket_route_requires_bearer_when_enabled(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    payload = {"userId": 7, "id": 42, "title": "Laptop will not start", "completed": False}
    assert client.post("/tickets", json=payload).status_code == 401
    assert client.post("/tickets", json=payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
    created = client.post(
        "/tickets", json=payload, headers={"Authorization": "Bearer dev-secret"}
    )
    assert created.status_code == 201
    audit = client.get("/audit").json()
    assert any(row["actor_subject"] == "configured-bearer" for row in audit)
    filtered = client.get("/execution-logs", params={"actor": "configured-bearer"}).json()
    assert any(row["action"] == "ticket.created" for row in filtered)


def test_webhook_api_key_is_optional_and_enforced(client, monkeypatch):
    payload = {"userId": 7, "id": 77, "title": "VPN issue", "completed": False}
    monkeypatch.delenv("FLOWOPS_WEBHOOK_API_KEY", raising=False)
    assert client.post("/webhooks/intake", json=payload).status_code == 200
    monkeypatch.setenv("FLOWOPS_WEBHOOK_API_KEY", "hook-secret")
    assert client.post("/webhooks/intake", json=payload).status_code == 401
    assert client.post(
        "/webhooks/intake", json=payload, headers={"X-API-Key": "hook-secret"}
    ).status_code == 200


def test_operator_dependency_rejects_viewer_role():
    from fastapi import HTTPException
    from src.api import _operator_actor
    from src.auth import Actor

    with pytest.raises(HTTPException) as error:
        _operator_actor(None, Actor(subject="viewer", roles=("automation.viewer",)))
    assert error.value.status_code == 403


def test_auth_me_and_startup_validation(client, monkeypatch):
    from src.auth import validate_auth_configuration

    with pytest.raises(AuthenticationError):
        validate_auth_configuration({"FLOWOPS_AUTH_REQUIRED": "true"})
    validate_auth_configuration(
        {
            "FLOWOPS_AUTH_REQUIRED": "true",
            "FLOWOPS_ENVIRONMENT": "development",
            "FLOWOPS_DEV_BEARER_TOKEN": "dev-secret",
        }
    )
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    me = client.get("/auth/me", headers={"Authorization": "Bearer dev-secret"})
    assert me.status_code == 200
    assert me.json()["subject"] == "configured-bearer"
    assert "automation.operator" in me.json()["roles"]


def test_admin_only_requeue_rejects_operator(client, monkeypatch):
    client.get("/dashboard")  # ensure tables
    session = db.SessionLocal()
    try:
        row = db.NotificationOutboxRecord(
            url="https://hooks.example/auth",
            payload_json="{}",
            status="failed",
            attempts=3,
            last_error="boom",
        )
        session.add(row)
        session.commit()
        notification_id = row.id
    finally:
        session.close()

    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    headers = {"Authorization": "Bearer dev-secret"}
    assert client.post(f"/notifications/{notification_id}/requeue").status_code == 401
    assert client.post(
        f"/notifications/{notification_id}/requeue", headers=headers
    ).status_code == 403


def test_rate_limit_returns_429(client, monkeypatch):
    from src import api

    api._RATE_LIMIT_BUCKETS.clear()
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "2")
    payload = {"userId": 7, "id": 42, "title": "Laptop will not start", "completed": False}
    assert client.post("/tickets", json=payload).status_code == 201
    assert client.post("/tickets", json={**payload, "id": 43}).status_code == 201
    limited = client.post("/tickets", json={**payload, "id": 44})
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After") == "60"
    api._RATE_LIMIT_BUCKETS.clear()
