"""Tests for the credential-free v0.6 action layer."""

import pytest
from fastapi.testclient import TestClient

from src import api, db
from src.actions import ActionResult
from src.notify import DeliveryResult


VALID_REQUEST = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/actions.db")
    return TestClient(api.app)


def test_approved_item_executes_simulated_action(client):
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][0]
    blocked = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert blocked.status_code == 409
    assert client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/approve").status_code == 200
    executed = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert executed.status_code == 200
    updated = next(i for i in executed.json()["checklist"] if i["id"] == item["id"])
    assert updated["status"] == "completed"
    logs = client.get("/execution-logs").json()
    action_logs = [log for log in logs if log["action"] == "onboarding.action.create_user_account"]
    assert action_logs and action_logs[0]["status"] == "success"
    assert "simulated Microsoft action" in action_logs[0]["detail"]


def test_action_retries_and_logs_failure(client, monkeypatch):
    calls = []

    class FailingAdapter:
        def execute(self, action, context):
            calls.append(action)
            raise RuntimeError("Microsoft service unavailable")

    monkeypatch.setattr(api, "ACTION_ADAPTER", FailingAdapter())
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][1]
    response = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert response.status_code == 502
    assert calls == ["provision_hardware"] * 3
    logs = client.get("/execution-logs", params={"status": "failure"}).json()
    assert logs[0]["action"] == "onboarding.action.provision_hardware"
    assert "attempts=3" in logs[0]["detail"]


def test_action_adapter_can_succeed_after_retries(client, monkeypatch):
    attempts = []

    class EventuallySuccessfulAdapter:
        def execute(self, action, context):
            attempts.append(action)
            if len(attempts) < 3:
                raise RuntimeError("temporary failure")
            return ActionResult(action, "adapter succeeded")

    monkeypatch.setattr(api, "ACTION_ADAPTER", EventuallySuccessfulAdapter())
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][1]
    response = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert response.status_code == 200
    assert len(attempts) == 3
    logs = client.get("/execution-logs").json()
    assert "attempts=3" in next(log["detail"] for log in logs if log["action"] == "onboarding.action.provision_hardware")


def test_power_automate_adapter_posts_sanitized_context(monkeypatch):
    from src.actions import PowerAutomateAdapter

    calls = []

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr("src.actions.requests.post", fake_post)
    result = PowerAutomateAdapter("https://flow.example/trigger").execute(
        "create_user_account", {"employee_email": "jane@example.com"}
    )
    assert result.action == "create_user_account"
    assert calls[0][0] == "https://flow.example/trigger"
    assert calls[0][1] == {"action": "create_user_account", "context": {"employee_email": "jane@example.com"}}


def test_adapter_selection_requires_webhook_url():
    from src.actions import PowerAutomateAdapter, adapter_from_environment

    assert adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "simulated"}).__class__.__name__ == "SimulatedMicrosoftAdapter"
    with pytest.raises(ValueError, match="FLOWOPS_POWER_AUTOMATE_URL"):
        adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "power_automate"})
    assert isinstance(adapter_from_environment({"FLOWOPS_ACTION_ADAPTER": "power_automate", "FLOWOPS_POWER_AUTOMATE_URL": "https://flow.example/trigger"}), PowerAutomateAdapter)


def test_per_action_retry_limit_and_failure_notification(client, monkeypatch):
    calls = []
    notifications = []

    class FailingAdapter:
        def execute(self, action, context):
            calls.append(action)
            raise RuntimeError("Power Automate unavailable")

    def fake_deliver(url, payload):
        notifications.append((url, payload))
        return DeliveryResult(True, 1, None)

    monkeypatch.setattr(api, "ACTION_ADAPTER", FailingAdapter())
    monkeypatch.setenv("FLOWOPS_ACTION_RETRIES", '{"provision_hardware": 2}')
    monkeypatch.setenv("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL", "https://alerts.example/flowops")
    monkeypatch.setattr(api, "deliver_webhook", fake_deliver)
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][1]
    response = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert response.status_code == 502
    assert calls == ["provision_hardware"] * 2
    assert notifications[0][0] == "https://alerts.example/flowops"
    assert notifications[0][1]["attempts"] == 2
    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.action_failure" for log in logs)


def test_action_execution_audit_records_success(client):
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][1]
    response = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert response.status_code == 200
    executions = client.get(f"/onboarding-requests/{request['id']}/action-executions").json()
    assert len(executions) == 1
    execution = executions[0]
    assert execution["action"] == "provision_hardware"
    assert execution["status"] == "succeeded"
    assert execution["adapter"] == "SimulatedMicrosoftAdapter"
    assert execution["actor_subject"] == "local-development"
    assert execution["attempt"] == 1
    assert execution["correlation_id"]


def test_action_execution_audit_records_retries(client, monkeypatch):
    class FailingAdapter:
        def execute(self, action, context):
            raise RuntimeError("temporary adapter failure")

    monkeypatch.setattr(api, "ACTION_ADAPTER", FailingAdapter())
    request = client.post("/onboarding-requests", json=VALID_REQUEST).json()
    item = request["checklist"][1]
    response = client.post(f"/onboarding-requests/{request['id']}/items/{item['id']}/execute")
    assert response.status_code == 502
    executions = client.get(f"/onboarding-requests/{request['id']}/action-executions").json()
    assert [entry["status"] for entry in executions] == ["retrying", "retrying", "failed"]
    assert [entry["attempt"] for entry in executions] == [1, 2, 3]
    assert all(entry["error_category"] == "adapter_error" for entry in executions)
