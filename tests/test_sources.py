import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from gilgamosh import sources
from gilgamosh.llm import Gateway, fake_response
from gilgamosh.models import Call, Event, Simulation
from gilgamosh.sources import clean_press_page, ingest, parse_law, parse_press, search, statistics, store

TOC = """Inhaltsverzeichnis
S. 3       Lange Wartezeiten beim Facharzt
           30.09.2026, Kurier, Seite 4
S. 5       Ein sehr langer Titel über die
           Finanzierung der Spitäler
           29.09.2026, Die Presse, Seite 10-12
S. 7       Ö1 Abendjournal 18:00
           29.09.2026, Ö1 - Ö1 Abendjournal 18:00
"""
ARTICLE = "Die Wartezeiten auf Facharzttermine steigen in mehreren Regionen weiter an, berichtet die Kasse."
PLAIN = "\f".join(
    [
        "Deckblatt",
        "Inhalt",
        f"Pressespiegel\n\nKurier vom 30.09.2026\n\nLange Wartezeiten beim Facharzt\n{ARTICLE}\nQuelle\n\nKurier\n",
        "Kurier\nMittwoch\nGEORG AUTOR\nspaltensatz der druckseite",
        f"Pressespiegel\n\nDie Presse vom 29.09.2026\n\nEin sehr langer Titel\n{ARTICLE} Spitäler.\nSeite 5 von 9\n\n© APA-Comm GmbH\n",
        "Pressespiegel\n\nLand\n\nAT\n\nSprache\n\nDeutsch\n\nSeite 6 von 9\n",
        "Pressespiegel\n\nÖ1 vom 29.09.2026\n\nkurz",
    ]
)
LAW = """<?xml version="1.0" encoding="UTF-8"?>
<sozdok dokid="ASVG=PARA=131=100=2018-12-22=2020-01-01"><rvo><metadaten gldart="PARA" gldnr="131" abkrvo="ASVG"
titrvo="Allgemeines Sozialversicherungsgesetz" ikra="2020-01-01"/><nutzdaten><abschnitt>
<ueberschrift>Erstattung von Kosten</ueberschrift><absatz><gldsym>§ 131.</gldsym> (1) Nimmt der Anspruchsberechtigte
nicht die <rvolink>Vertragspartner</rvolink> in Anspruch, so gebührt der Ersatz der Kosten.</absatz></abschnitt></nutzdaten></rvo></sozdok>"""


def test_press_is_split_by_contents_without_facsimile_or_metadata():
    articles = parse_press(TOC, PLAIN)
    assert [a["publisher"] for a in articles] == ["Kurier", "Die Presse"]  # the very short item is dropped
    first, second = articles
    assert first["citation"] == "Kurier, 30.09.2026, S. 4" and first["published"] == date(2026, 9, 30)
    assert ARTICLE in first["body"]
    assert "GEORG AUTOR" not in first["body"] and "Quelle" not in first["body"] and "Kurier vom" not in first["body"]
    assert second["title"] == "Ein sehr langer Titel über die Finanzierung der Spitäler"
    assert second["citation"] == "Die Presse, 29.09.2026, S. 10-12"
    assert "APA-Comm" not in second["body"] and "Seite 5" not in second["body"]


def test_facsimile_page_is_ignored():
    assert clean_press_page("Kurier\nMittwoch\ntext") == ""


def test_law_keeps_citation_and_flat_text():
    row = parse_law(LAW)
    assert row["citation"] == "ASVG § 131" and row["kind"] == "law"
    assert "gebührt der Ersatz" in row["body"] and "<" not in row["body"]
    assert row["published"] == date(2020, 1, 1)
    assert parse_law(LAW.replace('gldart="PARA"', 'gldart="INH"')) is None


def source(key, kind="press", body="Wartezeiten beim Facharzt steigen", published=None, citation="Kurier, 01.01.2026"):
    return {
        "key": key,
        "kind": kind,
        "title": "Titel",
        "publisher": "Kurier",
        "citation": citation,
        "published": published or date.today(),
        "body": body,
    }


def load(app, *rows):
    from gilgamosh.models import db

    with app.app_context():
        store(db.session, list(rows))
        db.session.commit()


