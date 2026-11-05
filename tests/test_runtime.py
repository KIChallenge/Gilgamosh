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
    sid = simulation(attempts=3, rounds=1)
    drain()
    with factory() as s:
        events = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "review")).all()
        # 2 institutions x 3 attempts x (individual, final group statement, ballot)
        assert len(events) == 18
        abstentions = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "abstention")).all()
        assert len(abstentions) == 6
        # Nothing but abstentions is no consensus.
        assert s.get(Simulation, sid).state["outcome"] == "teilkompromiss"


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
    fresh = rt.context("agent", next(iter(rt.actors)))
    assert not fresh["eigener_verlauf"] and not fresh["positionen_anderer"] and not fresh["letzte_abstimmung"]


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


def rejecting_votes(review_fields):
    def transform(task, ctx, value):
        if task == "abstimmung":
            value.update(stimme="ablehnen", begruendung="Der Vorschlag braucht eine Gegenfinanzierung.")
        if task == "statement_pruefen" and ctx["statement"].get("stimme") == "ablehnen":
            value.update(review_fields)
        return value

    return transform


def test_rejection_with_score_zero_and_named_line_still_counts(simulation, drain, factory, monkeypatch):
    fields = {"bewertung": 0, "entscheidung": "zurueckgewiesen", "verletzte_rote_linie": "R1"}
    instrument(monkeypatch, rejecting_votes(fields))
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.state["outcome"] == "teilkompromiss"
        assert {v["stimme"] for v in sim.state["votes"].values()} == {"ablehnen"}


def test_rejection_without_named_line_becomes_abstention(simulation, drain, factory, monkeypatch):
    fields = {"bewertung": 2, "entscheidung": "freigegeben", "verletzte_rote_linie": None}
    instrument(monkeypatch, rejecting_votes(fields))
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert all(v is None for v in sim.state["votes"].values())
        assert sim.state["outcome"] == "teilkompromiss"  # no support, so no consensus either


def test_rejection_for_damaged_top_priority_counts(simulation, drain, factory, monkeypatch):
    fields = {"bewertung": 2, "entscheidung": "freigegeben", "verletzte_rote_linie": "P1"}
    instrument(monkeypatch, rejecting_votes(fields))
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.state["outcome"] == "teilkompromiss"
        assert {v["stimme"] for v in sim.state["votes"].values()} == {"ablehnen"}


def test_rejection_for_lower_priority_is_not_valid(simulation, drain, factory, monkeypatch):
    fields = {"bewertung": 2, "entscheidung": "freigegeben", "verletzte_rote_linie": "P5"}
    instrument(monkeypatch, rejecting_votes(fields))
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert all(v is None for v in sim.state["votes"].values())
        assert sim.state["outcome"] == "teilkompromiss"


def test_toleration_alone_is_no_consensus(simulation, drain, factory, monkeypatch):
    def transform(task, ctx, value):
        if task == "abstimmung":
            value["stimme"] = "tolerieren"
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert {v["stimme"] for v in sim.state["votes"].values()} == {"tolerieren"}
        assert sim.state["outcome"] == "teilkompromiss"


def test_confirmed_rejection_survives_blocked_wording(simulation, drain, factory, monkeypatch):
    base = rejecting_votes({"bewertung": 2, "entscheidung": "freigegeben", "verletzte_rote_linie": "R1"})

    def transform(task, ctx, value):
        value = base(task, ctx, value)
        if task == "sicherheit" and ctx.get("pruefling", {}).get("stimme") == "ablehnen":
            value["keine_vertraulichen_statuten"] = False
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.state["outcome"] == "teilkompromiss"
        assert all(v["stimme"] == "ablehnen" for v in sim.state["votes"].values())
        votes = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "vote")).all()
        assert all("nicht zur Veröffentlichung" in e.payload["begruendung"] for e in votes)


def test_violated_line_id_is_not_published(simulation, drain, factory, monkeypatch):
    base = rejecting_votes({"bewertung": 2, "entscheidung": "freigegeben", "verletzte_rote_linie": "R1"})

    def transform(task, ctx, value):
        value = base(task, ctx, value)
        if task == "abstimmung":
            value["verletzte_rote_linie"] = "R1"
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        votes = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "vote")).all()
        assert votes and all("verletzte_rote_linie" not in e.payload for e in votes)


def test_abstention_blocks_consensus(simulation, drain, factory, monkeypatch):
    def transform(task, ctx, value):
        if task == "statement_pruefen" and ctx.get("institution") == first_name and ctx["statement"].get("stimme"):
            value.update(bewertung=0, entscheidung="zurueckgewiesen", hinweis_an_agent="Überarbeiten.")
        return value

    first_name = "Ärztekammer"
    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        # One institution approves, but the other was blocked and never consented: no consensus.
        assert sim.state["outcome"] == "teilkompromiss"
        assert sum(v is None for v in sim.state["votes"].values()) == 1


def test_invalid_safety_answer_pauses_instead_of_abstaining(simulation, drain, factory, monkeypatch):
    original = Gateway.complete

    def invalid(self, config, request, task):
        response = original(self, config, request, task)
        context = json.loads(request["messages"][-1]["content"])
        if task == "sicherheit" and context.get("pruefling", {}).get("stimme"):
            response["choices"][0]["message"]["content"] = "invalid-json"
        return response

    monkeypatch.setattr(Gateway, "complete", invalid)
    sid = simulation()
    drain()
    with factory() as s:
        assert s.get(Simulation, sid).status == "paused"
        assert not s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "ballot"))


