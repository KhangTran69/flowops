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
    environ = {"FLOWOPS_AUTH_REQUIRED": "true", "FLOWOPS_DEV_BEARER_TOKEN": "dev-secret"}
    with pytest.raises(AuthenticationError):
        authenticate_bearer(None, environ=environ)
    with pytest.raises(AuthenticationError):
        authenticate_bearer("Bearer wrong", environ=environ)
    actor = authenticate_bearer("Bearer dev-secret", environ=environ)
    assert actor.roles == ("automation.operator",)


def test_graph_adapter_sends_bearer_token(monkeypatch):
    calls = []
    class Response:
        def raise_for_status(self): pass
    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return Response()
    monkeypatch.setattr("src.actions.requests.post", fake_post)
    result = MicrosoftGraphAdapter("https://graph.example/v1.0", "access-token").execute(
        "create_user_account", {"employee_email": "jane@example.com"}
    )
    assert result.action == "create_user_account"
    assert calls[0][2] == {"Authorization": "Bearer access-token"}


def test_graph_adapter_selection_requires_token():
    adapter = adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "graph", "FLOWOPS_GRAPH_ACCESS_TOKEN": "token"})
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
    payload = {"iss": "https://issuer", "aud": "client", "sub": "user-1", "exp": 4102444800, "roles": ["automation.operator"]}
    token = __import__("jwt").encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})

    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"keys": [public_jwk]}

    monkeypatch.setattr("src.auth.requests.get", lambda *args, **kwargs: calls.append(args) or Response())
    environ = {"FLOWOPS_AUTH_REQUIRED": "true", "FLOWOPS_ENVIRONMENT": "production", "FLOWOPS_ENTRA_ISSUER": "https://issuer", "FLOWOPS_ENTRA_AUDIENCE": "client", "FLOWOPS_ENTRA_JWKS_URL": "https://issuer/keys"}
    authenticate_bearer(f"Bearer {token}", environ=environ)
    authenticate_bearer(f"Bearer {token}", environ=environ)
    assert len(calls) == 1


def test_execute_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    payload = {"request_type": "onboarding", "employee_name": "Jane Doe", "employee_email": "jane@example.com", "department": "Sales", "requester_id": 3}
    request = client.post("/onboarding-requests", json=payload).json()
    item = request["checklist"][1]
    assert client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute").status_code == 401
    class CapturingAdapter:
        def execute(self, action, context):
            assert context["actor_subject"] == "configured-bearer"
            return ActionResult(action, "captured")
    monkeypatch.setattr(api, "ACTION_ADAPTER", CapturingAdapter())
    assert client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute", headers={"Authorization": "Bearer dev-secret"}).status_code == 200
