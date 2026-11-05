'use strict';
(() => {
  const root = document.querySelector('#simulation');
  if (!root) return;
  const base = `/api/simulations/${root.dataset.id}`;
  const $ = selector => document.querySelector(selector);

  const STATUS = {
    running: ['Läuft', 'pill-live'], paused: ['Pausiert', 'pill-warn'], completed: ['Abgeschlossen', 'pill-good'],
    awaiting_decision: ['Entscheidung erforderlich', 'pill-warn'], ended: ['Beendet', 'pill-neutral']
  };
  const PHASES = {start: 'Vorbereitung', question: 'Frage', research: 'Recherche', individual: 'Einzelpositionen', coalition: 'Koalitionen', groups: 'Gruppenverhandlung', proposal: 'Kompromissvorschlag', vote: 'Abstimmung', closing: 'Abschluss', done: 'Abgeschlossen'};
  const PHASE_ORDER = ['start', 'question', 'research', 'individual', 'coalition', 'groups', 'proposal', 'vote', 'closing', 'done'];
  const TASKS = {
    suchbegriffe: 'Sucht in den Quellen', briefing: 'Erstellt Briefing', einzelphase: 'Formuliert Position', statement_pruefen: 'Wird geprüft', sicherheit: 'Inhalt wird geprüft',
    koalitionsphase: 'Prüft Koalitionen', zustimmung: 'Prüft Gruppenbeitritt', gruppennachricht: 'Verhandelt',
    gruppenstatement: 'Formuliert Position', abstimmung: 'Stimmt ab', frage_stellen: 'Formuliert Frage',
    gruppe_pruefen: 'Gruppe wird geprüft', sammelstand_und_vorschlag: 'Entwickelt Vorschlag',
    zwischenstand: 'Aktualisiert den Stand', abschluss: 'Erstellt Abschluss', abschlussbericht: 'Erstellt Abschlussbericht'
  };
  // Validated with the dataviz palette check; text labels and glyphs carry identity alongside color.
  const VOTES = {
    zustimmen: {label: 'Zustimmung', glyph: '✓', color: '#1baf7a', cls: 'marker-yes'},
    tolerieren: {label: 'Tolerierung', glyph: '~', color: '#2a78d6', cls: 'marker-tolerate'},
    nachbessern: {label: 'Nachbesserung', glyph: '↻', color: '#eda100', cls: 'marker-revise'},
    ablehnen: {label: 'Ablehnung', glyph: '✕', color: '#e34948', cls: 'marker-no'},
    enthaltung: {label: 'Enthaltung', glyph: '○', color: '#898781', cls: 'marker-abstain'}
  };
  const TIMELINE_STEPS = [['individual', 'Einzelposition'], ['coalition', 'Koalition'], ['groups', 'Gruppenverhandlung'], ['proposal', 'Kompromissvorschlag'], ['vote', 'Abstimmung']];

  let state, metrics, refreshing = false, again = false, selected = 'overview', timer = null, lastMetrics = 0;
  let timelineKey = '', watcherKey = '', analysisKey = '';
  const charts = {};

  const node = (tag, text = '', cls = '') => {const n = document.createElement(tag); n.textContent = text; n.className = cls; return n;};
  const clear = selector => {const n = $(selector); n.replaceChildren(); return n;};
  const spinner = text => {
    const n = node('span', '', 'inline-flex items-center gap-2');
    const spin = node('span', '', 'loading loading-spinner loading-xs text-teal-700');
    spin.setAttribute('aria-hidden', 'true');
    n.append(spin, node('span', text));
    return n;
  };
  const institution = code => state.actors[code]?.name || code || 'System';
  const textOf = p => p.position || p.statement || p.inhalt || p.kompromissvorschlag || p.ergebnis || p.frage || p.begruendung || '';
  const euro = value => value.toLocaleString('de-AT', {style: 'currency', currency: 'EUR', minimumFractionDigits: value < 1 ? 4 : 2, maximumFractionDigits: value < 1 ? 4 : 2});
  const shorten = (text, limit = 220) => text.length > limit ? `${text.slice(0, limit)} …` : text;
  const actorIcon = (code, extra = '') => {
    const actor = state.actors[code];
    if (actor?.icon) {
      const image = node('img', '', `inst-icon ${extra}`);
      image.src = actor.icon; image.alt = '';
      return image;
    }
    const mark = node('span', actor?.monogram || '', `inst-icon inst-monogram ${extra}`);
    mark.setAttribute('aria-hidden', 'true');
    return mark;
  };

  /* ---------- Header and status ---------- */
  function renderHeader() {
    const [statusLabel, statusStyle] = STATUS[state.status];
    const pill = $('#phase-status');
    pill.textContent = statusLabel; pill.className = `pill ${statusStyle}`;
    $('#question').textContent = state.question;
    $('#phase-detail').textContent = `Runde ${state.round} von max. ${state.params.rounds}${state.phase === 'done' ? '' : ` · ${state.phase_label}`}`;
    $('#error').textContent = state.error; $('#error').classList.toggle('hidden', !state.error);
    $('#decision').classList.toggle('hidden', state.status !== 'awaiting_decision');
    $('#pause').classList.toggle('hidden', state.status !== 'running');
    $('#resume').classList.toggle('hidden', state.status !== 'paused');
    $('#end').classList.toggle('hidden', ['ended', 'completed', 'awaiting_decision'].includes(state.status));
    const current = PHASE_ORDER.indexOf(state.phase);
    for (const item of document.querySelectorAll('[data-phase]')) {
      const index = PHASE_ORDER.indexOf(item.dataset.phase);
      item.classList.toggle('phase-done', index < current);
      item.classList.toggle('phase-current', index === current);
    }
    const activity = clear('#active-status');
    const active = state.active_calls || [];
    if (state.status === 'running' && state.worker_alive) activity.append(spinner(`${active.length} ${active.length === 1 ? 'Aufruf' : 'Aufrufe'} aktiv`));
    else activity.textContent = state.status === 'running' ? 'Wartet auf Worker' : '';
    const outcome = $('#outcome');
    const ballot = [...state.events].reverse().find(e => e.kind === 'ballot');
    const finished = ['completed', 'awaiting_decision', 'ended'].includes(state.status) && state.phase === 'done';
    outcome.classList.toggle('hidden', !finished || !state.outcome);
    if (finished && state.outcome) {
      const approving = ballot?.payload.zustimmend ?? ballot?.payload.tragend;
      const support = ballot?.payload.gesamt ? ` ${approving} von ${ballot.payload.gesamt} Institutionen stimmen zu.` : '';
      outcome.textContent = (state.outcome === 'konsens' ? 'Konsens erreicht.' : 'Kein Konsens: Teilkompromiss mit offenen Punkten.') + support;
      outcome.className = `notice mb-4 ${state.outcome === 'konsens' ? 'notice-good' : 'notice-info'}`;
    }
    $('#worker-status').textContent = state.worker_alive ? 'Worker aktiv' : state.status === 'running' ? 'Kein aktiver Arbeitsschritt erkannt' : '';
    const last = state.events.at(-1);
    $('#last-activity').textContent = last ? `Letzte Aktivität: ${new Date(last.at).toLocaleTimeString('de-AT')}` : '';
    $('#kpi-round').textContent = `${state.round} / ${state.params.rounds}${state.block > 1 ? ` · Block ${state.block}` : ''}`;
    updateElapsed();
  }

  function updateElapsed() {
    if (!state) return;
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(state.created_at)) / 1000));
    $('#elapsed').textContent = `Gestartet vor ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} min`;
    for (const item of document.querySelectorAll('[data-call-start]')) item.textContent = `seit ${Math.max(0, Math.floor((Date.now() - Date.parse(item.dataset.callStart)) / 1000))} s`;
  }

  /* ---------- Coalitions ---------- */
  // Color follows the coalition, not its rank: the first-seen order stays fixed for the whole run.
  const COALITION_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
  function coalitions() {
    const registry = new Map();
    const add = (id, members) => {
      if (members.length < 2 || registry.has(id)) return;
      const index = registry.size;
      registry.set(id, {id, label: `G${index + 1}`, color: COALITION_COLORS[index] || '#64748b'});
    };
    for (const e of state.events) if (e.kind === 'membership') for (const [id, members] of Object.entries(e.payload.gruppen)) add(id, members);
    for (const [id, members] of Object.entries(state.groups)) add(id, members);
    return registry;
  }
  function coalitionChip(info, cls = '') {
    const chip = node('span', info.label, `marker marker-coalition pointer-events-none ${cls}`);
    chip.style.backgroundColor = info.color;
    chip.style.color = info.color === '#eda100' ? '#0b0b0b' : '#ffffff';
    return chip;
  }
  function coalitionBox(info, cls = '') {
    const box = node('div', '', `coalition-box ${cls}`);
    box.style.setProperty('--coalition', info.color);
    return box;
  }

  /* ---------- Control roles ---------- */
  const CONTROL_ROLES = {
    recherche: {label: 'Recherche', mark: 'R', text: 'Bereitet je Institution Fakten aus Presse und Rechtstexten auf.', busy: n => `${n} ${n === 1 ? 'Recherche läuft' : 'Recherchen laufen'}`},
    orchestrator: {label: 'Orchestrator', mark: 'O', text: 'Prüft Beiträge, Stimmen und Gruppen gegen die Statuten.', busy: n => `${n} ${n === 1 ? 'Prüfung läuft' : 'Prüfungen laufen'}`},
    moderator: {label: 'Moderator', mark: 'M', text: 'Fasst den Stand zusammen und schlägt Kompromisse vor.', busy: () => 'Arbeitet'},
    watcher: {label: 'Watcher', mark: 'W', text: 'Beobachtet den Verlauf und berichtet nur an dich.', busy: () => 'Aktualisiert den Stand'}
  };
  let rolesKey = '';
  function renderControlRoles() {
    const active = state.worker_alive ? state.active_calls || [] : [];
    const key = JSON.stringify([active.map(c => [c.role, c.task, c.actor]), Object.entries(metrics?.roles || {}).map(([r, v]) => [r, v.calls]), state.watcher_updating]);
    if (key === rolesKey) return;
    rolesKey = key;
    const box = clear('#control-roles');
    for (const [role, info] of Object.entries(CONTROL_ROLES)) {
      const calls = active.filter(c => c.role === role);
      const busy = calls.length > 0 || (role === 'watcher' && state.watcher_updating);
      const card = node('article', '', `stakeholder stakeholder-compact ${busy ? 'stakeholder-active' : ''}`);
      const mark = node('span', info.mark, 'inst-icon inst-corner role-mark');
      mark.setAttribute('aria-hidden', 'true');
      card.append(mark, node('h3', info.label, 'pr-16 font-semibold leading-snug text-slate-900'));
      const line = node('div', '', 'pr-16 text-xs text-slate-500');
      if (busy) {
        const task = calls.at(-1)?.task;
        line.append(spinner(calls.length > 1 ? info.busy(calls.length) : TASKS[task] || info.busy(calls.length || 1)));
      } else line.textContent = 'Wartet';
      card.append(line);
      const about = [...new Set(calls.map(c => c.actor).filter(Boolean))].map(institution);
      card.append(node('p', about.length ? `Gerade: ${shorten(about.join(', '), 90)}` : info.text, 'text-xs leading-relaxed text-slate-600'));
      const count = metrics?.roles[role]?.calls;
      if (count) card.append(node('p', `${count.toLocaleString('de-AT')} Aufrufe bisher`, 'mt-auto text-xs text-slate-400'));
      box.append(card);
    }
  }

  /* ---------- Stakeholder cards ---------- */
  let stakeholdersKey = '';
  function renderStakeholders() {
    const active = state.status === 'running' && state.worker_alive ? state.active_calls || [] : [];
    const key = JSON.stringify([state.events.length, state.status, active.map(c => [c.actor, c.task]), Object.values(state.actors).map(a => a.name), state.groups]);
    if (key === stakeholdersKey) return;
    stakeholdersKey = key;
    const box = clear('#stakeholders');
    const registry = coalitions();
    const groupOf = code => Object.entries(state.groups).find(([id, members]) => members.length > 1 && members.includes(code))?.[0];
    // Members of one coalition sit side by side, in coalition order, ahead of the others.
    const inCoalition = [...registry.keys()].flatMap(id => (state.groups[id] || []));
    const ordered = [...inCoalition, ...Object.keys(state.actors).filter(code => !inCoalition.includes(code))];
    for (const code of ordered) {
      const current = active.find(c => c.actor === code);
      const group = registry.get(groupOf(code));
      const card = node('article', '', `stakeholder ${current ? 'stakeholder-active' : ''} ${group ? 'stakeholder-coalition' : ''}`);
      if (group) card.style.setProperty('--coalition', group.color);
      card.append(actorIcon(code, 'inst-corner'));
      card.append(node('h3', institution(code), 'pr-16 font-semibold leading-snug text-slate-900'));
      const own = state.events.filter(e => e.actor === code);
      const statement = own.filter(e => e.kind === 'statement').at(-1);
      const line = node('div', '', 'flex flex-wrap items-center gap-2 pr-16 text-xs text-slate-500');
      if (current) line.append(spinner(TASKS[current.task] || 'Arbeitet'));
      else {
        const vote = own.filter(e => e.kind === 'vote').at(-1);
        line.append(node('span', state.status === 'paused' ? 'Pausiert' : vote ? `Letzte Stimme: ${VOTES[vote.payload.stimme].label}` : statement ? 'Position abgegeben' : 'Wartet'));
      }
      card.append(line);
      if (group) {
        const tag = node('div', '', 'flex items-center gap-1.5 text-xs font-medium text-slate-700');
        tag.append(coalitionChip(group, 'scale-75'), node('span', `Koalition ${group.label}`));
        card.append(tag);
      }
      if (statement) card.append(node('p', shorten(textOf(statement.payload), 140), 'text-xs leading-relaxed text-slate-600'));
      const button = node('button', 'Aufrufprotokoll', 'link-quiet mt-auto self-start');
      button.type = 'button'; button.dataset.trajectory = code;
      card.append(button);
      box.append(card);
    }
  }

  /* ---------- Timeline ---------- */
  function marker(cls, glyph, hint, eventId) {
    const button = node('button', glyph, `marker ${cls}`);
    button.type = 'button';
    button.dataset.hint = hint;
    button.setAttribute('aria-label', hint);
    if (eventId) button.dataset.event = eventId;
    return button;
  }

  function roundsOf() {
    const rounds = new Map();
    for (const e of state.events) {
      if (TIMELINE_STEPS.some(([step]) => step === e.phase)) rounds.set(`${e.block}:${e.round}`, {block: e.block, round: e.round});
    }
    if (!['start', 'question', 'research', 'done'].includes(state.phase)) rounds.set(`${state.block}:${state.round}`, {block: state.block, round: state.round});
    return [...rounds.values()];
  }

  function renderTimeline() {
    const active = state.status === 'running' && state.worker_alive ? state.active_calls || [] : [];
    const key = JSON.stringify([state.events.length, state.phase, state.round, state.block, state.status, active.map(c => [c.actor, c.role, c.task]), Object.keys(state.actors).length, state.actors[Object.keys(state.actors)[0]]?.name]);
    if (key === timelineKey) return;
    timelineKey = key;
    const table = clear('#timeline');
    const rounds = roundsOf();
    const at = (e, r, step) => e.block === r.block && e.round === r.round && e.phase === step;
    const isNow = (r, step) => state.status === 'running' && r.block === state.block && r.round === state.round && state.phase === step;
    const head = node('thead');
    const top = node('tr');
    const corner = node('th', 'Institution', 'tl-row-head'); corner.rowSpan = 2; top.append(corner);
    for (const r of rounds) {
      const ballot = state.events.find(e => e.kind === 'ballot' && e.block === r.block && e.round === r.round);
      const label = `${r.block > 1 ? `B${r.block} · ` : ''}Runde ${r.round}${ballot ? (ballot.payload.konsens ? ' · Konsens' : ' · kein Konsens') : ''}`;
      const th = node('th', label, 'tl-round'); th.colSpan = TIMELINE_STEPS.length; th.scope = 'colgroup'; top.append(th);
    }
    head.append(top);
    const steps = node('tr');
    for (const r of rounds) {
      TIMELINE_STEPS.forEach(([step, label], index) => {
        const th = node('th', label, `${index === 0 ? 'tl-round-first ' : ''}${isNow(r, step) ? 'tl-now' : ''}`);
        th.scope = 'col'; steps.append(th);
      });
    }
    head.append(steps); table.append(head);
    const body = node('tbody');

    const moderator = node('tr');
    const modHead = node('th', '', 'tl-row-head'); modHead.scope = 'row';
    modHead.append(node('span', 'Moderator', 'block truncate font-medium text-slate-800'));
    const modActive = active.find(c => c.role === 'moderator');
    if (modActive) {const s = spinner(TASKS[modActive.task] || 'Arbeitet'); s.classList.add('text-[11px]', 'text-slate-500'); modHead.append(s);}
    moderator.append(modHead);
    for (const r of rounds) {
      TIMELINE_STEPS.forEach(([step], index) => {
        const td = node('td', '', `${index === 0 ? 'tl-round-first ' : ''}${isNow(r, step) ? 'tl-now' : ''}`);
        if (step === 'proposal') {
          const proposal = state.events.find(e => e.kind === 'proposal' && e.block === r.block && e.round === r.round);
          if (proposal) td.append(marker('marker-proposal', '◆', `Kompromissvorschlag: ${shorten(proposal.payload.kompromissvorschlag || '')}`, proposal.id));
          else if (modActive && isNow(r, step)) td.append(node('span', '', 'loading loading-spinner loading-xs text-teal-700'));
        }
        moderator.append(td);
      });
    }
    body.append(moderator);

    for (const code of Object.keys(state.actors)) {
      const row = node('tr');
      const rowHead = node('th', '', 'tl-row-head'); rowHead.scope = 'row';
      const title = node('button', '', 'flex w-full items-center gap-2 text-left font-medium text-slate-800 hover:text-teal-800');
      title.type = 'button'; title.dataset.trajectory = code; title.dataset.hint = `${institution(code)}: Aufrufprotokoll öffnen`;
      title.append(actorIcon(code, 'inst-icon-sm'), node('span', institution(code), 'truncate'));
      rowHead.append(title);
      const current = active.find(c => c.actor === code);
      if (current) {
        const status = spinner(TASKS[current.task] || 'Arbeitet'); status.classList.add('text-[11px]', 'text-slate-500');
        rowHead.append(status);
      }
      row.append(rowHead);
      for (const r of rounds) {
        const membership = [...state.events].reverse().find(e => e.kind === 'membership' && e.block === r.block && e.round === r.round);
        const multi = membership ? Object.entries(membership.payload.gruppen).filter(([, members]) => members.length > 1) : [];
        const registry = coalitions();
        TIMELINE_STEPS.forEach(([step], index) => {
          const td = node('td', '', `${index === 0 ? 'tl-round-first ' : ''}${isNow(r, step) ? 'tl-now' : ''}`);
          const own = state.events.filter(e => e.actor === code && at(e, r, step));
          if (step === 'coalition') {
            const position = multi.findIndex(([, members]) => members.includes(code));
            if (position >= 0) {
              const info = registry.get(multi[position][0]);
              const chip = marker('marker-coalition', info.label, `Koalition ${info.label}: ${multi[position][1].map(institution).join(', ')}`, membership.id);
              chip.style.backgroundColor = info.color;
              chip.style.color = info.color === '#eda100' ? '#0b0b0b' : '#ffffff';
              td.append(chip);
            }
          } else if (step === 'vote') {
            const vote = own.find(e => e.kind === 'vote');
            const abstain = own.find(e => e.kind === 'abstention');
            if (vote) {const v = VOTES[vote.payload.stimme]; td.append(marker(v.cls, v.glyph, `${v.label}: ${shorten(vote.payload.begruendung || '')}`, vote.id));}
            else if (abstain) td.append(marker(VOTES.enthaltung.cls, VOTES.enthaltung.glyph, `Enthaltung: ${abstain.payload.grund}`, abstain.id));
          } else if (step !== 'proposal') {
            const statement = own.find(e => e.kind === 'statement');
            const abstain = own.find(e => e.kind === 'abstention');
            if (statement) {
              const concessions = statement.payload.zugestaendnisse?.length;
              td.append(marker(concessions ? 'marker-concession' : 'marker-position', concessions ? '+' : '', `${concessions ? `${concessions} Zugeständnis(se). ` : ''}${shorten(textOf(statement.payload))}`, statement.id));
            } else if (abstain) td.append(marker(VOTES.enthaltung.cls, VOTES.enthaltung.glyph, `Enthaltung: ${abstain.payload.grund}`, abstain.id));
          }
          if (!td.children.length && current && isNow(r, step)) td.append(node('span', '', 'loading loading-spinner loading-xs text-teal-700'));
          row.append(td);
        });
      }
      body.append(row);
    }
    table.append(body);
  }

  function renderTimelineLegend() {
    const legend = $('#timeline-legend');
    if (legend.children.length) return;
    const entries = [['marker-position', '', 'Position'], ['marker-concession', '+', 'Position mit Zugeständnis'], ['marker-group', 'G1', 'Koalition (je eine Farbe)'], ['marker-proposal', '◆', 'Vorschlag'], ...Object.values(VOTES).map(v => [v.cls, v.glyph, v.label])];
    for (const [cls, glyph, label] of entries) {
      const item = node('span', '', 'inline-flex items-center gap-1.5');
      const icon = node('span', glyph, `marker ${cls} pointer-events-none scale-75`);
      icon.setAttribute('aria-hidden', 'true');
      item.append(icon, node('span', label));
      legend.append(item);
    }
  }

  /* ---------- Pie charts ---------- */
  function pie(canvasId, legendId, rows, format, emptyText) {
    const total = rows.reduce((sum, row) => sum + row.value, 0);
    const canvas = $(canvasId);
    const legend = clear(legendId);
    canvas.parentElement.classList.toggle('hidden', !total);
    if (!total) {legend.append(node('p', emptyText, 'muted')); charts[canvasId]?.destroy(); delete charts[canvasId]; return;}
    for (const row of rows) {
      const item = node('div', '', 'flex items-center gap-2');
      const swatch = node('span', '', 'swatch'); swatch.style.backgroundColor = row.color;
      item.append(swatch, node('span', row.label, 'flex-1 text-slate-700'), node('span', format(row.value), 'font-medium tabular-nums text-slate-900'), node('span', `${Math.round(row.value / total * 100)} %`, 'w-12 whitespace-nowrap text-right tabular-nums text-slate-500'));
      legend.append(item);
    }
    if (!window.Chart) return;
    const data = {labels: rows.map(r => r.label), datasets: [{data: rows.map(r => r.value), backgroundColor: rows.map(r => r.color), borderColor: '#ffffff', borderWidth: 2, hoverOffset: 4}]};
    if (charts[canvasId]) {
      charts[canvasId].data = data;
      charts[canvasId].update('none');
      return;
    }
    charts[canvasId] = new Chart(canvas, {
      type: 'pie', data,
      options: {
        responsive: true, maintainAspectRatio: false, animation: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? false : {duration: 250},
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: item => ` ${format(item.parsed)} (${Math.round(item.parsed / total * 100)} %)`}}}
      }
    });
  }

  function renderVoteChart() {
    // Fills up live during a vote, then holds the last ballot until the next one starts.
    const voting = state.phase === 'vote' && state.status !== 'ended';
    const ballot = [...state.events].reverse().find(e => e.kind === 'ballot');
    let round = ballot, counts = {zustimmen: 0, tolerieren: 0, nachbessern: 0, ablehnen: 0, enthaltung: 0}, note = 'Noch keine Abstimmung.';
    const live = state.events.filter(e => e.block === state.block && e.round === state.round && e.phase === 'vote' && ['vote', 'abstention'].includes(e.kind));
    const liveBallot = ballot && ballot.block === state.block && ballot.round === state.round;
    if (voting && !liveBallot && live.length) {
      for (const e of live) counts[e.kind === 'vote' ? e.payload.stimme : 'enthaltung']++;
      note = `Runde ${state.round}: ${live.length} von ${Object.keys(state.actors).length} Stimmen eingegangen`;
    } else if (round) {
      for (const stimme of Object.values(round.payload.stimmen)) counts[stimme]++;
      note = `Runde ${round.round}: ${round.payload.konsens ? 'Konsens' : 'kein Konsens'}`;
    }
    $('#vote-chart-note').textContent = note;
    pie('#vote-chart', '#vote-legend', Object.entries(VOTES).map(([key, v]) => ({label: v.label, color: v.color, value: counts[key]})), value => String(value), 'Die Verteilung erscheint mit der ersten Stimme.');
  }

  function renderKpis() {
    if (!metrics) return;
    $('#kpi-cost').textContent = metrics.known_cost_eur === null ? (state.mode === 'fake' ? '0,00 €' : 'nicht gemeldet') : euro(metrics.known_cost_eur);
    $('#kpi-calls').textContent = metrics.calls.toLocaleString('de-AT');
    $('#kpi-review').textContent = `${metrics.rejections} · ${metrics.abstentions}`;
    const share = metrics.largest_context / metrics.context_limit;
    $('#kpi-context').textContent = `${Math.round(share * 100)} %`;
    $('#kpi-context').title = `${metrics.largest_context.toLocaleString('de-AT')} von ${metrics.context_limit.toLocaleString('de-AT')} Zeichen`;
    const fill = $('#kpi-context-fill');
    fill.style.width = `${Math.min(100, share * 100)}%`;
    fill.classList.toggle('bg-amber-500', share >= 0.7);
    fill.classList.toggle('bg-teal-700', share < 0.7);
  }

  async function refreshMetrics(force = false) {
    if (!force && Date.now() - lastMetrics < 3000) return;
    lastMetrics = Date.now();
    try {metrics = await api(`${base}/metrics`, null, 'GET'); renderKpis(); renderControlRoles();} catch {/* the next poll retries */}
  }

  /* ---------- Flow diagram ---------- */
  const FLOW_STEPS = [['individual', 'Einzelpositionen'], ['coalition', 'Koalitionen'], ['groups', 'Gruppenverhandlung'], ['proposal', 'Kompromissvorschlag'], ['vote', 'Abstimmung']];
  const expanded = new Set();
  const openItems = new Set();
  let flowKey = '', groupEvents = [], lastGroupFetch = 0;

  const nodeKeyOf = e => ['question', 'research', 'closing'].includes(e.phase) ? e.phase : `${e.block}:${e.round}:${e.phase}`;
  const eventsOf = key => state.events.filter(e => nodeKeyOf(e) === key && ['question', 'briefing', 'statement', 'vote', 'abstention', 'proposal', 'closing', 'ballot', 'membership'].includes(e.kind));

  async function refreshGroupEvents() {
    if (selected !== 'flow' || !state.events.some(e => e.kind === 'membership') || Date.now() - lastGroupFetch < 3000) return;
    lastGroupFetch = Date.now();
    try {
      groupEvents = (await api(`${base}/groups`, null, 'GET')).events.filter(e => e.kind === 'group_message');
      renderFlow();
    } catch {/* the next refresh retries */}
  }

  function collapsible(label, key, build) {
    const details = node('details', '', 'mt-1.5 text-sm text-slate-600');
    details.open = openItems.has(key);
    details.addEventListener('toggle', () => {if (details.open) openItems.add(key); else openItems.delete(key);});
    details.append(node('summary', label, 'cursor-pointer text-xs font-medium text-slate-500'));
    build(details);
    return details;
  }

  function subList(parent, title, items) {
    if (!items?.length) return;
    parent.append(node('h5', title, 'mt-2 text-xs font-semibold text-slate-500'));
    const list = node('ul', '', 'mt-0.5 list-disc space-y-0.5 pl-4 text-sm text-slate-700');
    for (const item of items) list.append(node('li', item));
    parent.append(list);
  }

  function conflicts(summary) {
    return (summary?.streitpunkte || []).map(item => {
      const positions = Array.isArray(item.positionen) ? item.positionen.map(p => `${p.institution}: ${p.position}`) : Object.entries(item.positionen || {}).map(([k, v]) => `${k}: ${v}`);
      return `${item.thema} — ${positions.join(' | ')}`;
    });
  }

  // Citations only: full articles stay out of the interface because they name real people.
  function sourceIndex() {
    const index = new Map();
    for (const e of state.events) if (e.kind === 'briefing') for (const fact of e.payload.fakten) index.set(fact.quelle_id, fact);
    return index;
  }
  function citationList(parent, ids) {
    const index = sourceIndex();
    const known = (ids || []).map(id => index.get(id)).filter(Boolean);
    if (!known.length) return;
    const line = node('div', '', 'mt-2 flex flex-wrap items-center gap-1.5');
    line.append(node('span', 'Quellen', 'text-xs font-semibold text-slate-500'));
    for (const fact of known) {
      const chip = node('span', fact.zitat, 'pill pill-neutral');
      chip.dataset.hint = fact.aussage;
      line.append(chip);
    }
    parent.append(line);
  }

  function flowItem(e) {
    const item = node('article', '', 'flow-item'); item.id = `event-${e.id}`;
    item.append(actorIcon(e.actor));
    const body = node('div', '', 'min-w-0 flex-1');
    const head = node('div', '', 'flex flex-wrap items-center gap-2');
    head.append(node('h4', institution(e.actor), 'text-sm font-semibold text-slate-900'));
    if (e.kind === 'vote') {
      const v = VOTES[e.payload.stimme];
      const icon = node('span', v.glyph, `marker ${v.cls} pointer-events-none scale-75`);
      icon.setAttribute('aria-hidden', 'true');
      head.append(icon, node('span', v.label, 'text-xs font-medium text-slate-600'));
    }
    if (e.kind === 'abstention') head.append(node('span', 'Enthaltung', 'pill pill-neutral'));
    const trajectory = node('button', 'Aufrufprotokoll', 'link-quiet ml-auto');
    trajectory.type = 'button'; trajectory.dataset.trajectory = e.actor;
    head.append(trajectory);
    body.append(head);
    const text = e.kind === 'vote' ? e.payload.begruendung : textOf(e.payload) || e.payload.grund || '';
    body.append(node('p', text, 'mt-0.5 whitespace-pre-wrap text-sm leading-relaxed text-slate-700'));
    subList(body, 'Zugeständnisse', e.payload.zugestaendnisse);
    subList(body, 'Einigungen in der Gruppe', e.payload.einigungen_in_gruppe);
    citationList(body, e.payload.quellen);
    if (e.payload.begruendung && e.kind !== 'vote') body.append(collapsible('Begründung', `why-${e.id}`, d => d.append(node('p', e.payload.begruendung, 'mt-1 whitespace-pre-wrap'))));
    item.append(body);
    return item;
  }

  function memberNames(members) {
    const line = node('div', '', 'flex flex-wrap items-center gap-x-3 gap-y-1');
    for (const code of members) {
      const member = node('span', '', 'inline-flex items-center gap-1.5 text-sm text-slate-700');
      member.append(actorIcon(code, 'inst-icon-sm'), node('span', institution(code)));
      line.append(member);
    }
    return line;
  }

  function flowPanelContent(step, r, key) {
    const panel = node('div', '', 'space-y-3');
    const own = eventsOf(key);
    const registry = coalitions();
    const statements = own.filter(e => ['statement', 'abstention'].includes(e.kind));
    if (step === 'question') {
      const question = own.find(e => e.kind === 'question');
      panel.append(node('p', question?.payload.frage || state.question, 'text-sm leading-relaxed text-slate-800'));
      if (question?.payload.kontext) panel.append(node('p', question.payload.kontext, 'text-sm text-slate-500'));
      if (question) panel.firstChild.id = `event-${question.id}`;
    } else if (step === 'research') {
      for (const e of own.filter(e => e.kind === 'briefing')) {
        const box = node('article', '', 'flow-item'); box.id = `event-${e.id}`;
        box.append(actorIcon(e.actor));
        const body = node('div', '', 'min-w-0 flex-1');
        body.append(node('h4', institution(e.actor), 'text-sm font-semibold text-slate-900'));
        if (!e.payload.fakten.length) body.append(node('p', 'Keine verwertbaren Quellen gefunden. Diese Institution verhandelt ohne Briefing.', 'muted'));
        for (const fact of e.payload.fakten) {
          const line = node('div', '', 'mt-1.5 text-sm text-slate-700');
          line.append(node('span', fact.aussage), node('span', fact.zitat, 'pill pill-neutral ml-2'));
          body.append(line);
        }
        box.append(body);
        panel.append(box);
      }
    } else if (step === 'individual') {
      for (const e of statements) panel.append(flowItem(e));
    } else if (step === 'coalition') {
      const membership = own.filter(e => e.kind === 'membership').at(-1);
      if (membership) {
        panel.id = `event-${membership.id}`;
        const entries = Object.entries(membership.payload.gruppen);
        const multi = entries.filter(([, members]) => members.length > 1);
        for (const [id, members] of multi) {
          const box = coalitionBox(registry.get(id));
          const head = node('div', '', 'mb-1.5 flex items-center gap-2 text-sm font-semibold text-slate-800');
          head.append(coalitionChip(registry.get(id), 'scale-90'), node('span', `Koalition ${registry.get(id).label}`));
          box.append(head, memberNames(members));
          panel.append(box);
        }
        if (!multi.length) panel.append(node('p', 'In dieser Runde hat sich keine Koalition gebildet.', 'muted'));
        const single = entries.filter(([, members]) => members.length === 1).map(([, members]) => members[0]);
        if (single.length && multi.length) {
          panel.append(node('p', 'Verhandeln einzeln', 'text-xs font-semibold text-slate-500'), memberNames(single));
        }
      }
    } else if (step === 'groups') {
      const messages = groupEvents.filter(e => e.block === r.block && e.round === r.round);
      const seen = new Set();
      for (const [id, info] of registry) {
        const mine = statements.filter(e => e.group === id);
        const talk = messages.filter(e => e.group === id);
        if (!mine.length && !talk.length) continue;
        mine.forEach(e => seen.add(e.id));
        const box = coalitionBox(info, 'space-y-2');
        const head = node('div', '', 'flex items-center gap-2 text-sm font-semibold text-slate-800');
        head.append(coalitionChip(info, 'scale-90'), node('span', `Koalition ${info.label}`));
        box.append(head);
        if (talk.length) box.append(collapsible(`Verhandlung im Kanal · ${talk.length} Nachrichten`, `talk-${id}-${r.block}-${r.round}`, d => {
          for (const e of talk) {
            const line = node('p', '', 'mt-1.5 text-sm text-slate-700');
            line.append(node('span', `${institution(e.actor)}: `, 'font-medium'), node('span', e.payload.inhalt));
            d.append(line);
          }
        }));
        for (const e of mine) box.append(flowItem(e));
        panel.append(box);
      }
      for (const e of statements) if (!seen.has(e.id)) panel.append(flowItem(e));
    } else if (step === 'proposal') {
      const proposal = own.find(e => e.kind === 'proposal');
      if (proposal) {
        const box = node('div'); box.id = `event-${proposal.id}`;
        box.append(node('p', proposal.payload.kompromissvorschlag, 'whitespace-pre-wrap text-sm font-medium leading-relaxed text-slate-800'));
        subList(box, 'Gemeinsamkeiten', proposal.payload.sammelstand?.gemeinsamkeiten);
        subList(box, 'Streitpunkte', conflicts(proposal.payload.sammelstand));
        subList(box, 'Zugeständnisse', proposal.payload.sammelstand?.zugestaendnisse);
        subList(box, 'Vom Vorschlag nicht entschieden', proposal.payload.offene_konflikte);
        if (proposal.payload.zielbeitrag) {
          box.append(node('h5', 'Beitrag zum Ziel', 'mt-2 text-xs font-semibold text-slate-500'), node('p', proposal.payload.zielbeitrag, 'mt-0.5 text-sm text-slate-700'));
        }
        if (proposal.payload.begruendung) box.append(collapsible('Begründung', `why-${proposal.id}`, d => d.append(node('p', proposal.payload.begruendung, 'mt-1 whitespace-pre-wrap'))));
        panel.append(box);
      }
    } else if (step === 'vote') {
      const ballot = own.find(e => e.kind === 'ballot');
      if (ballot) {
        const counts = {};
        for (const stimme of Object.values(ballot.payload.stimmen)) counts[stimme] = (counts[stimme] || 0) + 1;
        const summary = node('p', `${ballot.payload.konsens ? 'Konsens' : 'Kein Konsens'} · ${Object.entries(VOTES).filter(([k]) => counts[k]).map(([k, v]) => `${counts[k]} ${v.label}`).join(' · ')}`, `notice ${ballot.payload.konsens ? 'notice-good' : 'notice-info'}`);
        summary.id = `event-${ballot.id}`;
        panel.append(summary);
      }
      for (const e of own.filter(e => ['vote', 'abstention'].includes(e.kind))) panel.append(flowItem(e));
    } else if (step === 'closing') {
      const closing = own.find(e => e.kind === 'closing');
      if (closing) {
        const box = node('div'); box.id = `event-${closing.id}`;
        box.append(node('p', closing.payload.ergebnis, 'whitespace-pre-wrap text-sm font-medium leading-relaxed text-slate-800'));
        subList(box, 'Zustimmung', closing.payload.zustimmung);
        subList(box, 'Tolerierung', closing.payload.tolerierung);
        subList(box, 'Nachbesserung', (closing.payload.nachbesserung || []).map(a => `${a.agent}: ${a.grund}`));
        subList(box, 'Ablehnung', (closing.payload.ablehnung || []).map(a => `${a.agent}: ${a.grund}`));
        subList(box, 'Offene Punkte', closing.payload.offene_punkte);
        panel.append(box);
      }
    }
    if (!panel.children.length) panel.append(node('p', 'Noch keine geprüften Beiträge in diesem Schritt.', 'muted'));
    return panel;
  }

  function flowSummary(step, key, r) {
    const own = eventsOf(key);
    const count = kind => own.filter(e => e.kind === kind).length;
    if (step === 'question') return shorten(own.find(e => e.kind === 'question')?.payload.frage || 'Frage wird vorbereitet', 90);
    if (step === 'research') {
      const briefings = own.filter(e => e.kind === 'briefing');
      const facts = briefings.reduce((sum, e) => sum + e.payload.fakten.length, 0);
      return `${briefings.length} von ${Object.keys(state.actors).length} Institutionen · ${facts} Fakten mit Quelle`;
    }
    if (step === 'individual' || step === 'groups') {
      const concessions = own.reduce((sum, e) => sum + (e.payload.zugestaendnisse?.length || 0), 0);
      const talk = step === 'groups' ? groupEvents.filter(e => e.block === r.block && e.round === r.round).length : 0;
      return [`${count('statement')} von ${Object.keys(state.actors).length} Positionen`, concessions && `${concessions} Zugeständnisse`, talk && `${talk} Nachrichten`, count('abstention') && `${count('abstention')} Enthaltungen`].filter(Boolean).join(' · ');
    }
    if (step === 'coalition') {
      const membership = own.filter(e => e.kind === 'membership').at(-1);
      if (!membership) return '';
      const multi = Object.values(membership.payload.gruppen).filter(m => m.length > 1).length;
      return multi ? `${multi} ${multi === 1 ? 'Koalition' : 'Koalitionen'}` : 'Keine Koalition';
    }
    if (step === 'proposal') return shorten(own.find(e => e.kind === 'proposal')?.payload.kompromissvorschlag || '', 70);
    if (step === 'vote') {
      const ballot = own.find(e => e.kind === 'ballot');
      if (ballot) {
        const support = ballot.payload.gesamt ? ` · ${ballot.payload.zustimmend ?? ballot.payload.tragend} von ${ballot.payload.gesamt} stimmen zu` : '';
        return `${ballot.payload.konsens ? 'Konsens' : 'Kein Konsens'}${support}`;
      }
      return `${count('vote') + count('abstention')} von ${Object.keys(state.actors).length} Stimmen`;
    }
    if (step === 'closing') {
      const closing = own.find(e => e.kind === 'closing');
      return closing ? (closing.payload.status === 'konsens' ? 'Konsens' : 'Teilkompromiss') : '';
    }
    return '';
  }

  function flowBox(step, label, key, r) {
    const own = eventsOf(key);
    const order = PHASE_ORDER.indexOf(step);
    const current = !r || (r.block === state.block && r.round === state.round);
    const now = current && state.phase === step;
    const reached = own.length > 0 || now || !current || PHASE_ORDER.indexOf(state.phase) > order;
    const box = node('button', '', 'flow-box');
    box.type = 'button';
    box.dataset.flow = key;
    box.disabled = !reached;
    box.setAttribute('aria-expanded', String(expanded.has(key)));
    if (now && state.status === 'running') box.classList.add('flow-box-active');
    else if (now && state.status === 'paused') box.classList.add('flow-box-paused');
    else if (reached) box.classList.add('flow-box-done');
    if (expanded.has(key)) box.classList.add('flow-box-open');
    const head = node('span', '', 'flex items-start justify-between gap-2');
    head.append(node('span', label, 'text-[13px] font-semibold leading-tight text-slate-900'));
    box.append(head);
    if (reached) box.append(node('span', expanded.has(key) ? '▾' : '▸', 'flow-chevron'));
    if (now && state.status === 'running' && state.worker_alive) {
      const calls = (state.active_calls || []).filter(c => c.role !== 'watcher').length;
      const status = spinner(calls ? `${calls} ${calls === 1 ? 'Aufruf' : 'Aufrufe'} aktiv` : 'Wird vorbereitet');
      status.classList.add('text-xs', 'text-teal-800');
      box.append(status);
    }
    box.append(node('span', reached ? flowSummary(step, key, r) : 'Noch nicht erreicht', 'text-xs leading-snug text-slate-500'));
    return box;
  }

  function flowDecision(r, next) {
    const ballot = state.events.find(e => e.kind === 'ballot' && e.block === r.block && e.round === r.round);
    if (!ballot) return null;
    const wanted = Object.values(ballot.payload.stimmen).filter(s => s === 'nachbessern').length;
    const prefix = wanted ? `${wanted} ${wanted === 1 ? 'Nachbesserung' : 'Nachbesserungen'} verlangt` : 'Kein Konsens';
    const text = ballot.payload.konsens ? 'Konsens erreicht: weiter zum Abschluss'
      : next ? `${prefix}: weiter mit ${next.block !== r.block ? `Block ${next.block}, ` : ''}Runde ${next.round}`
      : state.params.rounds <= r.round ? `${prefix} und Rundenlimit erreicht: Teilkompromiss` : `${prefix}: nächste Runde folgt`;
    return node('div', text, `flow-decision ${ballot.payload.konsens ? 'flow-decision-good' : ''}`);
  }

  function renderFlow(force = false) {
    const key = JSON.stringify([state.events.length, groupEvents.length, state.phase, state.status, state.round, state.block, (state.active_calls || []).filter(c => c.role !== 'watcher').length, state.worker_alive, [...expanded], Object.keys(state.actors).length, state.actors[Object.keys(state.actors)[0]]?.name]);
    if (!force && key === flowKey) return;
    flowKey = key;
    const focused = document.activeElement?.dataset?.flow;
    const flow = clear('#flow');

    const addRow = (boxes, panels) => {
      const row = node('div', '', 'flow-row');
      boxes.forEach((box, index) => {
        if (index) {const arrow = node('span', '→', 'flow-arrow'); arrow.setAttribute('aria-hidden', 'true'); row.append(arrow);}
        row.append(box);
      });
      flow.append(row);
      for (const [title, content] of panels) {
        const panel = node('div', '', 'flow-panel');
        panel.append(node('h3', title, 'mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500'), content);
        flow.append(panel);
      }
    };
    const connector = () => {const arrow = node('div', '↓', 'flow-connector'); arrow.setAttribute('aria-hidden', 'true'); flow.append(arrow);};

    addRow([flowBox('question', 'Frage', 'question', null)], expanded.has('question') ? [['Frage', flowPanelContent('question', null, 'question')]] : []);
    if (state.phase === 'research' || state.events.some(e => e.kind === 'briefing')) {
      connector();
      addRow([flowBox('research', 'Recherche', 'research', null)], expanded.has('research') ? [['Recherche je Institution', flowPanelContent('research', null, 'research')]] : []);
    }
    const rounds = roundsOf();
    rounds.forEach((r, index) => {
      connector();
      flow.append(node('h2', `${r.block > 1 ? `Block ${r.block} · ` : ''}Runde ${r.round}`, 'flow-round'));
      const boxes = FLOW_STEPS.map(([step, label]) => flowBox(step, label, `${r.block}:${r.round}:${step}`, r));
      const panels = FLOW_STEPS.filter(([step]) => expanded.has(`${r.block}:${r.round}:${step}`)).map(([step, label]) => [`Runde ${r.round} · ${label}`, flowPanelContent(step, r, `${r.block}:${r.round}:${step}`)]);
      addRow(boxes, panels);
      const decision = flowDecision(r, rounds[index + 1]);
      if (decision) flow.append(decision);
    });
    if (['closing', 'done'].includes(state.phase) || state.events.some(e => e.kind === 'closing')) {
      connector();
      addRow([flowBox('closing', 'Abschluss', 'closing', null)], expanded.has('closing') ? [['Abschluss', flowPanelContent('closing', null, 'closing')]] : []);
    }
    if (focused) flow.querySelector(`[data-flow="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }

  /* ---------- Side panel ---------- */
  const WATCHER_SECTIONS = {gemeinsamkeiten: 'Gemeinsamkeiten', vorschlaege: 'Vorschläge', konflikte: 'Offene Konflikte', zugestaendnisse: 'Zugeständnisse', positionsaenderungen: 'Positionsänderungen', koalitionen: 'Koalitionen', abstimmung: 'Abstimmung'};
  const openSections = new Set();
  function sideSection(key, title, count, build) {
    const details = node('details', '', 'side-section');
    details.open = openSections.has(key);
    details.addEventListener('toggle', () => {if (details.open) openSections.add(key); else openSections.delete(key);});
    const summary = node('summary', '', 'side-summary');
    summary.append(node('span', '▸', 'side-chevron'), node('span', title, 'flex-1'), node('span', String(count), 'pill pill-neutral'));
    details.append(summary);
    build(details);
    return details;
  }
  let coalitionsKey = '';
  function renderSide() {
    const status = clear('#watcher-status');
    if (state.watcher_updating) status.append(spinner('Wird aktualisiert …'));
    else if (state.watcher_error) status.append(node('p', `${state.watcher_error.text} Voriger Stand bleibt sichtbar.`, 'notice notice-warn'));
    else status.textContent = state.watcher ? `Stand: ${new Date(state.watcher.at).toLocaleTimeString('de-AT')}` : 'Erste Zusammenfassung folgt nach der ersten Phase.';
    const key = String(state.watcher?.id ?? '');
    if (key !== watcherKey) {
      watcherKey = key;
      const watcher = clear('#watcher');
      if (state.watcher) {
        const summary = state.watcher.payload.summary;
        for (const [name, title] of Object.entries(WATCHER_SECTIONS)) {
          if (!summary[name]?.length) continue;
          watcher.append(sideSection(name, title, summary[name].length, details => {
            const list = node('ul', '', 'mt-1.5 list-disc space-y-1 pl-4 text-sm text-slate-600');
            for (const text of summary[name]) list.append(node('li', text));
            details.append(list);
          }));
        }
        if (summary.referenzen?.length) watcher.append(sideSection('referenzen', 'Belege', summary.referenzen.length, details => {
          const refs = node('div', '', 'mt-1.5 flex flex-wrap gap-x-3 gap-y-1');
          for (const ref of summary.referenzen) {const a = node('button', `Beleg #${ref}`, 'link-quiet'); a.type = 'button'; a.dataset.event = ref; refs.append(a);}
          details.append(refs);
        }));
      }
    }
    $('#proposal').textContent = state.proposal?.kompromissvorschlag || 'Noch kein Vorschlag.';
    const coalitionKey = JSON.stringify([state.groups, Object.values(state.actors).map(a => a.name)]);
    if (coalitionKey === coalitionsKey) return;
    coalitionsKey = coalitionKey;
    const box = clear('#coalitions');
    const registry = coalitions();
    const active = [...registry.values()].filter(info => state.groups[info.id]);
    if (!active.length) box.append(node('p', 'Alle Institutionen verhandeln einzeln.', 'muted'));
    for (const info of active) {
      const group = coalitionBox(info, 'space-y-1.5');
      const head = node('div', '', 'flex items-center gap-2 text-sm font-semibold text-slate-800');
      head.append(coalitionChip(info, 'scale-90'), node('span', `Koalition ${info.label}`));
      group.append(head);
      for (const code of state.groups[info.id]) {
        const member = node('div', '', 'flex items-center gap-2');
        member.append(actorIcon(code, 'inst-icon-sm'), node('span', institution(code), 'text-slate-700'));
        group.append(member);
      }
      box.append(group);
    }
  }

  /* ---------- Groups ---------- */
  async function renderGroups() {
    try {
      const result = await api(`${base}/groups`, null, 'GET');
      const box = clear('#groups');
      const all = {...result.groups};
      for (const e of result.events.filter(e => e.kind === 'group_message')) all[e.group] ||= e.payload.members;
      const entries = Object.entries(all).filter(([, members]) => members.length > 1);
      if (!entries.length) box.append(node('p', 'Bisher hat sich keine Koalition gebildet. Einzelgruppen führen keinen internen Dialog.', 'muted'));
      for (const [code, members] of entries) {
        const section = node('section', '', 'rounded-xl border border-slate-200 p-4');
        section.append(node('h2', members.map(institution).join(' · ') + (result.groups[code] ? '' : ' · Archivierter Kanal'), 'mb-3 text-sm font-semibold text-slate-900'));
        const messages = result.events.filter(e => e.kind === 'group_message' && e.group === code);
        for (const e of messages) {
          const line = node('p', '', 'mb-2 text-sm text-slate-700');
          line.append(node('span', `${institution(e.actor)}: `, 'font-medium'), node('span', e.payload.inhalt));
          section.append(line);
        }
        if (!messages.length) section.append(node('p', 'Noch keine Gruppennachrichten.', 'muted'));
        box.append(section);
      }
      const history = node('details', '', 'text-sm');
      history.append(node('summary', 'Mitgliedschaftsverlauf', 'cursor-pointer font-medium text-slate-600'));
      for (const e of result.events.filter(e => e.kind === 'membership')) history.append(node('p', `Runde ${e.round}: ${Object.values(e.payload.gruppen).map(m => m.map(institution).join(' + ')).join(' | ')}`, 'my-2 text-xs text-slate-600'));
      box.append(history);
    } catch (exc) {notify(exc.message);}
  }

  /* ---------- Analysis ---------- */
  const REPORT_LABELS = {status: 'Status', ergebnis: 'Ergebnis', zustimmung: 'Zustimmung', tolerierung: 'Tolerierung', nachbesserung: 'Nachbesserung', ablehnung: 'Ablehnung', offene_punkte: 'Offene Punkte', empfehlung_neustart: 'Empfehlung', begruendung_empfehlung: 'Begründung der Empfehlung', koalitionsbildung: 'Koalitionsbildung', entscheidungsfindung: 'Entscheidungsfindung', offene_konflikte: 'Offene Konflikte', auffaelligkeiten: 'Auffälligkeiten', wendepunkte: 'Wendepunkte', verlauf: 'Verlauf je Runde', statistik: 'Statistik'};

  function reportValue(parent, value) {
    if (Array.isArray(value)) {
      if (!value.length) {parent.append(node('p', '–', 'text-sm text-slate-400')); return;}
      const list = node('ul', '', 'list-disc space-y-1 pl-4 text-sm text-slate-700');
      for (const item of value) {
        const li = node('li', typeof item === 'object' ? '' : item);
        if (typeof item === 'object') {
          if (item.kernpunkte) li.textContent = `Runde ${item.runde}: ${item.kernpunkte.join(' ')}`;
          else if (item.beschreibung) li.textContent = `Runde ${item.runde}, ${item.phase}: ${item.beschreibung}`;
          else if (item.grund) li.textContent = `${item.agent}: ${item.grund}`;
          else li.textContent = Object.values(item).join(' · ');
        }
        list.append(li);
      }
      parent.append(list);
    } else if (value && typeof value === 'object') {
      const labels = {runden: 'Runden', rueckweisungen: 'Rückweisungen', enthaltungen: 'Enthaltungen', gruppenwechsel: 'Gruppenwechsel'};
      parent.append(node('p', Object.entries(value).map(([k, v]) => `${labels[k] || k}: ${v}`).join(' · '), 'text-sm text-slate-700'));
    } else parent.append(node('p', String(value), 'whitespace-pre-wrap text-sm text-slate-700'));
  }

  async function renderAnalysis() {
    const key = JSON.stringify([state.events.length, state.run]);
    if (key === analysisKey) return;
    analysisKey = key;
    const ballots = clear('#ballots');
    const rows = state.events.filter(e => e.kind === 'ballot');
    if (!rows.length) ballots.append(node('p', 'Noch keine Abstimmung.', 'muted'));
    else {
      const table = node('table', '', 'data-table');
      const head = node('tr');
      head.append(node('th', 'Institution'));
      for (const e of rows) head.append(node('th', `${e.block > 1 ? `B${e.block}/` : ''}R${e.round}`));
      table.append(head);
      for (const code of Object.keys(state.actors)) {
        const row = node('tr');
        const nameCell = node('td', '', 'font-medium text-slate-800');
        const nameLine = node('span', '', 'flex items-center gap-2');
        nameLine.append(actorIcon(code, 'inst-icon-sm'), node('span', institution(code)));
        nameCell.append(nameLine);
        row.append(nameCell);
        for (const e of rows) {
          const v = VOTES[e.payload.stimmen[code] || 'enthaltung'];
          const td = node('td');
          const line = node('span', '', 'inline-flex items-center gap-1.5');
          const icon = node('span', v.glyph, `marker ${v.cls} pointer-events-none scale-75`); icon.setAttribute('aria-hidden', 'true');
          line.append(icon, node('span', v.label, 'text-xs text-slate-600'));
          td.append(line); row.append(td);
        }
        table.append(row);
      }
      ballots.append(table);
    }
    const positions = clear('#positions');
    for (const [code, actor] of Object.entries(state.actors)) {
      const section = node('details', '', 'border-b border-slate-100 py-2.5');
      const statements = state.events.filter(e => e.kind === 'statement' && e.actor === code);
      const summary = node('summary', '', 'flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800');
      summary.append(actorIcon(code, 'inst-icon-sm'), node('span', `${actor.name} · ${statements.length} Beiträge`));
      section.append(summary);
      for (const e of statements) {
        section.append(node('p', `Runde ${e.round} · ${PHASES[e.phase]}: ${textOf(e.payload)}`, 'mt-2 text-sm text-slate-700'));
        for (const c of e.payload.zugestaendnisse || []) section.append(node('p', `+ Zugeständnis: ${c}`, 'mt-1 text-xs font-medium text-slate-600'));
      }
      positions.append(section);
    }
    try {
      const result = await api(`${base}/reports`, null, 'GET');
      const box = clear('#reports');
      if (!result.items.length) box.append(node('p', 'Der Bericht erscheint nach dem Abschluss.', 'muted'));
      for (const e of result.items) {
        const details = node('details', '', 'rounded-xl border border-slate-200 p-4');
        details.append(node('summary', `${e.kind === 'report' ? 'Watcher-Bericht' : 'Moderatorabschluss'} · Durchlauf ${e.run}, Block ${e.block}`, 'cursor-pointer text-sm font-medium text-slate-800'));
        for (const [name, label] of Object.entries(REPORT_LABELS)) {
          if (e.payload[name] === undefined) continue;
          details.append(node('h4', label, 'mb-1 mt-3 text-xs font-semibold uppercase tracking-wider text-slate-500'));
          reportValue(details, e.payload[name]);
        }
        box.append(details);
      }
    } catch (exc) {notify(exc.message);}
  }

  /* ---------- Render loop ---------- */
  function render() {
    renderHeader();
    renderControlRoles();
    renderStakeholders();
    renderTimeline();
    renderTimelineLegend();
    renderVoteChart();
    renderKpis();
    renderFlow();
    refreshGroupEvents();
    renderSide();
    if (selected === 'analysis') renderAnalysis();
    if (selected === 'groups') renderGroups();
  }

  async function refresh() {
    if (refreshing) {again = true; return;}
    refreshing = true;
    try {
      state = await api(base, null, 'GET');
      render();
      refreshMetrics();
    } catch (exc) {notify(exc.message);} finally {
      refreshing = false;
      if (again) {again = false; refresh();}
    }
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(() => {timer = null; refresh();}, 700);
  }

  const VIEWS = ['overview', 'flow', 'groups', 'analysis'];
  function selectView(view, remember = true) {
    // Every tab change is a history entry, so the back button returns to the previous tab.
    if (remember && view !== selected) history.pushState(null, '', `#${view}`);
    selected = view;
    for (const panel of document.querySelectorAll('[data-view-panel]')) panel.classList.toggle('hidden', panel.id !== (view === 'flow' ? 'flow-view' : view));
    for (const tab of document.querySelectorAll('[data-view]')) {
      tab.classList.toggle('view-tab-active', tab.dataset.view === view);
      tab.setAttribute('aria-selected', String(tab.dataset.view === view));
    }
    if (view === 'analysis') {analysisKey = ''; renderAnalysis();}
    if (view === 'groups') renderGroups();
    if (view === 'flow' && state) {refreshGroupEvents(); renderFlow(true);}
    if (view === 'overview') for (const chart of Object.values(charts)) chart.resize();
  }
  const viewFromUrl = () => VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  window.addEventListener('popstate', () => selectView(viewFromUrl(), false));

  async function showReference(ref) {
    const event = state.events.find(e => e.id === ref);
    if (event && event.kind !== 'watcher') {
      expanded.add(nodeKeyOf(event));
      selectView('flow');
      renderFlow(true);
      const target = document.getElementById(`event-${ref}`);
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.classList.add('rounded-lg', 'ring-2', 'ring-teal-600', 'ring-offset-4');
        setTimeout(() => target.classList.remove('rounded-lg', 'ring-2', 'ring-teal-600', 'ring-offset-4'), 2000);
        return;
      }
    }
    try {
      const entry = await api(`${base}/protocol/${ref}`, null, 'GET');
      $('#trajectory-content').textContent = JSON.stringify(entry, null, 2);
      $('#trajectory-modal').showModal();
    } catch (exc) {notify(exc.message);}
  }

  const tooltip = $('#viz-tooltip');
  function showHint(target) {
    tooltip.textContent = target.dataset.hint;
    tooltip.classList.remove('hidden');
    const box = target.getBoundingClientRect();
    const own = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(8, Math.min(window.innerWidth - own.width - 8, box.left + box.width / 2 - own.width / 2))}px`;
    tooltip.style.top = `${box.top - own.height - 8 < 8 ? box.bottom + 8 : box.top - own.height - 8}px`;
  }
  const hideHint = () => tooltip.classList.add('hidden');
  for (const type of ['mouseover', 'focusin']) document.addEventListener(type, event => {
    const target = event.target.closest?.('[data-hint]');
    if (target) showHint(target); else hideHint();
  });
  document.addEventListener('focusout', hideHint);

  document.addEventListener('click', async event => {
    const tab = event.target.closest('[data-view]');
    if (tab) selectView(tab.dataset.view);
    const box = event.target.closest('[data-flow]');
    if (box && !box.disabled) {
      if (expanded.has(box.dataset.flow)) expanded.delete(box.dataset.flow); else expanded.add(box.dataset.flow);
      renderFlow(true);
    }
    const reference = event.target.closest('[data-event]');
    if (reference) showReference(Number(reference.dataset.event));
    const trajectory = event.target.closest('[data-trajectory]');
    if (trajectory) {
      try {
        const result = await api(`${base}/trajectories?actor=${encodeURIComponent(trajectory.dataset.trajectory)}`, null, 'GET');
        $('#trajectory-content').textContent = JSON.stringify(result.items, null, 2);
        $('#trajectory-modal').showModal();
      } catch (exc) {notify(exc.message);}
    }
    const command = event.target.closest('[data-command]');
    if (command) {
      command.disabled = true;
      try {await api(`${base}/control`, {action: command.dataset.command, key: crypto.randomUUID()}); await refresh();}
      catch (exc) {notify(exc.message);} finally {command.disabled = false;}
    }
  });

  async function start() {
    selectView(viewFromUrl(), false);
    await refresh();
    refreshMetrics(true);
    const source = new EventSource(`${base}/events?after=${state?.events.at(-1)?.id || 0}`);
    source.onopen = () => {$('#connection').textContent = 'Live-Verbindung aktiv';};
    source.onerror = () => {$('#connection').textContent = 'Live-Verbindung unterbrochen · erneuter Verbindungsaufbau …';};
    source.onmessage = schedule;
    setInterval(refresh, 10000);
    setInterval(updateElapsed, 1000);
  }
  start();
})();