def test_every_stakeholder_gets_a_bounded_context_of_its_own(simulation, drain, factory, monkeypatch):
    long_text = "Langes Argument. " * 200

    def transform(task, ctx, value):
        if task == "einzelphase":
            value["position"] = f"{ctx['institution']}: {long_text}"
        if task == "abstimmung":
            value.update(stimme="ablehnen")
        if task == "statement_pruefen" and ctx["statement"].get("stimme") == "ablehnen":
            value["verletzte_rote_linie"] = "R1"
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=4)
    drain()

    def largest(s, role, round_number, task):
        rows = s.scalars(
            select(Call).where(Call.simulation_id == sid, Call.role == role, Call.round == round_number)
        ).all()
        sizes = [
            len(json.dumps(c.request, ensure_ascii=False))
            for c in rows
            if json.loads(c.request["messages"][-1]["content"]).get("aufgabe") == task
        ]
        return max(sizes)

    with factory() as s:
        # Context does not grow with the rounds: own history is capped, others appear once.
        assert largest(s, "agent", 4, "einzelphase") < largest(s, "agent", 2, "einzelphase") * 1.5
        # A check of one statement carries one institution's statutes and no foreign history.
        review = next(
            json.loads(c.request["messages"][-1]["content"])
            for c in s.scalars(select(Call).where(Call.simulation_id == sid, Call.round == 3))
            if json.loads(c.request["messages"][-1]["content"]).get("aufgabe") == "statement_pruefen"
        )
        assert list(review["institutionen_profile"]) and len(review["institutionen_profile"]) == 1
        assert list(review["bisherige_positionen"]) == [review["institution"]]
        # The safety check reads the text under review, not the negotiation.
        safety = next(
            json.loads(c.request["messages"][-1]["content"])
            for c in s.scalars(select(Call).where(Call.simulation_id == sid, Call.round == 3))
            if json.loads(c.request["messages"][-1]["content"]).get("aufgabe") == "sicherheit"
        )
        assert "verlauf" not in safety and "bisherige_positionen" not in safety
        assert long_text not in json.dumps(safety["institutionen_profile"])


def test_requested_change_prevents_consensus_and_opens_next_round(simulation, drain, factory, monkeypatch):
    seen = []

    def transform(task, ctx, value):
        # The first ballot asks for a change, the second one is fine.
        if task == "abstimmung" and ctx["runde"] == 1:
            value.update(stimme="nachbessern", begruendung="Die Finanzierung muss konkret benannt werden.")
        if task == "sammelstand_und_vorschlag":
            seen.append(ctx.get("letzte_abstimmung"))
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=3)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        ballots = s.scalars(
            select(Event).where(Event.simulation_id == sid, Event.kind == "ballot").order_by(Event.id)
        ).all()
        assert [b.payload["konsens"] for b in ballots] == [False, True]
        assert sim.round == 2 and sim.state["outcome"] == "konsens" and sim.status == "completed"
    # The moderator's next proposal receives the wanted changes.
    assert seen[0] is None
    assert len(seen[1]["nachbesserungen"]) == 2 and "Finanzierung" in seen[1]["nachbesserungen"][0]["aenderung"]
    assert seen[1]["ablehnungsgruende"] == []


def test_requested_change_needs_no_red_line_and_ends_as_partial_result(simulation, drain, factory, monkeypatch):
    def transform(task, ctx, value):
        if task == "abstimmung":
            value.update(stimme="nachbessern", begruendung="Mehr Mittel für den ländlichen Raum.")
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=2)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.status == "awaiting_decision" and sim.state["outcome"] == "teilkompromiss" and sim.round == 2
        assert all(v["stimme"] == "nachbessern" for v in sim.state["votes"].values())
        closing = s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "closing"))
        assert len(closing.payload["nachbesserung"]) == 2 and closing.payload["ablehnung"] == []
        # Other institutions see the wanted changes through the last ballot.
        agent_call = next(
            c
            for c in s.scalars(select(Call).where(Call.simulation_id == sid, Call.role == "agent", Call.round == 2))
            if json.loads(c.request["messages"][-1]["content"]).get("aufgabe") == "einzelphase"
        )
        assert json.loads(agent_call.request["messages"][-1]["content"])["letzte_abstimmung"]["nachbesserungen"]


def test_request_without_concrete_change_becomes_abstention(simulation, drain, factory, monkeypatch):
    def transform(task, ctx, value):
        if task == "abstimmung":
            value.update(stimme="nachbessern", begruendung="Passt nicht.")
        if task == "statement_pruefen" and ctx["statement"].get("stimme") == "nachbessern":
            value.update(bewertung=0, entscheidung="zurueckgewiesen", hinweis_an_agent="Konkrete Änderung nennen.")
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert all(v is None for v in sim.state["votes"].values())
        assert sim.state["outcome"] == "teilkompromiss"


def test_requested_change_and_rejection_can_coexist_for_different_reasons(simulation, drain, factory, monkeypatch):
    def transform(task, ctx, value):
        if task == "abstimmung":
            value.update(stimme="nachbessern", begruendung="Konkrete Finanzierung nennen.")
            if ctx["institution"] == "Ärztekammer":
                value.update(stimme="ablehnen", verletzte_rote_linie="P1")
        if task == "statement_pruefen" and ctx["statement"].get("stimme") == "ablehnen":
            value["verletzte_rote_linie"] = "P1"
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert {v["stimme"] for v in sim.state["votes"].values()} == {"ablehnen", "nachbessern"}
        assert sim.state["outcome"] == "teilkompromiss"
