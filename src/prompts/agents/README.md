# Agenten-Statuten und Systemprompts (HealthPolicy Simulator, UC2)

Dieser Ordner enthält (ohne Generator, alles fest hinterlegt) für jeden Stakeholder-Agenten einen fertigen Systemprompt im Format von
`../prompt_agent.md`: Interessenssäule, rote Linien (nicht verhandelbar) und verhandelbare Positionen
mit Priorität.

**Wichtig:** Alle Positionen sind **modellierte Entwürfe**. Sie wurden aus den bereitgestellten Rechtstexten
und dem Pressespiegel 2026 (Jänner bis Mai) abgeleitet. Sie sind keine offiziellen Statuten der
genannten Organisationen und müssen von Fachleuten geprüft werden.

## Dateien

| Datei | Zweck |
|---|---|
| `<code>.md` (24 Stück) | Fixer Systemprompt je Agent mit fest eingetragenen Statuten. Direkt als Systemprompt verwendbar und von Hand editierbar. |
| `_statuten_aller_agents.md` | Wert für `{{STATUTEN_ALLER_AGENTS}}` im Orchestrator-Prompt. Vertraulich, nie an Agents oder Moderator geben. |
| `HINTERGRUND.md` | Belege aus dem Pressespiegel und Konfliktfelder je Agent, nur zur menschlichen Prüfung. Nicht Teil der Prompts. |

Die Prompts enthalten bewusst **keine** Konfliktfelder und Belege, damit kein Agent indirekt etwas über
die Statuten anderer Agents erfährt (siehe `docs/AGENTS.md`, Abschnitt 3).

## Agenten

15 bundesweite Agenten und 9 Landes-Agenten (`land_*`, Elternteil `bundeslaender`):

`sozialversicherung`, `gesundheitsministerium`, `aerztekammer`, `vertragsaerzte`, `wahlaerzte`,
`krankenanstalten`, `pflege`, `patientenvertretung`, `gesetzgeber`, `finanzministerium`, `public_health`,
`wko`, `ak`, `lk`, `industrie`, `land_wien`, `land_noe`, `land_bgld`, `land_ktn`, `land_stmk`, `land_ooe`,
`land_sbg`, `land_tirol`, `land_vbg`.

Abweichung von der ursprünglichen Stakeholder-Liste: "Wirtschaftskammer & Arbeiterkammer &
Landwirtschaftskammer" ist in **drei** Agenten (`wko`, `ak`, `lk`) getrennt, weil ihre Interessen
gegensätzlich sind, und "Bundesländer" in neun Landes-Agenten. Für die Datenbank (`schema.sql`) heißt das:
`sozialpartner` wird zu `wko`, `ak`, `lk`; `bundeslaender` bleibt als Elternknoten mit den neun `land_*`.

## Aufbau eines Agent-Prompts

1. Kopf und Grundregeln aus `prompt_agent.md` (unverändert).
2. `## Dein Selbstverständnis` und `## Deine Rechtsgrundlagen` (fest eingetragen).
3. `## Deine Statuten`: `{{ROTE_LINIEN}}` als `R1`, `R2`, ... und `{{VERHANDELBARE_POSITIONEN}}` nach Priorität.
4. Phasen und JSON-Ausgabeformate aus `prompt_agent.md`.

Die Rechtsgrundlagen sind in zwei Gruppen getrennt: **geprüft** (Paragraph im Rechtstexte-Ordner gelesen)
und **extern** (aus Fachwissen, nicht geprüft). Der Prompt weist den Agenten an, nur genannte Paragraphen
zu zitieren.

## Grenzen der Rechtstexte

Der Ordner `Rechtstexte` ist ein Export des Sozialversicherungsrechts: ASVG, GSVG, BSVG, B-KUVG, BPGG,
GesRefFinG, Erstattungskodex und VO-EKO, Satzungen, Krankenordnungen, Geschäftsordnungen und
Sozialversicherungsabkommen. **Nicht enthalten** sind ÄrzteG, KAKuG, GuKG, G-ZG, B-VG, Finanzausgleichs-
und Haushaltsrecht sowie die Kammergesetze. Solche Normen stehen in den Prompts unter
"extern" und sollten nachgeprüft werden.

Der "Ärzte-Gesamtvertrag" im Ordner (`OEGK_Aerzte-GV`) ist nur ein Verzeichnis der Zusatzprotokolle, ohne
Vertragstext.

## Offene Punkte

- Fachliche Prüfung der roten Linien durch Expert:innen der jeweiligen Säule.
- Ob die Landes-Agenten wirklich neun getrennte Agenten sein sollen oder eine Auswahl pro Simulation.
- Einzelne Länder-Positionen stützen sich nur auf wenige Pressemeldungen (siehe `HINTERGRUND.md`).
