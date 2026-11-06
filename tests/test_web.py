import re

import pytest
from sqlalchemy import select

from gilgamosh import connections
from gilgamosh.llm import MAX_OUTPUT_TOKENS, Gateway, fake_response
from gilgamosh.models import Actor, Call, Command, Endpoint, Job, Prompt, Setting, Simulation
from gilgamosh.services import latest_prompts, sync_prompts, text_hash
from gilgamosh.worker import claim


def test_login_and_csrf(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    assert client.get("/api/simulations").status_code == 401
    assert client.post("/login", data={"username": "admin", "password": "test-password-long-enough"}).status_code == 400
    page = client.get("/login").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    assert (
        client.post(
            "/login", data={"username": "admin", "password": "test-password-long-enough", "csrf_token": token}
        ).status_code
        == 302
    )
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.post("/api/simulations", json={}).status_code == 400


def test_endpoint_secret_inheritance_and_disabled_assignment(app, client, simulation, factory):
    response = client.post(
        "/api/admin/endpoints",
        json={
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "example/model",
            "api_key": "secret-test-value",
            "enabled": True,
            "timeout": 120,
        },
    )
    assert response.status_code == 200
    eid = response.json["id"]
    assert "secret-test-value" not in response.text and "secret" not in response.json
    assert client.post("/api/admin/settings", json={"endpoint_id": eid, "parallelism": 4}).status_code == 200
    with factory() as s:
        assert s.get(Endpoint, eid).secret != "secret-test-value"
    sid = simulation()
    assert client.get(f"/api/simulations/{sid}").json["params"]["rounds"] == 6
    assert client.delete(f"/api/admin/endpoints/{eid}").status_code == 400
    assert (
        client.post(
            "/api/admin/endpoints",
            json={
                "id": eid,
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "example/model",
                "enabled": False,
                "timeout": 120,
            },
        ).status_code
        == 200
    )
    # Explicitly assigned disabled endpoints never fall back, including demo mode.
    with app.app_context():
        from gilgamosh.security import PublicError

        with pytest.raises(PublicError):
            simulation()


def test_pause_resume_commands_idempotent_and_stale_controls(client, simulation, factory):
    sid = simulation()
    url = f"/api/simulations/{sid}/control"
    assert client.post(url, json={"action": "pause", "key": "pause-0000001"}).status_code == 200
    assert client.post(url, json={"action": "pause", "key": "pause-0000001"}).status_code == 200
    assert client.post(url, json={"action": "end", "key": "pause-0000001"}).status_code == 400
    assert claim(factory) is None
    assert client.post(url, json={"action": "continue", "key": "continue-0001"}).status_code == 400
    assert client.post(url, json={"action": "resume", "key": "resume-000001"}).status_code == 200
    with factory() as s:
        assert s.get(Simulation, sid).status == "running"
        assert len(s.scalars(select(Command).where(Command.simulation_id == sid)).all()) == 2
        assert len(s.scalars(select(Job).where(Job.simulation_id == sid, Job.kind == "phase")).all()) == 1


def test_input_validation(client):
    assert (
        client.post("/api/admin/actors", json={"items": [{"id": "person", "name": "Dr. Beispiel"}]}).status_code == 400
    )
    assert (
        client.post(
            "/api/admin/endpoints", json={"name": "Bad", "base_url": "http://example.com", "model": "x"}
        ).status_code
        == 400
    )
    assert client.post("/api/admin/settings", json={"parallelism": 0}).status_code == 400
    assert client.post("/api/simulations", json={"question": "short", "actors": []}).status_code == 400


def endpoint_payload(**overrides):
    return {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "z-ai/glm-5.3-flash",
        "api_key": "secret-test-value",
        "enabled": True,
        "timeout": 120,
        **overrides,
    }


@pytest.fixture
def provider(monkeypatch, app):
    """Real mode with a fake provider catalog; no network and no LLM call."""
    app.config["LLM_MODE"] = "openrouter"
    connections._cache.clear()
    state = {"key_status": 200, "catalog_status": 200}

    def fetch(url, key=""):
        if url.endswith("/key"):
            return state["key_status"], {"data": {}}
        return state["catalog_status"], {
            "data": [
                {
                    "id": "z-ai/glm-5.3-flash",
                    "context_length": 1048576,
                    "supported_parameters": ["structured_outputs"],
                },
                {"id": "~z-ai/glm-flash-latest", "supported_parameters": ["structured_outputs"]},
                {"id": "z-ai/plain-model", "supported_parameters": ["temperature"]},
            ]
        }

    monkeypatch.setattr(connections, "fetch_json", fetch)
    yield state
    connections._cache.clear()


def configure(client, **overrides):
    eid = client.post("/api/admin/endpoints", json=endpoint_payload(**overrides)).json["id"]
    assert client.post("/api/admin/settings", json={"endpoint_id": eid, "parallelism": 4}).status_code == 200
    return eid


def checked(client):
    return client.get("/api/admin/connections/check").json["items"]


def test_valid_model_passes_check(client, provider):
    configure(client)
    item = checked(client)[0]
    assert item["level"] == "ok" and item["context_length"] == 1048576
    assert "Moderator" in item["used_by"] and "Default" in item["used_by"]


def test_alias_and_variant_models_are_valid(client, provider):
    configure(client, model="~z-ai/glm-flash-latest")
    assert checked(client)[0]["level"] == "ok"
    configure(client, name="Variante", model="z-ai/glm-5.3-flash:free")
    assert all(i["level"] == "ok" for i in checked(client))


def test_wrong_model_is_reported_with_suggestion_and_blocks_start(client, provider):
    configure(client, model="z-ai/glm-flash-latest")
    item = checked(client)[0]
    assert item["level"] == "error" and "~z-ai/glm-flash-latest" in item["message"]
    response = client.post(
        "/api/simulations",
        json={
            "question": "Wie kann die Versorgung verbessert werden?",
            "actors": ["aerztekammer", "finanzministerium"],
        },
    )
    assert response.status_code == 400
    assert "z-ai/glm-flash-latest" in response.json["error"]


def test_wrong_override_names_who_is_affected(client, provider):
    configure(client)
    roles = {"moderator": {"endpoint_id": None, "model": "falsches/modell"}}
    assert (
        client.post(
            "/api/admin/settings",
            json={"endpoint_id": checked(client)[0]["endpoint_id"], "parallelism": 4, "roles": roles},
        ).status_code
        == 200
    )
    bad = [i for i in checked(client) if i["level"] == "error"]
    assert len(bad) == 1 and bad[0]["used_by"] == ["Moderator"] and bad[0]["model"] == "falsches/modell"


def test_rejected_key_is_reported(client, provider):
    configure(client)
    provider["key_status"] = 401
    connections._cache.clear()
    item = checked(client)[0]
    assert item["level"] == "error" and "API-Key" in item["message"]


def test_unreachable_catalog_does_not_block(client, provider, simulation):
    configure(client)
    provider["catalog_status"] = 0
    connections._cache.clear()
    assert checked(client)[0]["level"] == "unknown"
    response = client.post(
        "/api/simulations",
        json={
            "question": "Wie kann die Versorgung verbessert werden?",
            "actors": ["aerztekammer", "finanzministerium"],
        },
    )
    assert response.status_code == 201


def test_model_without_structured_output_warns(client, provider):
    configure(client, model="z-ai/plain-model")
    assert checked(client)[0]["level"] == "warning"


def test_missing_default_is_reported(client, provider):
    assert "Default-Verbindung" in checked(client)[0]["message"]


def test_costs_are_converted_to_eur(client, simulation, drain, factory, monkeypatch):
    def complete(self, config, request, task):
        response = fake_response(request, task)
        response["usage"] = {"total_tokens": 100, "cost": 0.01}
        return response

    monkeypatch.setattr(Gateway, "complete", complete)
    assert client.post("/api/admin/settings", json={"parallelism": 4, "eur_per_usd": 0.5}).status_code == 200
    sid = simulation()
    drain()
    metrics = client.get(f"/api/simulations/{sid}/metrics").json
    assert "known_cost_usd" not in metrics
    assert metrics["known_cost_eur"] == pytest.approx(metrics["cost_calls"] * 0.01 * 0.5)
    assert metrics["roles"]["agent"]["cost_eur"] > 0
    assert 0 < metrics["largest_context"] < metrics["context_limit"]
    assert client.post("/api/admin/settings", json={"parallelism": 4, "eur_per_usd": 0}).status_code == 400


def test_unmodified_prompts_are_upgraded_and_edited_ones_kept(app, factory):
    old = "Früher ausgelieferter Agent-Prompt mit genug Text"
    with factory.begin() as s:
        s.add(Prompt(role="agent", version=50, text=old))
        s.add(Prompt(role="watcher", version=50, text="Eigener Watcher-Prompt mit genug Text"))
        seeds = s.get(Setting, "prompt_seeds")
        seeds.value = {**seeds.value, "agent": text_hash(old)}
    with factory.begin() as s:
        sync_prompts(s)
    with factory() as s:
        latest = latest_prompts(s)
        assert latest["agent"].version == 51 and latest["agent"].text != old
        assert latest["watcher"].version == 50 and latest["watcher"].text.startswith("Eigener")
    # Without changes another start adds no versions.
    with factory.begin() as s:
        sync_prompts(s)
    with factory() as s:
        assert latest_prompts(s)["agent"].version == 51


def test_icons_are_served_and_missing_ones_fall_back(app, client, simulation):
    response = client.get("/icons/land_wien.png")
    assert response.status_code == 200 and response.mimetype == "image/png"
    assert "max-age" in response.headers["Cache-Control"]
    assert client.get("/icons/industrie.png").status_code == 404
    assert client.get("/icons/..%2F..%2Fsecret.png").status_code == 404
    assert app.test_client().get("/icons/land_wien.png").status_code == 302
    admin = client.get("/admin").text
    assert "/icons/aerztekammer.png" in admin and "inst-monogram" in admin
    assert "/icons/aerztekammer.png" in client.get("/").text
    sid = simulation()
    assert client.get(f"/simulations/{sid}").status_code == 200


def test_max_tokens_per_role_reach_the_calls(client, simulation, drain, factory):
    budget = {"agent": 1000, "orchestrator": 2000, "moderator": 3000, "watcher": 40000}
    assert client.post("/api/admin/settings", json={"parallelism": 4, "max_tokens": budget}).status_code == 200
    # Saving other settings keeps the budget, and later changes leave earlier simulations alone.
    assert client.post("/api/admin/settings", json={"parallelism": 3}).status_code == 200
    sid = simulation(rounds=1)
    assert client.post("/api/admin/settings", json={"parallelism": 4, "max_tokens": {"agent": 500}}).status_code == 200
    drain()
    with factory() as s:
        used = {c.role: c.request["max_tokens"] for c in s.scalars(select(Call).where(Call.simulation_id == sid))}
    assert used == budget
    assert "max_tokens.watcher" in client.get("/admin").text


@pytest.mark.parametrize("value", [0, 255, 1000001, "viel", None])
def test_invalid_max_tokens_are_rejected(client, value):
    response = client.post("/api/admin/settings", json={"parallelism": 4, "max_tokens": {"watcher": value}})
    assert response.status_code == 400 and "Output-Tokens" in response.json["error"]


def test_simulation_without_budget_in_snapshot_uses_the_default(simulation, drain, factory):
    sid = simulation(rounds=1)
    with factory.begin() as s:
        sim = s.get(Simulation, sid)
        snapshot = dict(sim.snapshot)
        del snapshot["max_tokens"]
        sim.snapshot = snapshot
    drain()
    with factory() as s:
        assert {c.request["max_tokens"] for c in s.scalars(select(Call).where(Call.simulation_id == sid))} == {
            MAX_OUTPUT_TOKENS
        }


def test_profile_form_keeps_everything_it_does_not_show(app, client, factory):
    with factory.begin() as s:
        actor = s.get(Actor, "land_wien")
        profile = {**actor.profile, "db_infos": [{"quelle": "manuell"}]}
        actor.profile = profile
    response = client.post(
        "/api/admin/actors",
        json={
            "id": "land_wien",
            "name": "Land Wien",
            "system_prompt": "Neuer Prompt mit genug Text.",
            "rote_linien": ["R1: Neue Linie"],
            "verhandelbar": [{"position": "Wichtig", "prioritaet": 1}, {"position": "Weniger", "prioritaet": 2}],
            "endpoint_id": None,
            "model": "",
        },
    )
    assert response.status_code == 200
    with factory() as s:
        actor = s.get(Actor, "land_wien")
        assert actor.parent_id == "bundeslaender"
        assert actor.profile["db_infos"] == [{"quelle": "manuell"}]
        assert actor.profile["selectable"] is True and actor.profile["source_file"]
        assert actor.profile["system_prompt"] == "Neuer Prompt mit genug Text."
        assert actor.profile["rote_linien"] == ["R1: Neue Linie"]
        assert actor.profile["verhandelbar"][1] == {"position": "Weniger", "prioritaet": 2}


def test_profile_form_shows_only_the_simplified_fields(client):
    page = client.get("/admin").text
    form = page[page.index('name="id" value="land_wien"') :]
    form = form[: form.index("</form>")]
    for name in ("system_prompt", "rote_linien", "verhandelbar"):
        assert f'name="{name}"' in form
    for name in ("parent_id", "db_infos", "demo", "selectable"):
        assert f'name="{name}"' not in form
    # Statutes are editable as plain lines, not as JSON.
    assert "R1: " in form and "data-json" not in form and "[&#34;" not in form
    assert 'value="bundeslaender"' not in page.split("Institutionen importieren")[0]


def test_the_interface_shows_the_product_name(app, client):
    login = app.test_client().get("/login").text
    assert "RoundTable" in login and "Gilgamosh" not in login
    for path in ("/", "/admin"):
        page = client.get(path).text
        assert "RoundTable" in page and "Gilgamosh" not in page
