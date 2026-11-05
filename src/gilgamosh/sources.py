"""Press and legal sources: parsing, loading and full-text search."""

import hashlib
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert

from .models import Setting, Source
from .security import PublicError

log = logging.getLogger(__name__)
MAX_BODY_CHARS = 60000
LAW_TYPES = {"PARA": "§", "ART": "Art.", "ANL": "Anl.", "ANH": "Anh.", "ZIFF": "Z", "ZAHL": "Nr."}
TOC_ENTRY = re.compile(
    r"^S\. (?P<page>\d+)\s+(?P<title>.+?)\n(?P<more>(?:\s+(?!\d{2}\.\d{2}\.\d{4},).+\n)*)"
    r"\s+(?P<date>\d{2}\.\d{2}\.\d{4}), (?P<rest>.+)$",
    re.MULTILINE,
)
ARTICLE_END = re.compile(r"^(Quelle|Autor:in|Land|Mutation|Sprache|Medientyp|Ressort)\s*$", re.MULTILINE)
PAGE_NOISE = re.compile(r"^(Pressespiegel|Seite \d+ von \d+|© APA-Comm GmbH)\s*$", re.MULTILINE)


def content_key(prefix, *parts):
    return f"{prefix}:{hashlib.sha256(' '.join(parts).encode()).hexdigest()[:32]}"


def clean_press_page(page):
    """Keeps an article's text; facsimile pages of the printed newspaper do not start with the header."""
    if not page.lstrip().startswith("Pressespiegel"):
        return ""
    end = ARTICLE_END.search(page)
    return PAGE_NOISE.sub("", page[: end.start()] if end else page).strip()


def parse_press(toc_text, plain_text):
    """Splits one press review into articles using its table of contents."""
    pages = plain_text.split("\f")
    entries = [m.groupdict() for m in TOC_ENTRY.finditer(toc_text)]
    articles = []
    for index, entry in enumerate(entries):
        first = int(entry["page"]) - 1
        last = int(entries[index + 1]["page"]) - 1 if index + 1 < len(entries) else len(pages)
        body = "\n".join(filter(None, map(clean_press_page, pages[first:last])))
        title = " ".join((entry["title"] + " " + entry["more"]).split())
        # The article opens with "<medium> vom <date>", followed by the title already kept separately.
        body = re.sub(r"\A.*? vom \d{2}\.\d{2}\.\d{4}\s*", "", body, count=1, flags=re.DOTALL)
        if len(body) < 80:
            continue
        medium, _, location = entry["rest"].partition(", Seite ")
        articles.append(
            {
                "key": content_key("press", title.lower(), body[:500].lower()),
                "kind": "press",
                "title": title,
                "publisher": medium.strip(),
                "citation": f"{medium.strip()}, {entry['date']}" + (f", S. {location.strip()}" if location else ""),
                "published": datetime.strptime(entry["date"], "%d.%m.%Y").date(),
                "body": body[:MAX_BODY_CHARS],
            }
        )
    return articles


def parse_law(xml_text):
    """Returns one legal provision, or None for tables of contents, promulgations and empty entries."""
    root = ET.fromstring(xml_text)
    meta = root.find(".//metadaten")
    content = root.find(".//nutzdaten")
    if meta is None or content is None or meta.get("gldart") not in LAW_TYPES:
        return None
    body = " ".join(" ".join(content.itertext()).split())
    if len(body) < 40:
        return None
    number = meta.get("gldnr", "")
    abbreviation = meta.get("abkrvo", "")
    citation = f"{abbreviation} {LAW_TYPES[meta.get('gldart')]} {number}".strip()
    try:
        published = date.fromisoformat(meta.get("ikra", ""))
    except ValueError:
        published = None
    return {
        "key": f"law:{root.get('dokid') or citation}",
        "kind": "law",
        "title": f"{citation} ({meta.get('titrvo', '').strip()})",
        "publisher": abbreviation,
        "citation": citation,
        "published": published,
        "body": body[:MAX_BODY_CHARS],
    }


def pdf_text(path, layout=False):
    command = ["pdftotext", *(["-layout"] if layout else []), str(path), "-"]
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=120).stdout.decode(errors="replace")
    except FileNotFoundError:
        raise PublicError("pdftotext fehlt. Paket poppler-utils installieren.") from None


def store(session, rows):
    if not rows:
        return 0
    unique = list({row["key"]: row for row in rows}.values())
    statement = insert(Source).values(unique).on_conflict_do_nothing(index_elements=["key"]).returning(Source.id)
    return len(session.execute(statement).all())


def stamp(path):
    info = path.stat()
    return f"{info.st_size}:{int(info.st_mtime)}"


