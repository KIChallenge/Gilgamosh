import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from gilgamosh.llm import Gateway, fake_response
from gilgamosh.models import Call, Event, Job, Simulation, now
from gilgamosh.runtime import Runtime, Stopped
from gilgamosh.services import event
from gilgamosh.worker import claim, execute


def instrument(monkeypatch, transform):
    def complete(self, config, request, task):
        ctx = json.loads(request["messages"][-1]["content"])
        response = fake_response(request, task)
        value = json.loads(response["choices"][0]["message"]["content"])
        value = transform(task, ctx, value)
        response["choices"][0]["message"]["content"] = json.dumps(value, ensure_ascii=False)
        return response

    monkeypatch.setattr(Gateway, "complete", complete)


def test_complete_run_and_watcher(simulation, drain, factory, client):
    sid = simulation()
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.status == "completed"
        assert sim.snapshot["params"]["rounds"] == 6
        assert s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "report"))
        observation = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "watcher")).all()
        assert observation and all(e.payload["summary"]["referenzen"] for e in observation)
        assert s.scalar(select(Call).where(Call.simulation_id == sid)).request["messages"]
    response = client.get(f"/api/simulations/{sid}").json
    assert not any(e["kind"] == "review" for e in response["events"])
    assert response["watcher"]
    assert client.get(f"/api/simulations/{sid}/export?format=md").status_code == 200


def test_redline_retries_are_bounded(simulation, drain, factory, monkeypatch):
    def reject(task, ctx, value):
        if task == "statement_pruefen":
            value.update(
                bewertung=0,
                entscheidung="zurueckgewiesen",
                verletzte_rote_linie="Eigene rote Linie",
                hinweis_an_agent="Überarbeiten.",
            )
        return value

    instrument(monkeypatch, reject)
    sid = simulation(attempts=3)
    drain()
    with factory() as s:
        events = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "review")).all()
        # 2 institutions x 3 attempts x (individual, final group statement, ballot)
        assert len(events) == 18
        abstentions = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "abstention")).all()
        assert len(abstentions) == 6
        assert s.get(Simulation, sid).state["outcome"] == "konsens"


def test_valid_rejection_round_limit_continue_and_reset(simulation, drain, factory, client, monkeypatch):
    def reject(task, ctx, value):
        if task == "abstimmung":
            value["stimme"] = "ablehnen"
        if task == "statement_pruefen" and ctx["statement"].get("stimme") == "ablehnen":
            value["verletzte_rote_linie"] = next(
                a for a in ctx["institutionen_profile"].values() if a["name"] == ctx["institution"]
            )["rote_linien"][0]
        return value

    instrument(monkeypatch, reject)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.status == "awaiting_decision" and sim.round == 1
        groups = sim.state["groups"]
    assert (
        client.post(f"/api/simulations/{sid}/control", json={"action": "continue", "key": "continue-0001"}).status_code
        == 200
    )
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.block == 2 and sim.state["groups"] == groups and sim.state["proposal"]
    drain()
    assert (
        client.post(f"/api/simulations/{sid}/control", json={"action": "reset", "key": "reset-000001"}).status_code
        == 200
    )
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.run == 2 and "proposal" not in sim.state
        assert s.scalar(select(Event).where(Event.simulation_id == sid, Event.run == 1, Event.kind == "report"))
    job = claim(factory)
    rt = Runtime(client.application, factory, *job)
    assert not rt.context("agent", next(iter(rt.actors)))["verlauf"]


def test_mutual_consent_and_round_robin_limits(simulation, drain, factory, monkeypatch):
    def join(task, ctx, value):
        if task == "koalitionsphase":
            ids = list(ctx["institutions_ids"])
            if ctx["institution"] == ctx["institutions_ids"][ids[0]]:
                value.update(aktion="gruenden", partner=[ids[1]])
        return value

    instrument(monkeypatch, join)
    sid = simulation(messages=3)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert len(sim.state["groups"]) == 1
        events = s.scalars(
            select(Event).where(Event.simulation_id == sid, Event.kind == "group_message").order_by(Event.id)
        ).all()
        assert len(events) == 6
        assert [e.actor for e in events] == list(sim.snapshot["actors"]) * 3
        assert max(e.payload["nachricht_nr"] for e in events) == 3
        assert len(s.scalars(select(Event).where(Event.kind == "consent")).all()) == 2


def test_declined_invitation_does_not_move_members(simulation, drain, factory, monkeypatch):
    def decline(task, ctx, value):
        if task == "koalitionsphase":
            value.update(aktion="gruenden", partner=list(ctx["institutions_ids"]))
        if task == "zustimmung":
            value["zustimmung"] = False
        return value

    instrument(monkeypatch, decline)
    sid = simulation()
    drain()
    with factory() as s:
        assert all(len(m) == 1 for m in s.get(Simulation, sid).state["groups"].values())
        assert not s.scalar(select(Event).where(Event.kind == "group_message"))


