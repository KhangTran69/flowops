"""Protected browser journey using synthetic signed sessions, never live credentials."""
import os

from playwright.sync_api import expect, sync_playwright

from browser_smoke import wait_ready


def main():
    api = "http://127.0.0.1:8000"
    console = "http://127.0.0.1:8787"
    wait_ready(api + "/health")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.goto(console + "/tickets")
        expect(page.get_by_role("heading", name="Console access unavailable")).to_be_visible()
        page.goto(api + "/login")
        page.get_by_label("Username").fill("ci-operator")
        page.get_by_label("Password").fill(os.environ["FLOWOPS_BROWSER_TEST_PASSWORD"])
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url("**/dashboard")
        page.goto(console + "/tickets")
        expect(page.get_by_role("button", name="New ticket")).to_be_visible()
        session = page.request.get(console + "/api/session")
        assert session.json()["email"] == "ci-operator"
        assert session.json()["role"] == "operator"
        page.get_by_role("button", name="New ticket").click()
        dialog = page.get_by_role("dialog", name="Create ticket")
        dialog.get_by_label("Title", exact=True).fill("Protected browser ticket")
        dialog.get_by_label("Description", exact=True).fill("Signed session and CSRF")
        dialog.get_by_label("Requester ID").fill("7")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(dialog).not_to_be_visible(timeout=15000)
        expect(page.locator("table:visible tbody tr").filter(has_text="Protected browser ticket")).to_be_visible()
        # Backend enforces CSRF independently of the proxy.
        direct = page.request.post(api + "/tickets/manual", data={
            "title": "Unauthorized CSRF", "description": "must fail", "requester_id": 7,
        }, headers={"Idempotency-Key": "must-fail"})
        assert direct.status == 403
        forged = page.request.post(console + "/api/flowops", data={
            "method": "POST", "path": "/workflow-definitions", "body": {},
        }, headers={"X-FlowOps-Request": "1", "X-User-Role": "admin"})
        assert forged.status == 403
        for path in ("/triggers", "/event-sources", "/triggers/browser-smoke-trigger/deliveries"):
            denied = page.request.post(console + "/api/flowops", data={"method": "GET", "path": path},
                                       headers={"X-FlowOps-Request": "1", "X-User-Role": "admin"})
            assert denied.status == 403
        for method, path in (("GET", "/workforce/employees"), ("POST", "/workforce/employees"),
                             ("PATCH", "/workforce/tasks/1")):
            denied = page.request.post(console + "/api/flowops", data={"method": method, "path": path, "body": {}},
                                       headers={"X-FlowOps-Request": "1", "X-User-Role": "admin"})
            assert denied.status in (400, 403)
        page.goto(console + "/workforce")
        expect(page.get_by_role("heading", name="Workforce browser access unavailable")).to_be_visible()
        print("WORKFORCE_PROTECTED_OPERATOR_DENIED", flush=True)
        page.goto(console + "/triggers")
        expect(page.get_by_text("Global administrator access required.")).to_be_visible()
        page.goto(console + "/tickets")
        expect(page.get_by_role("button", name="New ticket")).to_be_visible()
        # Verify server-side revocation, even when a previously copied cookie is replayed.
        copied = context.cookies()
        identity = page.request.get(api + "/command-center/session").json()
        logged_out = page.request.post(api + "/logout", form={"csrf_token": identity["csrf_token"]}, max_redirects=0)
        assert logged_out.status == 303
        context.add_cookies(copied)
        assert page.request.get(console + "/api/session").status == 401
        page.get_by_label("Search").fill("trigger identity check")
        expect(page.get_by_role("heading", name="Console access unavailable")).to_be_visible(timeout=15000)
        browser.close()


if __name__ == "__main__":
    main()