def folder_stamp(folder):
    """Cheap fingerprint of a large folder, so that a restart need not read every file again."""
    files = sorted(folder.glob("*.xml"))
    return hashlib.sha256("".join(f"{p.name}{stamp(p)}" for p in files).encode()).hexdigest()


def ingest(session, root, batch=200, report=print, optional=False):
    """Loads press folders and legal texts below root. Already loaded files are skipped on later runs."""
    root = Path(root)
    press_files = sorted(p for folder in root.glob("*Pressespiegel*") if folder.is_dir() for p in folder.glob("*.pdf"))
    law_dir = root / "Rechtstexte"
    added = {"press": 0, "law": 0}
    if not press_files and not law_dir.is_dir():
        if optional:
            report(f"Keine Quellen unter {root}: Simulationen laufen ohne Recherche.")
            return added
        raise PublicError(f"Unter {root} wurden weder Pressespiegel-Ordner noch Rechtstexte gefunden.")
    marker = session.get(Setting, "ingested") or Setting(key="ingested", value={})
    session.add(marker)
    known = dict(marker.value)
    # The same file name can appear in two monthly folders.
    file_key = lambda p: f"{p.parent.name}/{p.name}"
    pending = [p for p in press_files if known.get(file_key(p)) != stamp(p)]
    for number, path in enumerate(pending, 1):
        try:
            articles = parse_press(pdf_text(path, layout=True), pdf_text(path))
        except (subprocess.SubprocessError, ValueError):
            log.warning("Pressespiegel übersprungen: %s", path.name)
            continue
        added["press"] += store(session, articles)
        known[file_key(path)] = stamp(path)
        marker.value = dict(known)
        session.commit()
        report(f"Presse {number}/{len(pending)}: {path.name}, {len(articles)} Artikel")
    skipped = 0
    law_stamp = folder_stamp(law_dir) if law_dir.is_dir() else None
    if law_stamp and known.get("Rechtstexte") != law_stamp:
        rows = []
        for path in sorted(law_dir.glob("*.xml")):
            try:
                row = parse_law(path.read_text(encoding="utf-8"))
            except (ET.ParseError, UnicodeDecodeError):
                skipped += 1
                continue
            if row:
                rows.append(row)
            if len(rows) >= batch:
                added["law"] += store(session, rows)
                session.commit()
                rows = []
        added["law"] += store(session, rows)
        known["Rechtstexte"] = law_stamp
        marker.value = dict(known)
        session.commit()
    report(f"Neu: {added['press']} Artikel, {added['law']} Rechtstexte. Unlesbar übersprungen: {skipped}.")
    return added


def search_query(terms):
    """OR across terms, AND within a multi-word term; operators in the input have no effect."""
    cleaned = [" ".join(re.sub(r'["\'\-]', " ", term).split()) for term in terms]
    return " OR ".join(term for term in cleaned if term)


def search(session, terms, kind, limit):
    """Best matches of one kind. Newer press counts more; the halving time is about four months."""
    expression = search_query(terms)
    if not expression:
        return []
    query = func.websearch_to_tsquery("german", expression)
    age = func.greatest(0, func.current_date() - func.coalesce(Source.published, func.current_date()))
    # Normalized by document length, so that long contracts do not crowd out single provisions.
    rank = func.ts_rank_cd(Source.search, query, 1 | 32)
    if kind == "press":
        rank = rank * func.exp(-age / 180.0)
    top = (
        select(Source.id, Source.kind, Source.citation, Source.published, Source.body, rank.label("rank"))
        .where(Source.kind == kind, Source.search.op("@@")(query))
        .order_by(literal_column("rank").desc(), Source.id)
        .limit(limit)
        .subquery()
    )
    excerpt = func.ts_headline(
        "german", top.c.body, query, "MaxFragments=3,MaxWords=45,MinWords=15,StartSel='',StopSel=''"
    )
    rows = session.execute(
        select(top.c.id, top.c.kind, top.c.citation, top.c.published, excerpt.label("excerpt")).order_by(
            top.c.rank.desc(), top.c.id
        )
    ).all()
    return [
        {
            "quelle_id": row.id,
            "art": "Presse" if row.kind == "press" else "Rechtstext",
            "zitat": row.citation,
            "datum": row.published.isoformat() if row.published else None,
            "auszug": row.excerpt,
        }
        for row in rows
    ]


def statistics(session):
    rows = session.execute(
        select(Source.kind, func.count(), func.min(Source.published), func.max(Source.published)).group_by(Source.kind)
    ).all()
    return {row[0]: {"count": row[1], "first": row[2], "last": row[3]} for row in rows}


def has_sources(session):
    return session.scalar(select(text("1")).select_from(Source).limit(1)) is not None
