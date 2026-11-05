import json
import logging
import time

import httpx
from pydantic import ValidationError

from .schemas import SCHEMAS
from .security import InvalidOutput, PublicError, local_check

log = logging.getLogger(__name__)
# Output budget per call. Reasoning models spend it before the answer: 6000 left some answers empty.
MAX_OUTPUT_TOKENS = 16000


def provider_error(response):
    """Classify provider errors; never publish provider text or request content."""
    try:
        error = response.json().get("error", {})
        text = json.dumps(error, ensure_ascii=False).lower()
    except (ValueError, AttributeError, TypeError):
        text = ""
    status = response.status_code
    if status == 401:
        category, message = "authentication", "OpenRouter-Zugang abgelehnt. Den hinterlegten API-Key prüfen."
    elif status == 402:
        category, message = "credits", "OpenRouter-Guthaben oder Ausgabenlimit erreicht. Guthaben und Key-Limit prüfen."
    elif status == 403:
        category, message = (
            "permissions",
            "LLM-Zugriff nicht erlaubt. Berechtigungen und Provider-Einstellungen prüfen.",
        )
    elif any(term in text for term in ("not a valid model", "invalid model", "model not found", "unknown model")):
        category, message = (
            "invalid_model",
            (
                "Ungültige Modellkennung. Die vollständige Model-ID aus dem OpenRouter-Katalog übernehmen "
                "(einschließlich eines eventuellen ~ am Anfang). Nach Korrektur eine neue Simulation starten."
            ),
        )
    elif "context" in text and any(term in text for term in ("length", "limit", "maximum", "exceed")):
        category, message = (
            "context",
            "Modell-Kontextlimit überschritten. Einen kleineren neuen Lauf oder ein Modell mit größerem Kontext verwenden.",
        )
    elif "schema" in text or "response_format" in text or "structured output" in text:
        category, message = (
            "structured_output",
            ("Strukturierte JSON-Ausgabe wurde abgelehnt. Modell- und Provider-Unterstützung für JSON-Schema prüfen."),
        )
    elif status == 404:
        category, message = (
            "not_found",
            "LLM-Endpunkt oder Modell nicht gefunden. Basis-URL und vollständige Model-ID prüfen.",
        )
    else:
        category, message = "request", "LLM-Anfrage abgelehnt. Verbindung, Modell und Modellfähigkeiten prüfen."
    log.warning("LLM request rejected: HTTP %d, category=%s", status, category)
    return PublicError(message)


