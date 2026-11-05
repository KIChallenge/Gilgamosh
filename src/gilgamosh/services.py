import copy
import hashlib
import secrets
from pathlib import Path

from sqlalchemy import select

from .models import Actor, Endpoint, Event, Job, Prompt, Setting, Simulation, User, db
from .schemas import ActorInput
from .security import PublicError, local_check

ROLES = ("agent", "orchestrator", "moderator", "watcher")
# ECB reference rate of 2026-11-05 (1 EUR = 1.1224 USD); editable in the administration.
DEFAULTS = {
    "endpoint_id": None,
    "parallelism": 4,
    "roles": {},
    "blocked_names": [],
    "eur_per_usd": 0.8909,
    # The watcher summarizes whole negotiations and needs the largest output budget.
    "max_tokens": {"agent": 16000, "orchestrator": 16000, "moderator": 16000, "watcher": 32000},
}
# Prompt texts shipped before seeds were recorded; an installation still on one of them is unmodified.
FIRST_PROMPT_HASHES = {
    "agent": "8ec081987180f4c06bf6067ad547ab87142617b0e747992e8a3ef4c0f7003801",
    "moderator": "5e808984d3fa77fa3575b89ab2f02c56de0ed18af9f1c4506bb9e83bfba9afef",
    "orchestrator": "fa48bb36f1d1ab262f4e5faf1d1959d2a3d9f5b93ce2ca5aa61d41da4efbf94b",
    "watcher": "05992cb50ed4b837d57ec3a0f89100d5cfd3c037f36ed4da55c1530783ce5e9a",
}
PHASE_LABELS = {
    "start": "Vorbereitung",
    "question": "Frage",
    "individual": "Einzelpositionen",
    "coalition": "Koalitionen",
    "groups": "Gruppenverhandlung",
    "proposal": "Kompromissvorschlag",
    "vote": "Abstimmung",
    "closing": "Abschluss",
    "done": "Abgeschlossen",
}


def settings(session):
    row = session.get(Setting, "system")
    value = {**DEFAULTS, **(row.value if row else {})}
    value["max_tokens"] = {**DEFAULTS["max_tokens"], **value["max_tokens"]}
    return value


def event(session, sim, kind, payload, actor="", group="", visibility="public"):
    row = Event(
        simulation_id=sim.id,
        run=sim.run,
        block=sim.block,
        round=sim.round,
        phase=sim.phase,
        kind=kind,
        payload=payload,
        actor=actor,
        group=group,
        visibility=visibility,
    )
    session.add(row)
    return row


def enqueue(session, sim, kind="phase", payload=None):
    session.add(Job(simulation_id=sim.id, kind=kind, payload=payload or {}))


def latest_prompts(session):
    return {
        role: session.scalars(select(Prompt).where(Prompt.role == role).order_by(Prompt.version.desc())).first()
        for role in ROLES
    }


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def sync_prompts(session):
    """Adopts changed repository prompts as new versions; edited prompts in the database stay in charge."""
    prompts = latest_prompts(session)
    row = session.get(Setting, "prompt_seeds") or Setting(key="prompt_seeds", value={})
    session.add(row)
    seeds = dict(row.value)
    for role in ROLES:
        text = (Path(__file__).parent / "prompts" / f"prompt_{role}.md").read_text()
        latest = prompts[role]
        unmodified = latest and text_hash(latest.text) == seeds.get(role, FIRST_PROMPT_HASHES[role])
        if not latest or (unmodified and latest.text != text):
            session.add(Prompt(role=role, version=latest.version + 1 if latest else 1, text=text))
            seeds[role] = text_hash(text)
    row.value = seeds


def import_actors(session, items):
    values = [ActorInput.model_validate(item) for item in items]
    if len({item.id for item in values}) != len(values):
        raise PublicError("Akteurskennungen müssen eindeutig sein.")
    for item in values:
        local_check(item.model_dump(), settings(session)["blocked_names"])
        if item.endpoint_id and not session.get(Endpoint, item.endpoint_id):
            raise PublicError("Unbekannte Verbindung.")
        if item.parent_id == item.id:
            raise PublicError("Eine Institution kann nicht ihre eigene übergeordnete Institution sein.")
        if item.selectable and not item.rote_linien:
            raise PublicError("Verhandelnde Institutionen benötigen mindestens eine rote Linie.")
        data = item.model_dump()
        existing = session.get(Actor, item.id)
        fields = (
            "system_prompt",
            "rote_linien",
            "verhandelbar",
            "db_infos",
            "demo",
            "modeled",
            "selectable",
            "source_file",
            "source_hash",
            "seed_hash",
        )
        profile = {
            key: (existing.profile.get(key, data[key]) if existing and key not in item.model_fields_set else data[key])
            for key in fields
        }
        if existing:
            existing.name, existing.profile = item.name, profile
            if "endpoint_id" in item.model_fields_set:
                existing.endpoint_id = item.endpoint_id
            if "model" in item.model_fields_set:
                existing.model = item.model
        else:
            session.add(
                Actor(id=item.id, name=item.name, profile=profile, endpoint_id=item.endpoint_id, model=item.model)
            )
    session.flush()
    for item in values:
        if item.parent_id and not session.get(Actor, item.parent_id):
            raise PublicError("Übergeordnete Institution fehlt im Import oder in der Datenbank.")
        session.get(Actor, item.id).parent_id = item.parent_id
    session.flush()
    for item in values:
        seen, current = set(), session.get(Actor, item.id)
        while current:
            if current.id in seen:
                raise PublicError("Die Institutionshierarchie darf keine Schleifen enthalten.")
            seen.add(current.id)
            current = session.get(Actor, current.parent_id) if current.parent_id else None


