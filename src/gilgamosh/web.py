import copy
import json
import secrets
import time
from urllib.parse import urlparse

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_login import login_required, login_user, logout_user
from pydantic import ValidationError
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash

from .connections import catalog, check_assignments, preflight
from .llm import MAX_OUTPUT_TOKENS, Gateway, parse_response
from .models import Actor, Call, Command, Endpoint, Event, Job, Prompt, Setting, Simulation, User, db, now
from .security import PublicError, cipher, local_check
from .services import PHASE_LABELS, ROLES, create_simulation, enqueue, event, import_actors, latest_prompts, settings
from .sources import statistics
from .stakeholders import DEFAULT_SELECTION, has_icon, icon_directory, initials

web = Blueprint("web", __name__)
MIN_OUTPUT_TOKENS, MAX_OUTPUT_LIMIT = 256, 1000000


@web.app_template_global()
def icon_url(actor_id):
    return url_for("web.actor_icon", actor_id=actor_id) if has_icon(actor_id) else None


web.add_app_template_global(initials)


@web.get("/icons/<actor_id>.png")
@login_required
def actor_icon(actor_id):
    if not has_icon(actor_id):
        abort(404)
    return send_from_directory(icon_directory(), f"{actor_id}.png", max_age=86400)


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise PublicError("JSON-Objekt erwartet.")
    return data


def sim_or_404(sim_id, lock=False):
    query = select(Simulation).where(Simulation.id == sim_id)
    if lock:
        query = query.with_for_update()
    sim = db.session.scalar(query)
    if not sim:
        abort(404)
    return sim


def public_event(e):
    return {
        "id": e.id,
        "run": e.run,
        "block": e.block,
        "round": e.round,
        "phase": e.phase,
        "kind": e.kind,
        "actor": e.actor,
        "group": e.group,
        "visibility": e.visibility,
        "payload": e.payload,
        "at": e.created_at.isoformat(),
    }


def endpoint_data(e):
    return {
        "id": e.id,
        "name": e.name,
        "base_url": e.base_url,
        "model": e.model,
        "enabled": e.enabled,
        "timeout": e.timeout,
        "api_key_configured": bool(e.secret),
    }


@web.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        # Session-bound throttling supplements the localhost-only prototype deployment.
        last = session.get("login_failed", 0)
        user = db.session.scalar(select(User).where(User.username == request.form.get("username", "")))
        if time.time() - last < 2:
            error = "Bitte kurz warten und erneut versuchen."
        elif user and check_password_hash(user.password_hash, request.form.get("password", "")):
            session.clear()
            login_user(user)
            session.permanent = True
            return redirect(url_for("web.index"))
        else:
            session["login_failed"] = time.time()
            error = "Anmeldung fehlgeschlagen."
    return render_template("login.html", error=error)


@web.post("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("web.login"))


@web.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        default_selection=DEFAULT_SELECTION,
        sources=statistics(db.session),
        actors=db.session.scalars(select(Actor).order_by(Actor.name)).all(),
        simulations=db.session.scalars(select(Simulation).order_by(Simulation.created_at.desc())).all(),
    )


@web.get("/admin")
@login_required
def admin():
    return render_template(
        "admin.html",
        actors=db.session.scalars(select(Actor).order_by(Actor.name)).all(),
        endpoints=db.session.scalars(select(Endpoint).order_by(Endpoint.name)).all(),
        system=settings(db.session),
        prompts=latest_prompts(db.session),
        sources=statistics(db.session),
    )


@web.get("/simulations/<sim_id>")
@login_required
def simulation_page(sim_id):
    sim = sim_or_404(sim_id)
    return render_template("simulation.html", sim=sim, phases=PHASE_LABELS)


@web.get("/api/admin/endpoints")
@login_required
def endpoints_get():
    return jsonify(items=[endpoint_data(e) for e in db.session.scalars(select(Endpoint).order_by(Endpoint.name))])


