# RoundTable

Webbasierter Prototyp für institutionelle Verhandlungen im österreichischen Gesundheitswesen. Flask, Tailwind/DaisyUI, PostgreSQL/SQLAlchemy, separater Worker und OpenRouter.

- 24 modellierte Stakeholder-Profile inklusive neun Landesinstitutionen
- Recherche in Pressespiegel und Rechtstexten als Briefing je Institution mit Quellenangabe
- Einzelpositionen, Koalitionen, Gruppenverhandlungen, Prüfung und Abstimmung
- Live-Verlauf als aufklappbares Flussdiagramm, Zeitstrahl und laufendes Watcher-Seitenpanel
- Persistente Trajectories, Pause/Fortsetzen, archivierende Resets und Ergebnisexporte
- Default: sechs Runden; ausschließlich institutionelle Akteure, keine Einzelpersonen

Start und Konfiguration: **[docs/development.md](docs/development.md)**. Nach Konfiguration der `.env` startet `docker compose up --build -d` die Anwendung unter http://localhost:5000.

`LLM_MODE=fake` erlaubt eine gekennzeichnete Offline-Demo ohne API-Kosten. Für echte Calls `LLM_MODE=openrouter` setzen und eine Verbindung in der Administration konfigurieren. Ein OpenRouter-Key reicht für sämtliche Modelle und Rollen. Die mitgelieferten Profile sind Entwürfe, keine offiziellen Institutionspositionen.
