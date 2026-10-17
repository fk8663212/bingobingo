'use strict';
const $ = (id) => document.getElementById(id);
const page = document.body.dataset.page;
let state = null, csrf = '', busy = false, archiveId = null, latestGameId = null;
let resetGameId = null;
let refreshing = false, refreshAgain = false, online = false, toastTimer, eventStream;
let pendingMarks = null, savingMarks = false, lastCardSignature = '', lastTicketSignature = '';
let scannedCode = new URLSearchParams(location.hash.slice(1)).get('code') || '';
if (location.hash) history.replaceState(null, '', location.pathname + location.search);

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function show(id, visible) { if ($(id)) $(id).hidden = !visible; }
function text(id, value) { if ($(id)) $(id).textContent = value; }
function connection(ok, message) {
  online = ok;
  const el = $('connection');
  el.className = 'connection ' + (ok ? 'online' : 'offline');
  el.textContent = message || (ok ? '即時連線' : '重新連線中');
  if (page === 'host' && state) hostButtons();
}
function toast(message) {
  text('toast', message); show('toast', true); clearTimeout(toastTimer);
  toastTimer = setTimeout(() => show('toast', false), 5000);
}
function error(message) { text('page-error', message); show('page-error', !!message); }
async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout ? AbortSignal.timeout(15000) : undefined,
    });
  } catch (cause) {
    connection(false);
    throw new Error('連線中斷，請確認網路。畫面恢復後會自動同步。', { cause });
  }
  const result = await response.json();
  if (!response.ok) {
    const e = new Error(result.error || '操作失敗，請重新整理。');
    e.status = response.status;
    throw e;
  }
  return result;
}
async function refresh() {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    const url = page === 'host' ? '/api/admin/state' + (archiveId ? '?game=' + archiveId : '') : '/api/state';
    state = await api(url);
    csrf = state.csrf || '';
    if (page === 'host') latestGameId = state.games[0].id;
    show('demo-banner', state.demo);
    connection(true);
    error('');
    if (page === 'host') renderHost();
    else if (page === 'player') renderPlayer();
    else renderDisplay();
    renderHistory();
  } catch (e) {
    if (page === 'host' && e.status === 401) {
      state = null; csrf = ''; show('login-panel', true); show('host-console', false); show('logout', false);
      connection(true, '請登入');
    } else { error(e.message); }
  } finally {
    refreshing = false;
    if (refreshAgain) { refreshAgain = false; setTimeout(refresh, 100); }
  }
}
function renderHistory() {
  text('history-count', state.draws.length + ' / 49');
  const history = $('draw-history'); history.replaceChildren();
  if (!state.draws.length) { history.append(node('p', 'empty', '好運正在路上，請等待第一個號碼。')); return; }
  state.draws.forEach((d, i) => {
    const ball = node('span', 'history-ball' + (i === state.draws.length - 1 ? ' latest' : ''), d.number);
    ball.title = '第 ' + d.round + ' 回合';
    ball.setAttribute('aria-label', '第 ' + d.round + ' 回合，號碼 ' + d.number);
    history.append(ball);
  });
}
function latestNumber() { return state.draws.length ? state.draws[state.draws.length - 1].number : '—'; }
function codeFormat(code) { return (code.match(/.{1,4}/g) || []).join('-'); }
function gameStatus() { return ({waiting: '等待開獎', playing: '開獎進行中', finished: '本局已結束'})[state.game.status]; }

