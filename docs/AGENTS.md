# AGENTS.md – Diskussionsprozess (Meinungs- und Koalitionsphase)

Dieses Dokument beschreibt für den Coding Agent, **was** gebaut werden soll. Es gibt keinen Tech-Stack vor. Sprache, Framework und LLM-Anbieter legt das Team fest.

Fachliche Grundlage:
- `prozess_spezifikation.md` – vollständige Prozessregeln
- `prompt_agent.md`, `prompt_orchestrator.md`, `prompt_moderator.md`, `prompt_watcher.md` – Systemprompts mit JSON-Ausgabeformaten

Bei Widersprüchen gilt: Prozessspezifikation vor diesem Dokument vor eigenen Annahmen. Unklares nicht raten, sondern als offene Frage markieren.

---

## 1. Ziel

Mehrere LLM-Agents mit unterschiedlichen Interessen (Statuten) diskutieren eine Frage und sollen in höchstens `N` Runden einen Kompromiss finden. Ein Orchestrator prüft jede Agent-Aussage gegen die Statuten, ein Moderator fasst zusammen und schlägt Kompromisse vor, ein Watcher protokolliert alles und schreibt am Ende einen Bericht.

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
| `N` | Anzahl der initial festgelegten Agents | Höchstzahl der Runden ab Runde 2 |
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
 ├─ VERTEILUNG       Orchestrator.verteilung_einzelantworten → an alle Agents (ohne Statuten)
 ├─ KOALITION        je Agent: Agent.koalitionsphase → Orchestrator.gruppe_pruefen
 │                   Freigabe → Kanal anlegen; Austritt → wieder Einzelgruppe
 ├─ GRUPPENPHASE     je Gruppe: Verhandlung im Kanal (≤ MAX_NACHRICHTEN je Agent)
 │                   dann je Agent: Einzelstatement → Orchestrator.statement_pruefen (wie oben)
 ├─ WEITERLEITUNG    Orchestrator.weiterleitung_moderator → Moderator
 ├─ VORSCHLAG        Moderator.sammelstand_und_vorschlag → an alle Agents
 ├─ ABSTIMMUNG       je Agent: Agent.abstimmung → Orchestrator.statement_pruefen
 │                   → Orchestrator.abstimmung_auswerten
 └─ ENTSCHEIDUNG     konsens = true          → ABSCHLUSS (konsens)
                     r < N                   → nächste Runde mit Sammelstand
                     r = N                   → ABSCHLUSS (teilkompromiss)
ABSCHLUSS
 ├─ Moderator.abschluss
 ├─ Watcher.abschlussbericht → Operator
 └─ nur bei Teilkompromiss: Operator wählt
      FORTSETZEN   Teilkompromiss = neuer Sammelstand, Gruppen + Kanäle bleiben,
                   r zurückgesetzt, weiter bei Runde 2, Watcher-Protokoll läuft weiter
      RESET        Verläufe, Gruppen, Kanäle löschen, Start bei FRAGE,
                   neues Watcher-Protokoll (altes archivieren),
                   Agent-Reihenfolge oder Temperature variieren
      ENDE         Prozess beenden
```

### Regeln, die die Runtime prüft (nicht nur das LLM)
- Ein Agent ist immer in **genau einer** Gruppe. Initial ist jeder Agent seine eigene Einzelgruppe.
- Gruppen geben **nur Einzelstatements** ab, nie ein gemeinsames Statement.
- `ablehnen` ist nur gültig, wenn der Orchestrator eine verletzte rote Linie bestätigt. Sonst wird die Stimme zurückgewiesen (zählt als Retry).
- **Konsens** = keine gültige Stimme `ablehnen`. Enthaltungen blockieren nicht.
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
- **Konsens:** Eine gültige Ablehnung verhindert Konsens; Enthaltungen nicht.
- **Rundenlimit:** Nach Runde `N` ohne Konsens → Teilkompromiss, keine weitere Runde ohne Operator.
- **Fortsetzen:** Gruppen und Kanäle bleiben, Rundenzähler zurückgesetzt, Watcher-Protokoll läuft weiter.
- **Reset:** Alles gelöscht außer Frage, DB-Infos, Statuten; neues Watcher-Protokoll.
- LLM-Aufrufe für Tests mocken (feste JSON-Antworten), damit der Ablauf deterministisch testbar ist.

---

## 10. Offener Punkt

- `MAX_NACHRICHTEN` = 3 ist aktuell als **pro Agent** und Gruppenphase umgesetzt. Bestätigung durch das Team ausstehend; die Zählung sollte leicht auf „pro Gruppe“ umstellbar sein.
