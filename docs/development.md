# Prototyp betreiben

Die Anwendung heißt RoundTable. Technische Bezeichner wie das Python-Paket und der Befehl `gilgamosh`, die Datenbank und das Docker-Volume tragen weiterhin den früheren Namen, damit bestehende Daten und Abläufe unverändert funktionieren.

## Schnellstart mit Docker Compose

```bash
cp .env.example .env
uv run python -c 'import secrets; print(secrets.token_hex(32))'
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Die beiden generierten Werte als `SECRET_KEY` beziehungsweise `ENCRYPTION_KEY` in `.env` eintragen. `POSTGRES_PASSWORD` auf einen langen zufälligen Hex-Wert und `ADMIN_PASSWORD` auf ein eigenes Passwort mit mindestens 12 Zeichen setzen. `.env` nicht versionieren; den Verschlüsselungsschlüssel zusammen mit Datenbankbackups sicher aufbewahren. Verlust des Schlüssels macht gespeicherte API-Keys unlesbar.

`LLM_MODE=fake` aktiviert eine ausdrücklich gekennzeichnete Offline-Demo mit synthetischen Antworten ohne Providerkosten. `LLM_MODE=openrouter` verwendet die konfigurierten Verbindungen. Die 24 gelieferten Stakeholder-Profile aus `src/prompts/agents/` sind modellierte Entwürfe, keine offiziellen Positionen. Neun Landesprofile sind dem nicht auswählbaren Elternknoten Bundesländer zugeordnet. Alle Akteure handeln institutionell; keine Personennamen oder Personenzitate verwenden.

```bash
docker compose up --build -d
```

Die Anwendung ist unter http://localhost:5000 erreichbar. Anmeldung mit den Werten `ADMIN_USERNAME` und `ADMIN_PASSWORD` aus `.env`. Der einmalige Init-Dienst führt Migrationen aus und legt Konto, Stakeholder-Profile und Rollenprompts an. Unveränderte Rollenprompts erhalten bei geänderten Repository-Dateien automatisch eine neue Version, manuell bearbeitete bleiben bestehen. Unveränderte Repository-Profile und die ursprünglichen Demoprofile werden aktualisiert; manuell bearbeitete Profile und vorhandene Konten werden beibehalten. Endpoint- und Modellzuweisungen bleiben erhalten. Zum Aktualisieren `docker compose up --build -d` verwenden. Keine Volumes löschen, wenn Verhandlungen erhalten bleiben sollen.

In **Administration → Modelle & Verbindungen** eine aktive Verbindung mit Basis-URL `https://openrouter.ai/api/v1`, einem Modell mit Unterstützung für strukturierte JSON-Ausgaben und einem API-Key anlegen. Die Verbindung als Default auswählen. Ein Key kann alle Modelle und Rollen bedienen. Die Oberfläche prüft Modellnamen und API-Key automatisch und kostenlos gegen den Modellkatalog des Anbieters: beim Öffnen der Administration und der Startseite, nach dem Speichern und beim Tippen. Eine Simulation mit falschem Modell oder abgelehntem Key startet nicht. Ist der Katalog nicht erreichbar, wird das als „nicht geprüft“ angezeigt und der Start nicht blockiert. Der manuelle Verbindungstest verursacht einen kleinen LLM-Aufruf. Rollen und Institutionen können Verbindung und Modell individuell überschreiben. Leere Zuweisungen erben den Default; eine deaktivierte explizite Zuweisung fällt nicht auf einen anderen Anbieter zurück.

Unter **Default und Rollenzuweisungen** lassen sich die maximalen Output-Tokens je Rolle einstellen (Default 16.000, Watcher 32.000). Der Wert umfasst das Reasoning; ein zu kleiner Wert führt zu leeren Antworten. Das Limit des jeweiligen Modells prüft die Oberfläche nicht. Änderungen gelten für neue Simulationen. Der Parallelitätswert wird auch beim Workerstart gelesen: nach Änderungen `docker compose restart worker` ausführen. V1 ist für einen Worker-Prozess vorgesehen, dessen Threads vier Calls gleichzeitig ausführen können. Ein zusätzlicher Ausführungsslot verarbeitet Watcher-Jobs; diese teilen das konfigurierte Call-Limit mit der Verhandlung.

## Entwicklung ohne Container für Web und Worker

PostgreSQL 16 bereitstellen. `.env` enthält weiterhin die oben beschriebenen Werte. Zusätzlich `DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/DATABASE` setzen. Python mindestens 3.11.7 und Node 22 verwenden.

```bash
uv sync
npm ci
npm run build
cp node_modules/chart.js/dist/chart.umd.js src/gilgamosh/static/
set -a
source .env
set +a
uv run alembic upgrade head
uv run gilgamosh init
uv run gilgamosh web
```

