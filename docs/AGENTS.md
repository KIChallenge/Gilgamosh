# AGENTS.md – Diskussionsprozess (Meinungs- und Koalitionsphase)

Dieses Dokument beschreibt für den Coding Agent, **was** gebaut werden soll. Es gibt keinen Tech-Stack vor. Sprache, Framework und LLM-Anbieter legt das Team fest.

Fachliche Grundlage:
- `../src/prompts/prozess_spezifikation.md` – vollständige Prozessregeln
- `prompt_agent.md`, `prompt_orchestrator.md`, `prompt_moderator.md`, `prompt_watcher.md` – Systemprompts mit JSON-Ausgabeformaten

Bei Widersprüchen gilt: Prozessspezifikation vor diesem Dokument vor eigenen Annahmen. Unklares nicht raten, sondern als offene Frage markieren.

---

## 1. Ziel

Mehrere LLM-Agents mit unterschiedlichen Interessen (Statuten) diskutieren eine Frage und sollen in höchstens `N` Runden einen Kompromiss finden. Ein Orchestrator prüft jede Agent-Aussage gegen die Statuten, ein Moderator fasst zusammen und schlägt Kompromisse vor, ein Watcher erstellt laufende Zwischenstände für die Oberfläche und am Ende einen Bericht.

Der Code ist die **Ablaufsteuerung (Runtime)**: Er ruft die LLM-Rollen auf, leitet Nachrichten nach festen Regeln weiter, hält den Zustand und erzwingt alle Limits. Die LLMs treffen nur inhaltliche Entscheidungen.

---

## 2. Rollen

| Rolle | Typ | Aufgabe |
|---|---|---|
| Moderator | LLM | Frage stellen, Sammelstand und Kompromissvorschlag erstellen, Abschluss mit Neustart-Empfehlung |
| Agent (1…k) | LLM, je eine Instanz | Vertritt eine Interessenssäule mit Statuten, antwortet, bildet Gruppen, verhandelt, stimmt ab |
| Orchestrator | LLM | Prüft Statements und Stimmen gegen Statuten (Skala 0–2), gibt Gruppen frei, wertet Abstimmung aus |
| Watcher | LLM | Liest den gesamten Verlauf, erstellt am Ende den Abschlussbericht |
| Operator | Mensch | Startet den Prozess, entscheidet nach `N` Runden ohne Konsens über den Neustart, erhält den Watcher-Bericht |

---

## 3. Kommunikationsregeln (hart erzwingen)

Die Runtime ist der einzige Weg, auf dem Nachrichten fließen. LLMs rufen sich **nie** gegenseitig auf.

| Von → An | Moderator | Agents | Orchestrator | Watcher |
|---|---|---|---|---|
| Moderator | – | Frage, Sammelstand, Kompromissvorschlag | ✗ | liest mit |
| Agents | ✗ | nur im freigegebenen Gruppenkanal | Antworten, Gruppenanträge, Stimmen | liest mit |
| Orchestrator | freigegebene Statements, Abstimmungsergebnis | Rückweisungen, Freigaben, freigegebene Einzelantworten | – | liest mit, inkl. interner Begründungen |
| Watcher | ✗ | ✗ | ✗ | – (Bericht nur an Operator) |

### Sichtbarkeit / Kontext je Rolle
Beim Bau des LLM-Kontexts nur das einfügen, was die Rolle sehen darf:

| Daten | Agent | Orchestrator | Moderator | Watcher |
|---|---|---|---|---|
| Eigene Statuten | ✓ | ✓ (alle) | ✗ | ✓ (alle) |
| Fremde Statuten | **✗** | ✓ | **✗** | ✓ |
| DB-Infos der eigenen Interessenssäule | ✓ | ✓ | ✗ | ✓ |
| Freigegebene Statements anderer Agents | ✓ | ✓ | ✓ | ✓ |
| Zurückgewiesene Statements | nur eigene | ✓ | ✗ | ✓ |
| Bewertungen und interne Begründungen des Orchestrators | ✗ (nur `hinweis_an_agent` bzw. `begruendung_an_agents`) | ✓ | ✗ | ✓ |
| Gruppenkanal | nur eigene Gruppe | ✓ | ✗ | ✓ (alle) |
| Watcher-Bericht | ✗ | ✗ | ✗ | – |

**Kritisch:** Statuten dürfen nie in den Kontext eines anderen Agents oder des Moderators gelangen. Das muss die Runtime garantieren und darf sich nicht nur auf Prompt-Anweisungen verlassen. Felder wie `begruendung_intern` des Orchestrators vor der Weitergabe entfernen.

---

## 4. Parameter

