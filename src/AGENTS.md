# AGENTS.md – Pressespiegel-Agenten (Gesundheitswesen Österreich)

## Ziel
Pro Stakeholder ein KI-Agent, der auf Basis des täglichen Pressespiegels (APA-Comm, PDF) Fragen beantwortet, Artikel bewertet und eigenes Wissen aufbaut. Jeder Agent sieht ausschließlich seine eigenen Daten. Fokus: Österreich.

## Datengrundlage
- Pressespiegel Jänner bis Mai 2026: 99 PDFs, ca. 1.400 Artikel, ca. 11 MB Text.
- Jedes PDF hat ein Inhaltsverzeichnis (Seite, Titel, Datum, Medium, Seitenangabe) und je Artikel Metadaten (Quelle, Mutation, Typ, Land, Sprache). Das Inhaltsverzeichnis dient zum Trennen der Artikel.
- Der Text ist mehrspaltig und enthält Layoutreste, deshalb vor dem Einlesen bereinigen.
- Lizenz: Der Pressespiegel ist "ausschließlich zur persönlichen Information". Vor der Nutzung durch KI-Agenten und der Weitergabe an mehrere Stakeholder die APA-Comm-Lizenz klären.

## Stakeholder (Agenten)
1. Sozialversicherung
2. Gesundheitsministerium
3. Bundesländer, getrennt in 9 Agenten: Burgenland, Kärnten, Niederösterreich, Oberösterreich, Salzburg, Steiermark, Tirol, Vorarlberg, Wien (per `parent_id` unter "Bundesländer")
4. Ärztekammer
5. Vertragsärzt:innen
6. Wahlärzt:innen
7. Krankenanstalten
8. Pflegevertretungen
9. Patient:innenvertretungen
10. Gesetzgeber
11. Finanzministerium
12. Public-Health-Expert:innen
13. Wirtschaftskammer, Arbeiterkammer & Landwirtschaftskammer (Empfehlung: in 3 Agenten trennen, da gegensätzliche Interessen)
14. Industrie (z. B. Pharma)

## Datenmodell (PostgreSQL, Schema in `schema.sql`)
Zwei Schemas:

- **`core`: gemeinsamer Korpus, einmal eingelesen**
  - `source`, `source_issue`, `article`, `article_chunk`, `topic`, `article_topic`
  - `stakeholder`, `stakeholder_topic` (Interessenprofil)
  - `stakeholder_article`: **Freigabe**, welcher Stakeholder welchen Artikel sieht. Befüllt nur die Pipeline (Regeln, LLM, manuell), nie der Agent.
  - Neu für die Länder-Agenten: `article_region` (Bundesland je Artikel).
- **`agent`: stakeholder-eigene Daten**
  - `profile` (System-Prompt, Modell), `article_analysis` (Zusammenfassung, Haltung, Handlungsbedarf)
  - `knowledge_item` (Positionen, Fakten, Notizen, Gedächtnis), je Stakeholder partitioniert
  - `conversation`, `message` (mit `cited_chunk_ids` zur Nachvollziehbarkeit)

## Isolation
- Je Agent eine eigene DB-Rolle `agent_<code>` (Mitglied von `agent_base`).
- Row-Level-Security (RLS) auf allen Tabellen:
  - Artikel und Textabschnitte sind nur sichtbar, wenn sie über `stakeholder_article` zugewiesen sind.
  - Eigene Tabellen sind nur mit eigener `stakeholder_id` les- und schreibbar. Schreiben auf Analysen nur für zugewiesene Artikel.
  - Zuweisungen und Profile sind für Agenten nur lesbar.
- Die Stakeholder-Identität kommt aus der DB-Rolle (`current_user`), nie aus Agenten- oder Nutzereingaben.
- Getestet auf Postgres 16 mit zwei Rollen: fremde Daten lesen, schreiben, Rollenwechsel und Zugriff auf fremde Partitionen werden abgelehnt. pgvector war im Test nicht verfügbar, die Vektorteile sind ungetestet.

