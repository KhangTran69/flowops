"""Real browser smoke tests against an isolated simulated FlowOps stack."""
import os
import json
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


def wait_ready(url):
    for _ in range(60):
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError(f"Service unavailable: {url}")


def main():
    base = os.environ.get("FLOWOPS_BROWSER_URL", "http://127.0.0.1:8787")
    wait_ready(base + "/api/session")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(base + "/tickets")
        trigger = page.get_by_role("button", name="New ticket")
        expect(trigger).to_be_visible(timeout=15000)
        trigger.click()
        dialog = page.get_by_role("dialog", name="Create ticket")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
        for _ in range(9):
            page.keyboard.press("Tab")
            assert dialog.evaluate("(el) => el.contains(document.activeElement)")
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(trigger).to_be_focused()
        trigger.click()
        dialog.get_by_label("Title", exact=True).fill("Browser smoke VPN issue")
        dialog.get_by_label("Description", exact=True).fill("Created through React and FastAPI")
        dialog.get_by_label("Requester ID").fill("7")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(dialog).not_to_be_visible(timeout=15000)
        page.get_by_label("Search").fill("Browser smoke VPN")
        ticket = page.locator("table:visible tbody tr").filter(has_text="Browser smoke VPN issue")
        expect(ticket).to_be_visible(timeout=15000)
        ticket.click()
        expect(page.get_by_text("Created through React and FastAPI")).to_be_visible(timeout=15000)
        page.goto(base + "/requests")
        page.get_by_role("button", name="New request").click()
        request = page.get_by_role("dialog", name="Create workflow request")
        request.get_by_label("Employee name").fill("Browser Test")
        request.get_by_label("Employee email").fill("browser@example.test")
        request.get_by_label("Department").fill("IT")
        request.get_by_label("Effective date").fill("2026-11-27")
        request.get_by_label("Requester ID").fill("7")
        request.get_by_role("button", name="Create", exact=True).click()
        expect(request).not_to_be_visible(timeout=15000)
        row = page.locator("table:visible tbody tr").filter(has_text="Browser Test")
        expect(row).to_be_visible(timeout=15000)
        row.click()
        page.get_by_role("button", name="Start run").click()
        page.get_by_role("dialog").get_by_role("button", name="Start run").click()
        expect(page.get_by_role("link", name="Open run")).to_be_visible(timeout=15000)
        page.get_by_role("link", name="Open run").click()
        expect(page.get_by_role("heading", name="Steps", exact=True)).to_be_visible(timeout=15000)
        page.goto(base + "/approvals")
        approve = page.get_by_role("button", name="Approve", exact=True).first
        expect(approve).to_be_visible(timeout=15000)
        approve.click()
        decision = page.get_by_role("dialog", name="Approve step")
        decision.get_by_label("Reason").fill("Verified in browser smoke test")
        decision.get_by_role("button", name="Confirm").click()
        expect(decision).not_to_be_visible(timeout=15000)
        page.goto(base + "/definitions")
        page.get_by_role("button", name="Create draft").click()
        create = page.get_by_role("dialog", name="Create workflow definition")
        create.get_by_role("button", name="Create draft").click()
        expect(create).not_to_be_visible(timeout=15000)
        definition = page.locator("table:visible tbody tr").filter(has_text="my-workflow")
        expect(definition).to_be_visible(timeout=15000)
        definition.click()
        page.get_by_role("button", name="Publish v1").click()
        page.get_by_role("dialog").get_by_role("button", name="Publish", exact=True).click()
        expect(page.get_by_role("dialog")).not_to_be_visible(timeout=15000)
        page.get_by_role("button", name="New version").click()
        editor = page.get_by_role("dialog", name="Create new version")
        editor.get_by_role("button", name="Add step").click()
        editor.get_by_role("group", name="Step 2").get_by_label("Dependencies (comma-separated keys)").fill("create_user")
        editor.get_by_role("button", name="Validate DAG").click()
        expect(editor.get_by_text("DAG validated by FlowOps. No version was saved.")).to_be_visible(timeout=15000)
        editor.get_by_role("button", name="Create version").click()
        expect(editor).not_to_be_visible(timeout=15000)
        expect(page.get_by_role("button", name="v2", exact=False)).to_be_visible(timeout=15000)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("navigation", name="Main")).not_to_be_visible()
        menu = page.get_by_role("button", name="Open menu")
        menu.click()
        expect(page.get_by_role("button", name="Close menu")).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).to_be_focused()
        expect(page.get_by_role("navigation", name="Main")).not_to_be_visible()
        page.get_by_role("button", name="Open menu").click()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        page.get_by_role("link", name="Tickets", exact=True).click()
        expect(page.get_by_role("heading", name="Tickets", exact=True)).to_be_visible()
        axe_path = os.environ.get('FLOWOPS_AXE_PATH')
        if axe_path:
            for route, heading in [('/tickets', 'Tickets'), ('/definitions', 'Workflow definitions')]:
                page.set_viewport_size({'width': 1280, 'height': 900})
                page.goto(base + route)
                expect(page.get_by_role('heading', name=heading, exact=True)).to_be_visible()
                page.add_script_tag(path=axe_path)
                result = page.evaluate('''async () => window.axe.run({runOnly: {type: 'rule', values: ['button-name', 'link-name', 'label', 'aria-valid-attr-value', 'aria-roles', 'aria-required-attr', 'document-title', 'html-has-lang']}})''')
                assert not result['violations'], json.dumps(result['violations'], default=str)
                print('AXE_FOCUSED_PASS ' + route, flush=True)
        browser.close()


if __name__ == "__main__":
    main()