def test_context_isolation_and_watchers_never_feed_agents(app, simulation, factory):
    sid = simulation()
    claimed = claim(factory)
    rt = Runtime(app, factory, *claimed)
    first, second = list(rt.actors)
    rt.actors[first]["rote_linien"] = ["PRIVATE_FIRST"]
    rt.actors[second]["rote_linien"] = ["PRIVATE_SECOND"]
    rt.snap["prompts"]["agent"] += "\n{{STATUTEN_ALLER_AGENTS}}"
    with factory.begin() as s:
        sim = s.get(Simulation, sid)
        event(s, sim, "review", {"begruendung_intern": "SECRET_REVIEW"}, second, visibility="private")
        event(s, sim, "feedback", {"hinweis_an_agent": "OWN_FEEDBACK"}, first, visibility="private")
        event(s, sim, "group_message", {"inhalt": "SECRET_GROUP", "members": [second]}, second, "foreign", "group")
        event(s, sim, "watcher", {"summary": "WATCHER_SECRET"})
        event(s, sim, "report", {"ergebnis": "REPORT_SECRET"})
    # Reports are operator-only even though their UI visibility is public.
    for role, actor in [("agent", first), ("moderator", "")]:
        text = json.dumps(rt.context(role, actor)) + rt.prompt(role, actor)
        assert "PRIVATE_SECOND" not in text and "SECRET_REVIEW" not in text and "SECRET_GROUP" not in text
        assert "WATCHER_SECRET" not in text and "REPORT_SECRET" not in text
    assert "OWN_FEEDBACK" in json.dumps(rt.context("agent", first))


def test_expired_lease_reuses_saved_calls_and_fences_old_worker(app, simulation, factory):
    sid = simulation()
    # Preparation, then question, leaving individual phase queued.
    for _ in range(2):
        execute(app, factory, *claim(factory))
    job = claim(factory)
    rt = Runtime(app, factory, *job)
    while rt.kind != "phase":
        execute(app, factory, *job)
        job = claim(factory)
        rt = Runtime(app, factory, *job)
    actor = next(iter(rt.actors))
    rt.approved("einzelphase", actor, "position")
    with factory.begin() as s:
        count = len(s.scalars(select(Call).where(Call.simulation_id == sid, Call.actor == actor)).all())
        s.get(Job, job[0]).lease_until = now() - timedelta(seconds=1)
    recovered = claim(factory)
    assert recovered[0] == job[0] and recovered[1] != job[1]
    with pytest.raises(Stopped):
        rt.emit("statement", {"position": "stale"})
    execute(app, factory, *recovered)
    with factory() as s:
        assert len(s.scalars(select(Call).where(Call.simulation_id == sid, Call.actor == actor)).all()) == count
        assert (
            len(
                s.scalars(
                    select(Event).where(Event.simulation_id == sid, Event.actor == actor, Event.kind == "statement")
                ).all()
            )
            == 1
        )


def test_person_output_is_not_published(simulation, drain, factory, monkeypatch):
    def person(task, ctx, value):
        if task == "einzelphase":
            value["position"] = "Frau Beispiel schlägt eine Reform vor."
        return value

    instrument(monkeypatch, person)
    sid = simulation()
    drain()
    with factory() as s:
        events = s.scalars(select(Event).where(Event.simulation_id == sid, Event.visibility == "public")).all()
        assert "Frau Beispiel" not in json.dumps([e.payload for e in events])
        calls = s.scalars(select(Call).where(Call.simulation_id == sid, Call.status == "invalid")).all()
        assert calls  # raw data remains in the protected technical record


def test_invalid_json_gets_one_repair_then_abstention(simulation, drain, factory, monkeypatch):
    original = Gateway.complete

    def invalid(self, config, request, task):
        response = original(self, config, request, task)
        if task == "einzelphase":
            response["choices"][0]["message"]["content"] = "invalid-json"
        return response

    monkeypatch.setattr(Gateway, "complete", invalid)
    sid = simulation()
    drain()
    with factory() as s:
        calls = s.scalars(
            select(Call).where(Call.simulation_id == sid, Call.phase == "individual", Call.role == "agent")
        ).all()
        assert len(calls) == 4  # exactly 2 calls per institution, independent of fachliche MAX_RETRY
        assert all(c.status == "invalid" for c in calls)
        assert (
            len(
                s.scalars(
                    select(Event).where(
                        Event.simulation_id == sid, Event.phase == "individual", Event.kind == "abstention"
                    )
                ).all()
            )
            == 2
        )


def test_invalid_orchestrator_pauses_instead_of_inventing_consensus(simulation, drain, factory, monkeypatch):
    original = Gateway.complete

    def invalid(self, config, request, task):
        response = original(self, config, request, task)
        if task == "statement_pruefen":
            response["choices"][0]["message"]["content"] = "invalid-json"
        return response

    monkeypatch.setattr(Gateway, "complete", invalid)
    sid = simulation()
    drain()
    with factory() as s:
        assert s.get(Simulation, sid).status == "paused"
        assert not s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "ballot"))


def test_semantic_person_check_blocks_unrecognized_names(simulation, drain, factory, monkeypatch):
    def unsafe(task, ctx, value):
        if task == "einzelphase":
            value["position"] = "Testvorname Testnachname fordert eine Reform."
        if task == "sicherheit" and "Testvorname" in json.dumps(ctx.get("pruefling", {})):
            value["keine_personen"] = False
        return value

    instrument(monkeypatch, unsafe)
    sid = simulation()
    drain()
    with factory() as s:
        public = s.scalars(select(Event).where(Event.simulation_id == sid, Event.visibility == "public")).all()
        assert "Testvorname" not in json.dumps([e.payload for e in public])
        assert s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "abstention"))
