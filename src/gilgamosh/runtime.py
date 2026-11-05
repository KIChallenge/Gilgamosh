"""Durable phase execution. Calls are cached; the job lease fences every write."""

import copy
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .llm import Gateway, parse_response
from .models import Call, Endpoint, Event, Job, Simulation, now
from .schemas import SCHEMAS
from .security import INSTITUTION_RULE, ContentBlocked, InvalidOutput, PublicError, local_check
from .services import enqueue, event


class Stopped(Exception):
    pass


class Runtime:
    def __init__(self, app, factory, job_id, token):
        self.app, self.factory, self.job_id, self.token = app, factory, job_id, token
        with factory() as s:
            job = s.get(Job, job_id)
            sim = s.get(Simulation, job.simulation_id)
            self.sim_id = sim.id
            self.run, self.block, self.round, self.phase = sim.run, sim.block, sim.round, sim.phase
            self.snap = copy.deepcopy(sim.snapshot)
            self.state = copy.deepcopy(sim.state)
            self.question = sim.question
            self.kind, self.job_payload = job.kind, job.payload
            from sqlalchemy import func

            self.context_cutoff = s.scalar(select(func.max(Event.id)).where(Event.simulation_id == sim.id)) or 0
        self.actors = self.snap["actors"]
        self.params = self.snap["params"]
        self.gateway = Gateway(app, self.snap["mode"])

    def locked(self, session, allow_paused=False):
        # Consistent lock order: simulation, then job (also used by controls/claim).
        sim = session.scalar(select(Simulation).where(Simulation.id == self.sim_id).with_for_update())
        job = session.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
        if not job or job.status != "running" or job.token != self.token or job.lease_until <= now():
            raise Stopped()
        if sim.run != self.run or sim.block != self.block or sim.status == "ended":
            raise Stopped()
        if sim.status != "running" and not allow_paused and self.kind == "phase":
            raise Stopped()
        return sim, job

    def key(self, suffix):
        return f"{self.run}:{self.block}:{self.round}:{self.phase}:{suffix}"

    def emit(self, kind, payload, actor="", group="", visibility="public", key=None):
        with self.factory.begin() as s:
            sim, _ = self.locked(s, allow_paused=True)
            if key and s.scalar(select(Event).where(Event.simulation_id == sim.id, Event.key == key)):
                return
            row = event(s, sim, kind, payload, actor, group, visibility)
            row.key = key
            # Events must retain the executing phase when another job has advanced it.
            row.round, row.phase = self.round, self.phase

    def context(self, role, actor="", group=""):
        with self.factory() as s:
            events = s.scalars(
                select(Event).where(Event.simulation_id == self.sim_id, Event.run == self.run).order_by(Event.id)
            ).all()
        visible = []
        for e in events:
            if role == "agent" and e.visibility == "public" and e.id > self.context_cutoff:
                continue
            if self.kind == "watcher" and e.id > self.job_payload["cutoff"]:
                continue
            if e.kind in {"activity", "watcher", "report", "watcher_error", "status", "error"}:
                continue
            if role in {"watcher", "orchestrator"} or e.visibility == "public":
                allowed = True
            elif role == "agent" and e.visibility == "group" and group and e.group == group:
                # Membership at the time of sending: new members never see old private conversations.
                allowed = actor in e.payload.get("members", [])
            elif role == "agent" and e.kind == "feedback" and e.actor == actor:
                allowed = True
            else:
                allowed = False
            if allowed:
                visible.append(
                    {
                        "id": e.id,
                        "runde": e.round,
                        "block": e.block,
                        "phase": e.phase,
                        "typ": e.kind,
                        "institution": self.actors.get(e.actor, {}).get("name", e.actor),
                        "payload": e.payload,
                    }
                )
        ctx = {
            "frage": self.state.get("question", {}).get("frage", self.question),
            "runde": self.round,
            "phase": self.phase,
            "N": self.params["rounds"],
            "gruppen": self.state["groups"],
            "institutionen": [a["name"] for a in self.actors.values()],
            "institutions_ids": {key: a["name"] for key, a in self.actors.items()},
            "verlauf": visible,
            "vorschlag": self.state.get("proposal"),
            "ergebnis_status": self.state.get("outcome", "konsens"),
        }
        if role == "agent":
            a = self.actors[actor]
            ctx.update(
                institution=a["name"],
                eigene_statuten={"rote_linien": a["rote_linien"], "verhandelbar": a["verhandelbar"]},
                db_infos=a["db_infos"],
                gruppe=group,
            )
        if role in {"orchestrator", "watcher"}:
            ctx["institutionen_profile"] = {
                key: {k: v for k, v in a.items() if k != "endpoint"} for key, a in self.actors.items()
            }
            ctx["event_ids"] = [e["id"] for e in visible]
        if role == "watcher" and self.kind == "watcher":
            proposals = [e["payload"] for e in visible if e["typ"] == "proposal"]
            ctx["vorschlag"] = proposals[-1] if proposals else None
        if role == "watcher":
            ids = [
                e.payload.get("call_id")
                for e in events
                if e.kind == "activity"
                and e.payload.get("call_id")
                and (self.kind != "watcher" or e.id <= self.job_payload["cutoff"])
            ]
            with self.factory() as s:
                calls = s.scalars(select(Call).where(Call.id.in_(ids))).all() if ids else []
                ctx["llm_protokoll"] = [
                    {
                        "rolle": c.role,
                        "institution": self.actors.get(c.actor, {}).get("name", ""),
                        "runde": c.round,
                        "phase": c.phase,
                        "status": c.status,
                        "antwort": c.response,
                    }
                    for c in calls
                ]
        return ctx

    def prompt(self, role, actor=""):
        a = self.actors.get(actor, {})
        values = {
            "AGENT_NAME": a.get("name", ""),
            "INTERESSENSSAEULE": a.get("name", ""),
            "ROTE_LINIEN": json.dumps(a.get("rote_linien", []), ensure_ascii=False),
            "VERHANDELBARE_POSITIONEN": json.dumps(a.get("verhandelbar", []), ensure_ascii=False),
            "STATUTEN_ALLER_AGENTS": json.dumps(
                {
                    k: {"institution": v["name"], "rote_linien": v["rote_linien"], "verhandelbar": v["verhandelbar"]}
                    for k, v in self.actors.items()
                }
                if role in {"orchestrator", "watcher"}
                else {},
                ensure_ascii=False,
            ),
            "N": str(self.params["rounds"]),
            "RUNDE": str(self.round),
            "MAX_RETRY": str(self.params["attempts"]),
            "MAX_NACHRICHTEN": str(self.params["messages"]),
        }
        text = self.snap["prompts"][role]
        for key, value in values.items():
            text = re.sub(r"\{\{\s*" + key + r"\s*\}\}", lambda _, replacement=value: replacement, text)
        if role == "agent":
            text += "\n" + a["system_prompt"]
        return (
            text
            + "\n"
            + INSTITUTION_RULE
            + (
                "\nVerbindliche Runtime-Regeln: partner enthält Institutionskennungen aus institutions_ids. "
                'zustimmung beantwortet eine Gruppeneinladung mit {"zustimmung": true|false}. '
                "gruppennachricht und gruppenstatement sind getrennte Aufgaben. "
                "Bei vorhandenem vorschlag gilt tragbar=true|false auch in Runde 1 eines neuen Blocks. "
                "Bei einer bestätigten roten Linie nenne in verletzte_rote_linie deren exakte Kennung R1 usw. oder den Wortlaut. "
                "Das angeforderte JSON-Schema und die übergebenen Limits haben Vorrang vor Beispielen."
            )
        )

    def call(self, role, task, suffix, actor="", group="", extra=None):
        ctx = self.context(role, actor, group)
        ctx.update(extra or {})
        ctx["aufgabe"] = task
        ctx["phase"] = task
        config = self.actors[actor]["endpoint"] if role == "agent" else self.snap["roles"][role]
        for repair in range(2):
            key = self.key(f"{suffix}:repair{repair}")
            if repair:
                ctx["formatfehler"] = (
                    "Vorige Ausgabe ungültig oder unzulässiger Personenbezug. Erneut strikt im Schema antworten."
                )
            request = {
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": self.prompt(role, actor)},
                    {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
                ],
                "max_tokens": 6000,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": task, "strict": True, "schema": SCHEMAS[task].model_json_schema()},
                },
            }
            if sum(len(m["content"]) for m in request["messages"]) > self.app.config.get("MAX_CONTEXT_CHARS", 240000):
                raise PublicError("Kontextbudget überschritten. Größeres Modell oder kürzeren neuen Lauf verwenden.")
            cached = None
            with self.factory.begin() as s:
                sim, _ = self.locked(s)
                if self.snap["mode"] != "fake":
                    endpoint = s.get(Endpoint, config["id"])
                    if not endpoint or not endpoint.enabled:
                        raise PublicError("Zugewiesene Verbindung wurde deaktiviert. Bitte Administration prüfen.")
                old = s.scalar(select(Call).where(Call.simulation_id == self.sim_id, Call.key == key))
                if old and old.status in {"completed", "invalid"}:
                    cached = old.response
                else:
                    if not old:
                        old = Call(
                            simulation_id=self.sim_id,
                            key=key,
                            run=self.run,
                            block=self.block,
                            round=self.round,
                            phase=self.phase,
                            role=role,
                            actor=actor,
                            group=group,
                            request=request,
                        )
                        s.add(old)
                    old.status = "waiting"
                    old.request = request
                    subject = actor or next(
                        (k for k, v in self.actors.items() if v["name"] == (extra or {}).get("institution")), ""
                    )
                    event(s, sim, "activity", {"role": role, "task": task, "status": "waiting"}, subject, group)
            if cached is None:
                started = time.monotonic()

                def invoke(key=key, subject=subject, request=request):
                    with self.factory.begin() as s:
                        sim, _ = self.locked(s)
                        row = s.scalar(select(Call).where(Call.simulation_id == self.sim_id, Call.key == key))
                        row.status = "running"
                        event(s, sim, "activity", {"role": role, "task": task, "status": "running"}, subject, group)
                    return self.gateway.complete(config, request, task)

                try:
                    slots = self.app.extensions.get("llm_slots")
                    if slots:
                        with slots:
                            response = invoke()
                    else:
                        response = invoke()
                except Exception:
                    with self.factory.begin() as s:
                        self.locked(s, allow_paused=True)
                        row = s.scalar(select(Call).where(Call.simulation_id == self.sim_id, Call.key == key))
                        row.status = "failed"
                    raise
                duration = time.monotonic() - started
            else:
                response, duration = cached, 0
            try:
                result = parse_response(response, task, self.snap["blocked_names"])
                valid = True
            except InvalidOutput:
                result, valid = {}, False
            if cached is None:
                with self.factory.begin() as s:
                    sim, _ = self.locked(s, allow_paused=True)
                    row = s.scalar(select(Call).where(Call.simulation_id == self.sim_id, Call.key == key))
                    row.response, row.result, row.status = response, result, "completed" if valid else "invalid"
                    row.duration, row.usage = duration, response.get("usage", {})
                    event(
                        s,
                        sim,
                        "activity",
                        {"role": role, "task": task, "status": "completed" if valid else "invalid", "call_id": row.id},
                        actor,
                        group,
                    )
            if valid:
                return result
        raise InvalidOutput("Auch die korrigierte Ausgabe war ungültig.")

    def safety(self, value, suffix, input_check=False):
        try:
            local_check(value, self.snap["blocked_names"])
        except InvalidOutput:
            raise ContentBlocked("Personenbezug erkannt.") from None
        result = self.call(
            "orchestrator",
            "sicherheit",
            suffix,
            extra={
                "pruefling": value,
                "eingabepruefung": input_check,
                "anweisung": (
                    "Prüfe alle Texte auf Einzelpersonen, Personennamen, Zitate oder persönliche Zuschreibungen. "
                    "Akteure müssen Institutionen/Interessenvertretungen sein. "
                    "Bei Eingabeprüfung dürfen eigene Statuten enthalten sein; sonst dürfen keine vertraulichen "
                    "Statuten, roten Linien oder Prioritäten offengelegt werden. "
                    "Die institutionen_profile und der Verlauf dienen nur als Prüfkontext, nicht als Prüfling."
                ),
            },
        )
        if (
            not result["institutionell"]
            or not result["keine_personen"]
            or (not input_check and not result["keine_vertraulichen_statuten"])
        ):
            raise ContentBlocked("Inhalt wegen Personenbezug oder vertraulicher Statuten blockiert.")

    def approved(self, task, actor, suffix, group="", extra=None):
        feedback = None
        for attempt in range(1, self.params["attempts"] + 1):
            try:
                value = self.call(
                    "agent",
                    task,
                    f"{suffix}:{actor}:v{attempt}",
                    actor,
                    group,
                    {**(extra or {}), "hinweis_an_agent": feedback},
                )
                if value.get("agent") != self.actors[actor]["name"]:
                    raise InvalidOutput("Unzulässige Sprecheridentität.")
                if task == "einzelphase" and value["tragbar"] is None and self.state.get("proposal"):
                    raise InvalidOutput("Tragbarkeit fehlt.")
                if task == "einzelphase" and not self.state.get("proposal") and value["tragbar"] is not None:
                    raise InvalidOutput("Tragbarkeit muss in der ersten Runde leer sein.")
                try:
                    review = self.call(
                        "orchestrator",
                        "statement_pruefen",
                        f"{suffix}:{actor}:review{attempt}",
                        extra={
                            "institution": self.actors[actor]["name"],
                            "statement": value,
                            "versuch": attempt,
                            "konkreter_vorschlag": self.state.get("proposal"),
                        },
                    )
                except InvalidOutput:
                    raise PublicError("Orchestrator-Prüfung ungültig. Verhandlung wurde angehalten.") from None
                if review["agent"] != self.actors[actor]["name"] or review["versuch"] != attempt:
                    raise PublicError("Orchestrator-Prüfung konnte nicht eindeutig zugeordnet werden.")
                self.emit(
                    "review",
                    {"statement": value, "pruefung": review},
                    actor,
                    group,
                    "private",
                    self.key(f"{suffix}:{actor}:review{attempt}"),
                )
                rejected = review["bewertung"] == 0
                if task == "abstimmung" and value["stimme"] == "ablehnen":
                    line = review["verletzte_rote_linie"] or ""
                    known = self.actors[actor]["rote_linien"]
                    identifiers = re.findall(r"\bR\d+\b", line)
                    confirmed = line in known or any(
                        rule.startswith(identifier + ":") for rule in known for identifier in identifiers
                    )
                    if not confirmed:
                        rejected = True
                if not rejected:
                    self.safety(value, f"{suffix}:{actor}:safe{attempt}")
                    self.emit(
                        "statement" if task != "abstimmung" else "vote",
                        value,
                        actor,
                        group,
                        key=self.key(f"{suffix}:{actor}:published"),
                    )
                    return value
                feedback = review.get("hinweis_an_agent") or "Bitte institutionelle Position überarbeiten."
                # Feedback itself can contain own statutes; only the affected institution sees it.
                self.emit(
                    "feedback",
                    {"hinweis_an_agent": feedback},
                    actor,
                    visibility="private",
                    key=self.key(f"{suffix}:{actor}:feedback{attempt}"),
                )
            except ContentBlocked:
                feedback = "Nicht zur Veröffentlichung geeignet. Keine Personen oder vertraulichen Statuten nennen."
            except InvalidOutput:
                break
        self.emit(
            "abstention",
            {"institution": self.actors[actor]["name"], "grund": "Versuchsgrenze erreicht."},
            actor,
            key=self.key(f"{suffix}:{actor}:abstention"),
        )
        return None

    def parallel(self, fn):
        ids = list(self.actors)
        random.Random(self.snap["seed"] + self.round).shuffle(ids)
        with ThreadPoolExecutor(max_workers=self.snap["parallelism"]) as executor:
            return dict(zip(ids, executor.map(fn, ids)))

    def checked_role(self, role, task, suffix, extra=None):
        for attempt in range(2):
            value = self.call(
                role,
                task,
                f"{suffix}:{attempt}",
                extra={**(extra or {}), "publikationshinweis": "Keine Personen oder vertraulichen Statuten nennen."},
            )
            try:
                self.safety(value, f"{suffix}:safety:{attempt}")
                return value
            except InvalidOutput:
                pass
        raise PublicError("Ausgabe der Steuerrolle konnte nicht freigegeben werden.")

    def coalition(self):
        def proposal(a):
            try:
                return self.call(
                    "agent",
                    "koalitionsphase",
                    f"coalition:{a}",
                    a,
                    extra={
                        "anweisung": "partner enthält ausschließlich institutions_ids. ziel_gruppe ist eine vorhandene Gruppen-ID."
                    },
                )
            except InvalidOutput:
                return {"aktion": "bleiben"}

        proposals = self.parallel(proposal)
        groups = copy.deepcopy(self.state["groups"])
        changed = set()
        for actor in self.actors:
            if actor in changed:
                continue
            p = proposals[actor]
            current = next(g for g, members in groups.items() if actor in members)
            if p["aktion"] == "austreten":
                groups[current].remove(actor)
                if not groups[current]:
                    del groups[current]
                groups[f"solo:{actor}"] = [actor]
                changed.add(actor)
                continue
            if p["aktion"] not in {"gruenden", "beitreten"}:
                continue
            target = p["ziel_gruppe"]
            if p["aktion"] == "beitreten" and target not in groups:
                continue
            members = sorted(set(([actor] + p["partner"]) if p["aktion"] == "gruenden" else groups[target] + [actor]))
            if len(members) < 2 or any(m not in self.actors or m in changed for m in members):
                continue
            gid = self.key(f"group:{actor}")
            accepted = True
            for member in members:
                try:
                    consent = self.call(
                        "agent",
                        "zustimmung",
                        f"consent:{actor}:{member}",
                        member,
                        extra={
                            "vorgeschlagene_mitglieder": {m: self.actors[m]["name"] for m in members},
                            "anweisung": "Stimme dem Gruppenbeitritt ausdrücklich zu oder lehne ihn ab.",
                        },
                    )
                except InvalidOutput:
                    consent = {"zustimmung": False}
                accepted = accepted and consent["zustimmung"]
                self.emit(
                    "consent",
                    {"mitglieder": members, **consent},
                    member,
                    visibility="private",
                    key=self.key(f"consent:{actor}:{member}"),
                )
            if not accepted:
                continue
            check = self.call(
                "orchestrator", "gruppe_pruefen", f"group-review:{actor}", extra={"gruppe": gid, "mitglieder": members}
            )
            self.emit("group_review", check, visibility="private", key=self.key(f"group-review:{actor}"))
            if check["entscheidung"] != "freigegeben":
                continue
            for member in members:
                for old in list(groups):
                    if member in groups[old]:
                        groups[old].remove(member)
                        if not groups[old]:
                            del groups[old]
            groups[gid] = members
            changed.update(members)
        self.state["groups"] = groups
        self.emit("membership", {"gruppen": groups}, key=self.key("membership"))

    def group_phase(self):
        def negotiate(item):
            group, members = item
            if len(members) > 1:
                for index in range(1, self.params["messages"] + 1):
                    for actor in members:
                        for attempt in range(1, self.params["attempts"] + 1):
                            try:
                                value = self.call(
                                    "agent",
                                    "gruppennachricht",
                                    f"message:{group}:{actor}:{index}:{attempt}",
                                    actor,
                                    group,
                                    {"nachricht_nr": index},
                                )
                                if (
                                    value["agent"] != self.actors[actor]["name"]
                                    or value["gruppe"] != group
                                    or value["nachricht_nr"] != index
                                ):
                                    raise InvalidOutput("Ungültige Gruppennachricht.")
                                self.safety(value, f"message:{group}:{actor}:{index}:{attempt}:safe")
                                self.emit(
                                    "group_message",
                                    {**value, "members": members},
                                    actor,
                                    group,
                                    "group",
                                    self.key(f"message:{group}:{actor}:{index}"),
                                )
                                break
                            except InvalidOutput:
                                continue
            for actor in members:
                self.approved("gruppenstatement", actor, "group-final", group)

        with ThreadPoolExecutor(max_workers=self.snap["parallelism"]) as executor:
            list(executor.map(negotiate, self.state["groups"].items()))

    def execute_phase(self):
        phase = self.phase
        next_phase = {
            "start": "question",
            "question": "individual",
            "individual": "coalition",
            "coalition": "groups",
            "groups": "proposal",
            "proposal": "vote",
            "vote": "closing",
            "closing": "done",
        }[phase]
        if phase == "start":
            self.safety(
                {
                    "frage": self.question,
                    "institutionen": {
                        k: {field: value for field, value in a.items() if field != "endpoint"}
                        for k, a in self.actors.items()
                    },
                },
                "input-safety",
                input_check=True,
            )
            self.state["verified"] = True
        elif phase == "question":
            value = self.checked_role("moderator", "frage_stellen", "question")
            self.state["question"] = value
            self.emit("question", value, key=self.key("question"))
        elif phase == "individual":
            self.parallel(lambda a: self.approved("einzelphase", a, "position"))
        elif phase == "coalition":
            self.coalition()
        elif phase == "groups":
            self.group_phase()
        elif phase == "proposal":
            value = self.checked_role("moderator", "sammelstand_und_vorschlag", "proposal")
            self.state["proposal"] = value
            self.emit("proposal", value, key=self.key("proposal"))
        elif phase == "vote":
            votes = self.parallel(lambda a: self.approved("abstimmung", a, "vote"))
            self.state["votes"] = votes
            consensus = not any(v and v["stimme"] == "ablehnen" for v in votes.values())
            self.state["outcome"] = "konsens" if consensus else "teilkompromiss"
            self.emit(
                "ballot",
                {"stimmen": {k: v["stimme"] if v else "enthaltung" for k, v in votes.items()}, "konsens": consensus},
                key=self.key("ballot"),
            )
            if not consensus and self.round < self.params["rounds"]:
                next_phase = "individual"
        elif phase == "closing":
            value = self.checked_role("moderator", "abschluss", "closing")
            value["status"] = self.state["outcome"]
            votes = self.state.get("votes", {})
            value["zustimmung"] = [self.actors[k]["name"] for k, v in votes.items() if v and v["stimme"] == "zustimmen"]
            value["tolerierung"] = [
                self.actors[k]["name"] for k, v in votes.items() if v and v["stimme"] == "tolerieren"
            ]
            value["ablehnung"] = [
                {"agent": self.actors[k]["name"], "grund": v["begruendung"]}
                for k, v in votes.items()
                if v and v["stimme"] == "ablehnen"
            ]
            self.emit("closing", value, key=self.key("closing"))
        with self.factory.begin() as s:
            sim, job = self.locked(s, allow_paused=True)
            sim.state = self.state
            sim.phase = next_phase
            if phase == "vote" and next_phase == "individual":
                sim.round += 1
            job.status, job.token = "done", ""
            if phase not in {"start", "closing"}:
                # Coalesce pending observations; already running snapshots remain independently versioned.
                for old in s.scalars(
                    select(Job).where(Job.simulation_id == sim.id, Job.kind == "watcher", Job.status == "queued")
                ):
                    old.status = "superseded"
                cutoff = (
                    s.scalar(
                        select(Event.id)
                        .where(Event.simulation_id == sim.id, Event.run == sim.run)
                        .order_by(Event.id.desc())
                        .limit(1)
                    )
                    or 0
                )
                enqueue(s, sim, "watcher", {"cutoff": cutoff, "run": sim.run, "block": sim.block})
            if next_phase == "done":
                sim.status = "completed" if self.state["outcome"] == "konsens" else "awaiting_decision"
                enqueue(s, sim, "report", {"run": sim.run, "block": sim.block})
            elif sim.status == "running":
                enqueue(s, sim)
            event(s, sim, "status", {"phase": sim.phase, "status": sim.status})

    def execute_observation(self):
        is_report = self.kind == "report"
        task = "abschlussbericht" if is_report else "zwischenstand"
        cutoff = self.job_payload.get("cutoff")
        # Distinct observation key: several phases may share the same current simulation phase.
        suffix = f"{task}:{cutoff or self.block}"
        value = self.checked_role(
            "watcher",
            task,
            suffix,
            {
                "bis_event_id": cutoff,
                "anweisung": "Für Zwischenstände ausschließlich Verlaufseinträge mit id <= bis_event_id verwenden. "
                "Jede fachliche Aussage mit referenzen belegen. Keine Statuten offenlegen. "
                "Verlauf vollständig berücksichtigen; keine erfundenen Quellen oder Auswirkungen.",
            },
        )
        if is_report:
            value["status"] = self.state["outcome"]
            with self.factory() as s:
                rows = s.scalars(
                    select(Event).where(Event.simulation_id == self.sim_id, Event.run == self.run).order_by(Event.id)
                ).all()
            membership = {a: a for a in self.actors}
            changes = 0
            for row in rows:
                if row.kind == "membership":
                    updated = {a: g for g, members in row.payload["gruppen"].items() for a in members}
                    changes += sum(membership.get(a) != g for a, g in updated.items())
                    membership = updated
            value["statistik"] = {
                "runden": len({(e.block, e.round) for e in rows}),
                "rueckweisungen": sum(
                    e.kind == "review" and e.payload.get("pruefung", {}).get("bewertung") == 0 for e in rows
                ),
                "enthaltungen": sum(e.kind == "abstention" for e in rows),
                "gruppenwechsel": changes,
            }
        if not is_report:
            with self.factory() as s:
                valid = set(
                    s.scalars(
                        select(Event.id).where(
                            Event.simulation_id == self.sim_id, Event.run == self.run, Event.id <= cutoff
                        )
                    )
                )
            if any(ref not in valid for ref in value["referenzen"]):
                raise PublicError("Watcher hat ungültige Nachrichtenreferenzen geliefert.")
        with self.factory.begin() as s:
            sim, job = self.locked(s, allow_paused=True)
            if is_report:
                if not s.scalar(select(Event.id).where(Event.simulation_id == sim.id, Event.key == self.key(suffix))):
                    row = event(s, sim, "report", value)
                    row.key = self.key(suffix)
            else:
                latest = s.scalars(
                    select(Event)
                    .where(Event.simulation_id == sim.id, Event.run == self.run, Event.kind == "watcher")
                    .order_by(Event.id.desc())
                ).first()
                if not latest or latest.payload.get("cutoff", 0) < cutoff:
                    event(s, sim, "watcher", {"cutoff": cutoff, "summary": value})
            job.status, job.token = "done", ""
