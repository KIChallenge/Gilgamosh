"""Import the versioned stakeholder sources without leaking cross-stakeholder background."""

import hashlib
import json
import re
from pathlib import Path

from .models import Actor
from .security import INSTITUTION_RULE, PublicError

DEFAULT_SELECTION = {
    "sozialversicherung",
    "gesundheitsministerium",
    "aerztekammer",
    "patientenvertretung",
    "finanzministerium",
    "krankenanstalten",
}
SOURCE_NOTICE = (
    "Modellierter Entwurf aus den bereitgestellten Stakeholder-Prompts. "
    "Keine offiziellen Statuten; fachliche Prüfung erforderlich. "
    "Die im Prompt als geprüft bezeichneten Rechtsgrundlagen wurden vom Autor so gekennzeichnet; "
    "die Anwendung hat sie nicht unabhängig verifiziert."
)
INSTITUTION_NAMES = {
    "public_health": "Public-Health-Fachvertretung",
    "vertragsaerzte": "Vertretung der Vertragsärzt:innen",
    "wahlaerzte": "Vertretung der Wahlärzt:innen",
}
OFFICE_REPLACEMENTS = {
    "Vorsitz der Landeshauptleutekonferenz": "Landeshauptleutekonferenz",
    "Landeshauptfrau": "Landesregierung",
    "Landeshauptmann": "Landesregierung",
    "Gesundheitsreferentin": "Gesundheitsressort",
    "Gesundheitslandesrat": "Gesundheitsressort",
    "Gesundheitsstadtrat": "Gesundheitsressort",
}


def source_directory():
    packaged = Path(__file__).parent / "stakeholders"
    return packaged if packaged.is_dir() else Path(__file__).parents[1] / "prompts" / "agents"


def icon_directory():
    packaged = Path(__file__).parent / "icons"
    return packaged if packaged.is_dir() else Path(__file__).parents[1] / "assets" / "icons"


def has_icon(actor_id):
    return re.fullmatch(r"[a-z][a-z0-9_-]*", actor_id) is not None and (icon_directory() / f"{actor_id}.png").is_file()


def initials(name):
    """Fallback mark for institutions without an icon file: distinct even for similar names."""
    words = [w for w in re.split(r"[\s-]+", re.sub(r":\S*", "", name)) if w not in {"Vertretung", "der", "Land"}]
    return "".join(w[0] for w in words[:2]).upper() if len(words) > 1 else "".join(words)[:2].capitalize()


def fingerprint(name, parent_id, profile):
    data = {"name": name, "parent_id": parent_id, "profile": {k: v for k, v in profile.items() if k != "seed_hash"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_profiles(directory=None):
    directory = directory or source_directory()
    profiles = []
    for path in sorted(directory.glob("*.md")):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", path.stem):
            continue
        raw = path.read_text()
        match = re.search(r"Du bist \*\*(.+?)\*\*", raw)
        if not match or "## Deine Statuten" not in raw:
            raise PublicError("Stakeholder-Quelldatei hat ein unbekanntes Format.")
        original_name = match[1]
        name = INSTITUTION_NAMES.get(path.stem, original_name)
        red_section = raw.split("### Rote Linien", 1)[1].split("### Verhandelbare Positionen", 1)[0]
        lines = re.findall(r"^- \*\*(R\d+):\*\* (.+)$", red_section, re.MULTILINE)
        positions_section = raw.split("### Verhandelbare Positionen", 1)[1].split("## Grundregeln", 1)[0]
        positions = re.findall(r"^(\d+)\. (.+)$", positions_section, re.MULTILINE)
        if not lines or not positions:
            raise PublicError("Stakeholder-Quelldatei enthält keine auswertbaren Statuten.")
        # Identity and legal context augment the shared runtime prompt. The structured statutes
        # remain the single editable source of truth; duplicated static JSON examples are omitted.
        identity = raw.split("## Deine Statuten", 1)[0].strip().replace(original_name, name)
        for old, new in OFFICE_REPLACEMENTS.items():
            identity = identity.replace(old, new)
        profiles.append(
            {
                "id": path.stem,
                "name": name,
                "parent_id": "bundeslaender" if path.stem.startswith("land_") else None,
                "system_prompt": identity + "\n\n" + INSTITUTION_RULE,
                "rote_linien": [f"{number}: {text}" for number, text in lines],
                "verhandelbar": [{"position": text, "prioritaet": int(priority)} for priority, text in positions],
                "db_infos": [],
                "demo": False,
                "modeled": True,
                "selectable": True,
                "source_file": f"src/prompts/agents/{path.name}",
                "source_hash": hashlib.sha256(raw.encode()).hexdigest(),
                "seed_hash": "",
            }
        )
    if len(profiles) != 24:
        raise PublicError("Die gebündelten 24 Stakeholder-Profile sind unvollständig.")
    parent = {
        "id": "bundeslaender",
        "name": "Bundesländer",
        "parent_id": None,
        "system_prompt": "Organisatorischer Elternknoten der neun Landesinstitutionen; kein Verhandlungsakteur.",
        "rote_linien": [],
        "verhandelbar": [],
        "db_infos": [],
        "demo": False,
        "modeled": True,
        "selectable": False,
        "source_file": "src/prompts/agents/README.md",
        "source_hash": "",
        "seed_hash": "",
    }
    return [parent, *profiles]


def seed_stakeholders(session):
    from .services import import_actors

    imported, preserved = [], []
    for value in read_profiles():
        existing = session.get(Actor, value["id"])
        legacy_demo = (
            existing and existing.profile.get("demo") and existing.profile.get("system_prompt") == INSTITUTION_RULE
        )
        unchanged = existing and existing.profile.get("seed_hash") == fingerprint(
            existing.name, existing.parent_id, existing.profile
        )
        if existing and not legacy_demo and not unchanged:
            preserved.append(value["id"])
            continue
        item = dict(value)
        if existing:
            item.update(endpoint_id=existing.endpoint_id, model=existing.model)
        import_actors(session, [item])
        actor = session.get(Actor, value["id"])
        actor.profile = {**actor.profile, "seed_hash": fingerprint(actor.name, actor.parent_id, actor.profile)}
        imported.append(actor.id)
    return imported, preserved
