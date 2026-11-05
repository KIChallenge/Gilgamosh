'use strict';
window.api = async (path, data, method = 'POST') => {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content}, ...(method !== 'GET' ? {body: JSON.stringify(data)} : {})});
  const result = response.status === 204 ? {} : await response.json();
  if (!response.ok) throw new Error(result.error || 'Anfrage fehlgeschlagen.');
  return result;
};
window.notify = text => {
  const box = document.querySelector('#notification');
  box.querySelector('span').textContent = text; box.classList.remove('hidden');
  setTimeout(() => box.classList.add('hidden'), 5000);
};

const LEVELS = {
  ok: {pill: 'pill-good', notice: 'notice-good', label: '✓ In Ordnung'},
  warning: {pill: 'pill-warn', notice: 'notice-warn', label: '! Warnung'},
  unknown: {pill: 'pill-neutral', notice: 'notice-info', label: '? Nicht geprüft'},
  error: {pill: 'pill-bad', notice: 'notice-bad', label: '✕ Fehler'}
};

function element(tag, cls = '', text = '') {
  const node = document.createElement(tag);
  node.className = cls; node.textContent = text;
  return node;
}

function connectionNotice(item) {
  const level = LEVELS[item.level];
  const box = element('div', `notice ${level.notice} flex-col gap-1`);
  const head = element('div', 'flex flex-wrap items-center gap-2');
  head.append(element('span', `pill ${level.pill}`, level.label));
  if (item.model) head.append(element('code', 'text-xs font-semibold', item.model));
  if (item.endpoint) head.append(element('span', 'text-xs opacity-75', `über ${item.endpoint}`));
  if (item.context_length) head.append(element('span', 'text-xs opacity-75', `${item.context_length.toLocaleString('de-AT')} Tokens Kontext`));
  box.append(head, element('p', '', item.message));
  if (item.used_by?.length) box.append(element('p', 'text-xs opacity-75', `Genutzt von: ${item.used_by.join(', ')}`));
  return box;
}

async function checkConnections() {
  const box = document.querySelector('#connection-status');
  if (!box) return;
  const showAll = box.dataset.mode === 'all';
  if (showAll) box.replaceChildren(element('p', 'muted', 'Verbindungen werden geprüft …'));
  try {
    const result = await api('/api/admin/connections/check', null, 'GET');
    box.replaceChildren();
    if (result.mode === 'fake') {
      if (showAll) box.append(element('p', 'notice notice-info', 'Offline-Demo: Es wird kein Anbieter aufgerufen, Verbindungen bleiben ungeprüft.'));
      return;
    }
    const problems = result.items.filter(i => i.level !== 'ok');
    for (const item of showAll ? result.items : problems) box.append(connectionNotice(item));
    if (!showAll && problems.length) {
      const link = element('a', 'link-quiet', 'Zur Verbindungsverwaltung');
      link.href = '/admin#models';
      box.append(link);
    }
    for (const holder of document.querySelectorAll('[data-endpoint-status]')) {
      const own = result.items.filter(i => i.endpoint_id === holder.dataset.endpointStatus);
      holder.replaceChildren();
      const worst = ['error', 'warning', 'unknown', 'ok'].find(level => own.some(i => i.level === level));
      if (worst) holder.append(element('span', `pill ${LEVELS[worst].pill}`, LEVELS[worst].label));
    }
    const badge = document.querySelector('#connection-badge');
    if (badge) {
      const errors = result.items.filter(i => i.level === 'error').length;
      badge.replaceChildren();
      if (errors) badge.append(element('span', 'pill pill-bad ml-1', String(errors)));
    }
  } catch (exc) {
    box.replaceChildren(element('p', 'notice notice-warn', `Verbindungsprüfung fehlgeschlagen: ${exc.message}`));
  }
}

