"""Operator-console session, CSRF, and role-enforcement tests."""

import re
import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from src import api, db, ui_session
from src.auth import Actor

SECRET = "test-session-secret-0123456789abcdef"
TICKET = {
    "userId": 7,
    "id": 42,
    "title": "VPN outage - all users cannot work",
    "completed": False,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_UI_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_SESSION_SECRET", SECRET)
    monkeypatch.setenv("FLOWOPS_SESSION_MAX_AGE_SECONDS", "3600")
    monkeypatch.setenv("FLOWOPS_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    monkeypatch.setenv("FLOWOPS_DEMO_USERS", __import__("json").dumps({"admin": {
        "password_hash": ui_session.hash_demo_password("admin", salt="test-salt"),
        "roles": ["automation.operator", "automation.admin"]}}))
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    db.use_database(f"sqlite:///{tmp_path}/ui-auth.db")
    with TestClient(api.app) as test_client:
        yield test_client


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "csrf meta missing"
    return match.group(1)


def _login(client, password="admin", next_path="/dashboard", username="admin"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": next_path},
        follow_redirects=False,
    )


def _operator_cookie(client):
    session = ui_session.create_session(
        Actor("operator-test", ("automation.operator",), source="session")
    )
    client.cookies.clear()
    client.cookies.set(ui_session.SESSION_COOKIE, ui_session.encode_session(session))
    return session


def _admin_cookie(client):
    session = ui_session.create_session(
        Actor("admin-test", ("automation.admin",), source="session")
    )
    client.cookies.clear()
    client.cookies.set(ui_session.SESSION_COOKIE, ui_session.encode_session(session))
    return session


def test_ui_auth_disabled_keeps_existing_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_UI_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "false")
    monkeypatch.delenv("FLOWOPS_SESSION_SECRET", raising=False)
    db.use_database(f"sqlite:///{tmp_path}/ui-disabled.db")
    with TestClient(api.app) as client:
        assert client.get("/dashboard").status_code == 200
        assert client.get("/ui/tickets").status_code == 200


def test_unauthenticated_ui_redirects_and_login_sets_cookie(client):
    denied = client.get("/dashboard", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"].startswith("/login?next=")
    assert client.get("/login").status_code == 200

    wrong = _login(client, password="wrong")
    assert wrong.status_code == 401
    assert "Invalid username or password." in wrong.text
    assert _login(client, username="dev-secret", password="dev-secret").status_code == 401

    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "csrf-token" in page.text
    assert "admin" in page.text
    me = client.get("/auth/me").json()
    assert me["source"] == "session"
    assert me["subject"] == "admin"


def test_open_redirects_are_rejected(client):
    for target in ("https://evil.example", "//evil.example", "/\\evil"):
        response = _login(client, next_path=target)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"


def test_secure_cookie_flag_and_expired_session(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_SESSION_COOKIE_SECURE", "true")
    response = _login(client)
    assert "Secure" in response.headers["set-cookie"]

    monkeypatch.setenv("FLOWOPS_SESSION_MAX_AGE_SECONDS", "60")
    serializer = URLSafeTimedSerializer(SECRET, salt=ui_session.SESSION_SALT)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 7200)
    expired = serializer.dumps(
        {
            "subject": "expired-user",
            "roles": ["automation.operator"],
            "csrf": "expired-csrf",
        }
    )
    monkeypatch.setattr(time, "time", real_time)
    client.cookies.set(ui_session.SESSION_COOKIE, expired)
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303


def test_session_csrf_is_enforced_but_bearer_is_exempt(client):
    _login(client)
    page = client.get("/dashboard").text
    token = _csrf(page)
    client.post(
        "/tickets", json=TICKET, headers={"Authorization": "Bearer dev-secret"}
    )

    missing = client.post("/tickets/42/ai-triage")
    assert missing.status_code == 403
    wrong = client.post("/tickets/42/ai-triage", headers={"X-CSRF-Token": "wrong"})
    assert wrong.status_code == 403
    allowed = client.post(
        "/tickets/42/ai-triage", headers={"X-CSRF-Token": token}
    )
    assert allowed.status_code == 200

    bearer = client.post(
        "/tickets/42/ai-triage", headers={"Authorization": "Bearer dev-secret"}
    )
    assert bearer.status_code == 200


def test_logout_clears_session_and_writes_audit(client):
    _login(client)
    token = _csrf(client.get("/dashboard").text)
    response = client.post(
        "/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    after = client.get("/dashboard", follow_redirects=False)
    assert after.status_code == 303

    logs = client.get(
        "/execution-logs", headers={"Authorization": "Bearer dev-secret"}
    ).json()
    actions = [row["action"] for row in logs]
    assert "ui.login" in actions
    assert "ui.logout" in actions


def test_admin_only_controls_are_hidden_from_operators(client):
    _login(client)
    _operator_cookie(client)
    session = db.SessionLocal()
    try:
        session.add(
            db.NotificationOutboxRecord(
                url="https://example.test/hook",
                payload_json="{}",
                ticket_id=None,
                status="failed",
                attempts=5,
                last_error="test failure",
            )
        )
        session.commit()
    finally:
        session.close()

    operator_page = client.get("/ui/notifications").text
    assert "/requeue" not in operator_page
    assert "admin only" in operator_page

    _admin_cookie(client)
    admin_page = client.get("/ui/notifications").text
    assert "/requeue" in admin_page


def test_operator_cannot_call_admin_endpoint_with_session(client):
    _operator_cookie(client)
    token = _csrf(client.get("/dashboard").text)
    response = client.post(
        "/notifications/1/requeue",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 403


def test_session_config_requires_secret_and_auth_source():
    with pytest.raises(ui_session.UISessionError):
        ui_session.validate_ui_auth_configuration(
            {"FLOWOPS_UI_AUTH_REQUIRED": "true"}
        )
    with pytest.raises(ui_session.UISessionError):
        ui_session.validate_ui_auth_configuration(
            {
                "FLOWOPS_UI_AUTH_REQUIRED": "true",
                "FLOWOPS_SESSION_SECRET": SECRET,
                "FLOWOPS_ENVIRONMENT": "production",
            }
        )