function renderPlayer() {
  const card = state.card;
  show('claim-panel', !card); show('card-panel', !!card);
  if (!card) {
    if (scannedCode) { $('ticket-code').value = codeFormat(scannedCode); scannedCode = ''; }
    text('claim-hint', state.game.registration_open ? '領卡已開放，使用座位上的專屬 QR 碼或輸入領卡碼。' : '新領卡尚未開放或已關閉。已領過卡片可用原碼恢復。');
    return;
  }
  text('card-label', card.card_label + ' · YOUR LUCKY CARD');
  text('player-greeting', card.name); text('game-badge', '第 ' + state.game.id + ' 局');
  text('player-latest', latestNumber());
  text('player-round', state.draws.length ? '第 ' + state.draws.length + ' 回合 · ' + gameStatus() : '等待主持人抽號');
  if (pendingMarks && pendingMarks.cardId !== card.id) {
    removeOutbox(pendingMarks.cardId); pendingMarks = null;
  }
  if (!pendingMarks) {
    try {
      const saved = JSON.parse(localStorage.getItem('bingo-marks-' + card.id));
      if (saved && saved.gameId === state.game.id && Array.isArray(saved.marks)) pendingMarks = saved;
    } catch (_) { /* Storage may be disabled in private browsing. Server saves still work. */ }
  }
  renderCard();
  if (pendingMarks && !savingMarks) saveMarks();
  const win = state.winners.find(w => w.player_id === card.player_id);
  const status = $('player-status'); status.replaceChildren();
  let title, detail;
  if (win) {
    title = 'BINGO！今晚的幸運得主 ✦';
    detail = '第 ' + win.round + ' 回合達成連線，' + (win.claimed_at ? '已完成領獎。' : '請帶著 ' + card.card_label + ' 與此畫面向工作人員領獎。');
  } else if (card.qualified_round) {
    title = '已達成連線，謝謝參與！';
    detail = '第 ' + card.qualified_round + ' 回合多人同時連線，剩餘名額由系統隨機抽選，本次未入選。';
  } else if (state.game.status === 'finished') {
    title = '本局已結束'; detail = '謝謝你一起參與，下一次好運再相聚。';
  } else {
    title = state.game.status === 'waiting' ? '幸運卡已就位' : '等候你的幸運連線';
    detail = state.game.status === 'waiting' ? '保留原領卡碼，重新整理也能回到這張卡。' : '已開出的格子會有金色邊框。即使沒有勾選，仍會自動判定中獎。';
  }
  status.append(node('strong', '', title), node('span', '', detail));
}
function renderCard() {
  const card = state.card;
  const marks = new Set(pendingMarks && pendingMarks.cardId === card.id ? pendingMarks.marks : card.marks);
  const drawn = new Set(state.draws.map(d => d.number));
  const winning = new Set(card.lines.flat());
  const signature = JSON.stringify([card.id, [...marks], [...drawn], [...winning]]);
  if (signature === lastCardSignature) return;
  lastCardSignature = signature;
  const focusIndex = document.activeElement?.dataset?.index;
  const grid = $('bingo-card'); grid.replaceChildren();
  card.numbers.forEach((number, i) => {
    const free = i === 12;
    const cell = node('button', 'bingo-cell' + (drawn.has(number) ? ' called' : '') + (marks.has(i) ? ' marked' : '') + (free ? ' free' : '') + (winning.has(i) ? ' winning' : ''));
    cell.type = 'button'; cell.dataset.index = String(i);
    cell.setAttribute('aria-pressed', marks.has(i) ? 'true' : 'false');
    cell.setAttribute('aria-label', free ? '中央免費格，已勾選' : '號碼 ' + number + (drawn.has(number) ? '，已開出' : '，尚未開出') + (marks.has(i) ? '，已勾選' : '，未勾選'));
    cell.append(node('span', '', free ? '✦' : number));
    if (free) { cell.append(node('small', '', 'FREE')); cell.disabled = true; }
    else cell.addEventListener('click', () => toggleMark(i));
    grid.append(cell);
  });
  if (focusIndex !== undefined) grid.querySelector('[data-index="' + focusIndex + '"]')?.focus({preventScroll: true});
}
function persistOutbox() {
  try { localStorage.setItem('bingo-marks-' + pendingMarks.cardId, JSON.stringify(pendingMarks)); } catch (_) { /* Optional offline cache. */ }
}
function removeOutbox(cardId) {
  try { localStorage.removeItem('bingo-marks-' + cardId); } catch (_) { /* Optional offline cache. */ }
}
function toggleMark(i) {
  const current = new Set(pendingMarks ? pendingMarks.marks : state.card.marks);
  current.has(i) ? current.delete(i) : current.add(i); current.add(12);
  pendingMarks = { cardId: state.card.id, gameId: state.game.id, marks: [...current].sort((a,b) => a-b) };
  persistOutbox(); text('save-status', '儲存中…'); renderCard(); saveMarks();
}
async function saveMarks() {
  if (savingMarks || !pendingMarks) return;
  savingMarks = true;
  try {
    while (pendingMarks) {
      const submitted = pendingMarks;
      await api('/api/marks', {game_id: submitted.gameId, marks: submitted.marks});
      if (state.card && state.card.id === submitted.cardId) state.card.marks = submitted.marks;
      if (pendingMarks === submitted) { removeOutbox(submitted.cardId); pendingMarks = null; }
    }
    text('save-status', '已保存');
  } catch (e) {
    text('save-status', '待連線後儲存');
    if (e.status === 409 && pendingMarks) { removeOutbox(pendingMarks.cardId); pendingMarks = null; refresh(); }
    else if (e.status === 401) { toast('請用原領卡碼恢復，手動勾選暫存在此裝置。'); refresh(); }
  } finally { savingMarks = false; }
}

