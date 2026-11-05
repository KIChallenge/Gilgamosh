# Prozessspezifikation – Meinungs- und Koalitionsphase

## Rollen und Kommunikationsrichtungen

| Von → An | Moderator | Agents | Orchestrator |
|---|---|---|---|
| **Moderator** | – | Frage, Sammelstand, Kompromissvorschlag | ✗ |
| **Agents** | ✗ | nur im freigegebenen Gruppenkanal | Antworten, Gruppenanträge, Stimmen |
| **Orchestrator** | freigegebene Statements, Abstimmungsergebnis | Rückweisungen, Freigaben, freigegebene Einzelantworten aller Agents | – |

- Der Orchestrator kennt alle Statuten und gibt **niemals** fremde Statuten weiter.
- Der Moderator erhält ausschließlich vom Orchestrator geprüfte Inhalte.

## Statuten

Jeder Agent hat:
- **Rote Linien** – nicht verhandelbar. Verletzung ⇒ Rückweisung durch den Orchestrator.
- **Verhandelbare Positionen** – Zugeständnisse sind erlaubt.

Bewertungsskala des Orchestrators pro Statement:
- `0` – verletzt mindestens eine rote Linie → Rückweisung
- `1` – Zugeständnis bei verhandelbarer Position → Freigabe
- `2` – vollständig statutenkonform → Freigabe

## Parameter

| Parameter | Wert |
|---|---|
| `N` (max. Runden ab Runde 2) | Anzahl der initial festgelegten Agents |
| `MAX_RETRY` (Neuversuche pro Statement) | 3 – danach Enthaltung, wird protokolliert |

## Ablauf

### Runde 1
1. **Frage** wird an den Moderator gestellt; Moderator gibt sie an alle Agents.
2. Jeder Agent erhält die für seine Interessenssäule spezifischen **DB-Infos**.
3. **Einzelphase:** Agent → Antwort an Orchestrator → Prüfung (Skala). Bei `0`: Hinweis auf die verletzte rote Linie, neuer Versuch (max. `MAX_RETRY`).
4. Orchestrator verteilt alle **freigegebenen Einzelantworten** an alle Agents (ohne Statuten).
5. **Koalitionsphase:**
   - Jeder Agent ist initial eine eigene Einzelgruppe.
   - Agents stellen Gruppenanträge. Ein Agent darf nur in **einer** Gruppe sein.
   - Orchestrator gibt eine Gruppe frei, wenn sich die roten Linien der Mitglieder nicht widersprechen und jedes Mitglied seinen Statuten treu bleibt.
   - Freigegebene Gruppen erhalten einen eigenen Kommunikationskanal.
6. **Gruppenphase:** Verhandlung im Gruppenkanal; jeder Agent gibt anschließend ein **Einzelstatement** ab. Orchestrator prüft jedes Statement einzeln.
7. Orchestrator leitet freigegebene Statements an den Moderator weiter.
8. **Moderator** erstellt Sammelstand und **Kompromissvorschlag**.
9. **Abstimmung:** Agents stimmen über den Orchestrator ab: `zustimmen` / `tolerieren` / `ablehnen`. Der Orchestrator prüft die Stimmen gegen die Statuten (eine Zustimmung, die eine rote Linie verletzt, wird zurückgewiesen).
   - **Konsens** = keine Ablehnung → Ergebnis.

### Runde 2 bis N
1. Sammelstand + Kompromissvorschlag der Vorrunde gehen an alle Agents.
2. Einzelphase: Agent prüft, ob der Stand für ihn tragbar ist → Antwort an Orchestrator.
3. Koalitionsphase: Gruppen dürfen neu gebildet, gewechselt oder aufgelöst werden (Austritt ⇒ wieder Einzelgruppe).
4. Gruppenphase, Prüfung, Moderator, Abstimmung wie in Runde 1.

### Ende ohne Konsens nach N Runden
- Ausgabe des **Teilkompromisses** inkl. Zustimmungen, Tolerierungen und dokumentierter Ablehnungen.
- Ein **Mensch/Operator** wählt:
  - **Fortsetzen mit Stand:** Teilkompromiss wird neuer Sammelstand, Start bei Runde 2, Rundenzähler zurückgesetzt.
  - **Kompletter Reset:** Alle Verläufe, Gruppen und Kanäle werden gelöscht; Start bei Runde 1 nur mit Frage und DB-Infos. Empfehlung: Agent-Reihenfolge oder Temperature variieren.

## Offene Implementierungsentscheidung
- Bleiben bei „Fortsetzen mit Stand“ die bestehenden Gruppen erhalten oder werden alle auf Einzelgruppen zurückgesetzt?
