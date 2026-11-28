"""Real browser smoke tests against an isolated simulated FlowOps stack."""
import os
import json
import time
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


def _seed_notification_rows():
    """Insert one pending and one dead-letter delivery for the console actions.

    The smoke environment has no FLOWOPS_NOTIFICATION_WEBHOOK_URL (intake never
    queues deliveries there), so the requeue/retry journey needs fixtures.
    Written through the same SQLite file the API uses.
    """
    import sqlite3
    from datetime import datetime, timezone

    url = os.environ.get("FLOWOPS_DATABASE_URL", "sqlite:///data/flowops.db")
    path = url.removeprefix("sqlite:///")
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("http://127.0.0.1:9/unreachable",
         '{"event":"ticket.created","ticket":{"ticket_id":"SMOKE-NTF-PENDING"}}',
         "SMOKE-NTF-PENDING", "pending", 0, None),
        ("http://127.0.0.1:9/unreachable",
         '{"event":"ticket.created","ticket":{"ticket_id":"SMOKE-NTF-DEAD"}}',
         "SMOKE-NTF-DEAD", "failed", 5, "Connection refused (smoke fixture)"),
    ]
    connection = sqlite3.connect(path, timeout=10)
    try:
        for entry in rows:
            for _ in range(3):
                try:
                    connection.execute(
                        "INSERT INTO notification_outbox "
                        "(url, payload_json, ticket_id, status, attempts, last_error, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (entry[0], entry[1], entry[2], entry[3], entry[4], entry[5], now, now),
                    )
                    break
                except sqlite3.OperationalError:
                    time.sleep(0.5)
        connection.commit()
    finally:
        connection.close()


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
    _seed_notification_rows()
    # Only the isolated synthetic browser backend is used. The source secret
    # is never displayed or printed and no webhook is fired.
    source = Request(
        os.environ.get("FLOWOPS_API_URL", "http://127.0.0.1:8000") + "/event-sources",
        data=json.dumps({"key": "browser-smoke-source", "name": "Browser smoke source", "global_scope": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urlopen(source, timeout=5) as response:
        assert response.status == 201
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
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.goto(base + '/deliveries')
        expect(page.get_by_role('heading', name='Notification deliveries')).to_be_visible()
        dead = page.locator('table:visible tbody tr').filter(has_text='SMOKE-NTF-DEAD')
        expect(dead).to_be_visible(timeout=15000)
        requeue = page.get_by_role('button', name='Requeue', exact=True).first
        expect(requeue).to_be_visible(timeout=15000)
        requeue.click()
        dialog = page.get_by_role('dialog', name='Requeue this delivery?')
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Requeue', exact=True).click()
        expect(dialog).not_to_be_visible(timeout=15000)
        page.get_by_label('Delivery state').select_option('pending')
        expect(page.locator('table:visible tbody tr').filter(has_text='SMOKE-NTF-DEAD')).to_be_visible(timeout=15000)
        page.get_by_role('button', name='Retry pending').click()
        expect(page.get_by_text('Pending deliveries retried once each.')).to_be_visible(timeout=15000)
        print('NOTIFICATIONS_PORT_PASS /deliveries', flush=True)
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.goto(base + '/jobs')
        expect(page.get_by_role('heading', name='Workflow jobs')).to_be_visible()
        job = page.locator('table:visible tbody tr').filter(has_text='onboarding.item.execute').first
        expect(job).to_be_visible(timeout=15000)
        job.click()
        expect(page.get_by_role('heading', name='What was requested')).to_be_visible(timeout=15000)
        cancel = page.get_by_role('button', name='Cancel', exact=True)
        expect(cancel).to_be_visible(timeout=15000)
        cancel.click()
        dialog = page.get_by_role('dialog', name='Cancel this job?')
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Cancel job').click()
        expect(dialog).not_to_be_visible(timeout=15000)
        expect(page.get_by_text('cancelled', exact=True)).to_be_visible(timeout=15000)
        print('JOBS_PORT_PASS /jobs', flush=True)
        page.goto(base + '/analytics')
        expect(page.get_by_role('heading', name='Run analytics and SLA reporting')).to_be_visible()
        expect(page.get_by_text('History: 30 days', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_role('heading', name='Workflow health')).to_be_visible()
        expect(page.get_by_role('heading', name='Step bottlenecks')).to_be_visible()
        page.get_by_label('History window').select_option('7')
        page.get_by_label('Stale threshold').select_option('15')
        page.get_by_role('button', name='Update view').click()
        expect(page.get_by_text('History: 7 days', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text('Stale after 15 minutes', exact=True)).to_be_visible(timeout=15000)
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.get_by_role('heading', name='Run analytics and SLA reporting')).to_be_visible()
        print('ANALYTICS_PORT_PASS /analytics', flush=True)
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.goto(base + '/triggers')
        expect(page.get_by_role('heading', name='Event triggers')).to_be_visible()
        expect(page.get_by_role('heading', name='Event sources')).to_be_visible()
        page.get_by_role('button', name='New trigger').click()
        expect(page.get_by_role('heading', name='Create trigger')).to_be_visible()
        page.get_by_label('Key', exact=True).fill('browser-smoke-trigger')
        page.get_by_label('Name', exact=True).fill('Browser smoke trigger')
        page.get_by_label('Event source').select_option('browser-smoke-source')
        page.get_by_label('Event type').fill('synthetic.ticket')
        page.get_by_label('Mapping JSON').fill('{"title":"$event.data.title"}')
        page.get_by_role('button', name='Create active trigger').click()
        expect(page.get_by_role('heading', name='Browser smoke trigger', exact=True)).to_be_visible(timeout=15000)
        page.get_by_label('Test event JSON').fill('{"data":{"title":"Synthetic preview"}}')
        page.get_by_role('button', name='Preview mapping').click()
        expect(page.get_by_text('Mapping preview complete.')).to_be_visible(timeout=15000)
        expect(page.get_by_role('status').locator('pre').filter(has_text='Synthetic preview')).to_be_visible()
        expect(page.get_by_role('heading', name='Recent deliveries')).to_be_visible()
        page.once('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='Deactivate trigger').click()
        expect(page.get_by_role('button', name='Activate trigger')).to_be_visible(timeout=15000)
        expect(page.get_by_role('button', name='Preview mapping')).to_be_disabled()
        page.goto(base + '/knowledge-base')
        expect(page.get_by_role('heading', name='Knowledge base')).to_be_visible()
        page.get_by_label('Search articles').fill('VPN')
        page.get_by_label('Category').select_option('network')
        page.get_by_role('button', name='Search', exact=True).click()
        expect(page.get_by_role('heading', name='Troubleshoot VPN and remote connectivity')).to_be_visible(timeout=15000)
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.get_by_role('heading', name='Knowledge base')).to_be_visible()
        print('TRIGGERS_KB_PORT_PASS', flush=True)
        if os.environ.get("FLOWOPS_COMMAND_CENTER_WORKFORCE_DEV") == "true":
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.goto(base + '/workforce')
            expect(page.get_by_role('heading', name='Workforce operations')).to_be_visible()
            page.get_by_role('button', name='Employees', exact=True).click()
            page.get_by_label('Employee number').fill('WF-SMOKE-001')
            page.get_by_label('Full name').fill('Synthetic Workforce Employee')
            page.get_by_label('Work email').fill('workforce-smoke@example.test')
            page.get_by_label('Department').fill('IT')
            page.get_by_label('Job title').fill('Synthetic technician')
            page.get_by_label('Start date').fill('2027-01-15')
            page.get_by_label('Manager account subject').fill('local-development')
            page.get_by_role('button', name='Create employee').click()
            employee = page.locator('table:visible tbody tr').filter(has_text='WF-SMOKE-001')
            expect(employee).to_be_visible(timeout=15000)
            employee_id = employee.locator('td').first.inner_text()
            page.get_by_role('button', name='Requests', exact=True).click()
            page.get_by_label('Employee ID', exact=True).fill(employee_id)
            page.get_by_role('button', name='Create draft').click()
            row = page.locator('table:visible tbody tr').filter(has_text='Synthetic Workforce Employee')
            expect(row).to_be_visible(timeout=15000)
            page.once('dialog', lambda dialog: dialog.accept())
            row.get_by_role('button', name='Submit for approval').click()
            expect(row.get_by_text('PENDING_APPROVAL', exact=True)).to_be_visible(timeout=15000)
            page.get_by_role('button', name='Approvals', exact=True).click()
            row = page.locator('table:visible tbody tr').filter(has_text='Synthetic Workforce Employee')
            expect(row).to_be_visible(timeout=15000)
            row.get_by_label('Approval comment').fill('Synthetic manager approval')
            row.get_by_role('button', name='Approve', exact=True).click()
            expect(row).not_to_be_visible(timeout=15000)
            page.get_by_role('button', name='IT tasks', exact=True).click()
            expect(page.locator('table:visible tbody tr').filter(has_text='Prepare laptop')).to_be_visible(timeout=15000)
            laptop = page.locator('table:visible tbody tr').filter(has_text='Prepare laptop')
            laptop.locator('summary').click()
            laptop.get_by_role('combobox', name='Task status', exact=True).select_option('DONE')
            laptop.get_by_label('Completion notes').fill('Synthetic verification')
            laptop.get_by_role('button', name='Save task').click()
            expect(laptop.get_by_role('cell', name='DONE', exact=True)).to_be_visible(timeout=15000)
            page.get_by_role('button', name='Automation', exact=True).click()
            simulate = page.get_by_role('button', name='Simulate provisioning', exact=True)
            expect(simulate).to_be_visible(timeout=15000)
            page.once('dialog', lambda dialog: dialog.accept())
            simulate.click()
            expect(page.locator('table:visible tbody tr').filter(has_text='SUCCEEDED')).to_be_visible(timeout=15000)
            page.get_by_role('button', name='IT tasks', exact=True).click()
            expect(page.locator('table:visible tbody tr').filter(has_text='Simulate account provisioning').get_by_text('DONE', exact=True)).to_be_visible(timeout=15000)
            page.get_by_role('button', name='Assets', exact=True).click()
            page.get_by_label('Asset tag').fill('WF-ASSET-001')
            page.get_by_label('Category', exact=True).fill('Laptop')
            page.get_by_label('Brand and model').fill('Synthetic model')
            page.get_by_role('button', name='Add asset').click()
            asset = page.locator('table:visible tbody tr').filter(has_text='WF-ASSET-001')
            expect(asset).to_be_visible(timeout=15000)
            asset.get_by_label('Employee ID', exact=True).fill(employee_id)
            asset.get_by_role('button', name='Assign', exact=True).click()
            expect(asset.get_by_role('cell', name='ASSIGNED', exact=True)).to_be_visible(timeout=15000)
            asset.get_by_label('Return notes').fill('Checked return')
            asset.get_by_role('button', name='Return asset', exact=True).click()
            expect(asset.get_by_role('cell', name='AVAILABLE', exact=True)).to_be_visible(timeout=15000)
            page.get_by_role('button', name='Audit', exact=True).click()
            expect(page.locator('table:visible tbody tr').filter(has_text='asset.returned')).to_be_visible(timeout=15000)
            page.set_viewport_size({'width':390,'height':844})
            expect(page.get_by_role('heading', name='Workforce operations')).to_be_visible()
            print('WORKFORCE_LOCAL_JOURNEY_PASS', flush=True)
        axe_path = os.environ.get('FLOWOPS_AXE_PATH')
        if axe_path:
            for route, heading in [('/tickets', 'Tickets'), ('/definitions', 'Workflow definitions'), ('/deliveries', 'Notification deliveries'), ('/jobs', 'Workflow jobs'), ('/analytics', 'Run analytics and SLA reporting'), ('/triggers', 'Event triggers'), ('/knowledge-base', 'Knowledge base'), ('/workforce', 'Workforce operations')]:
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