function renderWinners(id, host) {
  const list = $(id); list.replaceChildren();
  list.classList.toggle('has-many', !host && state.winners.length > 5);
  if (!state.winners.length) {
    const empty = node('div', 'empty-winners');
    empty.append(node('span', 'empty-symbol', '✧'), node('h3', '', '幸運席位，為你保留'), node('p', 'muted', '第一位得主即將登場。')); list.append(empty); return;
  }
  state.winners.forEach(w => {
    const row = node('div', 'winner-row');
    const info = node('div', 'winner-name');
    info.append(node('strong', '', w.name), node('small', '', w.card_label + ' · 第 ' + w.round + ' 回合連線'));
    row.append(node('span', 'winner-rank', String(w.position).padStart(2, '0')), info);
    if (host) {
      const button = node('button', 'button small' + (w.claimed_at ? ' claimed' : ''), w.claimed_at ? '已領獎 ✓' : '標記領獎');
      button.disabled = busy || !online;
      button.setAttribute('aria-label', w.name + (w.claimed_at ? '已領獎，點擊取消標記' : '標記為已領獎'));
      button.addEventListener('click', () => {
        if (w.claimed_at && !confirm('取消 ' + w.name + ' 的已領獎標記？')) return;
        hostAction('/api/admin/claimed', {game_id: state.game.id, player_id: w.player_id, claimed: !w.claimed_at}, '領獎紀錄已更新');
      }); row.append(button);
    } else row.append(node('span', 'winner-tag', w.claimed_at ? '已領獎' : '恭喜中獎'));
    list.append(row);
  });
}
function hostButtons() {
  if (!state) return;
  const historical = state.game.id !== latestGameId;
  $('draw-button').disabled = busy || !online || historical || state.game.status === 'finished' || !state.player_count;
  text('draw-button', busy ? '處理中，請稍候…' : state.game.status === 'finished' ? '本局已結束' : '抽出下一個號碼 ✦');
  $('registration-button').disabled = busy || !online || historical || state.game.status !== 'waiting';
  $('reset-button').disabled = busy || !online || historical;
}
function renderHost() {
  show('login-panel', false); show('host-console', true); show('logout', true);
  text('host-title', state.game.title); show('archive-banner', state.game.id !== latestGameId);
  const select = $('game-select');
  if (select.options.length !== state.games.length) {
    select.replaceChildren(...state.games.map(g => {
      const option = node('option', '', '第 ' + g.id + ' 局' + (g.id === latestGameId ? '（目前）' : ''));
      option.value = String(g.id); return option;
    }));
  }
  select.value = String(state.game.id);
  text('stat-players', state.player_count); text('stat-draws', state.draws.length);
  text('stat-winners', state.winners.length + ' / ' + state.game.max_winners);
  text('stat-claimed', state.winners.filter(w => w.claimed_at).length);
  text('host-latest', latestNumber()); text('host-game-status', gameStatus());
  text('host-round', state.draws.length ? '第 ' + state.draws.length + ' 回合 · 尚餘 ' + (49-state.draws.length) + ' 個號碼' : '每一次開獎，都是新的期待');
  text('registration-button', state.game.registration_open ? '關閉領卡' : '開放領卡');
  text('registration-hint', state.game.status === 'waiting' ? (state.game.registration_open ? '目前開放 · 首次開獎後自動關閉' : '目前關閉 · 已領卡者可恢復') : '已開始開獎 · 停止新領卡');
  hostButtons(); renderWinners('host-winners', true); renderTickets(); renderAudit();
}
function renderTickets() {
  const used = state.tickets.filter(t => t.redeemed_at).length;
  text('ticket-summary', '共 ' + state.tickets.length + ' 組 · 已使用 ' + used + ' 組 · 尚未使用 ' + (state.tickets.length-used) + ' 組');
  const signature = JSON.stringify(state.tickets);
  if (signature === lastTicketSignature) return;
  lastTicketSignature = signature;
  const table = $('ticket-table'); table.replaceChildren();
  state.tickets.forEach(t => {
    const row = node('tr');
    row.append(node('td', '', String(t.id).padStart(3,'0')), node('td', 'code', codeFormat(t.code)), node('td', '', t.redeemed_at ? '已領卡' : '未使用'), node('td', '', t.name ? 'P' + String(t.player_id).padStart(3,'0') + ' · ' + t.name : '—'));
    table.append(row);
  });
}
function renderAudit() {
  const audit = $('lottery-audit'); audit.replaceChildren();
  const results = state.round_results.filter(r => r.candidates.length);
  if (!results.length) { audit.append(node('p', 'empty', '尚無達成連線的回合。')); return; }
  const name = id => {
    const p = state.players.find(p => p.id === id);
    return 'P' + String(id).padStart(3,'0') + (p ? ' ' + p.name : '');
  };
  results.forEach(r => {
    const section = node('div', 'audit-round');
    section.append(node('strong', '', '第 ' + r.round + ' 回合 · 號碼 ' + r.number + (r.candidates.length > r.selected.length ? ' · 同回合超額抽選' : ' · 全數入選')),
      node('p', '', '達成連線：' + r.candidates.map(name).join('、')),
      node('p', '', '隨機順序：' + r.lottery_order.map(name).join(' → ')),
      node('p', '', '入選得主：' + r.selected.map(name).join('、')));
    audit.append(section);
  });
}
async function hostAction(path, body, message) {
  if (busy) return;
  busy = true; hostButtons();
  try { await api(path, body); if (message) toast(message); }
  catch (e) { error(e.message); toast(e.message); }
  finally { busy = false; await refresh(); }
}
function qrNode(value) {
  const qr = qrcode(0, 'M'); qr.addData(value, 'Byte'); qr.make();
  const template = document.createElement('template');
  // Only the vendored QR generator produces this markup; no user text is interpolated.
  template.innerHTML = qr.createSvgTag({cellSize: 4, margin: 16, scalable: true});
  return template.content.firstElementChild;
}
function printTickets() {
  const unused = state.tickets.filter(t => !t.redeemed_at);
  if (!unused.length) { toast('沒有尚未使用的領卡碼。'); return; }
  const base = state.public_url || location.origin;
  if (/^https?:\/\/(localhost|127\.0\.0\.1)(:|$)/.test(base) && !state.demo) {
    toast('目前為本機網址，手機無法連入。正式列印前請設定網域或以區網網址開啟。'); return;
  }
  const area = $('print-area'); area.replaceChildren();
  let sheet;
  unused.forEach((t, index) => {
    if (index % 9 === 0) { sheet = node('section', 'print-page'); area.append(sheet); }
    const card = node('article', 'printed-ticket');
    const qr = node('div', 'ticket-qr'); qr.append(qrNode(base + '/#code=' + t.code));
    card.append(node('h2', '', state.game.title + ' · 賓果'), node('p', '', '專屬領卡券 #' + String(t.id).padStart(3,'0')), qr,
      node('code', '', codeFormat(t.code)), node('p', '', '每人一張 · 請保留此券以找回卡片'), node('small', '', '勿轉傳。掃碼後輸入稱呼即可領卡。'));
    sheet.append(card);
  });
  window.print();
  toast('QR 券已準備好。若沒有出現預覽，請按 ⌘P／Ctrl+P。');
}
function renderDisplay() {
  text('display-game', '第 ' + state.game.id + ' 局');
  text('display-latest', state.draws.length ? latestNumber() : '✦');
  text('display-round', state.game.status === 'finished' ? '本局已結束，恭喜今晚的幸運得主。' : state.draws.length ? '第 ' + state.draws.length + ' 回合 · 幸運正在發生' : '與你同席的好運，即將登場。');
  text('display-players', state.player_count); text('display-drawn', state.draws.length);
  text('display-winner-count', state.winners.length + ' / ' + state.game.max_winners);
  renderWinners('display-winners', false);
  if (!$('join-qr').firstChild) $('join-qr').append(qrNode(location.origin + '/'));
}