def test_store_skips_known_entries_and_ingest_is_repeatable(app, tmp_path, monkeypatch):
    from gilgamosh.models import db

    (tmp_path / "Rechtstexte").mkdir()
    (tmp_path / "Rechtstexte" / "a.xml").write_text(LAW)
    (tmp_path / "Rechtstexte" / "kaputt.xml").write_text("<kein xml")
    with app.app_context():
        assert ingest(db.session, tmp_path, report=lambda _: None)["law"] == 1
        assert ingest(db.session, tmp_path, report=lambda _: None)["law"] == 0
        assert statistics(db.session)["law"]["count"] == 1
        # A later start reads nothing again, and a changed folder is picked up.
        monkeypatch.setattr(sources, "parse_law", lambda _: pytest.fail("Bekannter Ordner wurde neu gelesen."))
        ingest(db.session, tmp_path, report=lambda _: None)
        monkeypatch.undo()
        (tmp_path / "Rechtstexte" / "b.xml").write_text(LAW.replace("ASVG=PARA=131", "ASVG=PARA=132"))
        assert ingest(db.session, tmp_path, report=lambda _: None)["law"] == 1


def test_known_press_file_is_not_read_again(app, tmp_path, monkeypatch):
    from gilgamosh.models import db

    folder = tmp_path / "2026 Pressespiegel 09 September"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"pdf")
    calls = []

    def read(path, layout=False):
        calls.append(layout)
        return TOC if layout else PLAIN

    monkeypatch.setattr(sources, "pdf_text", read)
    with app.app_context():
        assert ingest(db.session, tmp_path, report=lambda _: None)["press"] == 2
        assert len(calls) == 2
        assert ingest(db.session, tmp_path, report=lambda _: None)["press"] == 0
        assert len(calls) == 2


def test_missing_sources_are_tolerated_only_when_optional(app, tmp_path):
    from gilgamosh.models import db
    from gilgamosh.security import PublicError

    with app.app_context():
        assert ingest(db.session, tmp_path, report=lambda _: None, optional=True) == {"press": 0, "law": 0}
        with pytest.raises(PublicError):
            ingest(db.session, tmp_path, report=lambda _: None)


def test_search_matches_any_term_stems_german_and_prefers_recent_press(app):
    from gilgamosh.models import db

    today = date.today()
    load(
        app,
        source("old", published=today - timedelta(days=720), citation="Alt"),
        source("new", published=today, citation="Neu"),
        source("other", body="Ganz anderes Thema über Budgets"),
        source("law", kind="law", citation="ASVG § 1", body="Wartezeit im Gesetz"),
    )
    with app.app_context():
        hits = search(db.session, ["Wartezeit", "gibtesnicht"], "press", 5)
        assert [h["zitat"] for h in hits] == ["Neu", "Alt"]
        assert hits[0]["auszug"] and hits[0]["art"] == "Presse"
        assert [h["zitat"] for h in search(db.session, ["Wartezeit"], "law", 5)] == ["ASVG § 1"]
        # Operators in model output cannot alter the query or break it.
        assert search(db.session, ['Wartezeit" OR -"', "'; DROP"], "press", 5)
        assert search(db.session, ["", "  "], "press", 5) == []


def instrument(monkeypatch, transform):
    def complete(self, config, request, task):
        ctx = json.loads(request["messages"][-1]["content"])
        response = fake_response(request, task)
        value = transform(task, ctx, json.loads(response["choices"][0]["message"]["content"]))
        response["choices"][0]["message"]["content"] = json.dumps(value, ensure_ascii=False)
        return response

    monkeypatch.setattr(Gateway, "complete", complete)


def test_no_sources_skips_research_without_any_call(simulation, drain, factory):
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        assert not s.scalars(select(Call).where(Call.simulation_id == sid, Call.role == "recherche")).all()
        assert not s.scalar(select(Event).where(Event.simulation_id == sid, Event.kind == "briefing"))
        assert s.get(Simulation, sid).status == "completed"


