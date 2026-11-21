'use strict';
const $ = id => document.getElementById(id);
const state = { rows: [], write: new Set(), readDefault: true, readAllow: new Set(), readDeny: new Set(), baseline: '', revision: null, csrf: '', filter: 'all', limit: 100, busy: true, dead: false };
const kinds = { user: 'Личный чат', group: 'Группа', bot: 'Бот', channel: 'Канал' };
const errors = {
  browser_required: 'Откройте редактор командой из Codex. Доступ предоставляется только в открывшемся браузере.',
  editor_expired: 'Время работы редактора истекло. Попросите агента открыть его снова.',
  session_busy_try_later: 'Telegram сейчас занят другой операцией. Подождите несколько секунд и обновите список.',
  dialogs_unavailable: 'Не удалось загрузить чаты Telegram. Проверьте подключение и повторите попытку. Если аккаунт ещё не подключён, выполните вход через telegram-setup.',
  whitelist_changed: 'Список разрешённых чатов уже изменён в другом окне. Обновите список и выберите чаты заново.',
  unknown_chat: 'Этот чат больше недоступен для добавления. Обновите список.',
  too_many_chats: 'В аккаунте слишком много чатов для этого редактора. Существующий список не изменён.',
  editor_unavailable: 'Редактор не смог выполнить действие. Попробуйте ещё раз.',
  network: 'Нет связи с локальным редактором. Если 30 минут уже прошли, попросите агента открыть его снова.'
};
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = !message; }
function fingerprint() { return JSON.stringify([state.readDefault, [...state.readAllow].sort(), [...state.readDeny].sort(), [...state.write].sort()]); }
function dirty() { return fingerprint() !== state.baseline; }
function readable(id) { return !state.readDeny.has(id) && (state.readDefault || state.readAllow.has(id)); }
function controls() {
  for (const id of ['search', 'refresh', 'all', 'allowed', 'blocked', 'close', 'read-default', 'read-all']) $(id).disabled = state.busy || state.dead;
  $('save').disabled = state.busy || state.dead || !dirty();
  document.querySelectorAll('.chat-row input').forEach(el => { el.disabled = state.busy || state.dead || el.dataset.available !== 'true'; });
}
function counts() {
  $('selected-count').textContent = state.write.size; $('allowed-count').textContent = state.write.size;
  $('all-count').textContent = state.rows.length;
  $('blocked-count').textContent = state.rows.filter(r => !readable(r.chat_id)).length;
  $('read-default').checked = state.readDefault;
  $('read-summary').textContent = state.readDefault ? `Чтение разрешено во всех чатах. Исключений: ${state.readDeny.size}.` : `Чтение только в выбранных чатах: ${state.readAllow.size}.`;
  $('change-title').textContent = dirty() ? 'Есть несохранённые изменения' : 'Права сохранены';
  $('change-detail').textContent = 'Чтение и отправка настраиваются отдельно.'; controls();
}
function render() {
  const query = $('search').value.trim().toLocaleLowerCase('ru');
  const matched = state.rows.filter(row => (state.filter !== 'allowed' || state.write.has(row.chat_id)) && (state.filter !== 'blocked' || !readable(row.chat_id)) && `${row.label} ${row.username ? '@' + row.username : ''} ${row.chat_id}`.toLocaleLowerCase('ru').includes(query));
  $('chat-list').replaceChildren();
  for (const row of matched.slice(0, state.limit)) {
    const container = document.createElement('div'); container.className = 'chat-row';
    container.classList.toggle('chosen', state.write.has(row.chat_id));
    const avatar = document.createElement('span'); avatar.className = 'avatar'; avatar.setAttribute('aria-hidden', 'true'); avatar.textContent = row.label.split(/\s+/u).slice(0, 2).map(s => [...s][0] || '').join('').toLocaleUpperCase('ru');
    const info = document.createElement('span'); info.className = 'chat-info';
    const name = document.createElement('span'); name.className = 'chat-name'; name.textContent = row.label;
    const meta = document.createElement('span'); meta.className = 'chat-meta'; meta.textContent = row.reason || [row.username ? '@' + row.username : '', row.chat_id].filter(Boolean).join(' · '); info.append(name, meta);
    container.append(avatar, info);
    for (const mode of ['read', 'write']) {
      const label = document.createElement('label'); label.className = 'permission-cell';
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
      checkbox.checked = mode === 'read' ? readable(row.chat_id) : state.write.has(row.chat_id);
      checkbox.setAttribute('aria-label', `${mode === 'read' ? 'Читать' : 'Отправлять в'} ${row.label}`);
      checkbox.dataset.available = String(row[mode + '_selectable']); checkbox.disabled = state.busy || !row[mode + '_selectable'];
      checkbox.addEventListener('change', () => {
        if (mode === 'write') checkbox.checked ? state.write.add(row.chat_id) : state.write.delete(row.chat_id);
        else if (checkbox.checked) { state.readDeny.delete(row.chat_id); if (!state.readDefault) state.readAllow.add(row.chat_id); }
        else { state.readAllow.delete(row.chat_id); if (state.readDefault) state.readDeny.add(row.chat_id); }
        container.classList.toggle('chosen', state.write.has(row.chat_id)); notice('');
        if (state.filter !== 'all') render(); else counts();
      });
      label.append(checkbox); container.append(label);
    }
    $('chat-list').append(container);
  }
  $('loading').hidden = true; $('empty').hidden = matched.length !== 0;
  $('empty').querySelector('h2').textContent = state.filter === 'blocked' && !query ? 'Чтение нигде не запрещено' : 'Чатов не найдено';
  $('empty').querySelector('p').textContent = 'Измените поиск или фильтр.';
  $('results-count').textContent = `Найдено: ${matched.length}`; $('more').hidden = matched.length <= state.limit; $('more').textContent = `Показать ещё ${Math.min(100, matched.length - state.limit)}`; counts();
}
async function api(path, body) {
  try {
    const headers = { 'X-Editor-Request': '1' }; const options = { headers, credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) { options.method = 'POST'; headers['Content-Type'] = 'application/json'; headers['X-Editor-CSRF'] = state.csrf; options.body = JSON.stringify(body); }
    const response = await fetch(path, options); const result = await response.json(); if (!response.ok) throw new Error(result.error || 'editor_unavailable'); return result;
  } catch (error) { if (error instanceof TypeError || error instanceof SyntaxError) throw new Error('network'); throw error; }
}
function lock(title, text) {
  state.dead = true; state.write.clear(); state.readAllow.clear(); state.readDeny.clear(); state.rows = []; $('chat-list').replaceChildren(); $('editor').hidden = true; $('footer').hidden = true; $('session-note').hidden = true;
  $('selected-count').textContent = '—'; $('locked-title').textContent = title; $('locked-text').textContent = text; $('locked').hidden = false; notice('');
}
function failure(error) { const message = errors[error.message] || errors.editor_unavailable; if (['browser_required', 'editor_expired'].includes(error.message)) lock('Откройте редактор заново', message); else notice(message, true); }
function accept(result) {
  state.rows = result.chats.sort((a, b) => Number(b.can_write) - Number(a.can_write) || a.label.localeCompare(b.label, 'ru'));
  state.write = new Set(result.write); state.readDefault = result.read.default;
  state.readAllow = new Set(result.read.allow); state.readDeny = new Set(result.read.deny);
  state.baseline = fingerprint(); state.revision = result.revision; state.limit = 100; render();
}
async function load(refresh = false) { state.busy = true; controls(); notice(''); try { accept(await api(refresh ? '/api/refresh' : '/api/chats', refresh ? {} : undefined)); } catch (error) { failure(error); $('loading').hidden = true; } finally { state.busy = false; controls(); } }
$('search').addEventListener('input', () => { state.limit = 100; render(); });
for (const filter of ['all', 'allowed', 'blocked']) $(filter).addEventListener('click', () => { state.filter = filter; state.limit = 100; for (const id of ['all', 'allowed', 'blocked']) { $(id).classList.toggle('active', id === filter); $(id).setAttribute('aria-pressed', String(id === filter)); } render(); });
$('read-default').addEventListener('change', () => { state.readDefault = $('read-default').checked; render(); notice(''); });
$('read-all').addEventListener('click', () => { state.readDefault = true; state.readAllow.clear(); state.readDeny.clear(); render(); notice(''); });
$('more').addEventListener('click', () => { state.limit += 100; render(); });
$('refresh').addEventListener('click', () => { if (!dirty() || confirm('Обновить список? Несохранённый выбор будет сброшен.')) load(true); });
$('save').addEventListener('click', async () => {
  if (!dirty() || state.busy) return; state.busy = true; controls(); notice('');
  try { accept(await api('/api/save', { revision: state.revision, read: {default: state.readDefault, allow: [...state.readAllow], deny: [...state.readDeny]}, write: [...state.write] })); notice('Готово. Доступ к выбранным чатам сохранён. Можно вернуться в Codex.'); } catch (error) { failure(error); } finally { state.busy = false; controls(); }
});
$('close').addEventListener('click', async () => {
  if (dirty() && !confirm('Закрыть редактор без сохранения изменений?')) return; state.busy = true; controls();
  try { await api('/api/close', {}); lock('Редактор закрыт', 'Можно закрыть эту вкладку и вернуться в Codex.'); } catch (error) { failure(error); } finally { state.busy = false; controls(); }
});
window.addEventListener('beforeunload', event => { if (dirty() && !state.dead) { event.preventDefault(); event.returnValue = ''; } });
(async () => {
  const token = new URLSearchParams(location.hash.slice(1)).get('token'); if (location.hash) history.replaceState(null, '', '/');
  try { if (token) await api('/api/unlock', { token }); const session = await api('/api/session'); state.csrf = session.csrf; $('search').value = session.search; setTimeout(() => lock('Время работы истекло', errors.editor_expired), session.seconds_left * 1000); await load(); $('search').focus(); }
  catch (error) { state.busy = false; failure(error); controls(); }
})();