| Name | Wert | Bedeutung |
|---|---|---|
| `N` | 6 | Höchstzahl aller Runden pro Rundenblock |
| `MAX_RETRY` | 3 | Versuche pro Statement; danach Enthaltung (wird protokolliert, nicht weitergeleitet) |
| `MAX_NACHRICHTEN` | 3 | Verhandlungsnachrichten pro Agent und Gruppenphase; weitere werden nicht zugestellt |

Alle Parameter konfigurierbar machen, nicht hart codieren.

---

## 5. Ablauf als Zustandsmaschine

```
START
 └─ FRAGE            Moderator.frage_stellen → Frage + DB-Infos an jeden Agent
RUNDE (r = 1 … N)
 ├─ EINZELPHASE      je Agent: Agent.einzelphase → Orchestrator.statement_pruefen
 │                   Bewertung 0 → Hinweis an Agent, neuer Versuch (≤ MAX_RETRY), sonst Enthaltung
 ├─ VERTEILUNG       Runtime verteilt freigegebene Positionen an alle Agents (ohne Statuten)
 ├─ KOALITION        je Agent: Agent.koalitionsphase → Orchestrator.gruppe_pruefen
 │                   Freigabe → Kanal anlegen; Austritt → wieder Einzelgruppe
 ├─ GRUPPENPHASE     je Gruppe: Verhandlung im Kanal (≤ MAX_NACHRICHTEN je Agent)
 │                   dann je Agent: Einzelstatement → Orchestrator.statement_pruefen (wie oben)
 ├─ WEITERLEITUNG    Runtime leitet freigegebene Statements an den Moderator
 ├─ VORSCHLAG        Moderator.sammelstand_und_vorschlag → an alle Agents
 ├─ ABSTIMMUNG       je Agent: Agent.abstimmung → Orchestrator.statement_pruefen
 │                   → Runtime wertet die Abstimmung aus
 └─ ENTSCHEIDUNG     konsens = true          → ABSCHLUSS (konsens)
                     r < N                   → nächste Runde mit Sammelstand
                     r = N                   → ABSCHLUSS (teilkompromiss)
ABSCHLUSS
 ├─ Moderator.abschluss
 ├─ Watcher.abschlussbericht → Operator
 └─ nur bei Teilkompromiss: Operator wählt
      FORTSETZEN   Teilkompromiss = neuer Sammelstand, Gruppen + Kanäle bleiben,
                   neuer Rundenblock bei r = 1 mit vorhandenem Sammelstand, Watcher-Protokoll läuft weiter
      RESET        alten Durchlauf archivieren, aktive Kontexte und Gruppen zurücksetzen, Start bei FRAGE,
                   neues Watcher-Protokoll (altes archivieren),
                   Agent-Reihenfolge oder Temperature variieren
      ENDE         Prozess beenden
```

### Regeln, die die Runtime prüft (nicht nur das LLM)
- Ein Agent ist immer in **genau einer** Gruppe. Initial ist jeder Agent seine eigene Einzelgruppe.
- Gruppen geben **nur Einzelstatements** ab, nie ein gemeinsames Statement.
- `ablehnen` ist nur gültig, wenn der Orchestrator eine verletzte rote Linie (`R…`) oder eine wesentlich beschädigte Priorität 1 oder 2 (`P1`, `P2`) bestätigt. Sonst wird die Stimme zurückgewiesen (zählt als Retry). Eine Zustimmung oder Tolerierung zu einem Vorschlag, der Finanzierung, Entscheidungsbefugnis oder Frist offenlässt, obwohl dies eine rote Linie oder Priorität 1 oder 2 betrifft, wird als Schein-Zustimmung zurückgewiesen.
- **Konsens** = keine gültige Stimme `ablehnen`, keine Enthaltung und mindestens die Hälfte der Institutionen stimmt mit `zustimmen`. Tolerierungen allein tragen keinen Konsens; eine Institution, deren Stimme wiederholt zurückgewiesen wurde (Enthaltung), hat nicht zugestimmt.
- In Runde 1 gibt es keinen Sammelstand; `tragbar` ist dann `null`.

---

## 6. Datenmodell (Vorschlag)

```
Session        id, frage, parameter, status, runde, neustart_zaehler, created_at
Agent          id, name, interessenssaeule, statuten {rote_linien[], verhandelbar[{position, prioritaet}]}, db_infos
Gruppe         id, mitglieder[agent_id], kanal_id, aktiv, erstellt_in_runde
Nachricht      id, session_id, runde, phase, von_rolle, von_id, an (Rolle/Gruppe/alle), typ, payload (JSON), versuch, created_at
Pruefung       nachricht_id, bewertung (0|1|2), entscheidung, verletzte_rote_linie, hinweis_an_agent, begruendung_intern
Abstimmung     session_id, runde, agent_id, stimme, gueltig
Bericht        session_id, protokoll_nr, typ (moderator_abschluss|watcher), payload
```

