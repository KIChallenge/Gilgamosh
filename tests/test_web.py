import re

from sqlalchemy import select

from gilgamosh.models import Command, Endpoint, Job, Simulation
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
        import pytest

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