class Gateway:
    def __init__(self, app, mode=None):
        self.app = app
        self.mode = mode or app.config["LLM_MODE"]

    def complete(self, config, request, task):
        if self.mode == "fake":
            return fake_response(request, task)
        if not config.get("secret"):
            raise PublicError("API-Key fehlt. Verbindung in der Administration konfigurieren.")
        from .security import cipher

        key = cipher(self.app).decrypt(config["secret"].encode()).decode()
        body = {
            **request,
            "model": config["model"],
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": task, "strict": True, "schema": SCHEMAS[task].model_json_schema()},
            },
        }
        attempts = config.get("attempts", 3)
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=config["timeout"], follow_redirects=False) as client:
                    response = client.post(
                        config["base_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=body,
                    )
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < attempts - 1:
                        try:
                            delay = min(60, max(1, float(response.headers.get("Retry-After", 2**attempt))))
                        except ValueError:
                            delay = 2**attempt
                        time.sleep(delay)
                        continue
                    raise PublicError("LLM-Dienst vorübergehend nicht verfügbar. Fortsetzen ist möglich.")
                if response.status_code >= 400:
                    raise provider_error(response)
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == attempts - 1:
                    raise PublicError("LLM-Dienst nicht erreichbar. Fortsetzen ist möglich.") from None
                time.sleep(2**attempt)
        raise PublicError("LLM-Anfrage fehlgeschlagen.")


def parse_response(response, task, blocked_names):
    try:
        content = response["choices"][0]["message"]["content"]
        result = SCHEMAS[task].model_validate_json(content).model_dump()
        local_check(result, blocked_names)
        return result
    except (KeyError, IndexError, TypeError, ValidationError, ValueError):
        raise InvalidOutput("Antwort entspricht nicht dem verlangten Schema.") from None


def fake_response(request, task):
    """Explicit offline demo, never represented as a real provider result."""
    ctx = json.loads(request["messages"][-1]["content"])
    a = ctx.get("institution", "Institution")
    r = ctx.get("runde", 1)
    g = ctx.get("gruppe", "")
    results = {
        "suchbegriffe": {"begriffe": ["Wartezeit", "Facharzt", "Kassenstelle"]},
        "briefing": {
            "fakten": [
                {"quelle": hit["quelle_id"], "aussage": "Synthetische Demoaussage aus der Quelle."}
                for hit in ctx.get("treffer", [])[:3]
            ]
        },
        "frage_stellen": {
            "frage": ctx.get("frage", ""),
            "kontext": "Demonstration mit synthetischen Institutionsprofilen.",
        },
        "einzelphase": {
            "agent": a,
            "runde": r,
            "position": "Gezielte regionale Versorgung und digitale Terminsteuerung gemeinsam erproben.",
            "begruendung": "Qualität, Zugänglichkeit und Budget müssen gemeinsam berücksichtigt werden.",
            "tragbar": None if r == 1 and not ctx.get("vorschlag") else True,
            "zugestaendnisse": [],
            "quellen": [info["quelle_id"] for info in ctx.get("db_infos", []) if "quelle_id" in info][:2],
        },
        "koalitionsphase": {
            "agent": a,
            "aktion": "bleiben",
            "ziel_gruppe": None,
            "partner": [],
            "begruendung": "Eigenständige Prüfung.",
        },
        "zustimmung": {"zustimmung": True},
        "gruppennachricht": {
            "agent": a,
            "gruppe": g,
            "nachricht_nr": ctx.get("nachricht_nr", 1),
            "inhalt": "Ein gemeinsamer Pilot soll Wirkung und Kosten transparent erfassen.",
        },
        "gruppenstatement": {
            "agent": a,
            "gruppe": g,
            "statement": "Regional begrenzte Pilotprojekte mit gemeinsamer Evaluierung.",
            "einigungen_in_gruppe": [],
            "zugestaendnisse": ["Zeitlich begrenzter Pilot statt sofortiger Gesamtlösung."],
            "quellen": [],
        },
        "abstimmung": {
            "agent": a,
            "stimme": "zustimmen",
            "begruendung": "Evaluierung und begrenzter Umfang entsprechen den Interessen der Institution.",
            "verletzte_rote_linie": None,
        },
        "statement_pruefen": {
            "agent": a,
            "versuch": ctx.get("versuch", 1),
            "bewertung": 2,
            "entscheidung": "freigegeben",
            "verletzte_rote_linie": None,
            "hinweis_an_agent": None,
            "zugestaendnisse_erkannt": [],
        },
        "gruppe_pruefen": {
            "gruppen_id": g,
            "mitglieder": ctx.get("mitglieder", []),
            "entscheidung": "freigegeben",
            "begruendung_intern": "Demoprüfung.",
            "begruendung_an_agents": "Gemeinsame Pilotziele.",
            "kanal_id": g,
        },
        "sammelstand_und_vorschlag": {
            "runde": r,
            "sammelstand": {
                "gemeinsamkeiten": ["Versorgung verbessern."],
                "streitpunkte": [],
                "zugestaendnisse": ["Mit Pilotprojekten beginnen."],
            },
            "kompromissvorschlag": "Regionale Pilotprojekte für digitale Terminsteuerung mit Evaluation und Budgetkontrolle.",
            "begruendung": "Ein schrittweises Vorgehen verbindet Versorgung und Wirtschaftlichkeit.",
            "offene_konflikte": [],
            "zielbeitrag": "Demolauf: Beitrag zu den Vorgaben der Frage nicht belegt.",
        },
        "abschluss": {
            "status": ctx.get("ergebnis_status", "konsens"),
            "ergebnis": "Gemeinsame Pilotprojekte evaluieren.",
            "zustimmung": [],
            "tolerierung": ctx.get("institutionen", []),
            "nachbesserung": [],
            "ablehnung": [],
            "offene_punkte": [],
            "empfehlung_neustart": "keiner",
            "begruendung_empfehlung": "Demolauf abgeschlossen.",
        },
        "zwischenstand": {
            "gemeinsamkeiten": ["Bessere regionale Versorgung."],
            "vorschlaege": ["Begrenzte Pilotprojekte."],
            "konflikte": [],
            "zugestaendnisse": [],
            "positionsaenderungen": [],
            "koalitionen": [],
            "abstimmung": [],
            "referenzen": ctx.get("event_ids", [])[-6:],
        },
        "abschlussbericht": {
            "status": ctx.get("ergebnis_status", "konsens"),
            "ergebnis": "Institutionen stimmen einem gemeinsamen Pilotansatz zu.",
            "verlauf": [],
            "koalitionsbildung": "Eigenständige Institutionsgruppen.",
            "wendepunkte": [],
            "entscheidungsfindung": "Schrittweise Evaluierung verbindet die Ziele.",
            "offene_konflikte": [],
            "auffaelligkeiten": ["Synthetischer Demolauf ohne echte LLM-Aufrufe."],
            "statistik": {"runden": r, "rueckweisungen": 0, "enthaltungen": 0, "gruppenwechsel": 0},
        },
        "sicherheit": {"institutionell": True, "keine_personen": True, "keine_vertraulichen_statuten": True},
    }
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps(results[task], ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "model": "offline-demo",
    }