def endpoint_snapshot(session, endpoint_id, model, system, app):
    endpoint = (
        session.get(Endpoint, endpoint_id or system["endpoint_id"]) if endpoint_id or system["endpoint_id"] else None
    )
    if app.config["LLM_MODE"] == "fake" and endpoint is None:
        return {"id": None, "base_url": "", "secret": "", "model": "offline-demo", "timeout": 120}
    if not endpoint or not endpoint.enabled or not (model or endpoint.model):
        raise PublicError("Aktive Verbindung und Modell für alle Rollen konfigurieren.")
    return {
        "id": endpoint.id,
        "base_url": endpoint.base_url,
        "secret": endpoint.secret,
        "model": model or endpoint.model,
        "timeout": endpoint.timeout,
    }


def create_simulation(session, data, app):
    system = settings(session)
    question = str(data.get("question", "")).strip()
    if not 10 <= len(question) <= 10000:
        raise PublicError("Bitte eine Frage mit 10 bis 10.000 Zeichen eingeben.")
    local_check(question, system["blocked_names"])
    ids = data.get("actors", [])
    if not isinstance(ids, list) or not 2 <= len(set(ids)) <= 30:
        raise PublicError("Bitte 2 bis 30 Institutionen auswählen.")
    actors = [session.get(Actor, key) for key in dict.fromkeys(ids)]
    if not all(actors):
        raise PublicError("Eine ausgewählte Institution fehlt.")
    if any(not a.profile.get("selectable", True) for a in actors):
        raise PublicError("Übergeordnete Gruppenknoten sind keine Verhandlungsakteure.")
    params = {}
    for key, default, upper in [("rounds", 6, 50), ("attempts", 3, 10), ("messages", 3, 10)]:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            raise PublicError("Limits müssen ganze Zahlen sein.") from None
        if not 1 <= value <= upper:
            raise PublicError("Limits liegen außerhalb des erlaubten Bereichs.")
        params[key] = value
    prompts = latest_prompts(session)
    if not all(prompts.values()):
        raise PublicError("Rollenprompts fehlen. Initialisierung ausführen.")
    snap = {
        "actors": {},
        "roles": {},
        "prompts": {k: v.text for k, v in prompts.items()},
        "prompt_versions": {k: v.version for k, v in prompts.items()},
        "params": params,
        "parallelism": system["parallelism"],
        "max_tokens": system["max_tokens"],
        "blocked_names": system["blocked_names"],
        "mode": app.config["LLM_MODE"],
        "seed": secrets.randbits(32),
    }
    for actor in actors:
        snap["actors"][actor.id] = {
            "id": actor.id,
            "name": actor.name,
            **copy.deepcopy(actor.profile),
            "endpoint": endpoint_snapshot(session, actor.endpoint_id, actor.model, system, app),
        }
    for role in ("moderator", "orchestrator", "watcher"):
        assignment = system.get("roles", {}).get(role, {})
        snap["roles"][role] = endpoint_snapshot(
            session, assignment.get("endpoint_id"), assignment.get("model", ""), system, app
        )
    sim = Simulation(
        question=question, snapshot=snap, state={"groups": {a.id: [a.id] for a in actors}, "verified": False}
    )
    session.add(sim)
    session.flush()
    event(session, sim, "status", {"text": "Institutionen und Eingaben werden geprüft."})
    enqueue(session, sim)
    return sim


def initialize(app, username, password):
    from werkzeug.security import generate_password_hash

    with app.app_context():
        if not db.session.scalar(select(User).where(User.username == username)):
            if len(password) < 12:
                raise PublicError("Das initiale Passwort muss mindestens 12 Zeichen haben.")
            db.session.add(User(username=username, password_hash=generate_password_hash(password)))
        if not db.session.get(Setting, "system"):
            db.session.add(Setting(key="system", value=DEFAULTS))
        sync_prompts(db.session)
        from .stakeholders import seed_stakeholders

        seed_stakeholders(db.session)
        db.session.commit()
