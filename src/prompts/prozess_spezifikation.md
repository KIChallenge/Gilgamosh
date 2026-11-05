# Prozessspezifikation – Meinungs- und Koalitionsphase

## Rollen und Kommunikationsrichtungen

| Von → An | Moderator | Agents | Orchestrator | Watcher |
|---|---|---|---|---|
| **Moderator** | – | Frage, Sammelstand, Kompromissvorschlag | ✗ | liest mit |
| **Agents** | ✗ | nur im freigegebenen Gruppenkanal | Antworten, Gruppenanträge, Stimmen | liest mit |
| **Orchestrator** | freigegebene Statements, Abstimmungsergebnis | Rückweisungen, Freigaben, freigegebene Einzelantworten aller Agents | – | liest mit, inkl. interner Begründungen |
| **Watcher** | ✗ | ✗ | ✗ | – (Abschlussbericht nur an den Operator) |

- Der Orchestrator kennt alle Statuten und gibt **niemals** fremde Statuten weiter.
- Der Moderator erhält ausschließlich vom Orchestrator geprüfte Inhalte.
- Der **Watcher** liest den gesamten Verlauf mit: alle Nachrichten, Statuten, interne Begründungen des Orchestrators, Rückweisungen, Gruppenkanäle und Abstimmungen. Er sendet nichts in den Prozess. Sein Abschlussbericht geht nur an den Operator, nie an die Agents.

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
| `N` (max. Runden insgesamt pro Block) | 6 (konfigurierbar) |
| `MAX_RETRY` (Versuche insgesamt pro Statement) | 3 – danach Enthaltung, wird protokolliert |
| `MAX_NACHRICHTEN` (Verhandlungsnachrichten pro Agent und Gruppenphase) | 3 |

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
6. **Gruppenphase:** Verhandlung im Gruppenkanal, jeder Agent mit höchstens `MAX_NACHRICHTEN` Nachrichten. Danach gibt jeder Agent ein **Einzelstatement** ab. Orchestrator prüft jedes Statement einzeln.
7. Orchestrator leitet freigegebene Statements an den Moderator weiter.
8. **Moderator** erstellt Sammelstand und **Kompromissvorschlag**.
9. **Abstimmung:** Agents stimmen über den Orchestrator ab: `zustimmen` / `tolerieren` / `nachbessern` / `ablehnen`. `nachbessern` verlangt eine konkrete Änderung ohne verletzte rote Linie und führt zu einer weiteren Runde. Der Orchestrator prüft die Stimmen gegen die Statuten (eine Zustimmung, die eine rote Linie verletzt, oder eine Schein-Zustimmung zu einem Vorschlag ohne konkrete Regelung von Finanzierung, Entscheidungsbefugnis oder Frist wird zurückgewiesen).
   - **Konsens** = keine gültige Ablehnung, keine Nachbesserung, keine Enthaltung und mindestens die Hälfte der Institutionen stimmt mit `zustimmen` (Tolerierungen allein genügen nicht) → Ergebnis → **Watcher-Bericht**.

### Runde 2 bis N
1. Sammelstand + Kompromissvorschlag der Vorrunde gehen an alle Agents.
2. Einzelphase: Agent prüft, ob der Stand für ihn tragbar ist → Antwort an Orchestrator.
3. Koalitionsphase: Gruppen dürfen neu gebildet, gewechselt oder aufgelöst werden (Austritt ⇒ wieder Einzelgruppe).
4. Gruppenphase, Prüfung, Moderator, Abstimmung wie in Runde 1.

### Ende ohne Konsens nach N Runden
- Ausgabe des **Teilkompromisses** inkl. Zustimmungen, Tolerierungen, Nachbesserungen und dokumentierter Ablehnungen.
- Der **Watcher** legt seinen Abschlussbericht an den Operator vor.
- Ein **Mensch/Operator** wählt:
  - **Fortsetzen mit Stand:** Teilkompromiss wird neuer Sammelstand, **Gruppen und Kanäle bleiben bestehen**, neuer Rundenblock bei Runde 1 mit vorhandenem Sammelstand. Der Watcher führt sein Protokoll weiter.
  - **Kompletter Reset:** Alle Verläufe, Gruppen und Kanäle des alten Durchlaufs werden archiviert und aus dem aktiven Kontext entfernt; Start bei Runde 1 nur mit Frage und DB-Infos. Der Watcher beginnt ein neues Protokoll, das alte bleibt erhalten. Empfehlung: Agent-Reihenfolge oder Temperature variieren.

## Verbindliche Ergänzungen für V1

- Alle Akteure sind ausschließlich Institutionen oder institutionelle Interessenvertretungen. Keine Einzelpersonen als Akteure, keine Personennamen, Personenzitate oder persönlichen Zuschreibungen in Beiträgen und Berichten.
- Gruppen benötigen explizite Zustimmung aller vorgesehenen Mitglieder vor der Orchestrator-Prüfung. Austritt ist jederzeit in der Koalitionsphase möglich; danach Einzelgruppe. Konkurrierende Anträge werden in gespeicherter Institutionsreihenfolge bearbeitet und nach relevanten Mitgliedschaftsänderungen verworfen.
- Gruppen verhandeln reihum mit höchstens MAX_NACHRICHTEN zugestellten Nachrichten pro Institution. Einzelgruppen erstellen ohne internen Dialog ein eigenes Statement.
- Der Watcher erstellt zusätzlich nach jeder abgeschlossenen Phase eine belegte Zusammenfassung für das Seitenpanel „Aktueller Stand“. Er greift nie in die Verhandlung ein. Die Zusammenfassungen werden nie als Agentenkontext verwendet.
- Reset bewahrt sämtliche alten Nachrichten, Prüfungen, Trajectories und Berichte im Archiv; neue Kontexte enthalten keine alten Nachrichten.
- Eine Ablehnung ist gültig, wenn der Orchestrator eine verletzte rote Linie oder eine wesentlich beschädigte Priorität 1 oder 2 (`P1`, `P2`) der Institution benennt. Die Kennung bleibt intern, veröffentlicht wird nur die neutrale Begründung. Eine bestätigte Ablehnung kann nicht durch eine Enthaltung ersetzt werden.
- Jeder Stakeholder erhält einen eigenen begrenzten Kontext statt des Gesamtverlaufs: eigener Verlauf, letzte Positionen der anderen, eigener Gruppenkanal, Sammelstand und letzte Abstimmung. Prüfrollen erhalten je Aufgabe nur die betroffenen Statuten. Der Watcher liest den Verlauf schrittweise. Die Gesamtdaten bleiben in Ereignissen und Aufrufprotokollen vollständig erhalten.
- Vor Runde 1 erstellt eine Rechercherolle je Institution ein Briefing aus Pressespiegel und Rechtstexten: Volltextsuche, neuere Presse zählt mehr, höchstens 10 personenfreie Fakten mit Quellenkennung. Es bildet die DB-Infos dieser Institution, bleibt für den Lauf fix und ist für andere Agenten und den Moderator nicht sichtbar. Ohne geladene Quellen entfällt der Schritt.
- Fachliche Limits erzwingt die Runtime. Über ungültige Steuerrollen-Ausgaben oder Providerfehler darf sie keinen Konsens erfinden.
- Technische Aufrufprotokolle sind ein geschützter interner Bereich für ungeprüfte Rohdaten; reguläre Ansichten und Exporte enthalten ausschließlich geprüfte Beiträge.