## Suche: Version 1 ohne Vektordatenbank
- Reine Postgres-Volltextsuche: `tsv` (deutsch) plus `pg_trgm` für unscharfe Titelsuche.
- Embedding-Spalte, HNSW-Index und Embedding-Modell vorerst weglassen. Später mit pgvector nachrüstbar, ohne Backend und Tools zu ändern.
- Schwäche: keine Synonyme (z. B. "Kassenarzt" vs. "Vertragsarzt"). Abhilfe: Synonymliste oder Query-Erweiterung durch das LLM.

## Agenten-Zugriff: feste Tools statt freiem SQL
Das Backend setzt jedes Tool auf Postgres um:

| Tool | Zweck |
|---|---|
| `search_articles(query, von, bis, medium, thema)` | Volltextsuche für inhaltliche Fragen |
| `get_article(id)` | Volltext eines Artikels |
| `list_articles(filter, sortierung)` | strukturierte Abfrage nach Datum, Medium, Thema |
| `count_by(thema / medium / monat)` | Zähl- und Trendfragen |
| `get_my_analysis`, `save_note`, `save_position` | eigenes Wissen lesen und schreiben |

Regeln fürs Backend:
- Der Agent übergibt nie eine `stakeholder_id`. Jede Query läuft in einer Transaktion mit `SET LOCAL ROLE agent_<code>`.
- Pool-User ohne Superuser und ohne `BYPASSRLS`, nur Mitglied der Agentenrollen.
- Limit, Zeitfenster und `statement_timeout` je Tool.
- Quellen der Antwort in `agent.message.cited_chunk_ids` speichern.

## Zuweisung der Artikel an Länder-Agenten
Kriterien kombinieren:
- Medium: Tiroler Tageszeitung, VN, Neue Vorarlberger, OÖNachrichten, Kleine Zeitung, MeinBezirk (Kärnten, Burgenland, Niederösterreich, Wien) u. a.
- Mutation im Pressespiegel (z. B. "Heute Oberösterreich").
- Nennungen im Text (Landesname, Landeshauptmann, Landesspital).
- Kronen Zeitung und Kurier decken mehrere Länder ab, die Zuordnung hängt dort am Text und ist fehleranfällig.
- Bundesthemen (Gesundheitsreform, Finanzausgleich) laufen über die Themenprofile in `stakeholder_topic`.

## Nächste Schritte
1. Schema auf Version 1 umbauen: ohne Embeddings, mit 9 Länder-Agenten und `article_region`.
2. Ingest-Pipeline: PDF, Artikel (über das Inhaltsverzeichnis), Bereinigung, Dublettenschutz über `content_hash`.
3. Klassifikation: Thema, Region und Zuweisung an Stakeholder, danach Stichprobenprüfung.
4. Tool-Definitionen (JSON-Schema) und SQL-Funktionen für das Backend.
5. System-Prompts je Agent und Evaluierung mit echten Fragen.

## Verbindlicher Prototyp-Umfang (vereinbart am 05.11.2026)

Die nachfolgenden Entscheidungen gehen den späteren Presse-Pipeline-Anforderungen für V1 vor:
Flask mit Jinja/Tailwind/DaisyUI, PostgreSQL/SQLAlchemy/Alembic und separater Python-Worker.
V1 lädt Pressespiegel und Rechtstexte in die Tabelle `source` und erstellt beim Start je Institution ein Briefing per Volltextsuche; keine Recherchetools für Agenten, kein SQL-Zugriff, keine Vektorsuche. Die getrennten Schemas und Datenbankrollen oben sind für V1 nicht umgesetzt.
Default N = 6 Runden insgesamt pro Block. Alle Akteure handeln ausschließlich institutionell;
keine Einzelpersonen, Personennamen oder Personenzitate in veröffentlichten Inhalten.
Watcher-Zwischenstände nur für die Bedienoberfläche, keine Rückkopplung in die Verhandlung.
Reset archiviert frühere Durchläufe. Anwendungscode liegt in `src/gilgamosh`.
Autoritative Prozessregeln stehen in `src/prompts/prozess_spezifikation.md`.
