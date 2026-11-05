"""Cost-free checks of model names and API keys against the provider catalog."""

import difflib
import hashlib
import logging
import time
from urllib.parse import urlparse

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select

from .models import Actor, Endpoint
from .security import PublicError, cipher

log = logging.getLogger(__name__)
CATALOG_TTL, KEY_TTL = 300, 60
_cache = {}


def cached(key, ttl, load):
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    value = load()
    _cache[key] = (time.monotonic(), value)
    return value


def fetch_json(url, key=""):
    """Returns (status, json); status 0 means the provider could not be reached or answered no JSON."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=10, follow_redirects=False)
        return response.status_code, response.json()
    except (httpx.HTTPError, ValueError):
        return 0, None


def catalog(base_url, key=""):
    """Maps model id to catalog entry, or None if the provider offers no readable catalog."""

    def load():
        status, data = fetch_json(base_url.rstrip("/") + "/models", key)
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("data"), list):
            log.info("Modellkatalog nicht lesbar: HTTP %d", status)
            return None
        return {m["id"]: m for m in data["data"] if isinstance(m, dict) and m.get("id")}

    return cached(("catalog", base_url), CATALOG_TTL, load)


def is_openrouter(base_url):
    return urlparse(base_url).hostname == "openrouter.ai"


def key_status(base_url, key):
    """True or False for OpenRouter's free key lookup; None where a provider has no such lookup."""
    if not is_openrouter(base_url):
        return None
    digest = hashlib.sha256(key.encode()).hexdigest()

    def load():
        status, _ = fetch_json(base_url.rstrip("/") + "/key", key)
        return False if status == 401 else True if status == 200 else None

    return cached(("key", base_url, digest), KEY_TTL, load)


def find_model(models, model):
    # Variant suffixes such as :free or :nitro are valid on the listed base model.
    return models.get(model) or models.get(model.split(":", 1)[0])


def check_model(endpoint, model, app):
    """Classifies one endpoint/model pair: ok, warning, unknown (not checkable) or error."""
    result = {"endpoint": endpoint.name, "endpoint_id": endpoint.id, "model": model, "context_length": None}
    if not endpoint.enabled:
        return {**result, "level": "error", "message": "Verbindung ist deaktiviert."}
    if not model:
        return {**result, "level": "error", "message": "Kein Modell eingetragen."}
    if not endpoint.secret:
        return {**result, "level": "error", "message": "Kein API-Key hinterlegt."}
    try:
        key = cipher(app).decrypt(endpoint.secret.encode()).decode()
    except InvalidToken:
        return {
            **result,
            "level": "error",
            "message": "API-Key nicht lesbar. ENCRYPTION_KEY prüfen oder Key neu eintragen.",
        }
    if key_status(endpoint.base_url, key) is False:
        return {**result, "level": "error", "message": "API-Key wurde vom Anbieter abgelehnt."}
    models = catalog(endpoint.base_url, key)
    if models is None:
        return {
            **result,
            "level": "unknown",
            "message": "Modellkatalog nicht erreichbar, Modell nicht geprüft. „Verbindung testen“ prüft per API-Aufruf.",
        }
    entry = find_model(models, model)
    if not entry:
        close = difflib.get_close_matches(model, list(models), n=3, cutoff=0.6)
        hint = f" Ähnlich: {', '.join(close)}." if close else ""
        return {**result, "level": "error", "message": f"Modell „{model}“ gibt es bei diesem Anbieter nicht.{hint}"}
    result["context_length"] = entry.get("context_length")
    parameters = entry.get("supported_parameters")
    if isinstance(parameters, list) and "structured_outputs" not in parameters:
        return {
            **result,
            "level": "warning",
            "message": "Modell gefunden, unterstützt laut Katalog aber keine strukturierten JSON-Ausgaben.",
        }
    return {**result, "level": "ok", "message": "Modell gefunden, API-Key gültig."}


def check_assignments(session, app, system, actor_ids=None):
    """Checks every endpoint/model pair in use, once, and reports who relies on it."""
    query = select(Actor).order_by(Actor.name)
    if actor_ids is not None:
        query = query.where(Actor.id.in_(actor_ids))
    actors = [a for a in session.scalars(query) if a.profile.get("selectable", True)]
    assignments = [(system.get("endpoint_id"), "", "Default")] if actor_ids is None else []
    for role, label in (
        ("recherche", "Recherche"),
        ("moderator", "Moderator"),
        ("orchestrator", "Orchestrator"),
        ("watcher", "Watcher"),
    ):
        value = system.get("roles", {}).get(role, {})
        assignments.append((value.get("endpoint_id"), value.get("model", ""), label))
    assignments += [(a.endpoint_id, a.model, a.name) for a in actors]

    items, seen = [], {}
    for endpoint_id, model, label in assignments:
        endpoint = session.get(Endpoint, endpoint_id or system.get("endpoint_id") or "")
        if not endpoint:
            pair = (None, "")
            if pair not in seen:
                seen[pair] = {
                    "endpoint": "",
                    "endpoint_id": None,
                    "model": "",
                    "context_length": None,
                    "level": "error",
                    "message": "Keine Verbindung zugewiesen. Default-Verbindung auswählen.",
                    "used_by": [],
                }
                items.append(seen[pair])
            seen[pair]["used_by"].append(label)
            continue
        pair = (endpoint.id, model or endpoint.model)
        if pair not in seen:
            seen[pair] = {**check_model(endpoint, pair[1], app), "used_by": []}
            items.append(seen[pair])
        seen[pair]["used_by"].append(label)
    return items


def preflight(session, app, system, actor_ids):
    """Stops a simulation before its first call if a definite configuration error would stop it later."""
    if app.config["LLM_MODE"] == "fake":
        return
    errors = [i for i in check_assignments(session, app, system, actor_ids) if i["level"] == "error"]
    if errors:
        raise PublicError(
            "Simulation nicht gestartet: "
            + " ".join(f"{i['message']} (betrifft: {', '.join(i['used_by'][:4])})" for i in errors)
        )
