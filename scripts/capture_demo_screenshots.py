"""Capture the portfolio demo screenshot pack with Playwright.

Run against a seeded demo stack (see docs/portfolio-demo.md):

    FLOWOPS_DEMO_URL=http://127.0.0.1:8000 python scripts/capture_demo_screenshots.py

Set FLOWOPS_CONSOLE_URL to a running command-center server (with
FLOWOPS_COMMAND_CENTER_DEV=true or an authenticated session) to also capture
the React console pages. Screenshots land in docs/assets/ by default.
"""

import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

APP_URL = os.environ.get("FLOWOPS_DEMO_URL", "http://127.0.0.1:8000")
CONSOLE_URL = os.environ.get("FLOWOPS_CONSOLE_URL", "")
OUTPUT_DIR = Path(os.environ.get("FLOWOPS_SCREENSHOT_DIR", "docs/assets"))
VIEWPORT = {"width": 1440, "height": 900}


def wait_ready(url: str, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError(f"Service unavailable: {url}")


def find_paused_request_id() -> int | None:
    """The seeded paused run (Marcus Webb) is the approval-journey subject."""
    with urlopen(f"{APP_URL}/onboarding-requests", timeout=10) as response:
        requests = json.load(response)
    for record in requests:
        if record.get("employee_name") == "Marcus Webb":
            return int(record["id"])
    return None


def main() -> int:
    wait_ready(APP_URL + "/ready")
    if CONSOLE_URL:
        wait_ready(CONSOLE_URL + "/api/session")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    paused_id = find_paused_request_id()
    captured = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        def capture(url: str, name: str) -> None:
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(OUTPUT_DIR / f"{name}.png"))
            captured.append(name)

        capture(APP_URL + "/", "landing")
        capture(APP_URL + "/dashboard", "dashboard")
        capture(APP_URL + "/ui/tickets", "tickets")
        if paused_id is not None:
            capture(APP_URL + f"/ui/onboarding-requests/{paused_id}",
                    "approval-journey")
        else:
            print("no paused request found; skipping approval-journey shot")
        capture(APP_URL + "/ui/analytics", "analytics")
        if CONSOLE_URL:
            # The React console covers runs and definitions; analytics,
            # notifications, and jobs are still Jinja-only (parity checklist).
            capture(CONSOLE_URL + "/runs", "console-runs")
            capture(CONSOLE_URL + "/definitions", "console-definitions")
        browser.close()
    print(f"Captured {len(captured)} screenshot(s) into {OUTPUT_DIR}:")
    for name in captured:
        print(f"  docs/assets/{name}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