Jede Nachricht und jede Prüfung wird **unveränderlich** gespeichert (append-only). Daraus entsteht der Kontext für den Watcher.

---

## 7. LLM-Aufrufe

- Jeder Aufruf = Systemprompt der Rolle + Aufgabe/Phase im Feld `phase` bzw. `aufgabe` + der nach Abschnitt 3 gefilterte Kontext.
- Platzhalter in den Prompts (`{{AGENT_NAME}}`, `{{ROTE_LINIEN}}`, `{{STATUTEN_ALLER_AGENTS}}`, `{{N}}`, `{{RUNDE}}`, `{{MAX_RETRY}}`, `{{MAX_NACHRICHTEN}}` …) vor dem Aufruf ersetzen.
- Antworten sind **ausschließlich JSON** nach den Formaten in den Prompt-Dateien. Gegen ein Schema validieren.
  - Ungültiges JSON: einmal mit Fehlerhinweis neu anfragen. Dieser technische Retry zählt **nicht** zu `MAX_RETRY`.
  - Bleibt es ungültig: als Enthaltung behandeln und protokollieren.
- Agents innerhalb einer Phase dürfen parallel aufgerufen werden, wenn die Reihenfolge fachlich egal ist (Einzelphase, Abstimmung). In der Gruppenverhandlung reihum (Round-Robin) und nachvollziehbar.
- Bei Reset Reihenfolge der Agents bzw. Temperature variieren, damit nicht dasselbe Ergebnis entsteht.

---

## 8. Operator-Schnittstelle (Minimum)

- Session anlegen: Frage, Agents mit Statuten und DB-Infos, Parameter
- Fortschritt ansehen: aktuelle Runde und Phase, Gruppen, freigegebene Statements
- Nach Teilkompromiss: Auswahl `FORTSETZEN` / `RESET` / `ENDE`
- Watcher-Bericht und Moderator-Abschluss anzeigen bzw. exportieren

---

## 9. Tests (Mindestumfang)

- **Vertraulichkeit:** Kein Agent- oder Moderator-Kontext enthält fremde Statuten oder `begruendung_intern`.
- **Retry-Limit:** Nach `MAX_RETRY` Rückweisungen → Enthaltung, kein weiterer Aufruf.
- **Nachrichtenlimit:** Die 4. Verhandlungsnachricht eines Agents wird nicht zugestellt.
- **Gruppen:** Agent kann nie in zwei Gruppen sein; Austritt → Einzelgruppe.
- **Konsens:** Eine gültige Ablehnung oder eine Enthaltung verhindert Konsens; Tolerierungen zählen nicht als Zustimmung.
- **Rundenlimit:** Nach Runde `N` ohne Konsens → Teilkompromiss, keine weitere Runde ohne Operator.
- **Fortsetzen:** Gruppen und Kanäle bleiben, Rundenzähler zurückgesetzt, Watcher-Protokoll läuft weiter.
- **Reset:** Alter Durchlauf archiviert; aktive Kontexte leer, Einzelgruppen; Frage, DB-Infos und Statuten bleiben.
- LLM-Aufrufe für Tests mocken (feste JSON-Antworten), damit der Ablauf deterministisch testbar ist.

---

## 10. Verbindliche Prototyp-Entscheidungen

- `N` hat Default 6 und zählt sämtliche Runden eines Blocks; alle Limits sind konfigurierbar.
- `MAX_RETRY` zählt insgesamt höchstens 3 fachliche Versuche, nicht 3 zusätzliche Wiederholungen.
- `MAX_NACHRICHTEN` gilt pro Institution und Gruppenphase.
- Gruppen benötigen Zustimmung sämtlicher vorgesehenen Mitglieder und Orchestrator-Freigabe.
- Reset archiviert unveränderliche Nachrichten; nur aktive Kontexte werden geleert.
- Der Watcher erstellt nach jeder Phase einen Zwischenstand ausschließlich für die Bedienoberfläche; niemals Rückkopplung an die verhandelnden Rollen.
- Alle Akteure sind Institutionen oder institutionelle Interessenvertretungen. Keine Einzelpersonen, Personennamen, persönlichen Zuschreibungen oder Personenzitate in veröffentlichten Inhalten.
- Jeder Stakeholder hat einen eigenen begrenzten Kontext; Prüfrollen sehen je Aufgabe nur die betroffenen Statuten. Details: `docs/development.md`.
- Der erste Prototyp nutzt vorbereitete Daten und Systemprompts; Presse-Ingest und Recherchetools sind spätere Ausbaustufen.
