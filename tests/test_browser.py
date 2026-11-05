"""Opt-in real browser smoke test against PostgreSQL and an offline worker."""

import json
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
            response = original(self, config, request, task)
            context = json.loads(request["messages"][-1]["content"])
            if task == "koalitionsphase" and context["institution"] == "Ärztekammer":
                value = json.loads(response["choices"][0]["message"]["content"])
                value.update(aktion="gruenden", partner=["finanzministerium"])
                response["choices"][0]["message"]["content"] = json.dumps(value, ensure_ascii=False)
            return response
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
            # The diagram already grows while the negotiation runs.
            page.get_by_role("tab", name="Verlauf", exact=True).click()
            expect(page.locator(".flow-box-active")).to_be_visible(timeout=20000)
            page.screenshot(path="test-results/flow-running.png", full_page=True)
            page.get_by_role("tab", name="Überblick", exact=True).click()
            expect(page.locator("#phase-status")).to_contain_text("Abgeschlossen", timeout=90000)
            expect(page.locator("#vote-legend")).to_contain_text("Zustimmung")
            expect(page.locator("#vote-chart")).to_be_visible()
            expect(page.locator("#timeline .marker-yes").first).to_be_visible()
            # Control roles and coalitions are visible on the overview.
            expect(page.locator("#control-roles")).to_contain_text("Orchestrator")
            expect(page.locator("#control-roles")).to_contain_text("Watcher")
            expect(page.locator(".stakeholder-coalition")).to_have_count(2)
            expect(page.locator("#timeline th", has_text="Gruppenverhandlung").first).to_be_visible()
            expect(page.get_by_text("Kosten nach Rolle")).to_have_count(0)
            # The side panel scrolls on its own.
            assert page.locator(".side-panel").evaluate("p => getComputedStyle(p).overflowY") == "auto"
            # Watcher sections start collapsed.
            assert page.locator("#watcher details[open]").count() == 0
            page.screenshot(path="test-results/simulation-done.png", full_page=True)
            # The flow diagram starts collapsed and opens on click.
            page.get_by_role("tab", name="Verlauf", exact=True).click()
            expect(page.locator(".flow-box").first).to_be_visible()
            expect(page.locator(".flow-panel")).to_have_count(0)
            page.screenshot(path="test-results/flow-collapsed.png", full_page=True)
            page.locator('[data-flow="1:1:groups"]').click()
            expect(page.locator(".flow-panel .coalition-box")).to_contain_text("Koalition G1")
            expect(page.locator(".flow-panel .flow-item").first).to_be_visible()
            page.locator('[data-flow="1:1:vote"]').click()
            expect(page.locator(".flow-panel")).to_have_count(2)
            page.screenshot(path="test-results/flow-open.png", full_page=True)
            page.locator('[data-flow="1:1:vote"]').click()
            expect(page.locator(".flow-panel")).to_have_count(1)
            # The back button returns to the previously open tab.
            page.get_by_role("tab", name="Auswertung", exact=True).click()
            expect(page.locator("#analysis")).to_be_visible()
            page.go_back()
            expect(page.locator("#flow-view")).to_be_visible()
            page.go_back()
            expect(page.locator("#overview")).to_be_visible()
            page.go_forward()
            expect(page.locator("#flow-view")).to_be_visible()
            # A marker in the timeline opens its step in the flow diagram.
            page.go_back()
            page.locator("#timeline .marker-yes").first.click()
            expect(page.locator("#flow-view")).to_be_visible()
            expect(page.locator(".flow-panel .flow-item").first).to_be_visible()
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