if (page === 'player') {
  $('claim-form').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.target.querySelector('button'); button.disabled = true;
    try {
      await api('/api/claim', {name: $('player-name').value, code: $('ticket-code').value});
      $('ticket-code').value = ''; toast('卡片已就位，祝你今晚好運！'); await refresh();
    } catch (err) { error(err.message); }
    finally { button.disabled = false; }
  });
}
if (page === 'host') {
  $('login-form').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.target.querySelector('button'); button.disabled = true;
    try { await api('/api/login', {password: $('password').value}); $('password').value = ''; await refresh(); }
    catch (err) { error(err.message); }
    finally { button.disabled = false; }
  });
  $('logout').addEventListener('click', () => hostAction('/api/admin/logout', {}));
  $('draw-button').addEventListener('click', () => hostAction('/api/admin/draw', {game_id: state.game.id, expected_round: state.draws.length}));
  $('registration-button').addEventListener('click', () => hostAction('/api/admin/registration', {game_id: state.game.id, open: !state.game.registration_open}));
  $('game-select').addEventListener('change', e => { archiveId = Number(e.target.value) === latestGameId ? null : Number(e.target.value); refresh(); });
  $('ticket-form').addEventListener('submit', e => { e.preventDefault(); hostAction('/api/admin/tickets', {count: Number($('ticket-count').value)}, '領卡碼已產生'); });
  $('print-tickets').addEventListener('click', printTickets);
  $('reset-button').addEventListener('click', () => { resetGameId = state.game.id; $('reset-confirm').value = ''; $('reset-dialog').showModal(); });
  $('cancel-reset').addEventListener('click', () => $('reset-dialog').close());
  $('reset-form').addEventListener('submit', async e => {
    e.preventDefault();
    if ($('reset-confirm').value !== '開啟下一局') { toast('請完整輸入「開啟下一局」。'); return; }
    $('reset-dialog').close();
    await hostAction('/api/admin/reset', {game_id: resetGameId, confirmation: $('reset-confirm').value}, '新局已準備好');
  });
  // Demo credentials are shown only for the explicitly marked local demo database.
  api('/api/state').then(s => { show('demo-banner', s.demo); show('demo-login-hint', s.demo); }).catch(() => {});
}
if (page === 'display') {
  $('fullscreen').addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else toast('此瀏覽器不支援全螢幕，請使用投影電腦的瀏覽器。');
    } catch (_) { toast('請在投影電腦上使用瀏覽器全螢幕功能。'); }
  });
}
function connectEvents() {
  eventStream?.close();
  eventStream = new EventSource('/api/events');
  eventStream.onmessage = () => refresh();
  eventStream.onopen = () => refresh();
  eventStream.onerror = () => connection(false, '重新連線中 · 自動同步');
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { connectEvents(); refresh(); }
  else { eventStream?.close(); }
});
window.addEventListener('online', () => { connectEvents(); refresh(); });
window.addEventListener('offline', () => connection(false, '離線 · 等待網路恢復'));
setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 15000);
refresh(); connectEvents();
