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
  const PHASES = {start: 'Vorbereitung', question: 'Frage', individual: 'Einzelpositionen', coalition: 'Koalitionen', groups: 'Gruppenverhandlung', proposal: 'Kompromissvorschlag', vote: 'Abstimmung', closing: 'Abschluss', done: 'Abgeschlossen'};
  const PHASE_ORDER = ['start', 'question', 'individual', 'coalition', 'groups', 'proposal', 'vote', 'closing', 'done'];
  const TASKS = {
    einzelphase: 'Formuliert Position', statement_pruefen: 'Wird geprüft', sicherheit: 'Inhalt wird geprüft',
    koalitionsphase: 'Prüft Koalitionen', zustimmung: 'Prüft Gruppenbeitritt', gruppennachricht: 'Verhandelt',
    gruppenstatement: 'Formuliert Position', abstimmung: 'Stimmt ab', frage_stellen: 'Formuliert Frage',
    gruppe_pruefen: 'Gruppe wird geprüft', sammelstand_und_vorschlag: 'Entwickelt Vorschlag',
    zwischenstand: 'Aktualisiert den Stand', abschluss: 'Erstellt Abschluss', abschlussbericht: 'Erstellt Abschlussbericht'
  };
  // Validated with the dataviz palette check; text labels and glyphs carry identity alongside color.
  const VOTES = {
    zustimmen: {label: 'Zustimmung', glyph: '✓', color: '#1baf7a', cls: 'marker-yes'},
    tolerieren: {label: 'Tolerierung', glyph: '~', color: '#2a78d6', cls: 'marker-tolerate'},
    ablehnen: {label: 'Ablehnung', glyph: '✕', color: '#e34948', cls: 'marker-no'},
    enthaltung: {label: 'Enthaltung', glyph: '○', color: '#898781', cls: 'marker-abstain'}
  };
  const ROLES = {
    agent: {label: 'Institutionen', color: '#2a78d6'}, orchestrator: {label: 'Orchestrator', color: '#eb6834'},
    moderator: {label: 'Moderator', color: '#1baf7a'}, watcher: {label: 'Watcher', color: '#eda100'}
  };
  const TIMELINE_STEPS = [['individual', 'Pos.', 'Einzelposition'], ['coalition', 'Koal.', 'Koalition'], ['groups', 'Verh.', 'Gruppenverhandlung'], ['proposal', 'Vorschl.', 'Kompromissvorschlag'], ['vote', 'Stimme', 'Abstimmung']];

  let state, metrics, refreshing = false, again = false, selected = 'overview', timer = null, lastMetrics = 0;
  let renderedRun = null, feedPhase = '', timelineKey = '', watcherKey = '', analysisKey = '';
  const rendered = new Set();
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
    if (state.status === 'running' && state.worker_alive) activity.append(spinner(`${active.length} Aufrufe aktiv`));
    else activity.textContent = state.status === 'running' ? 'Wartet auf Worker' : '';
    const outcome = $('#outcome');
    const ballot = [...state.events].reverse().find(e => e.kind === 'ballot');
    const finished = ['completed', 'awaiting_decision', 'ended'].includes(state.status) && state.phase === 'done';
    outcome.classList.toggle('hidden', !finished || !state.outcome);
    if (finished && state.outcome) {
      const support = ballot?.payload.gesamt ? ` ${ballot.payload.tragend} von ${ballot.payload.gesamt} Institutionen stimmen zu oder tolerieren.` : '';
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

  /* ---------- Stakeholder cards ---------- */
  let stakeholdersKey = '';
  function renderStakeholders() {
    const active = state.status === 'running' && state.worker_alive ? state.active_calls || [] : [];
    const key = JSON.stringify([state.events.length, state.status, active.map(c => [c.actor, c.task]), Object.values(state.actors).map(a => a.name), state.groups]);
    if (key === stakeholdersKey) return;
    stakeholdersKey = key;
    const box = clear('#stakeholders');
    const multi = Object.values(state.groups).filter(members => members.length > 1);
    for (const code of Object.keys(state.actors)) {
      const current = active.find(c => c.actor === code);
      const card = node('article', '', `stakeholder ${current ? 'stakeholder-active' : ''}`);
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
      const group = multi.findIndex(members => members.includes(code));
      if (group >= 0) line.append(node('span', `Koalition G${group + 1}`, 'pill pill-neutral'));
      card.append(line);
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
    if (!['start', 'question', 'done'].includes(state.phase)) rounds.set(`${state.block}:${state.round}`, {block: state.block, round: state.round});
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
      TIMELINE_STEPS.forEach(([step, short, long], index) => {
        const th = node('th', short, `${index === 0 ? 'tl-round-first ' : ''}${isNow(r, step) ? 'tl-now' : ''}`);
        th.title = long; th.scope = 'col'; steps.append(th);
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
        TIMELINE_STEPS.forEach(([step], index) => {
          const td = node('td', '', `${index === 0 ? 'tl-round-first ' : ''}${isNow(r, step) ? 'tl-now' : ''}`);
          const own = state.events.filter(e => e.actor === code && at(e, r, step));
          if (step === 'coalition') {
            const position = multi.findIndex(([, members]) => members.includes(code));
            if (position >= 0) td.append(marker('marker-group', `G${position + 1}`, `Koalition: ${multi[position][1].map(institution).join(', ')}`, membership.id));
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
    const entries = [['marker-position', '', 'Position'], ['marker-concession', '+', 'Position mit Zugeständnis'], ['marker-group', 'G1', 'Koalition'], ['marker-proposal', '◆', 'Vorschlag'], ...Object.values(VOTES).map(v => [v.cls, v.glyph, v.label])];
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
    let round = ballot, counts = {zustimmen: 0, tolerieren: 0, ablehnen: 0, enthaltung: 0}, note = 'Noch keine Abstimmung.';
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

  function renderCostChart() {
    if (!metrics) return;
    const hasCost = metrics.known_cost_eur !== null;
    $('#cost-chart-note').textContent = hasCost ? `Summe ${euro(metrics.known_cost_eur)} · 1 USD = ${metrics.eur_per_usd.toLocaleString('de-AT')} EUR` : state.mode === 'fake' ? 'Offline-Demo: keine Kosten, Anteil nach Aufrufen' : 'Der Anbieter hat noch keine Kosten gemeldet, Anteil nach Aufrufen';
    const rows = Object.entries(ROLES).map(([key, role]) => ({label: role.label, color: role.color, value: (hasCost ? metrics.roles[key]?.cost_eur : metrics.roles[key]?.calls) || 0}));
    pie('#cost-chart', '#cost-legend', rows, hasCost ? euro : value => `${value} Aufrufe`, 'Die Verteilung erscheint mit dem ersten Aufruf.');
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
    try {metrics = await api(`${base}/metrics`, null, 'GET'); renderKpis(); renderCostChart();} catch {/* the next poll retries */}
  }

  /* ---------- Discussion ---------- */
  function renderList(parent, title, items) {
    if (!items?.length) return;
    parent.append(node('h4', title, 'mt-3 text-xs font-semibold uppercase tracking-wider text-slate-500'));
    const list = node('ul', '', 'mt-1 list-disc space-y-1 pl-4 text-sm text-slate-700');
    for (const item of items) list.append(node('li', item));
    parent.append(list);
  }

  function conflicts(summary) {
    return (summary?.streitpunkte || []).map(item => {
      const positions = Array.isArray(item.positionen) ? item.positionen.map(p => `${p.institution}: ${p.position}`) : Object.entries(item.positionen || {}).map(([k, v]) => `${k}: ${v}`);
      return `${item.thema} — ${positions.join(' | ')}`;
    });
  }

  function post(e) {
    const box = node('article', '', 'post'); box.id = `event-${e.id}`;
    const isModerator = !e.actor;
    const name = isModerator ? 'Moderator' : institution(e.actor);
    box.append(isModerator ? node('div', 'M', 'post-avatar-mod') : actorIcon(e.actor));
    const content = node('div', '', 'min-w-0 flex-1');
    const head = node('div', '', 'flex flex-wrap items-center gap-2');
    head.append(node('h3', name, 'text-sm font-semibold text-slate-900'));
    if (e.kind === 'vote') {const v = VOTES[e.payload.stimme]; const icon = node('span', v.glyph, `marker ${v.cls} pointer-events-none scale-75`); icon.setAttribute('aria-hidden', 'true'); head.append(icon, node('span', v.label, 'text-xs font-medium text-slate-600'));}
    if (e.kind === 'abstention') head.append(node('span', 'Enthaltung', 'pill pill-neutral'));
    if (e.kind === 'proposal') head.append(node('span', 'Kompromissvorschlag', 'pill pill-good'));
    if (e.kind === 'closing') head.append(node('span', e.payload.status === 'konsens' ? 'Abschluss: Konsens' : 'Abschluss: Teilkompromiss', e.payload.status === 'konsens' ? 'pill pill-good' : 'pill pill-warn'));
    content.append(head);
    const text = e.kind === 'vote' ? e.payload.begruendung : textOf(e.payload) || e.payload.grund || '';
    content.append(node('p', text, 'post-body'));
    if (e.kind === 'question' && e.payload.kontext) content.append(node('p', e.payload.kontext, 'mt-1 text-sm text-slate-500'));
    if (e.kind === 'proposal') {
      renderList(content, 'Gemeinsamkeiten', e.payload.sammelstand?.gemeinsamkeiten);
      renderList(content, 'Streitpunkte', conflicts(e.payload.sammelstand));
      renderList(content, 'Zugeständnisse', e.payload.sammelstand?.zugestaendnisse);
    }
    if (e.kind === 'closing') {
      renderList(content, 'Zustimmung', e.payload.zustimmung);
      renderList(content, 'Tolerierung', e.payload.tolerierung);
      renderList(content, 'Ablehnung', (e.payload.ablehnung || []).map(a => `${a.agent}: ${a.grund}`));
      renderList(content, 'Offene Punkte', e.payload.offene_punkte);
    }
    if (e.kind === 'statement') {
      renderList(content, 'Zugeständnisse', e.payload.zugestaendnisse);
      renderList(content, 'Einigungen in der Gruppe', e.payload.einigungen_in_gruppe);
    }
    if (e.payload.begruendung && e.kind !== 'vote') {
      const details = node('details', '', 'mt-2 text-sm text-slate-600');
      details.append(node('summary', 'Begründung', 'cursor-pointer text-xs font-medium text-slate-500'), node('p', e.payload.begruendung, 'mt-1 whitespace-pre-wrap'));
      content.append(details);
    }
    if (e.actor) {
      const button = node('button', 'Aufrufprotokoll', 'link-quiet mt-1'); button.type = 'button'; button.dataset.trajectory = e.actor;
      content.append(button);
    }
    box.append(content);
    return box;
  }

  function systemLine(e) {
    const box = node('div', '', 'rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600'); box.id = `event-${e.id}`;
    if (e.kind === 'ballot') {
      const counts = {};
      for (const stimme of Object.values(e.payload.stimmen)) counts[stimme] = (counts[stimme] || 0) + 1;
      const parts = Object.entries(VOTES).filter(([k]) => counts[k]).map(([k, v]) => `${counts[k]} ${v.label}`);
      box.textContent = `${e.payload.konsens ? 'Konsens erreicht' : 'Kein Konsens'} · ${parts.join(' · ')}`;
    } else {
      const groups = Object.values(e.payload.gruppen).filter(m => m.length > 1);
      box.textContent = `Koalitionen: ${groups.map(m => m.map(institution).join(' + ')).join(' | ')}`;
    }
    return box;
  }

  function renderDiscussion() {
    if (renderedRun !== state.run) {
      rendered.clear(); feedPhase = ''; renderedRun = state.run;
      clear('#discussion-feed');
    }
    const feed = $('#discussion-feed');
    feed.querySelector('[data-empty]')?.remove();
    for (const e of state.events) {
      if (rendered.has(e.id) || !['question', 'statement', 'vote', 'abstention', 'proposal', 'closing', 'ballot', 'membership'].includes(e.kind)) continue;
      if (e.kind === 'membership' && !Object.values(e.payload.gruppen).some(m => m.length > 1)) continue;
      rendered.add(e.id);
      const key = `${e.block}:${e.round}:${e.phase}`;
      if (key !== feedPhase) {feed.append(node('h2', `Block ${e.block} · Runde ${e.round} · ${PHASES[e.phase] || e.phase}`, 'feed-phase')); feedPhase = key;}
      feed.append(['ballot', 'membership'].includes(e.kind) ? systemLine(e) : post(e));
    }
    if (!feed.children.length) {const empty = node('p', 'Die Verhandlung wird vorbereitet. Geprüfte Beiträge erscheinen hier.', 'muted'); empty.dataset.empty = 'true'; feed.append(empty);}
    const waiting = clear('#discussion-waiting');
    if (state.status !== 'running') return;
    if (!state.worker_alive) {waiting.textContent = 'Noch kein aktiver Worker erkannt. Die Simulation bleibt gespeichert.'; return;}
    for (const call of state.active_calls || []) {
      const who = call.actor ? institution(call.actor) : {watcher: 'Watcher', moderator: 'Moderator'}[call.role] || 'Orchestrator';
      const line = node('div', '', 'flex items-center gap-2');
      line.append(spinner(`${who} · ${TASKS[call.task] || 'Arbeitet'} …`));
      const since = node('span', '', 'text-xs text-slate-400'); since.dataset.callStart = call.started; line.append(since);
      waiting.append(line);
    }
    if (!state.active_calls?.length) waiting.append(spinner('Nächster Schritt wird vorbereitet …'));
  }

  /* ---------- Side panel ---------- */
  function renderSide() {
    const status = clear('#watcher-status');
    if (state.watcher_updating) status.append(spinner('Wird aktualisiert …'));
    else if (state.watcher_error) status.append(node('p', `${state.watcher_error.text} Voriger Stand bleibt sichtbar.`, 'notice notice-warn'));
    else status.textContent = state.watcher ? `Stand: ${new Date(state.watcher.at).toLocaleTimeString('de-AT')}` : 'Erste Zusammenfassung folgt nach der ersten Phase.';
    const key = String(state.watcher?.id ?? '');
    if (key !== watcherKey) {
      watcherKey = key;
      const watcher = clear('#watcher');
      const sections = {gemeinsamkeiten: 'Gemeinsamkeiten', vorschlaege: 'Vorschläge', konflikte: 'Offene Konflikte', zugestaendnisse: 'Zugeständnisse', positionsaenderungen: 'Positionsänderungen', koalitionen: 'Koalitionen', abstimmung: 'Abstimmung'};
      if (state.watcher) {
        const summary = state.watcher.payload.summary;
        for (const [name, title] of Object.entries(sections)) {
          if (!summary[name]?.length) continue;
          const box = node('section');
          box.append(node('h3', title, 'mb-1 text-xs font-semibold uppercase tracking-wider text-slate-500'));
          const list = node('ul', '', 'list-disc space-y-1 pl-4 text-sm text-slate-600');
          for (const text of summary[name]) list.append(node('li', text));
          box.append(list); watcher.append(box);
        }
        const refs = node('div', '', 'flex flex-wrap gap-x-3 gap-y-1');
        for (const ref of summary.referenzen || []) {const a = node('button', `Beleg #${ref}`, 'link-quiet'); a.type = 'button'; a.dataset.event = ref; refs.append(a);}
        watcher.append(refs);
      }
    }
    $('#proposal').textContent = state.proposal?.kompromissvorschlag || 'Noch kein Vorschlag.';
    const coalitions = clear('#coalitions');
    const groups = Object.values(state.groups).filter(m => m.length > 1);
    if (!groups.length) coalitions.append(node('p', 'Alle Institutionen verhandeln einzeln.', 'muted'));
    groups.forEach((members, index) => {
      const row = node('div', '', 'flex gap-2');
      const names = node('div', '', 'space-y-1');
      for (const code of members) {
        const member = node('div', '', 'flex items-center gap-2');
        member.append(actorIcon(code, 'inst-icon-sm'), node('span', institution(code), 'text-slate-700'));
        names.append(member);
      }
      row.append(node('span', `G${index + 1}`, 'marker marker-group shrink-0'), names);
      coalitions.append(row);
    });
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
  const REPORT_LABELS = {status: 'Status', ergebnis: 'Ergebnis', zustimmung: 'Zustimmung', tolerierung: 'Tolerierung', ablehnung: 'Ablehnung', offene_punkte: 'Offene Punkte', empfehlung_neustart: 'Empfehlung', begruendung_empfehlung: 'Begründung der Empfehlung', koalitionsbildung: 'Koalitionsbildung', entscheidungsfindung: 'Entscheidungsfindung', offene_konflikte: 'Offene Konflikte', auffaelligkeiten: 'Auffälligkeiten', wendepunkte: 'Wendepunkte', verlauf: 'Verlauf je Runde', statistik: 'Statistik'};

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
    renderStakeholders();
    renderTimeline();
    renderTimelineLegend();
    renderVoteChart();
    renderCostChart();
    renderKpis();
    renderDiscussion();
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

  function selectView(view) {
    selected = view;
    for (const panel of document.querySelectorAll('[data-view-panel]')) panel.classList.toggle('hidden', panel.id !== view);
    for (const tab of document.querySelectorAll('[data-view]')) {
      tab.classList.toggle('view-tab-active', tab.dataset.view === view);
      tab.setAttribute('aria-selected', String(tab.dataset.view === view));
    }
    if (view === 'analysis') {analysisKey = ''; renderAnalysis();}
    if (view === 'groups') renderGroups();
    if (view === 'overview') for (const chart of Object.values(charts)) chart.resize();
  }

  async function showReference(ref) {
    const target = document.getElementById(`event-${ref}`);
    if (target) {
      selectView('discussion');
      target.scrollIntoView({behavior: 'smooth', block: 'center'});
      target.classList.add('rounded-lg', 'ring-2', 'ring-teal-600', 'ring-offset-4');
      setTimeout(() => target.classList.remove('rounded-lg', 'ring-2', 'ring-teal-600', 'ring-offset-4'), 2000);
      return;
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