@web.post("/api/admin/endpoints")
@login_required
def endpoints_save():
    data = body()
    db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    endpoint = db.session.get(Endpoint, data.get("id")) if data.get("id") else Endpoint()
    if endpoint is None:
        abort(404)
    name, base_url = str(data.get("name", "")).strip(), str(data.get("base_url", "")).strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        not name
        or len(name) > 100
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise PublicError("Name und HTTPS-Basis-URL ohne Zugangsdaten oder Query erforderlich.")
    duplicate = db.session.scalar(select(Endpoint).where(func.lower(Endpoint.name) == name.lower()))
    if duplicate and duplicate.id != endpoint.id:
        raise PublicError("Dieser Verbindungsname ist bereits vergeben.")
    try:
        timeout = int(data.get("timeout", 120))
    except (TypeError, ValueError):
        raise PublicError("Timeout muss eine ganze Zahl sein.") from None
    if not 5 <= timeout <= 600:
        raise PublicError("Timeout muss zwischen 5 und 600 Sekunden liegen.")
    endpoint.name, endpoint.base_url, endpoint.model = name, base_url, str(data.get("model", "")).strip()
    endpoint.timeout, endpoint.enabled = timeout, bool(data.get("enabled", True))
    if data.get("api_key"):
        endpoint.secret = cipher(current_app).encrypt(str(data["api_key"]).encode()).decode()
    elif data.get("clear_key"):
        endpoint.secret = ""
    db.session.add(endpoint)
    db.session.commit()
    return jsonify(endpoint_data(endpoint))


@web.delete("/api/admin/endpoints/<endpoint_id>")
@login_required
def endpoint_delete(endpoint_id):
    db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    system = settings(db.session)
    if system["endpoint_id"] == endpoint_id or any(
        v.get("endpoint_id") == endpoint_id for v in system["roles"].values()
    ):
        raise PublicError("Die Verbindung ist noch als Default oder einer Rolle zugewiesen.")
    if db.session.scalar(select(Actor).where(Actor.endpoint_id == endpoint_id)):
        raise PublicError("Die Verbindung ist noch einer Institution zugewiesen.")
    for sim in db.session.scalars(select(Simulation)):
        if any(
            e.get("id") == endpoint_id
            for e in list(sim.snapshot["roles"].values()) + [a["endpoint"] for a in sim.snapshot["actors"].values()]
        ):
            raise PublicError(
                "Die Verbindung wird von gespeicherten Simulationen referenziert. Deaktivieren ist möglich."
            )
    endpoint = db.session.get(Endpoint, endpoint_id)
    if not endpoint:
        abort(404)
    db.session.delete(endpoint)
    db.session.commit()
    return "", 204


@web.get("/api/admin/connections/check")
@login_required
def connections_check():
    if current_app.config["LLM_MODE"] == "fake":
        return jsonify(mode="fake", items=[])
    return jsonify(mode="openrouter", items=check_assignments(db.session, current_app, settings(db.session)))


@web.get("/api/admin/connections/models")
@login_required
def connections_models():
    if current_app.config["LLM_MODE"] == "fake":
        return jsonify(ids=[])
    endpoint = db.session.get(Endpoint, settings(db.session)["endpoint_id"] or "")
    models = catalog(endpoint.base_url if endpoint else "https://openrouter.ai/api/v1")
    return jsonify(ids=sorted(models) if models else [])


@web.post("/api/admin/settings")
@login_required
def settings_save():
    data = body()
    row = db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    try:
        parallelism = int(data.get("parallelism", 4))
    except (TypeError, ValueError):
        raise PublicError("Parallelität muss eine ganze Zahl sein.") from None
    if not 1 <= parallelism <= 16:
        raise PublicError("Parallelität muss zwischen 1 und 16 liegen.")
    roles = data.get("roles", {})
    if not isinstance(roles, dict) or set(roles) - {"recherche", "moderator", "orchestrator", "watcher"}:
        raise PublicError("Unbekannte Rollen.")
    for assignment in [data, *roles.values()]:
        if not isinstance(assignment, dict):
            raise PublicError("Ungültige Rollenzuweisung.")
        if assignment.get("endpoint_id") and not db.session.get(Endpoint, assignment["endpoint_id"]):
            raise PublicError("Unbekannte Verbindung.")
    blocked = data.get("blocked_names", [])
    if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
        raise PublicError("Die Sperrliste muss eine Liste von Zeichenketten sein.")
    try:
        eur_per_usd = float(data.get("eur_per_usd", settings(db.session)["eur_per_usd"]))
    except (TypeError, ValueError):
        raise PublicError("Der Wechselkurs muss eine Zahl sein.") from None
    if not 0.1 <= eur_per_usd <= 10:
        raise PublicError("Der Wechselkurs muss zwischen 0,1 und 10 liegen.")
    max_tokens = {**settings(db.session)["max_tokens"], **(data.get("max_tokens") or {})}
    try:
        max_tokens = {role: int(max_tokens[role]) for role in ROLES}
    except (TypeError, ValueError):
        raise PublicError("Maximale Output-Tokens müssen ganze Zahlen sein.") from None
    if not all(MIN_OUTPUT_TOKENS <= value <= MAX_OUTPUT_LIMIT for value in max_tokens.values()):
        raise PublicError(f"Maximale Output-Tokens müssen zwischen {MIN_OUTPUT_TOKENS} und {MAX_OUTPUT_LIMIT} liegen.")
    row.value = {
        "endpoint_id": data.get("endpoint_id") or None,
        "max_tokens": max_tokens,
        "parallelism": parallelism,
        "eur_per_usd": eur_per_usd,
        "roles": roles,
        "blocked_names": blocked,
    }
    db.session.commit()
    return jsonify(ok=True)