In einem zweiten Terminal mit derselben Umgebung:

```bash
uv run gilgamosh worker
```

`gilgamosh worker --once` führt einen verfügbaren Arbeitsschritt aus. Der Entwicklungsserver bindet ausschließlich localhost. Produktiver Start im Container verwendet Gunicorn mit Threads, damit SSE-Verbindungen andere Requests nicht blockieren.

## Testen

Tests verwenden ausschließlich die Fake-LLM und eine **separate PostgreSQL-Datenbank**, deren Name `test` enthält. Die Tests löschen und erstellen ihre Anwendungstabellen. Niemals die Entwicklungs- oder Demo-Datenbank verwenden.

```bash
docker run -d --name gilgamosh-test-db \
  -e POSTGRES_PASSWORD=gilgamosh-test -e POSTGRES_DB=gilgamosh_test \
  -p 127.0.0.1:55432:5432 postgres:16-alpine
uv run pytest -q
uv run ruff check src/gilgamosh tests migrations
node --check src/gilgamosh/static/app.js
node --check src/gilgamosh/static/simulation.js
```

Alternativ `TEST_DATABASE_URL` setzen. Der Browser-Smoke-Test ist mit `RUN_BROWSER_TESTS=1 uv run pytest tests/test_browser.py -q` ausführbar; vorher `uv run playwright install chromium` installieren. Er prüft echte HTTP-Aufrufe, einen langsamen Offline-Provider, Ladezustände, Pausieren, Fortsetzen, Bericht und Download.

Für Migrationen `uv run alembic revision --autogenerate -m 'Beschreibung'`, Revision prüfen, anschließend `uv run alembic upgrade head`. `uv run alembic check` prüft Abweichungen zwischen Schema und Modellen. Migrationen schützen Ereignisse, Promptversionen und abgeschlossene Aufrufprotokolle vor Änderung oder Löschung.

## Daten und Grenzen

- Default sind sechs Runden pro Block, drei fachliche Versuche insgesamt und drei Gruppennachrichten je Institution. Alle Limits sind im Startformular konfigurierbar.
- Fortsetzen nach Teilkompromiss erhält Gruppen und Sammelstand; Reset erzeugt einen neuen Durchlauf und archiviert den alten. Exporte und technische Trajectories enthalten Durchlauf- und Blockkennungen.
- Nach jeder Phase wird ein Watcher-Zwischenstand vorgemerkt. Noch nicht gestartete veraltete Aufträge werden zusammengefasst; die Oberfläche zeigt Zeitpunkt und Aktualisierungszustand. Watcher-Inhalte gelangen niemals zurück in Agentenkontexte.
- Reguläre Diskussion und Exporte enthalten ausschließlich freigegebene Inhalte. Das gesonderte technische Aufrufprotokoll enthält absichtlich ungeprüfte Rohantworten, vertrauliche Request-Kontexte und gegebenenfalls vom Provider geliefertes Reasoning. Es ist nur nach Anmeldung erreichbar.
- Personenbezüge und Offenlegung vertraulicher Statuten werden durch Promptregeln, eine zusätzliche LLM-Prüfung und lokale Sperrmuster geprüft. Die zusätzliche Sperrliste erkennt bekannte Namen deterministisch. Die semantische Erkennung durch Modelle ist keine mathematische Garantie; Offline-Demoantworten sind fest vorgegeben.
- Ein Workerabsturz kann nach Ablauf der 90-Sekunden-Reservierung aufgeholt werden. Bereits gespeicherte Antworten werden wiederverwendet. Eine beim Provider verarbeitete, lokal noch nicht gespeicherte Antwort kann bei Wiederholung erneut Kosten verursachen.
- Jeder Stakeholder erhält einen eigenen begrenzten Kontext: eigener Verlauf (letzte neun Einträge), je Institution die letzte freigegebene Position, Gruppenkanal der laufenden Runde, Sammelstand und letzte Abstimmung. Ältere Runden stehen damit nur verdichtet im Prompt, vollständig aber in Ereignissen und Aufrufprotokollen. Orchestrator-Prüfungen sehen nur die Statuten der betroffenen Institutionen, der Watcher liest schrittweise. Die Kachel „Größter Kontext“ zeigt die Auslastung. Bei mehr als `MAX_CONTEXT_CHARS` (Default 240000 Zeichen pro Request) pausiert die Verhandlung beziehungsweise meldet der Watcher einen veralteten Stand. Ein neuer Lauf mit kleinerem Umfang oder eine größere explizite Grenze ist dann erforderlich. Das modellabhängige Tokenlimit muss zusätzlich berücksichtigt werden.
- Kosten werden nur angezeigt, wenn der Provider sie meldet. OpenRouter meldet USD; die Oberfläche rechnet mit dem in der Administration hinterlegten Kurs in EUR um (Default: EZB-Referenzkurs vom 05.11.2026). Der Kurs wird nicht automatisch aktualisiert. Keine erfundenen Wirkungszahlen oder offiziellen Institutionspositionen.
- V1 hat einen gemeinsamen geschützten Teamzugang, keine getrennten Benutzerrechte. Öffentliches Hosting, Presse-RLS und Recherchetools während der Verhandlung sind keine Bestandteile dieses Prototyps.
- Chart.js, Tailwind und DaisyUI werden lokal ausgeliefert; kein CDN erforderlich. Chart.js trägt seine MIT-Lizenz im ausgelieferten Bundle.

