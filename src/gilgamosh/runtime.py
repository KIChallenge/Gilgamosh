"""Durable phase execution. Calls are cached; the job lease fences every write."""

import copy
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .llm import MAX_OUTPUT_TOKENS, Gateway, parse_response, truncated
from .models import Call, Endpoint, Event, Job, Simulation, now
from .schemas import SCHEMAS
from .security import INSTITUTION_RULE, ContentBlocked, InvalidOutput, PublicError, local_check
from .services import enqueue, event
from .sources import has_sources, search

# Bounded per-role contexts: full data stays in events and calls, only prompts are compact.
OWN_HISTORY_LIMIT = 9
BRIEF_DROP = {"agent", "members", "runde", "gruppe", "nachricht_nr"}
POSITION_FIELDS = ("position", "statement", "begruendung", "zugestaendnisse", "einigungen_in_gruppe")
CONTROL_KINDS = {"activity", "watcher", "report", "watcher_error", "status", "error"}
REPORT_KINDS = {"question", "proposal", "ballot", "membership", "abstention", "group_review", "closing"}
PRESS_HITS, LAW_HITS = 12, 5
STATUTES_POINTER = "Die für diese Aufgabe relevanten Statuten stehen im Feld `institutionen_profile` des Aufrufs."


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

    def run_events(self):
        with self.factory() as s:
            rows = s.scalars(
                select(Event).where(Event.simulation_id == self.sim_id, Event.run == self.run).order_by(Event.id)
            ).all()
        if self.kind == "watcher":
            rows = [e for e in rows if e.id <= self.job_payload["cutoff"]]
        return rows

    def name(self, actor):
        return self.actors.get(actor, {}).get("name", actor)

    def infos(self, actor):
        """Prepared data of one institution: manual entries plus its own research briefing."""
        return self.actors[actor]["db_infos"] + self.state.get("briefings", {}).get(actor, [])

    def statutes(self, scope=None, with_infos=False):
        """Only the statutes a control role needs for this one task, never the prompts around them."""
        ids = self.actors if scope is None else scope
        return {
            k: {
                "name": self.actors[k]["name"],
                "rote_linien": self.actors[k]["rote_linien"],
                "verhandelbar": self.actors[k]["verhandelbar"],
                **({"db_infos": self.infos(k)} if with_infos else {}),
            }
            for k in ids
        }

    def brief(self, e):
        payload = {k: v for k, v in e.payload.items() if k not in BRIEF_DROP}
        if e.kind == "review":
            review = e.payload.get("pruefung", {})
            rejected = e.payload.get("zurueckgewiesen", review.get("bewertung") == 0)
            payload = {
                "bewertung": review.get("bewertung"),
                "zurueckgewiesen": rejected,
                "verletzte_rote_linie": review.get("verletzte_rote_linie"),
                "hinweis_an_agent": review.get("hinweis_an_agent"),
            }
            # Approved statements are published as their own event; only rejected text is unique here.
            if rejected:
                payload["zurueckgewiesener_beitrag"] = e.payload.get("statement")
        return {
            "id": e.id,
            "runde": e.round,
            "block": e.block,
            "phase": e.phase,
            "typ": e.kind,
            "institution": self.name(e.actor),
            "inhalt": payload,
        }

    def position(self, e):
        return {
            "institution": self.name(e.actor),
            "block": e.block,
            "runde": e.round,
            "phase": e.phase,
            **{k: e.payload[k] for k in POSITION_FIELDS if e.payload.get(k)},
        }

    def history(self, events, actor):
        rows = [
            self.position(e) if e.kind == "statement" else {"runde": e.round, "stimme": e.payload.get("stimme")}
            for e in events
            if e.actor == actor and e.visibility == "public" and e.kind in {"statement", "vote"}
        ]
        return rows[-OWN_HISTORY_LIMIT:]

    def last_ballot(self, events):
        ballot = next((e for e in reversed(events) if e.kind == "ballot"), None)
        if not ballot:
            return None
        reasons = [
            {"institution": self.name(e.actor), "grund": e.payload.get("begruendung", "")}
            for e in events
            if e.kind == "vote"
            and e.round == ballot.round
            and e.block == ballot.block
            and e.payload.get("stimme") == "ablehnen"
        ]
        return {
            "block": ballot.block,
            "runde": ballot.round,
            "stimmen": {self.name(k): v for k, v in ballot.payload["stimmen"].items()},
            "konsens": ballot.payload["konsens"],
            "ablehnungsgruende": reasons,
            "nachbesserungen": [
                {"institution": self.name(e.actor), "aenderung": e.payload.get("begruendung", "")}
                for e in events
                if e.kind == "vote"
                and e.round == ballot.round
                and e.block == ballot.block
                and e.payload.get("stimme") == "nachbessern"
            ],
        }

    def agent_context(self, actor, group, events):
        """One stakeholder's own working memory: bounded, independent of how long others have talked."""
        public = [e for e in events if e.visibility == "public" and e.id <= self.context_cutoff]
        latest = {e.actor: e for e in public if e.kind == "statement" and e.actor != actor}
        channel = [
            {
                "institution": self.name(e.actor),
                "nachricht_nr": e.payload.get("nachricht_nr"),
                "inhalt": e.payload.get("inhalt"),
            }
            for e in events
            if e.kind == "group_message"
            and group
            and e.group == group
            and e.block == self.block
            and e.round == self.round
            # Membership at the time of sending: new members never see old private conversations.
            and actor in e.payload.get("members", [])
        ]
        hints = [e.payload for e in events if e.kind == "feedback" and e.actor == actor]
        a = self.actors[actor]
        return {
            "institution": a["name"],
            "db_infos": self.infos(actor),
            "gruppe": group,
            "eigener_verlauf": self.history(public, actor),
            "positionen_anderer": [self.position(e) for e in latest.values()],
            "gruppenkanal": channel,
            "letzte_abstimmung": self.last_ballot(public),
            "eigene_hinweise": hints[-3:],
        }

    def moderator_context(self, events):
        public = [e for e in events if e.visibility == "public"]
        current = [e for e in public if e.block == self.block and e.round == self.round]
        latest = {e.actor: e for e in current if e.kind == "statement"}
        earlier, open_proposal = [], None
        for e in public:
            if e.kind == "proposal":
                open_proposal = {"block": e.block, "runde": e.round, "vorschlag": e.payload.get("kompromissvorschlag")}
            elif e.kind == "ballot" and open_proposal:
                earlier.append({**open_proposal, "stimmen": {self.name(k): v for k, v in e.payload["stimmen"].items()}})
                open_proposal = None
        return {
            "statements_dieser_runde": [self.position(e) for e in latest.values()],
            "enthaltungen": [self.name(e.actor) for e in current if e.kind == "abstention"],
            "fruehere_vorschlaege": earlier,
            "letzte_abstimmung": self.last_ballot(public),
        }

    def watcher_context(self, task, events, ctx):
        proposals = [e.payload for e in events if e.kind == "proposal"]
        ctx["vorschlag"] = proposals[-1] if proposals else None
        relevant = [e for e in events if e.kind not in CONTROL_KINDS]
        watcher_events = [e for e in events if e.kind == "watcher"]
        previous = watcher_events[-1].payload if watcher_events else None
        if task == "abschlussbericht":
            first, last = {}, {}
            for e in relevant:
                if e.kind == "statement":
                    first.setdefault(e.actor, e)
                    last[e.actor] = e
            shown = [
                e
                for e in relevant
                if e.kind in REPORT_KINDS
                or (
                    e.kind == "review"
                    and e.payload.get("zurueckgewiesen", e.payload.get("pruefung", {}).get("bewertung") == 0)
                )
            ]
            return {
                "institutionen_profile": self.statutes(with_infos=True),
                "verlauf": [self.brief(e) for e in shown],
                "positionsentwicklung": [
                    {"anfang": self.position(first[k]), "zuletzt": self.position(last[k])} for k in first
                ],
                "letzter_zwischenstand": previous["summary"] if previous else None,
                "event_ids": [e["id"] for e in map(self.brief, shown)],
            }
        since = previous["cutoff"] if previous else 0
        fresh = [e for e in relevant if e.id > since]
        rejected = {
            e.actor
            for e in fresh
            if e.kind == "review"
            and e.payload.get("zurueckgewiesen", e.payload.get("pruefung", {}).get("bewertung") == 0)
        }
        return {
            "institutionen_profile": self.statutes(sorted(a for a in rejected if a in self.actors), with_infos=True),
            "bisheriger_stand": previous["summary"] if previous else None,
            "verlauf": [self.brief(e) for e in fresh],
            "event_ids": [e.id for e in fresh],
        }

    def context(self, role, actor="", group="", task=None, scope=None):
        events = self.run_events()
        ctx = {
            "frage": self.state.get("question", {}).get("frage", self.question),
            "runde": self.round,
            "phase": self.phase,
            "N": self.params["rounds"],
            "gruppen": self.state["groups"],
            "institutionen": [a["name"] for a in self.actors.values()],
            "institutions_ids": {key: a["name"] for key, a in self.actors.items()},
            "vorschlag": self.state.get("proposal"),
            "ergebnis_status": self.state.get("outcome", "konsens"),
        }
        if role == "agent":
            ctx.update(self.agent_context(actor, group, events))
        elif role == "moderator":
            ctx.update(self.moderator_context(events))
        elif role == "orchestrator":
            # Each check sees the statutes it judges against, not every negotiation so far.
            scope = [] if scope is None else scope
            checks = task in {"statement_pruefen", "gruppe_pruefen"}
            ctx["institutionen_profile"] = self.statutes(scope, with_infos=checks)
            if checks:
                ctx["bisherige_positionen"] = {self.name(a): self.history(events, a) for a in scope}
        elif role == "watcher":
            ctx.update(self.watcher_context(task, events, ctx))
        return ctx

    def prompt(self, role, actor=""):
        a = self.actors.get(actor, {})
        values = {
            "AGENT_NAME": a.get("name", ""),
            "INTERESSENSSAEULE": a.get("name", ""),
            "ROTE_LINIEN": json.dumps(a.get("rote_linien", []), ensure_ascii=False),
            "VERHANDELBARE_POSITIONEN": json.dumps(a.get("verhandelbar", []), ensure_ascii=False),
            # Kept out of the system prompt: each task receives only its scoped statutes in the context.
            "STATUTEN_ALLER_AGENTS": STATUTES_POINTER if role in {"orchestrator", "watcher"} else "",
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
                "Bei einer bestätigten roten Linie nenne in verletzte_rote_linie deren exakte Kennung R1 usw. oder den Wortlaut; "
                "bei einer wesentlich beschädigten Priorität 1 oder 2 nenne P1 oder P2. "
                "Das angeforderte JSON-Schema und die übergebenen Limits haben Vorrang vor Beispielen. "
                "Halte jedes Textfeld knapp: höchstens 120 Wörter. "
                "quellen enthält die quelle_id der db_infos, auf die sich der Beitrag stützt, sonst eine leere Liste."
            )
        )

    def call(self, role, task, suffix, actor="", group="", extra=None, scope=None):
        ctx = self.context(role, actor, group, task, scope)
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
                # Simulations from before this setting carry no per-role budget in their snapshot.
                "max_tokens": self.snap.get("max_tokens", {}).get(role, MAX_OUTPUT_TOKENS),
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
        if truncated(response):
            raise InvalidOutput(
                f"Token-Limit erreicht: Die Rolle {role.capitalize()} hat {request['max_tokens']} Output-Tokens "
                "einschließlich Reasoning verbraucht, ohne eine vollständige Antwort zu liefern. Limit unter "
                "Administration → Default und Rollenzuweisungen erhöhen und eine neue Simulation starten."
            )
        raise InvalidOutput("Auch die korrigierte Ausgabe war ungültig.")

    def safety(self, value, suffix, input_check=False, actor="", scope=None):
        try:
            local_check(value, self.snap["blocked_names"])
        except InvalidOutput:
            raise ContentBlocked("Personenbezug erkannt.") from None
        try:
            result = self.safety_call(value, suffix, input_check, actor, scope)
        except InvalidOutput:
            # A broken check must never turn into an abstention that could produce a consensus.
            raise PublicError(
                "Sicherheitsprüfung lieferte eine ungültige Antwort. Verhandlung wurde angehalten."
            ) from None
        if (
            not result["institutionell"]
            or not result["keine_personen"]
            or (not input_check and not result["keine_vertraulichen_statuten"])
        ):
            raise ContentBlocked("Inhalt wegen Personenbezug oder vertraulicher Statuten blockiert.")

    def safety_call(self, value, suffix, input_check, actor, scope):
        return self.call(
            "orchestrator",
            "sicherheit",
            suffix,
            scope=scope,
            extra={
                "institution": self.name(actor) if actor else None,
                "pruefling": value,
                "eingabepruefung": input_check,
                "anweisung": (
                    "Prüfe alle Texte auf Einzelpersonen, Personennamen, Zitate oder persönliche Zuschreibungen. "
                    "Akteure müssen Institutionen/Interessenvertretungen sein. "
                    "Bei Eingabeprüfung dürfen eigene Statuten enthalten sein; sonst dürfen keine vertraulichen "
                    "Statuten, roten Linien oder Prioritäten offengelegt werden. "
                    "Die institutionen_profile dienen nur als Prüfkontext, nicht als Prüfling."
                ),
            },
        )

    def confirmed_red_line(self, actor, review):
        """A rejection counts for a red line of this institution or a substantially damaged top priority.

        The orchestrator names the ground in `verletzte_rote_linie`: a red line (`R1` or its wording)
        or `P1`/`P2` for a top-two negotiable position of this institution.
        """
        line = review["verletzte_rote_linie"] or ""
        known = self.actors[actor]["rote_linien"]
        identifiers = re.findall(r"\bR\d+\b", line)
        if line in known or any(rule.startswith(identifier + ":") for rule in known for identifier in identifiers):
            return True
        priority = re.fullmatch(r"\s*P([12])\s*", line)
        return bool(priority) and any(
            item.get("prioritaet") == int(priority[1]) for item in self.actors[actor].get("verhandelbar", [])
        )

    def approved(self, task, actor, suffix, group="", extra=None):
        feedback = None
        rejection_confirmed = False
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
                if "quellen" in value:
                    # Only sources this institution actually received count as evidence.
                    known = {info.get("quelle_id") for info in self.infos(actor)}
                    value["quellen"] = [q for q in dict.fromkeys(value["quellen"]) if q in known]
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
                        scope=[actor],
                    )
                except InvalidOutput:
                    raise PublicError("Orchestrator-Prüfung ungültig. Verhandlung wurde angehalten.") from None
                if review["agent"] != self.actors[actor]["name"] or review["versuch"] != attempt:
                    raise PublicError("Orchestrator-Prüfung konnte nicht eindeutig zugeordnet werden.")
                rejecting = task == "abstimmung" and value["stimme"] == "ablehnen"
                if rejecting:
                    # The red-line scale marks a violating statement; a rejection naming one is valid.
                    rejected = not self.confirmed_red_line(actor, review)
                    rejection_confirmed = rejection_confirmed or not rejected
                else:
                    rejected = review["bewertung"] == 0
                self.emit(
                    "review",
                    {"statement": value, "pruefung": review, "zurueckgewiesen": rejected},
                    actor,
                    group,
                    "private",
                    self.key(f"{suffix}:{actor}:review{attempt}"),
                )
                if not rejected:
                    # The violated line stays with orchestrator and watcher; only the reasoning is published.
                    public = {k: v for k, v in value.items() if k != "verletzte_rote_linie"}
                    self.safety(public, f"{suffix}:{actor}:safe{attempt}", actor=actor, scope=[actor])
                    self.emit(
                        "statement" if task != "abstimmung" else "vote",
                        public,
                        actor,
                        group,
                        key=self.key(f"{suffix}:{actor}:published"),
                    )
                    return public
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
        if rejection_confirmed:
            # A confirmed red line stands even if its wording could not be published.
            public = {
                "agent": self.actors[actor]["name"],
                "stimme": "ablehnen",
                "begruendung": "Vom Orchestrator bestätigter Ablehnungsgrund; Begründung nicht zur Veröffentlichung freigegeben.",
            }
            self.emit("vote", public, actor, group, key=self.key(f"{suffix}:{actor}:published"))
            return public
        self.emit(
            "abstention",
            {"institution": self.actors[actor]["name"], "grund": "Versuchsgrenze erreicht."},
            actor,
            key=self.key(f"{suffix}:{actor}:abstention"),
        )
        return None

    def research_available(self):
        if "recherche" not in self.snap["prompts"] or "recherche" not in self.snap["roles"]:
            return False  # simulations started before this step keep their original course
        with self.factory() as s:
            return has_sources(s)

    def briefing(self, actor):
        """Searches sources for one institution and condenses them into person-free, citable facts."""
        a = self.actors[actor]
        scope = {"institution": a["name"]}
        try:
            terms = self.call(
                "recherche",
                "suchbegriffe",
                f"terms:{actor}",
                extra={**scope, "themen": [p.get("position") for p in a["verhandelbar"]]},
            )["begriffe"]
            with self.factory() as s:
                hits = search(s, terms + [a["name"]], "press", PRESS_HITS) + search(s, terms, "law", LAW_HITS)
            facts = []
            for attempt in range(2):
                if not hits:
                    break
                result = self.call(
                    "recherche", "briefing", f"briefing:{actor}:{attempt}", extra={**scope, "treffer": hits}
                )
                known = {hit["quelle_id"]: hit for hit in hits}
                facts = [
                    {
                        "quelle_id": fact["quelle"],
                        "art": known[fact["quelle"]]["art"],
                        "zitat": known[fact["quelle"]]["zitat"],
                        "datum": known[fact["quelle"]]["datum"],
                        "aussage": fact["aussage"],
                    }
                    for fact in result["fakten"]
                    if fact["quelle"] in known
                ]
                try:
                    self.safety({"fakten": facts}, f"briefing:{actor}:{attempt}:safe", actor=actor, scope=[actor])
                    break
                except ContentBlocked:
                    facts = []  # personal references must not reach the negotiation
        except InvalidOutput:
            # Without a briefing the institution negotiates without sources; the run stays honest about it.
            facts = []
        self.state.setdefault("briefings", {})[actor] = facts
        self.emit("briefing", {"institution": a["name"], "fakten": facts}, actor, key=self.key(f"briefing:{actor}"))

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
                self.safety(value, f"{suffix}:safety:{attempt}", scope=list(self.actors))
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
                "orchestrator",
                "gruppe_pruefen",
                f"group-review:{actor}",
                extra={"gruppe": gid, "mitglieder": members},
                scope=members,
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
                                self.safety(
                                    value, f"message:{group}:{actor}:{index}:{attempt}:safe", actor=actor, scope=[actor]
                                )
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
            "research": "individual",
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
                scope=[],
            )
            self.state["verified"] = True
        elif phase == "question":
            value = self.checked_role("moderator", "frage_stellen", "question")
            self.state["question"] = value
            self.emit("question", value, key=self.key("question"))
            if self.research_available():
                next_phase = "research"
        elif phase == "research":
            self.parallel(self.briefing)
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
            # Consensus needs no valid rejection, no requested change, no abstention and real support: an
            # institution whose vote was blocked has not consented, and toleration alone carries no decision.
            cast = [v["stimme"] for v in votes.values() if v]
            supporting = sum(stimme in {"zustimmen", "tolerieren"} for stimme in cast)
            approving = cast.count("zustimmen")
            abstaining = len(votes) - len(cast)
            consensus = not {"ablehnen", "nachbessern"} & set(cast) and abstaining == 0 and approving * 2 >= len(votes)
            self.state["outcome"] = "konsens" if consensus else "teilkompromiss"
            self.emit(
                "ballot",
                {
                    "stimmen": {k: v["stimme"] if v else "enthaltung" for k, v in votes.items()},
                    "konsens": consensus,
                    "tragend": supporting,
                    "zustimmend": approving,
                    "enthaltungen": abstaining,
                    "gesamt": len(votes),
                },
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
            value["nachbesserung"] = [
                {"agent": self.actors[k]["name"], "grund": v["begruendung"]}
                for k, v in votes.items()
                if v and v["stimme"] == "nachbessern"
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
            if phase not in {"start", "closing", "research"}:
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
                    e.kind == "review"
                    and e.payload.get("zurueckgewiesen", e.payload.get("pruefung", {}).get("bewertung") == 0)
                    for e in rows
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
