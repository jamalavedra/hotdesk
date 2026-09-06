"""Check a disposable workspace: uv run --with playwright python tests/browser_check.py --config PATH."""

import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from hotdesk.__main__ import open_workspace, request
from hotdesk.config import load_config
from hotdesk.connection import read_connection

parser = argparse.ArgumentParser()
parser.add_argument("--config", default="hotdesk.toml")
parser.add_argument("--workspace")
args = parser.parse_args()
config = load_config(args.config)
connection = read_connection(config)
assert connection, "Start the manager before running the browser check"
workspace = args.workspace or next(iter(config.desktops))
output = Path(".hotdesk/browser-check")
output.mkdir(parents=True, exist_ok=True, mode=0o700)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        with patch("hotdesk.__main__.webbrowser.open", return_value=True) as opened:
            open_workspace(connection, workspace)
        page.goto(opened.call_args.args[0])
        expect(page.locator("#status")).to_contain_text("You have control.", timeout=30000)
        page.locator("canvas").wait_for(timeout=30000)
        assert "token=" not in page.url
        assert "view_only=false" in page.url
        cookie = next(
            cookie
            for cookie in page.context.cookies()
            if cookie["name"].startswith("hotdesk-session-")
        )
        assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
        page.screenshot(path=str(output / "viewer.png"))
        button = page.get_by_role("button", name="Return control to agents", exact=True)
        button.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#status")).to_contain_text("Observing.", timeout=30000)
        expect(button).to_be_hidden()
        assert (
            next(row for row in request(connection, "desktops") if row["name"] == workspace)[
                "state"
            ]
            == "idle"
        )
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "viewer-mobile.png"))
        with patch("hotdesk.__main__.webbrowser.open", return_value=True) as opened:
            open_workspace(connection, workspace)
        page.goto(opened.call_args.args[0])
        expect(page.locator("#status")).to_contain_text("You have control.", timeout=30000)
        subprocess.run(
            [sys.executable, "-m", "hotdesk", "--config", str(config.path), "release", workspace],
            cwd="/tmp",
            check=True,
        )
        expect(page.locator("#status")).to_contain_text("Disconnected.", timeout=15000)
        assert not errors, errors
    finally:
        row = next(row for row in request(connection, "desktops") if row["name"] == workspace)
        if row["state"] == "human":
            request(connection, f"desktops/{workspace}/control", {"control": "agent"})
        browser.close()
print(
    "PASS direct viewer bootstrap, fragment removal, cookie security, keyboard return control, mobile layout, CLI release from /tmp, no JS errors"
)
