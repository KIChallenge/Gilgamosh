"""Opt-in real browser smoke test against PostgreSQL and an offline worker."""

import os
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from gilgamosh.llm import Gateway
from gilgamosh.worker import run_worker

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in browser test")


def test_live_simulation_to_export(app, monkeypatch):
    from playwright.sync_api import expect, sync_playwright

    original = Gateway.complete
    activity_lock = threading.Lock()
    activity = {"active": 0, "maximum": 0}

    def delayed(self, config, request, task):
        with activity_lock:
            activity["active"] += 1
            activity["maximum"] = max(activity["maximum"], activity["active"])
        try:
            time.sleep(0.2)
            return original(self, config, request, task)
        finally:
            with activity_lock:
                activity["active"] -= 1

    monkeypatch.setattr(Gateway, "complete", delayed)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop = threading.Event()
    worker = threading.Thread(target=run_worker, args=(app,), kwargs={"stop": stop}, daemon=True)
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1512, "height": 1100})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/login")
            page.get_by_label("Benutzername").fill("admin")
            page.get_by_label("Passwort").fill("test-password-long-enough")
            page.get_by_role("button", name="Anmelden").click()
            page.get_by_role("link", name="Administration").click()
            page.get_by_role("tab", name="Modelle & Verbindungen").click()
            expect(page.get_by_role("heading", name="Default und Rollenzuweisungen")).to_be_visible()
            Path("test-results").mkdir(exist_ok=True)
            page.screenshot(path="test-results/admin-models.png", full_page=True)
            page.get_by_role("tab", name="Akteure & Prompts").click()
            page.screenshot(path="test-results/admin-actors.png", full_page=True)
            page.get_by_role("link", name="Simulationen", exact=True).click()
            expect(page.get_by_label("Max. Runden")).to_have_value("6")
            page.screenshot(path="test-results/index.png", full_page=True)
            page.get_by_role("button", name="Verhandlung starten").click()
            page.wait_for_url("**/simulations/*")
            expect(page.get_by_role("heading", name="Aktueller Stand")).to_be_visible()
            expect(page.locator("#active-status")).to_contain_text("Wartet auf Worker")
            worker.start()
            expect(page.locator("#active-status .loading")).to_be_visible(timeout=20000)
            page.get_by_role("button", name="Pause", exact=True).click()
            expect(page.locator("#phase-status")).to_contain_text("Pausiert")
            page.get_by_role("button", name="Fortsetzen", exact=True).click()
            expect(page.locator("#phase-status")).to_contain_text("Läuft")
            expect(page.locator("#watcher")).not_to_be_empty(timeout=45000)
            expect(page.locator("#timeline tbody tr")).not_to_have_count(0)
            # Header and body must share one grid, not merely exist.
            assert page.locator("#timeline").evaluate("t => getComputedStyle(t).display") == "table"
            expect(page.locator("#stakeholders img.inst-corner").first).to_be_visible()
            page.screenshot(path="test-results/simulation-running.png", full_page=True)
            expect(page.locator("#phase-status")).to_contain_text("Abgeschlossen", timeout=90000)
            expect(page.locator("#vote-legend")).to_contain_text("Tolerierung")
            expect(page.locator("#vote-chart")).to_be_visible()
            expect(page.locator("#cost-chart")).to_be_visible()
            expect(page.locator("#timeline .marker-tolerate").first).to_be_visible()
            page.screenshot(path="test-results/simulation-done.png", full_page=True)
            page.get_by_role("tab", name="Diskussion", exact=True).click()
            expect(page.locator("#discussion-feed article").first).to_be_visible()
            page.screenshot(path="test-results/simulation-discussion.png", full_page=True)
            page.get_by_role("tab", name="Auswertung", exact=True).click()
            expect(page.locator("#reports")).to_contain_text("Watcher-Bericht", timeout=45000)
            with page.expect_download() as result:
                page.get_by_role("link", name="Markdown exportieren").click()
            download = result.value
            assert download.suggested_filename.endswith(".md")
            assert "# Institutionelle Verhandlung" in Path(download.path()).read_text()
            assert not errors
            assert 1 < activity["maximum"] <= 4
            browser.close()
    finally:
        stop.set()
        if worker.is_alive():
            worker.join(timeout=30)
        server.shutdown()
        server_thread.join(timeout=5)
