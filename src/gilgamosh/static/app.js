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
      else notify('Gespeichert.');
    } catch (exc) { error.textContent = exc instanceof SyntaxError ? 'Ungültiges JSON. Bitte Eingabe prüfen.' : exc.message; }
    finally {button.disabled = false; form.querySelector('[data-loading]')?.classList.add('hidden');}
  });
}
for (const tab of document.querySelectorAll('[data-admin-tab]')) tab.addEventListener('click', () => {
  for (const panel of document.querySelectorAll('[data-admin-panel]')) panel.classList.toggle('hidden', panel.dataset.adminPanel !== tab.dataset.adminTab);
  for (const other of document.querySelectorAll('[data-admin-tab]')) other.classList.toggle('tab-active', other === tab);
});
for (const button of document.querySelectorAll('[data-probe]')) button.addEventListener('click', async () => {
  button.disabled = true; const original = button.textContent; button.textContent = 'Verbindung wird geprüft …';
  try {const result = await api(`/api/admin/endpoints/${button.dataset.probe}/test`, {}); notify(result.text);}
  catch (exc) {notify(exc.message);} finally {button.disabled = false; button.textContent = original;}
});