def test_briefing_reaches_own_institution_only_and_cites_real_sources(app, simulation, drain, factory, monkeypatch):
    load(app, source("a"), source("b", citation="Presse, 02.01.2026"), source("l", kind="law", citation="ASVG § 131"))

    def transform(task, ctx, value):
        if task == "briefing":
            # A made-up source id must not survive.
            value["fakten"] = [
                {"quelle": ctx["treffer"][0]["quelle_id"], "aussage": f"Fakt nur für {ctx['institution']}"},
                {"quelle": 999999, "aussage": "Erfunden"},
            ]
        if task == "einzelphase":
            value["quellen"] = [info["quelle_id"] for info in ctx["db_infos"]] + [999999]
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.status == "completed" and set(sim.state["briefings"]) == set(sim.snapshot["actors"])
        briefings = s.scalars(select(Event).where(Event.simulation_id == sid, Event.kind == "briefing")).all()
        assert len(briefings) == 2 and all(len(e.payload["fakten"]) == 1 for e in briefings)
        fact = briefings[0].payload["fakten"][0]
        assert fact["zitat"] and fact["art"] and fact["quelle_id"]
        statements = s.scalars(
            select(Event).where(Event.simulation_id == sid, Event.kind == "statement", Event.phase == "individual")
        ).all()
        assert statements and all(e.payload["quellen"] and 999999 not in e.payload["quellen"] for e in statements)
        names = {a["name"] for a in sim.snapshot["actors"].values()}
        calls = s.scalars(select(Call).where(Call.simulation_id == sid)).all()
        for call in calls:
            context = call.request["messages"][-1]["content"]
            if call.role == "agent":
                foreign = names - {json.loads(context)["institution"]}
                assert not any(f"Fakt nur für {name}" in context for name in foreign)
                if call.phase == "individual":
                    assert f"Fakt nur für {json.loads(context)['institution']}" in context
            if call.role == "moderator":
                assert "Fakt nur für" not in context
        review = next(
            json.loads(c.request["messages"][-1]["content"])
            for c in calls
            if c.role == "orchestrator"
            and json.loads(c.request["messages"][-1]["content"]).get("aufgabe") == "statement_pruefen"
        )
        assert next(iter(review["institutionen_profile"].values()))["db_infos"]


def test_article_text_and_titles_stay_out_of_public_events(app, simulation, drain, factory):
    load(app, source("a", body="Wartezeiten laut Hans Musterperson deutlich gestiegen"))
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        public = s.scalars(select(Event).where(Event.simulation_id == sid, Event.visibility == "public")).all()
        assert "Musterperson" not in json.dumps([e.payload for e in public], ensure_ascii=False)


def test_blocked_briefing_means_no_facts_rather_than_personal_data(app, simulation, drain, factory, monkeypatch):
    load(app, source("a"))

    def transform(task, ctx, value):
        if task == "briefing":
            value["fakten"] = [
                {"quelle": ctx["treffer"][0]["quelle_id"], "aussage": "Testvorname Testnachname erklärte."}
            ]
        if task == "sicherheit" and "Testvorname" in json.dumps(ctx.get("pruefling", {})):
            value["keine_personen"] = False
        return value

    instrument(monkeypatch, transform)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        sim = s.get(Simulation, sid)
        assert sim.status == "completed" and all(not v for v in sim.state["briefings"].values())
        events = s.scalars(select(Event).where(Event.simulation_id == sid, Event.visibility == "public")).all()
        assert "Testvorname" not in json.dumps([e.payload for e in events], ensure_ascii=False)


def test_failed_research_does_not_stop_the_negotiation(app, simulation, drain, factory, monkeypatch):
    load(app, source("a"))
    original = Gateway.complete

    def invalid(self, config, request, task):
        response = original(self, config, request, task)
        if task == "briefing":
            response["choices"][0]["message"]["content"] = "invalid-json"
        return response

    monkeypatch.setattr(Gateway, "complete", invalid)
    sid = simulation(rounds=1)
    drain()
    with factory() as s:
        assert s.get(Simulation, sid).status == "completed"


def test_old_snapshot_without_research_role_keeps_its_course(app, simulation, drain, factory):
    load(app, source("a"))
    sid = simulation(rounds=1)
    with factory.begin() as s:
        sim = s.get(Simulation, sid)
        snapshot = json.loads(json.dumps(sim.snapshot))
        del snapshot["prompts"]["recherche"]
        del snapshot["roles"]["recherche"]
        sim.snapshot = snapshot
    drain()
    with factory() as s:
        assert s.get(Simulation, sid).status == "completed"
        assert not s.scalars(select(Call).where(Call.simulation_id == sid, Call.role == "recherche")).all()


def test_start_page_reports_data_basis(client, app):
    assert "Keine Quellen geladen" in client.get("/").text
    load(app, source("a"), source("l", kind="law"))
    page = client.get("/").text
    assert "Presseartikeln" in page and "Rechtstexten" in page
