'use strict';
(() => {
  const root = document.querySelector('#simulation'); if (!root) return;
  const id = root.dataset.id, base = `/api/simulations/${id}`;
  let state, source, refreshing = false, again = false, selected = 'discussion', chart;
  const $ = selector => document.querySelector(selector);
  const label = {running:'Läuft', paused:'Pausiert', completed:'Abgeschlossen', awaiting_decision:'Entscheidung erforderlich', ended:'Beendet'};
  const phases = {start:'Vorbereitung',question:'Frage',individual:'Einzelpositionen',coalition:'Koalitionen',groups:'Gruppenverhandlung',proposal:'Kompromissvorschlag',vote:'Abstimmung',closing:'Abschluss',done:'Abgeschlossen'};
  const tasks = {einzelphase:'Antwortet', statement_pruefen:'Wird geprüft', sicherheit:'Inhalt wird geprüft',
    koalitionsphase:'Prüft Koalitionen', zustimmung:'Prüft Gruppenbeitritt', gruppennachricht:'Verhandelt',
    gruppenstatement:'Formuliert Position', abstimmung:'Stimmt ab', frage_stellen:'Formuliert Frage',
    sammelstand_und_vorschlag:'Entwickelt Vorschlag', zwischenstand:'Watcher aktualisiert den Stand',
    abschluss:'Erstellt Abschluss', abschlussbericht:'Watcher erstellt Abschlussbericht'};
  const node = (tag, text = '', cls = '') => {const n = document.createElement(tag); n.textContent = text; n.className = cls; return n;};
  const clear = selector => {const n = $(selector); n.replaceChildren(); return n;};
  function spinner(text) {const n = node('span', '', 'inline-flex items-center gap-2'); const spin = node('span','','loading loading-spinner loading-xs text-teal-700'); spin.setAttribute('aria-hidden','true'); n.append(spin,node('span',text)); return n;}
  function institution(code) {return state.actors[code]?.name || code || 'System';}
  function textOf(payload) {return payload.position || payload.statement || payload.inhalt || payload.kompromissvorschlag || payload.ergebnis || payload.frage || payload.begruendung || '';}
  function render() {
    $('#question').textContent = state.question;
    $('#phase-status').textContent = `${label[state.status]} · Runde ${state.round} von max. ${state.params.rounds} · ${state.phase_label}`;
    $('#error').textContent = state.error; $('#error').classList.toggle('hidden', !state.error);
    $('#decision').classList.toggle('hidden', state.status !== 'awaiting_decision');
    $('#pause').classList.toggle('hidden', state.status !== 'running'); $('#resume').classList.toggle('hidden', state.status !== 'paused');
    $('#end').classList.toggle('hidden', ['ended','completed'].includes(state.status));
    for (const item of document.querySelectorAll('[data-phase]')) {
      item.classList.toggle('badge-success', item.dataset.phase === state.phase);
      item.classList.toggle('badge-ghost', item.dataset.phase !== state.phase);
    }
    const active = state.active_calls || [];
    const activity = clear('#active-status');
    if (state.status === 'running' && state.worker_alive) activity.append(spinner(`${active.length} Calls aktiv`));
    else activity.textContent = state.status === 'running' ? 'Wartet auf Worker' : '';
    const actors = clear('#actors');
    for (const [code, actor] of Object.entries(state.actors)) {
      const box = node('div'); box.append(node('h3',actor.name,'font-medium text-sm'));
      const current = active.find(c => c.actor === code);
      const last = [...state.events].reverse().find(e => e.actor === code && ['statement','vote','abstention'].includes(e.kind) && e.round === state.round && e.block === state.block && e.phase === state.phase);
      if (current && state.worker_alive) {const status = spinner(tasks[current.task] || 'Arbeitet'); status.classList.add('text-xs','mt-2'); box.append(status); const since=node('p','','text-xs text-slate-400'); since.dataset.callStart=current.started; box.append(since);}
      else box.append(node('p', state.status === 'paused' ? 'Pausiert' : last ? (last.kind === 'abstention' ? 'Enthaltung' : '✓ Freigegeben') : ['completed','ended','awaiting_decision'].includes(state.status) ? 'Abgeschlossen' : '◷ Wartet', 'text-xs text-slate-500 mt-2'));
      const button = node('button','Aufrufprotokoll','block text-xs text-teal-800 mt-2 underline'); button.dataset.trajectory = code; box.append(button); actors.append(box);
    }
    const discussion = clear('#discussion');
    let phase = '';
    for (const e of state.events.filter(e => ['question','statement','vote','abstention','proposal','closing'].includes(e.kind))) {
      const key = `${e.block}:${e.round}:${e.phase}`;
      if (key !== phase) {discussion.append(node('h2',`Block ${e.block} · Runde ${e.round} · ${phases[e.phase] || e.phase}`, 'phase text-xs font-medium text-slate-500')); phase = key;}
      const box = node('article','','space-y-2'); box.id = `event-${e.id}`;
      box.append(node('h3',e.actor ? institution(e.actor) : 'Moderator','font-semibold text-sm'));
      const content = textOf(e.payload) || e.payload.grund || e.payload.stimme;
      box.append(node('p', e.kind === 'vote' ? `${e.payload.stimme}: ${e.payload.begruendung}` : content, 'chat-bubble w-full'));
      if (e.payload.begruendung && e.kind !== 'vote') {const details = node('details','','text-sm text-slate-600'); details.append(node('summary','Begründung','cursor-pointer'),node('p',e.payload.begruendung,'mt-2')); box.append(details);}
      if (e.actor) {const button=node('button','Aufrufprotokoll','text-xs underline text-teal-800'); button.dataset.trajectory=e.actor; box.append(button);}
      discussion.append(box);
    }
    if (!discussion.children.length) discussion.append(node('p','Die Verhandlung wird vorbereitet. Geprüfte Beiträge erscheinen hier.','text-slate-500'));
    if (state.status === 'running') {
      const waiting = node('div','','border-t pt-4 text-sm text-slate-500');
      if (state.worker_alive) {
        for (const call of active) waiting.append(spinner(`${call.actor ? institution(call.actor) : call.role === 'watcher' ? 'Watcher' : call.role === 'moderator' ? 'Moderator' : 'Orchestrator'} · ${tasks[call.task] || 'Arbeitet'} …`), node('br'));
        if (!active.length) waiting.append(spinner('Nächster Schritt wird vorbereitet …'));
      } else waiting.textContent = 'Noch kein aktiver Worker erkannt. Die Simulation bleibt gespeichert.';
      discussion.append(waiting);
    }
    const ws = clear('#watcher-status');
    if (state.watcher_updating) ws.append(spinner('Wird aktualisiert …'));
    else if (state.watcher_error) ws.append(node('p',`${state.watcher_error.text} Voriger Stand bleibt sichtbar.`, 'text-amber-800 stale pl-2'));
    else ws.textContent = state.watcher ? `Stand: ${new Date(state.watcher.at).toLocaleTimeString('de-AT')}` : 'Erste Zusammenfassung folgt nach der ersten Phase.';
    const watcher = clear('#watcher');
    const sections = {gemeinsamkeiten:'Gemeinsamkeiten',vorschlaege:'Vorschläge',konflikte:'Offene Konflikte',zugestaendnisse:'Zugeständnisse',positionsaenderungen:'Positionsänderungen',koalitionen:'Koalitionen',abstimmung:'Abstimmung'};
    if (state.watcher) {
      for (const [key,title] of Object.entries(sections)) {const values=state.watcher.payload.summary[key] || []; if (!values.length) continue;
        const box=node('section'); box.append(node('h3',title,'text-sm font-semibold mb-1')); const list=node('ul','','list-disc pl-4 text-sm text-slate-600 space-y-1'); for(const text of values) list.append(node('li',text)); box.append(list); watcher.append(box);}
      const refs=node('div','','flex flex-wrap gap-2 text-xs');
      for(const ref of state.watcher.payload.summary.referenzen || []) {const a=node('button',`Beleg #${ref}`,'underline text-teal-800'); a.addEventListener('click',()=>showReference(ref)); refs.append(a);} watcher.append(refs);
    }
    $('#proposal').textContent = state.proposal?.kompromissvorschlag || 'Noch kein Vorschlag.';
    const votes = clear('#votes'); for(const [code,vote] of Object.entries(state.votes)) votes.append(node('p',`${institution(code)}: ${vote}`));
    const coalitions = clear('#coalitions'); for(const members of Object.values(state.groups)) coalitions.append(node('p',members.map(institution).join(' · '),members.length > 1 ? 'font-medium text-teal-800' : 'text-slate-500'));
    $('#worker-status').textContent = state.worker_alive ? 'Worker aktiv' : state.status === 'running' ? 'Kein aktiver Arbeitsschritt erkannt' : '';
    const last = state.events.at(-1); $('#last-activity').textContent = last ? `Letzte Aktivität: ${new Date(last.at).toLocaleTimeString('de-AT')}` : '';
    updateElapsed();
    if (selected === 'analysis') renderAnalysis();
    if (selected === 'groups') renderGroups();
  }
  async function refresh() {if(refreshing) {again=true; return;} refreshing=true;
    try {state=await api(base, null, 'GET'); render();} catch(exc) {notify(exc.message);} finally {refreshing=false;if(again){again=false;refresh();}}
  }
  function updateElapsed() {if(!state)return; const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(state.created_at))/1000));
    for (const item of document.querySelectorAll('[data-call-start]')) item.textContent=`seit ${Math.max(0,Math.floor((Date.now()-Date.parse(item.dataset.callStart))/1000))} Sekunden`;
    $('#elapsed').textContent=`Gestartet vor ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} min`;
  }
  async function renderGroups() {try {const result=await api(`${base}/groups`,null,'GET'); const box=clear('#groups');
    const allGroups = {...result.groups};
    for (const e of result.events.filter(e=>e.kind==='group_message')) allGroups[e.group] ||= e.payload.members;
    for(const [code,members] of Object.entries(allGroups)) {const section=node('section','','border-b pb-4');section.append(node('h2',members.map(institution).join(' · ')+(result.groups[code]?'':' · Archivierter Kanal'),'font-semibold mb-3'));
      const messages=result.events.filter(e=>e.kind==='group_message'&&e.group===code);for(const e of messages)section.append(node('p',`${institution(e.actor)}: ${e.payload.inhalt}`,'text-sm mb-3'));if(!messages.length)section.append(node('p',members.length===1?'Einzelgruppe · kein interner Dialog.':'Noch keine Gruppennachrichten.','text-sm text-slate-500'));box.append(section);}
    const history=node('details'); history.append(node('summary','Mitgliedschaftsverlauf','cursor-pointer text-sm')); for(const e of result.events.filter(e=>e.kind==='membership')) history.append(node('p',`Runde ${e.round}: ${Object.values(e.payload.gruppen).map(m=>m.map(institution).join(' + ')).join(' | ')}`,'text-xs my-2'));box.append(history);
  } catch(exc){notify(exc.message);}}
  async function renderAnalysis() {
    const ballots=clear('#ballots');const table=node('table','','table table-sm');const head=node('tr');head.append(node('th','Block / Runde'));for(const a of Object.keys(state.actors))head.append(node('th',institution(a)));table.append(head);
    for(const e of state.events.filter(e=>e.kind==='ballot')){const row=node('tr');row.append(node('td',`${e.block} / ${e.round}`));for(const a of Object.keys(state.actors)){const v=e.payload.stimmen[a]||'enthaltung';row.append(node('td',v, v==='ablehnen'?'bg-red-50 text-red-800':v==='zustimmen'?'bg-teal-50 text-teal-800':'bg-amber-50'));}table.append(row);}ballots.append(table);
    const positions=clear('#positions');positions.append(node('h3','Positionsverlauf','font-semibold mb-3'));for(const [code,a] of Object.entries(state.actors)){const section=node('details','','border-b py-3');section.append(node('summary',a.name,'cursor-pointer font-medium'));for(const e of state.events.filter(e=>e.kind==='statement'&&e.actor===code)){section.append(node('p',`Runde ${e.round}: ${textOf(e.payload)}`,'text-sm mt-3'));for(const c of e.payload.zugestaendnisse||[])section.append(node('p',`Zugeständnis: ${c}`,'text-xs text-teal-800'));}positions.append(section);}
    try {const result=await api(`${base}/reports`,null,'GET');const box=clear('#reports');for(const e of result.items){const details=node('details','','border rounded p-3 mb-3');details.append(node('summary',`${e.kind==='report'?'Watcher-Bericht':'Moderatorabschluss'} · Durchlauf ${e.run}, Block ${e.block}`,'cursor-pointer'));details.append(node('pre',JSON.stringify(e.payload,null,2),'text-xs mt-3'));box.append(details);}
      const metrics=await api(`${base}/metrics`,null,'GET');const usage={};let duration=0;for(const [role,c] of Object.entries(metrics.roles)){usage[role]=c.tokens;duration+=c.duration;}
      if(window.Chart){chart?.destroy();chart=new Chart($('#usage-chart'),{type:'bar',data:{labels:Object.keys(usage),datasets:[{label:'Tokens',data:Object.values(usage),backgroundColor:'#14867d'}]},options:{maintainAspectRatio:false}});}
      const stats=node('p',`${metrics.calls} gespeicherte Calls · ${metrics.rejections} Rückweisungen · ${metrics.abstentions} Enthaltungen · ${duration.toFixed(1)} s summierte Aufrufzeit · ${metrics.known_cost_usd===null?'Kosten nicht gemeldet':metrics.known_cost_usd.toFixed(4)+' USD gemeldete Kosten'}${state.mode==='fake'?' · Offline-Demo: keine Providerkosten':''}`,'text-xs text-slate-500 mt-3');box.append(stats);
    }catch(exc){notify(exc.message);}
  }
  async function showReference(ref){const e=state.events.find(e=>e.id===ref);if(!e){try{const entry=await api(`${base}/protocol/${ref}`,null,'GET');$('#trajectory-content').textContent=JSON.stringify(entry,null,2);$('#trajectory-modal').showModal();}catch(exc){notify(exc.message);}return;}
    const target=document.getElementById(`event-${ref}`);if(target){selectView('discussion');target.scrollIntoView({behavior:'smooth',block:'center'});}else{notify(`Beleg #${ref}: ${textOf(e.payload)||e.kind}`);}}
  function selectView(view){selected=view;for(const p of document.querySelectorAll('[data-view-panel]'))p.classList.toggle('hidden',p.id!==view);for(const b of document.querySelectorAll('[data-view]'))b.classList.toggle('bg-slate-100',b.dataset.view===view);if(view==='analysis')renderAnalysis();if(view==='groups')renderGroups();}
  document.addEventListener('click',async event=>{
    const tab=event.target.closest('[data-view]');if(tab)selectView(tab.dataset.view);
    const trajectory=event.target.closest('[data-trajectory]');if(trajectory){try {const result=await api(`${base}/trajectories?actor=${encodeURIComponent(trajectory.dataset.trajectory)}`,null,'GET');$('#trajectory-content').textContent=JSON.stringify(result.items,null,2);$('#trajectory-modal').showModal();}catch(exc){notify(exc.message);}}
    const command=event.target.closest('[data-command]');if(command){command.disabled=true;try{await api(`${base}/control`,{action:command.dataset.command,key:crypto.randomUUID()});await refresh();}catch(exc){notify(exc.message);}finally{command.disabled=false;}}
  });
  async function start(){await refresh();source=new EventSource(`${base}/events?after=${state?.events.at(-1)?.id||0}`);source.onopen=()=>{$('#connection').textContent='Live-Verbindung aktiv';};source.onerror=()=>{$('#connection').textContent='Live-Verbindung unterbrochen · erneuter Verbindungsaufbau …';};source.onmessage=()=>refresh();setInterval(refresh,10000);setInterval(updateElapsed,1000);}
  start();
})();