@web.post("/api/admin/endpoints/<endpoint_id>/test")
@login_required
def endpoint_test(endpoint_id):
    endpoint = db.session.get(Endpoint, endpoint_id)
    if not endpoint:
        abort(404)
    if not endpoint.enabled:
        raise PublicError("Verbindung ist deaktiviert.")
    if current_app.config["LLM_MODE"] == "fake":
        return jsonify(ok=True, text="Offline-Demo: Es wurde kein Anbieter aufgerufen, die Verbindung ist ungeprüft.")
    # A deliberate administrative probe; simulation calls only execute in the worker.
    config = {**endpoint_data(endpoint), "secret": endpoint.secret, "timeout": min(endpoint.timeout, 30), "attempts": 1}
    request_data = {
        "messages": [
            {"role": "system", "content": "Prüfe den neutralen Text im Schema. Keine Personen."},
            {"role": "user", "content": json.dumps({"pruefling": "Institutionelle Verhandlung."})},
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    response = Gateway(current_app).complete(config, request_data, "sicherheit")
    parse_response(response, "sicherheit", [])
    return jsonify(ok=True, text="Verbindung und strukturierte Antwort erfolgreich geprüft.")


@web.get("/api/admin/actors")
@login_required
def actors_get():
    return jsonify(
        items=[
            {
                "id": a.id,
                "name": a.name,
                "parent_id": a.parent_id,
                **a.profile,
                "endpoint_id": a.endpoint_id,
                "model": a.model,
            }
            for a in db.session.scalars(select(Actor).order_by(Actor.name))
        ]
    )


@web.post("/api/admin/actors")
@login_required
def actors_save():
    data = body()
    db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    try:
        import_actors(db.session, data.get("items", [data]))
    except (ValidationError, TypeError, ValueError):
        db.session.rollback()
        raise PublicError("Akteursdaten ungültig. JSON-Format und Pflichtfelder prüfen.") from None
    db.session.commit()
    return jsonify(ok=True)


@web.post("/api/admin/prompts")
@login_required
def prompts_save():
    data = body()
    db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    role, text = data.get("role"), str(data.get("text", "")).strip()
    if role not in ROLES or not 10 <= len(text) <= 50000:
        raise PublicError("Gültige Rolle und Prompt mit 10 bis 50.000 Zeichen erforderlich.")
    local_check(text, settings(db.session)["blocked_names"])
    latest = latest_prompts(db.session)[role]
    db.session.add(Prompt(role=role, version=latest.version + 1 if latest else 1, text=text))
    db.session.commit()
    return jsonify(ok=True)


@web.post("/api/simulations")
@login_required
def simulations_create():
    data = body()
    if isinstance(data.get("actors"), list):
        preflight(db.session, current_app, settings(db.session), [str(a) for a in data["actors"]])
    sim = create_simulation(db.session, data, current_app)
    db.session.commit()
    return jsonify(id=sim.id, url=url_for("web.simulation_page", sim_id=sim.id)), 201


@web.get("/api/simulations")
@login_required
def simulations_list():
    return jsonify(
        items=[
            {
                "id": s.id,
                "status": s.status,
                "question": s.question if s.state.get("verified") else "Eingaben werden geprüft.",
            }
            for s in db.session.scalars(select(Simulation).order_by(Simulation.created_at.desc()))
        ]
    )


def simulation_data(sim):
    events = db.session.scalars(
        select(Event)
        .where(
            Event.simulation_id == sim.id,
            Event.run == sim.run,
            Event.visibility == "public",
            Event.kind != "activity",
        )
        .order_by(Event.id)
    ).all()
    watcher = [e for e in events if e.kind == "watcher"]
    last_watcher = watcher[-1] if watcher else None
    last_error = next((e for e in reversed(events) if e.kind == "watcher_error"), None)
    jobs = db.session.scalars(select(Job).where(Job.simulation_id == sim.id, Job.status == "running")).all()
    alive = any(j.lease_until and j.lease_until > now() for j in jobs)
    calls = db.session.scalars(
        select(Call).where(
            Call.simulation_id == sim.id, Call.run == sim.run, Call.block == sim.block, Call.status == "running"
        )
    ).all()
    active_calls = []
    for c in calls:
        subject = c.actor
        context = json.loads(c.request["messages"][-1]["content"])
        if not subject and c.role == "orchestrator":
            subject = next(
                (
                    k
                    for k, a in sim.snapshot["actors"].items()
                    if a["name"] == (context.get("institution") or context.get("pruefling", {}).get("agent"))
                ),
                "",
            )
        active_calls.append(
            {"actor": subject, "role": c.role, "task": context.get("aufgabe"), "started": c.created_at.isoformat()}
        )
    return {
        "active_calls": active_calls,
        "id": sim.id,
        "question": sim.question if sim.state.get("verified") else "Institutionen und Eingaben werden geprüft …",
        "status": sim.status,
        "phase": sim.phase,
        "phase_label": PHASE_LABELS[sim.phase],
        "run": sim.run,
        "block": sim.block,
        "round": sim.round,
        "params": sim.snapshot["params"],
        "groups": sim.state["groups"],
        "votes": {k: v["stimme"] if v else "enthaltung" for k, v in sim.state.get("votes", {}).items()},
        "proposal": sim.state.get("proposal"),
        "outcome": sim.state.get("outcome"),
        "actors": {
            k: {
                "name": a["name"] if sim.state.get("verified") else "Institution wird geprüft",
                "icon": icon_url(k) if sim.state.get("verified") else None,
                "monogram": initials(a["name"]) if sim.state.get("verified") else "",
                "demo": a.get("demo", False),
                "modeled": a.get("modeled", True),
            }
            for k, a in sim.snapshot["actors"].items()
        },
        "events": [public_event(e) for e in events],
        "watcher": public_event(last_watcher) if last_watcher else None,
        "watcher_updating": any(j.kind in {"watcher", "report"} for j in jobs),
        "watcher_error": last_error.payload
        if last_error and (not last_watcher or last_error.id > last_watcher.id)
        else None,
        "worker_alive": alive,
        "error": sim.error,
        "mode": sim.snapshot["mode"],
        "created_at": sim.created_at.isoformat(),
    }


@web.get("/api/simulations/<sim_id>")
@login_required
def simulation_get(sim_id):
    return jsonify(simulation_data(sim_or_404(sim_id)))


@web.post("/api/simulations/<sim_id>/control")
@login_required
def control(sim_id):
    data = body()
    action, key = data.get("action"), data.get("key")
    if (
        action not in {"pause", "resume", "continue", "reset", "end", "retry_report"}
        or not isinstance(key, str)
        or not 8 <= len(key) <= 100
    ):
        raise PublicError("Gültiger Steuerbefehl und Idempotenzkennung erforderlich.")
    sim = sim_or_404(sim_id, lock=True)
    previous = db.session.scalar(select(Command).where(Command.simulation_id == sim.id, Command.key == key))
    if previous:
        if previous.action != action:
            raise PublicError("Kennung gehört bereits zu einem anderen Befehl.")
        return jsonify(ok=True)
    if sim.status == "awaiting_decision" and action in {"continue", "reset", "end"}:
        ready = db.session.scalar(
            select(Event.id)
            .where(
                Event.simulation_id == sim.id, Event.run == sim.run, Event.block == sim.block, Event.kind == "report"
            )
            .limit(1)
        )
        if not ready:
            raise PublicError("Bitte den Abschlussbericht abwarten oder unter Auswertung erneut anfordern.")
    if action == "pause":
        if sim.status != "running":
            raise PublicError("Nur laufende Simulationen können pausiert werden.")
        sim.status = "paused"
    elif action == "resume":
        if sim.status != "paused":
            raise PublicError("Nur pausierte Simulationen können fortgesetzt werden.")
        sim.status, sim.error = "running", ""
        jobs = db.session.scalars(
            select(Job).where(
                Job.simulation_id == sim.id, Job.kind == "phase", Job.status.in_(["queued", "running", "failed"])
            )
        ).all()
        for job in jobs:
            if job.status == "failed":
                job.status = "queued"
        if not jobs:
            enqueue(db.session, sim)
    elif action in {"continue", "reset"}:
        if sim.status != "awaiting_decision":
            raise PublicError("Diese Entscheidung ist erst nach einem Teilkompromiss möglich.")
        if action == "continue":
            sim.block += 1
            sim.round, sim.phase = 1, "individual"
        else:
            sim.run, sim.block, sim.round, sim.phase = sim.run + 1, 1, 1, "start"
            sim.state = {"groups": {a: [a] for a in sim.snapshot["actors"]}, "verified": False}
            snap = copy.deepcopy(sim.snapshot)
            snap["seed"] = secrets.randbits(32)
            sim.snapshot = snap
        for job in db.session.scalars(
            select(Job).where(Job.simulation_id == sim.id, Job.status.in_(["queued", "running"]))
        ):
            job.status, job.token = "superseded", ""
        sim.status, sim.error = "running", ""
        enqueue(db.session, sim)
    elif action == "retry_report":
        if sim.phase != "done" or sim.status == "ended":
            raise PublicError("Noch kein Abschlussbericht verfügbar.")
        if db.session.scalar(
            select(Job).where(Job.simulation_id == sim.id, Job.kind == "report", Job.status.in_(["queued", "running"]))
        ):
            raise PublicError("Bericht wird bereits erstellt.")
        enqueue(db.session, sim, "report", {"run": sim.run, "block": sim.block})
    elif action == "end":
        if sim.status == "ended":
            raise PublicError("Simulation ist bereits beendet.")
        sim.status = "ended"
        for job in db.session.scalars(
            select(Job).where(Job.simulation_id == sim.id, Job.status.in_(["queued", "running"]))
        ):
            job.status, job.token = "superseded", ""
    db.session.add(Command(simulation_id=sim.id, key=key, action=action))
    event(db.session, sim, "status", {"status": sim.status, "action": action})
    db.session.commit()
    return jsonify(ok=True)


@web.get("/api/simulations/<sim_id>/events")
@login_required
def events_stream(sim_id):
    sim_or_404(sim_id)
    try:
        after = max(0, int(request.headers.get("Last-Event-ID", request.args.get("after", 0))))
    except ValueError:
        raise PublicError("Ungültige Ereigniskennung.") from None
    factory = sessionmaker(db.engine)
    db.session.remove()

    def generate():
        cursor = after
        for _ in range(25):  # reconnect periodically; no long-held DB connection
            with factory() as s:
                rows = s.scalars(
                    select(Event)
                    .where(Event.simulation_id == sim_id, Event.id > cursor, Event.visibility == "public")
                    .order_by(Event.id)
                    .limit(200)
                ).all()
                frames = []
                for row in rows:
                    cursor = row.id
                    frames.append(f"id: {row.id}\ndata: {json.dumps(public_event(row), ensure_ascii=False)}\n\n")
            yield "".join(frames) if frames else ": heartbeat\n\n"
            time.sleep(1)

    return Response(generate(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})


@web.get("/api/simulations/<sim_id>/groups")
@login_required
def groups_get(sim_id):
    sim = sim_or_404(sim_id)
    rows = db.session.scalars(
        select(Event)
        .where(Event.simulation_id == sim.id, Event.run == sim.run, Event.kind.in_(["group_message", "membership"]))
        .order_by(Event.id)
    ).all()
    return jsonify(groups=sim.state["groups"], events=[public_event(e) for e in rows])


@web.get("/api/simulations/<sim_id>/trajectories")
@login_required
def trajectories_get(sim_id):
    sim = sim_or_404(sim_id)
    query = select(Call).where(Call.simulation_id == sim.id)
    if request.args.get("actor"):
        query = query.where(Call.actor == request.args["actor"])
    rows = db.session.scalars(query.order_by(Call.created_at)).all()
    return jsonify(
        items=[
            {
                "id": c.id,
                "run": c.run,
                "block": c.block,
                "round": c.round,
                "phase": c.phase,
                "role": c.role,
                "actor": c.actor,
                "group": c.group,
                "status": c.status,
                "request": c.request,
                "response": c.response,
                "result": c.result,
                "usage": c.usage,
                "duration": c.duration,
            }
            for c in rows
        ]
    )


@web.get("/api/simulations/<sim_id>/reports")
@login_required
def reports_get(sim_id):
    sim = sim_or_404(sim_id)
    rows = db.session.scalars(
        select(Event).where(Event.simulation_id == sim.id, Event.kind.in_(["closing", "report"])).order_by(Event.id)
    ).all()
    return jsonify(items=[public_event(e) for e in rows])


@web.get("/api/simulations/<sim_id>/export")
@login_required
def export(sim_id):
    sim = sim_or_404(sim_id)
    rows = db.session.scalars(
        select(Event)
        .where(
            Event.simulation_id == sim.id,
            Event.visibility == "public",
            Event.kind.in_(
                ["question", "statement", "vote", "ballot", "proposal", "closing", "watcher", "report", "membership"]
            ),
        )
        .order_by(Event.id)
    ).all()
    data = [public_event(e) for e in rows]
    fmt = request.args.get("format", "json")
    if fmt == "md":
        content = "# Institutionelle Verhandlung\n\n" + "\n\n".join(
            f"## Durchlauf {e['run']}, Block {e['block']}, Runde {e['round']}: {e['kind']}\n\n"
            + "```json\n"
            + json.dumps(e["payload"], ensure_ascii=False, indent=2)
            + "\n```"
            for e in data
        )
        mime = "text/markdown"
    elif fmt == "json":
        content, mime = json.dumps(data, ensure_ascii=False, indent=2), "application/json"
    else:
        raise PublicError("Exportformat muss json oder md sein.")
    return Response(
        content, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="simulation-{sim.id}.{fmt}"'}
    )


@web.get("/api/simulations/<sim_id>/metrics")
@login_required
def metrics_get(sim_id):
    sim = sim_or_404(sim_id)
    calls = db.session.execute(
        select(Call.role, Call.usage, Call.duration).where(Call.simulation_id == sim.id, Call.run == sim.run)
    ).all()
    largest = db.session.scalar(
        select(func.max(func.length(cast(Call.request, String)))).where(
            Call.simulation_id == sim.id, Call.run == sim.run
        )
    )
    rate = settings(db.session)["eur_per_usd"]
    roles = {}
    known_cost, cost_calls = 0.0, 0
    for role, call_usage, duration in calls:
        row = roles.setdefault(role, {"calls": 0, "tokens": 0, "duration": 0, "cost_eur": 0.0})
        row["calls"] += 1
        row["tokens"] += call_usage.get(
            "total_tokens", call_usage.get("prompt_tokens", 0) + call_usage.get("completion_tokens", 0)
        )
        row["duration"] += duration
        cost = call_usage.get("cost")
        if isinstance(cost, (float, int)):
            row["cost_eur"] += cost * rate
            known_cost += cost
            cost_calls += 1
    reviews = db.session.scalars(
        select(Event).where(
            Event.simulation_id == sim.id, Event.run == sim.run, Event.kind.in_(["review", "abstention"])
        )
    ).all()
    return jsonify(
        roles=roles,
        known_cost_eur=known_cost * rate if cost_calls else None,
        eur_per_usd=rate,
        cost_calls=cost_calls,
        calls=len(calls),
        largest_context=largest or 0,
        context_limit=current_app.config["MAX_CONTEXT_CHARS"],
        rejections=sum(
            e.kind == "review" and e.payload.get("zurueckgewiesen", e.payload.get("pruefung", {}).get("bewertung") == 0)
            for e in reviews
        ),
        abstentions=sum(e.kind == "abstention" for e in reviews),
    )


@web.post("/api/admin/actors/seed")
@login_required
def actors_seed():
    from .stakeholders import seed_stakeholders

    db.session.scalar(select(Setting).where(Setting.key == "system").with_for_update())
    imported, preserved = seed_stakeholders(db.session)
    db.session.commit()
    return jsonify(ok=True, imported=len(imported), preserved=len(preserved))


@web.get("/api/simulations/<sim_id>/protocol/<int:event_id>")
@login_required
def protocol_event(sim_id, event_id):
    sim_or_404(sim_id)
    row = db.session.scalar(select(Event).where(Event.simulation_id == sim_id, Event.id == event_id))
    if not row:
        abort(404)
    return jsonify(public_event(row))