## Pressespiegel und Rechtstexte

Quellen liegen außerhalb des Repos und werden beim Init-Schritt von `docker compose up` in die Tabelle `source` geladen. Erwartet wird ein Ordner mit `*Pressespiegel*`-Unterordnern voller PDFs und dem Ordner `Rechtstexte` mit den SOZDOK-XML-Dateien. Default ist `../data` neben dem Repo, abweichend `DATA_DIR` in `.env` setzen.

Der erste Start dauert dadurch länger, `web` und `worker` warten auf das Ende des Init-Schritts. Fortschritt: `docker compose logs -f init`. Danach werden nur neue oder geänderte Dateien gelesen, neue PDFs im Ordner genügen also. Fehlt der Ordner, startet die Anwendung ohne Recherche. Ohne Docker: `uv run gilgamosh ingest ../data`, dafür muss `pdftotext` (poppler-utils) installiert sein.

Beim Start erzeugt die Rolle Recherche je Institution Suchbegriffe, durchsucht Presse (12 Treffer, neuere zählen mehr) und Rechtstexte (5 Treffer) und verdichtet die Auszüge zu höchstens 10 Fakten ohne Personenbezug. Das sind zwei LLM-Aufrufe plus Sicherheitsprüfung je Institution. Das Ergebnis steht im Verlauf unter **Recherche** und ist nur für die jeweilige Institution, Orchestrator und Watcher sichtbar. Beiträge verweisen über `quellen` darauf, unbekannte Kennungen werden entfernt. Die Oberfläche zeigt nur die Fundstelle, nie den Artikeltext, weil dieser Personennamen enthält. Grenzen: Die deutsche Volltextsuche zerlegt keine zusammengesetzten Wörter und kennt keine Synonyme; das fangen die Suchbegriffe nur teilweise ab. Die Fakten stammen vom Modell und sind nicht gegen die Quelle verifiziert. Ein Abbruch einer einzelnen Recherche führt zu einer Institution ohne Quellen, nicht zum Stopp.

## Icons

Icons liegen unter `src/assets/icons/<Institutionskennung>.png` und werden über `/icons/<Kennung>.png` nur nach Anmeldung ausgeliefert. Sie erscheinen im Startformular, in der Administration und in der Simulation. Institutionen ohne Datei zeigen ein Kürzel aus ihrem Namen; eine neue PNG-Datei mit passendem Namen genügt, im Container nach `docker compose up --build`. Originaldateien unter `_original/` werden nicht ausgeliefert.

## Stakeholder aus main

Commit `4b17bfd` liefert die 24 Profile in `src/prompts/agents/`. Der Import übernimmt Identität, Selbstverständnis und Rechtsgrundlagen als individuelle Prompt-Ergänzung; rote Linien und priorisierte Positionen werden separat editierbar gespeichert. Den gemeinsamen Ablauf liefert der versionierte Rollenprompt. Dadurch gelangen keine veralteten hart codierten Limits aus den Quelldateien in die Ablaufsteuerung.

`HINTERGRUND.md` ist ausdrücklich nur für menschliche Prüfung und wird nicht an Modelle gesendet. `_statuten_aller_agents.md` wird ebenfalls nicht pauschal importiert: Orchestrator und Watcher erhalten ausschließlich die strukturierten Statuten der tatsächlich ausgewählten Institutionen. Jeder andere Akteur erhält nur die eigenen Statuten.

Quellpfad und SHA-256-Hash dokumentieren die Herkunft. Persönliche Amtsrollen werden beim Import institutionell formuliert; Public Health, Vertrags- und Wahlärzteschaft treten als institutionelle Interessenvertretungen auf. Die originalen Quelldateien bleiben erhalten.

`gilgamosh init` synchronisiert die Profile bei der Initialisierung. Später kann die Administration über **Repository-Profile aktualisieren** oder die CLI über `gilgamosh seed` erneut importieren. Ein Inhaltsfingerabdruck schützt manuelle Änderungen vor automatischem Überschreiben. Bestehende Simulationen behalten ihre ursprünglichen Snapshots.
