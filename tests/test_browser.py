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
            expect(page.locator(".app-logo").first).to_be_visible()
            page.screenshot(path="test-results/login.png")
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
            # Opening a profile gives readable, wide text fields; saving keeps the statutes intact.
            card = page.locator("details.actor-card", has_text="Land Wien")
            card.locator("summary").first.click()
            prompt = card.get_by_label("Systemprompt")
            expect(prompt).to_be_visible()
            assert prompt.bounding_box()["height"] > 400 and prompt.bounding_box()["width"] > 800
            lines = card.locator('textarea[name="rote_linien"]')
            assert lines.input_value().startswith("R1: ")
            positions = card.locator('textarea[name="verhandelbar"]')
            positions.fill("Erste Position\n\n  Zweite Position  ")
            page.screenshot(path="test-results/admin-actor-open.png", full_page=True)
            card.get_by_role("button", name="Profil speichern").click()
            expect(page.locator("#notification")).to_contain_text("Gespeichert")
            page.reload()
            card = page.locator("details.actor-card", has_text="Land Wien")
            card.locator("summary").first.click()
            expect(card.locator('textarea[name="verhandelbar"]')).to_have_value("Erste Position\nZweite Position")
            page.goto(f"http://127.0.0.1:{server.server_port}/admin")
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
            # The navigation stays visible while scrolling, without covering the side panel.
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(300)
            assert page.evaluate("window.scrollY") > 0
            header = page.locator("header").bounding_box()
            assert header["y"] == 0
            assert page.locator(".side-panel").bounding_box()["y"] >= header["height"]
            page.screenshot(path="test-results/sticky-header.png")
            page.evaluate("window.scrollTo(0, 0)")
            # The side panel scrolls on its own.
            assert page.locator(".side-panel").evaluate("p => getComputedStyle(p).overflowY") == "auto"
            # Watcher sections start collapsed.
            assert page.locator("#watcher details[open]").count() == 0
            page.screenshot(path="test-results/simulation-done.png", full_page=True)
            # Dark mode: applies at once, repaints the charts and survives a reload.
            assert page.locator(".app-logo").first.is_visible()
            light_body = page.evaluate("getComputedStyle(document.body).backgroundColor")
            page.locator("#theme-toggle").check(force=True)
            assert page.evaluate("document.documentElement.dataset.theme") == "gilgamosh-dark"
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") != light_body
            expect(page.locator("#vote-chart")).to_be_visible()
            page.wait_for_timeout(300)
            page.screenshot(path="test-results/dark-overview.png", full_page=True)
            page.get_by_role("tab", name="Verlauf", exact=True).click()
            page.locator('[data-flow="1:1:groups"]').click()
            page.wait_for_timeout(300)
            page.screenshot(path="test-results/dark-flow.png", full_page=True)
            page.reload()
            assert page.evaluate("document.documentElement.dataset.theme") == "gilgamosh-dark"
            assert page.locator("#theme-toggle").is_checked()
            page.goto(f"http://127.0.0.1:{server.server_port}/admin")
            page.locator("details.actor-card").first.locator("summary").first.click()
            page.wait_for_timeout(300)
            page.screenshot(path="test-results/dark-admin.png", full_page=True)
            page.go_back()
            page.locator("#theme-toggle").uncheck(force=True)
            page.get_by_role("tab", name="Überblick", exact=True).click()
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