for (const form of document.querySelectorAll('form[data-api]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const error = form.querySelector('[data-error]'); error.textContent = '';
    const button = form.querySelector('button[type="submit"], button:not([type])');
    button.disabled = true; form.querySelector('[data-loading]')?.classList.remove('hidden');
    try {
      const data = {};
      for (const input of form.querySelectorAll('[name]')) {
        if (input.name === 'actors') continue;
        let value = input.type === 'checkbox' ? input.checked : input.value;
        if (input.hasAttribute('data-json')) value = JSON.parse(value);
        if (input.type === 'number') value = Number(value);
        if (input.name.endsWith('endpoint_id') || input.name === 'parent_id') value ||= null;
        const parts = input.name.split('.'); let target = data;
        for (const key of parts.slice(0, -1)) target = target[key] ||= {};
        target[parts.at(-1)] = value;
      }
      if (form.querySelector('[name="actors"]')) data.actors = [...form.querySelectorAll('[name="actors"]:checked')].map(x => x.value);
      const result = await api(form.dataset.api, data);
      if (form.dataset.success === 'navigate') location.assign(result.url);
      else if (form.dataset.success === 'reload') location.reload();
      else {notify('Gespeichert.'); checkConnections();}
    } catch (exc) { error.textContent = exc instanceof SyntaxError ? 'Ungültiges JSON. Bitte Eingabe prüfen.' : exc.message; }
    finally {button.disabled = false; form.querySelector('[data-loading]')?.classList.add('hidden');}
  });
}

function selectAdminTab(name) {
  const tab = document.querySelector(`[data-admin-tab="${name}"]`);
  if (!tab) return;
  for (const panel of document.querySelectorAll('[data-admin-panel]')) panel.classList.toggle('hidden', panel.dataset.adminPanel !== name);
  for (const other of document.querySelectorAll('[data-admin-tab]')) other.classList.toggle('view-tab-active', other === tab);
}
// Every tab change is a history entry, so the back button returns to the previous tab.
for (const tab of document.querySelectorAll('[data-admin-tab]')) tab.addEventListener('click', () => {
  selectAdminTab(tab.dataset.adminTab);
  history.pushState(null, '', `#${tab.dataset.adminTab}`);
});
if (location.hash) selectAdminTab(location.hash.slice(1));
window.addEventListener('popstate', () => selectAdminTab(location.hash.slice(1) || 'actors'));

for (const button of document.querySelectorAll('[data-probe]')) button.addEventListener('click', async () => {
  const result = button.parentElement.querySelector('[data-probe-result]');
  button.disabled = true; const original = button.textContent; button.textContent = 'Verbindung wird geprüft …';
  result.textContent = ''; result.className = 'text-sm';
  try {
    result.textContent = (await api(`/api/admin/endpoints/${button.dataset.probe}/test`, {})).text;
    result.classList.add('text-teal-800');
  } catch (exc) {
    result.textContent = exc.message;
    result.classList.add('text-red-700');
  } finally {button.disabled = false; button.textContent = original;}
});
document.querySelector('#connection-recheck')?.addEventListener('click', checkConnections);

// Live feedback while typing a model name, against the same catalog the server checks.
(async () => {
  const inputs = [...document.querySelectorAll('[data-model-input]')];
  if (!inputs.length) return;
  let ids;
  try { ids = new Set((await api('/api/admin/connections/models', null, 'GET')).ids); } catch { return; }
  if (!ids.size) return;
  const list = document.querySelector('#model-catalog');
  list.append(...[...ids].map(id => {const option = document.createElement('option'); option.value = id; return option;}));
  for (const input of inputs) {
    const hint = element('span', 'text-xs');
    input.after(hint);
    const update = () => {
      const value = input.value.trim();
      const known = ids.has(value) || ids.has(value.split(':')[0]);
      hint.textContent = !value ? '' : known ? '✓ Im Modellkatalog' : '✕ Nicht im Modellkatalog';
      hint.className = `text-xs ${known ? 'text-teal-800' : 'text-red-700'}`;
      input.classList.toggle('input-error', Boolean(value) && !known);
    };
    input.addEventListener('input', update);
    update();
  }
})();

// Start form: selection helpers.
(() => {
  const boxes = [...document.querySelectorAll('input[name="actors"]')];
  if (!boxes.length) return;
  const update = () => {
    const count = boxes.filter(box => box.checked).length;
    document.querySelector('#selection-count').textContent = `· ${count} ausgewählt`;
    document.querySelector('#call-estimate').textContent = count ? `mindestens ${(count * 10 + 2).toLocaleString('de-AT')} LLM-Aufrufe je Runde` : '';
  };
  for (const box of boxes) box.addEventListener('change', update);
  for (const button of document.querySelectorAll('[data-select]')) button.addEventListener('click', () => {
    for (const box of boxes) box.checked = button.dataset.select === 'all' || (button.dataset.select === 'default' && box.hasAttribute('data-default'));
    update();
  });
  update();
})();

checkConnections();
