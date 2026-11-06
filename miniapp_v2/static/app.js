/* TI:IT TOP Mini App 5.0 — vanilla JS, без сборки и зависимостей.
 * Принципы: каждый раздел грузит свои данные отдельно; ошибка раздела не ломает
 * приложение; у каждого запроса есть таймаут; null никогда не превращается в 0. */
(() => {
  'use strict';

  const VERSION = '5.15.0';
  const $view = document.getElementById('view');
  const $nav = document.getElementById('nav');
  const $toast = document.getElementById('toast');
  const $modal = document.getElementById('modal');
  const $title = document.getElementById('topTitle');

  // =========================================================================
  // Telegram WebApp bridge (SDK грузится async; initData есть и в URL-хэше)
  // =========================================================================
  const tgApp = () => { try { return (window.Telegram && window.Telegram.WebApp) || null; } catch (e) { return null; } };

  function readInitData() {
    const app = tgApp();
    if (app && app.initData) return app.initData;
    try {
      const hash = new URLSearchParams(String(location.hash || '').replace(/^#/, ''));
      const data = hash.get('tgWebAppData');
      if (data) return data;
    } catch (e) { /* ignore */ }
    if (/^(localhost|127\.0\.0\.1)$/.test(location.hostname)) {
      const q = new URLSearchParams(location.search);
      if (q.has('dev')) return 'dev:' + (q.get('dev') || '');
    }
    return '';
  }

  let INIT_DATA = readInitData();
  let tgReady = false;

  function onTelegram(app) {
    if (tgReady) return;
    tgReady = true;
    try {
      app.ready();
      app.expand();
      if (app.setHeaderColor) app.setHeaderColor('#0e1117');
      if (app.setBackgroundColor) app.setBackgroundColor('#0e1117');
      if (app.BackButton) app.BackButton.onClick(() => goBack());
      if (!INIT_DATA && app.initData) INIT_DATA = app.initData;
    } catch (e) { /* старые клиенты Telegram */ }
    syncBackButton();
  }

  (function waitTelegram(attempt) {
    const app = tgApp();
    if (app) return onTelegram(app);
    if (attempt < 120) setTimeout(() => waitTelegram(attempt + 1), 50);
  })(0);

  const haptic = (type) => {
    try {
      const h = tgApp() && tgApp().HapticFeedback;
      if (!h) return;
      if (type === 'ok' || type === 'error' || type === 'warning') h.notificationOccurred(type === 'ok' ? 'success' : type);
      else h.impactOccurred('light');
    } catch (e) { /* ignore */ }
  };

  // =========================================================================
  // API: таймаут, отмена, понятные ошибки для 401/403/404/429/5xx/сети/JSON
  // =========================================================================
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status || 0; this.data = data || null; }
  }

  function errorMessage(status, data) {
    const d = data && data.detail;
    if (typeof d === 'string' && d.trim()) return d;
    if (Array.isArray(d)) {
      const parts = d.map((x) => (x && (x.msg || x.message)) || '').filter(Boolean);
      if (parts.length) return 'Некорректные данные: ' + parts.join('; ');
    }
    if (status === 401) return 'Сессия Telegram недействительна. Закройте и снова откройте Mini App.';
    if (status === 403) return 'Недостаточно прав для этого действия.';
    if (status === 404) return 'Не найдено.';
    if (status === 413) return 'Файл слишком большой.';
    if (status === 429) return 'Слишком часто. Подождите немного.';
    if (status === 502 || status === 503 || status === 504) return 'Сервер временно недоступен (' + status + '). Попробуйте через минуту.';
    if (status >= 500) return 'Ошибка сервера (' + status + '). Попробуйте ещё раз.';
    return 'Ошибка запроса (' + status + ').';
  }

  async function api(path, opts) {
    const o = opts || {};
    const method = o.method || 'GET';
    const timeout = o.timeout || 15000;
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, timeout);
    if (o.signal) {
      if (o.signal.aborted) ctrl.abort();
      else o.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
    }
    const headers = { 'X-Telegram-Init-Data': INIT_DATA, Accept: 'application/json' };
    let body;
    if (o.body instanceof FormData) body = o.body;
    else if (o.body !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(o.body); }
    try {
      let res;
      try {
        res = await fetch(path, { method, headers, body, signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin' });
      } catch (e) {
        if (timedOut) throw new ApiError('Сервер не ответил за ' + Math.round(timeout / 1000) + ' с. Проверьте интернет и попробуйте ещё раз.', 0);
        if (ctrl.signal.aborted) throw new ApiError('Запрос отменён.', -1);
        throw new ApiError('Нет связи с сервером. Проверьте интернет или VPN.', 0);
      }
      let text = '';
      try { text = await res.text(); } catch (e) {
        if (timedOut) throw new ApiError('Сервер не ответил вовремя.', 0);
        throw new ApiError('Ответ сервера оборвался. Попробуйте ещё раз.', res.status);
      }
      let data = null;
      if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
      if (!res.ok) throw new ApiError(errorMessage(res.status, data), res.status, data);
      if (data === null || typeof data !== 'object') throw new ApiError('Сервер вернул некорректный ответ.', res.status);
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  // =========================================================================
  // Утилиты отображения
  // =========================================================================
  // 5.5: нарисованные иконки (SVG-спрайт в index.html) вместо эмодзи.
  function ic(name, cls) { return '<svg class="ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const arr = (v) => (Array.isArray(v) ? v : []);
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const POS = { 1: 'Керри', 2: 'Мид', 3: 'Хард', 4: 'Четвёрка', 5: 'Пятёрка' };
  const RANK_ICON = { 11: 4, 12: 5, 13: 6, 14: 7, 15: 8, 21: 10, 22: 11, 23: 12, 24: 13, 25: 14, 31: 16, 32: 17, 33: 18, 34: 19, 35: 20, 41: 22, 42: 23, 43: 24, 44: 25, 45: 26, 51: 28, 52: 29, 53: 30, 54: 31, 55: 32, 61: 34, 62: 35, 63: 36, 64: 37, 65: 38, 71: 40, 72: 41, 73: 42, 74: 43, 75: 44, 80: 48 };
  const T_STATUS = { DRAFT: 'Черновик', REGISTRATION: 'Регистрация', ACTIVE: 'Идёт', FINISHED: 'Завершён', CANCELLED: 'Отменён' };
  const A_STATUS = { DRAFT: 'Черновик', PENDING: 'На рассмотрении', APPROVED: 'Одобрена', REJECTED: 'Отклонена', CANCELLED: 'Отменена' };
  const S_STATUS = { OPEN: 'Открыто', ANSWERED: 'Отвечено', CLOSED: 'Закрыто' };
  const V_STATUS = { unlinked: 'Steam не привязан', UNVERIFIED: 'Не проверен', PENDING: 'На проверке', VERIFIED: 'Проверен', REJECTED: 'Отклонён' };

  // Время проекта — московское, независимо от часового пояса устройства (5.2).
  const TZ = 'Europe/Moscow';
  const TZ_LABEL = 'МСК';
  const MSK_OFFSET_MIN = 180; // запасной путь, если WebView не знает часовых поясов
  function mskParts(d) {
    try {
      const f = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
      const o = {};
      f.formatToParts(d).forEach((p) => { o[p.type] = p.value; });
      return { y: o.year, m: o.month, d: o.day, h: o.hour === '24' ? '00' : o.hour, mi: o.minute };
    } catch (e) {
      const x = new Date(d.getTime() + MSK_OFFSET_MIN * 60000);
      const p = (n) => String(n).padStart(2, '0');
      return { y: String(x.getUTCFullYear()), m: p(x.getUTCMonth() + 1), d: p(x.getUTCDate()), h: p(x.getUTCHours()), mi: p(x.getUTCMinutes()) };
    }
  }
  function fmtDate(iso, withTime) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    const p = mskParts(d);
    return p.d + '.' + p.m + '.' + p.y + (withTime !== false ? ' ' + p.h + ':' + p.mi : '');
  }
  // ISO (UTC) -> значение для <input type=datetime-local> по Москве.
  function mskInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p = mskParts(d);
    return p.y + '-' + p.m + '-' + p.d + 'T' + p.h + ':' + p.mi;
  }
  function fmtRel(iso) {
    if (!iso) return '';
    const d = new Date(iso); const s = (Date.now() - d.getTime()) / 1000;
    if (isNaN(s)) return '';
    if (s < 60) return 'только что';
    if (s < 3600) return Math.floor(s / 60) + ' мин назад';
    if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
    return fmtDate(iso);
  }
  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100; const b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }
  function initials(name) {
    // Array.from делит строку по символам Unicode: эмодзи не режется пополам («M�»).
    const s = String(name || '?').replace(/^@/, '').trim();
    const parts = s.split(/\s+/).filter(Boolean).map((p) => Array.from(p));
    const out = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || ['?']).slice(0, 2).join('');
    return esc((out || '?').toUpperCase());
  }
  function avatarHtml(url, name, cls) {
    const c = 'avatar' + (cls ? ' ' + cls : '');
    if (url) return '<span class="' + c + '"><img src="' + esc(url) + '" alt="" loading="lazy" data-fallback="' + initials(name) + '"></span>';
    return '<span class="' + c + '">' + initials(name) + '</span>';
  }
  // Битая картинка -> инициалы (без inline-обработчиков).
  document.addEventListener('error', (e) => {
    const t = e.target;
    if (t && t.tagName === 'IMG' && t.dataset && t.dataset.fallback !== undefined) {
      const span = t.parentElement; if (span) span.textContent = t.dataset.fallback || '?';
    }
  }, true);

  function medalHtml(medal, small) {
    const cls = 'medal' + (small ? ' sm' : '');
    if (!medal || !medal.tier) return '<span class="' + cls + '" title="Медаль недоступна"><span class="none">?</span></span>';
    const n = RANK_ICON[medal.tier];
    const title = esc(medal.name || '') + (medal.leaderboard_rank ? ' #' + esc(medal.leaderboard_rank) : '');
    if (!n) return '<span class="' + cls + '" title="' + title + '"><span class="none">' + ic('medal') + '</span></span>';
    return '<span class="' + cls + '" title="' + title + '"><img src="/v2/static/ranks/Dota2RanksIcons_' + String(n).padStart(3, '0') + '.webp" alt="' + title + '"></span>';
  }
  // 5.1: источники медали (OpenDota / STRATZ / Dotabuff), выбор самого достоверного, риск смурфа.
  const CONFIDENCE = { high: ['высокая', 'ok'], medium: ['средняя', 'warn'], low: ['низкая', 'bad'], none: ['нет данных', ''] };
  const RISK = { low: ['низкий', 'ok'], medium: ['средний', 'warn'], high: ['высокий', 'bad'], unknown: ['нет данных', ''] };
  function medalCheckHtml(check, admin) {
    if (!check) return '<div class="muted small">Источники ещё не проверялись.</div>';
    const m = check.medal || {};
    const conf = CONFIDENCE[m.confidence] || CONFIDENCE.none;
    let html = '<div class="src-list">' + arr(check.sources).map((s) =>
      '<div class="src' + (s.ok ? '' : ' off') + '">' + medalHtml(s.tier ? { tier: s.tier, name: s.name } : null, true) +
      '<div class="grow"><b>' + esc(s.label) + '</b><div class="small muted">' + (s.ok ? (s.name ? esc(s.name) + (s.leaderboard_rank ? ' · #' + esc(s.leaderboard_rank) : '') : 'медаль не показана') : 'нет данных' + (admin && s.error ? ': ' + esc(s.error) : '')) + '</div></div>' +
      (m.source && String(m.source).split('+').indexOf(s.source) >= 0 ? pill('выбран', 'ok') : '') + '</div>').join('') + '</div>';
    html += '<div class="src-final"><span>Итог:</span> <b>' + esc(m.name || 'медаль неизвестна') + '</b>' + (m.source_label ? ' · ' + esc(m.source_label) : '') + ' ' + pill('достоверность: ' + conf[0], conf[1]) + '</div>' +
      (m.why ? '<div class="note" style="margin-top:4px">' + esc(m.why) + '</div>' : '') +
      '<div class="note">Проверено ' + esc(fmtDate(check.created_at)) + ' (' + TZ_LABEL + ')</div>';
    if (admin && check.risk) {
      const r = RISK[check.risk.level] || RISK.unknown;
      html += '<div class="divider"></div><div class="row between"><b>Риск смурфа / твинка</b>' + pill(r[0] + (num(check.risk.score) !== null ? ' · ' + check.risk.score + '/100' : ''), r[1]) + '</div>' +
        (arr(check.risk.reasons).length ? '<ul class="reasons">' + arr(check.risk.reasons).map((x) => '<li>' + esc(x.text) + ' <span class="muted">+' + esc(x.points) + '</span></li>').join('') + '</ul>'
          : '<div class="small muted" style="margin-top:6px">Подозрительных признаков не найдено.</div>');
      const ai = check.ai;
      if (ai && (ai.summary || (ai.risk && ai.risk.why))) {
        html += '<div class="ai-box"><div class="small muted">AI' + (check.ai_model ? ' · ' + esc(check.ai_model) : '') + '</div>' + (ai.summary ? '<div>' + esc(ai.summary) + '</div>' : '') +
          (ai.risk && arr(ai.risk.why).length ? '<ul class="reasons">' + arr(ai.risk.why).map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '') + '</div>';
      }
      if (check.ai_error) html += '<div class="note">AI: ' + esc(check.ai_error) + '</div>';
      html += '<div class="note">Это подсказка для организатора — автоматических отказов нет.</div>';
    }
    return html;
  }
  function personAvatar(url, name, cls) { return avatarHtml(url || null, name, 'round' + (cls ? ' ' + cls : '')); }

  // MMR: только факты. confirmed -> число; pending -> «на проверке»; none -> «нет данных».
  function mmrInline(mmr) {
    const m = mmr || {};
    if (num(m.confirmed) !== null) return '<span class="mmr-inline">' + esc(m.confirmed) + ' MMR</span>';
    if (num(m.declared) !== null && m.state === 'pending') return '<span class="mmr-inline pending" title="Ждёт проверки организатором">' + esc(m.declared) + ' · на проверке</span>';
    if (num(m.declared) !== null && m.state === 'rejected') return '<span class="mmr-inline none">MMR отклонён</span>';
    return '<span class="mmr-inline none">MMR нет данных</span>';
  }
  function positionsText(list) {
    const p = arr(list);
    return p.length ? p.map((x) => x + ' · ' + (POS[x] || '')).join(', ') : 'позиции не выбраны';
  }
  function pill(text, kind) { return '<span class="pill' + (kind ? ' ' + kind : '') + '">' + esc(text) + '</span>'; }
  function vPill(status) {
    const kind = status === 'VERIFIED' ? 'ok' : status === 'PENDING' ? 'warn' : status === 'REJECTED' ? 'bad' : '';
    return pill(V_STATUS[status] || status || '—', kind);
  }
  function aPill(status) {
    const kind = status === 'APPROVED' ? 'ok' : status === 'PENDING' ? 'warn' : status === 'REJECTED' ? 'bad' : '';
    return pill(A_STATUS[status] || status || '—', kind);
  }
  function tPill(status, open) {
    const kind = status === 'REGISTRATION' && open ? 'ok' : status === 'ACTIVE' ? 'info' : status === 'CANCELLED' ? 'bad' : '';
    return pill(T_STATUS[status] || status || '—', kind);
  }
  const skeleton = (n) => '<div class="skeleton">' + '<div class="sk"></div>'.repeat(n || 3) + '</div>';
  const stateBox = (title, text, extra) => '<div class="state"><b>' + esc(title) + '</b>' + (text ? '<div>' + esc(text) + '</div>' : '') + (extra || '') + '</div>';
  const errorBox = (title, err, retryAct) => '<div class="state error"><b>' + esc(title) + '</b><div>' + esc((err && err.message) || err || 'Неизвестная ошибка') + '</div>' +
    (retryAct ? '<button class="btn small" type="button" data-act="' + esc(retryAct) + '">Повторить</button>' : '') + '</div>';
  const backBtn = () => '<button class="back" type="button" data-act="back">← Назад</button>';
  const localDateInput = (d) => {
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  };

  // =========================================================================
  // Toast, диалоги (вместо confirm/prompt, которые в WebView Telegram ненадёжны)
  // =========================================================================
  let toastTimer = null;
  function toast(message, kind) {
    $toast.textContent = String(message || '');
    $toast.className = 'toast' + (kind ? ' ' + kind : '');
    $toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $toast.hidden = true; }, kind === 'bad' ? 4200 : 2600);
    if (kind === 'bad') haptic('error'); else if (kind === 'ok') haptic('ok');
  }

  let modalResolve = null;
  function closeModal(value) {
    $modal.hidden = true; $modal.innerHTML = '';
    const r = modalResolve; modalResolve = null;
    if (r) r(value);
    syncBackButton();
  }
  function openModal(html) {
    if (modalResolve) closeModal(null);
    $modal.innerHTML = '<div class="sheet" role="dialog" aria-modal="true">' + html + '</div>';
    $modal.hidden = false;
    syncBackButton();
    return new Promise((resolve) => { modalResolve = resolve; });
  }
  $modal.addEventListener('click', (e) => { if (e.target === $modal) closeModal(null); });

  function confirmDialog(o) {
    const html = '<h3>' + esc(o.title || 'Подтвердите действие') + '</h3>' + (o.text ? '<p>' + esc(o.text) + '</p>' : '') +
      '<div class="actions"><button class="btn ghost" type="button" data-modal="cancel">Отмена</button>' +
      '<button class="btn ' + (o.danger ? 'danger' : 'primary') + '" type="button" data-modal="ok">' + esc(o.ok || 'Подтвердить') + '</button></div>';
    return openModal(html).then((v) => v === true);
  }
  function promptDialog(o) {
    const field = o.multiline
      ? '<textarea class="textarea" id="modalInput" maxlength="' + (o.max || 1000) + '" placeholder="' + esc(o.placeholder || '') + '">' + esc(o.value || '') + '</textarea>'
      : '<input class="input" id="modalInput" maxlength="' + (o.max || 300) + '" placeholder="' + esc(o.placeholder || '') + '" value="' + esc(o.value || '') + '"' + (o.numeric ? ' inputmode="numeric"' : '') + '>';
    const html = '<h3>' + esc(o.title || '') + '</h3>' + (o.text ? '<p>' + esc(o.text) + '</p>' : '') + field +
      '<div class="actions"><button class="btn ghost" type="button" data-modal="cancel">Отмена</button>' +
      '<button class="btn ' + (o.danger ? 'danger' : 'primary') + '" type="button" data-modal="submit">' + esc(o.ok || 'Готово') + '</button></div>';
    const p = openModal(html);
    setTimeout(() => { const i = document.getElementById('modalInput'); if (i) i.focus(); }, 30);
    return p;
  }
  $modal.addEventListener('click', (e) => {
    const b = e.target.closest('[data-modal]');
    if (!b) return;
    const kind = b.dataset.modal;
    if (kind === 'cancel') closeModal(null);
    else if (kind === 'ok') closeModal(true);
    else if (kind === 'form') {
      const values = {};
      $modal.querySelectorAll('[data-field]').forEach((f) => {
        values[f.dataset.field] = f.type === 'checkbox' ? f.checked : f.type === 'file' ? ((f.files && f.files[0]) || null) : f.value;
      });
      values.__intent = b.dataset.intent || '';
      closeModal(values);
    } else if (kind === 'submit') {
      const i = document.getElementById('modalInput');
      const v = i ? i.value.trim() : '';
      closeModal(v);
    }
  });

  async function withBusy(btn, fn) {
    if (btn && btn.classList.contains('busy')) return undefined;
    if (btn) btn.classList.add('busy');
    try { return await fn(); }
    finally { if (btn && btn.isConnected) btn.classList.remove('busy'); }
  }

  // =========================================================================
  // Состояние и навигация (стек внутри вкладки + Telegram BackButton)
  // =========================================================================
  const TAB_TITLES = { home: 'Главная', team: 'Команда', search: 'Поиск', tournaments: 'Турниры', feed: 'Лента', profile: 'Профиль', admin: 'Админ-панель' };
  const state = {
    me: null,
    meAt: 0,
    tab: 'home',
    stack: [{ view: 'home', params: {} }],
    seq: 0,
    ctrl: null,
    timers: [],
    ui: { searchMode: 'players', feedMode: 'chat', players: { q: '', position: null, min: '', max: '', sort: 'mmr_desc' }, teamsQ: '', aiHistory: [], appStatus: 'PENDING', ticketStatus: 'OPEN', usersQ: '', usersStatus: 'ALL', adminTeamsQ: '' },
  };

  function clearTimers() { state.timers.forEach((t) => clearInterval(t)); state.timers = []; }
  function addTimer(fn, ms) { const t = setInterval(fn, ms); state.timers.push(t); return t; }

  function syncBackButton() {
    const app = tgApp();
    if (!app || !app.BackButton) return;
    try { if (state.stack.length > 1 || !$modal.hidden) app.BackButton.show(); else app.BackButton.hide(); } catch (e) { /* ignore */ }
  }
  function syncNav() {
    $nav.hidden = !state.me;
    $nav.querySelectorAll('button[data-tab]').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === state.tab);
      if (b.dataset.tab === 'admin') b.hidden = !(state.me && state.me.is_admin);
      if (b.dataset.tab === 'team' || b.dataset.tab === 'search') b.hidden = !!(state.me && state.me.kind === 'VIEWER');
    });
    const unread = state.me && state.me.counts ? Number(state.me.counts.unread_notifications || 0) : 0;
    const badge = document.getElementById('feedBadge');
    if (badge) { badge.hidden = !unread; badge.textContent = unread > 99 ? '99+' : String(unread); }
    $title.textContent = TAB_TITLES[state.tab] || 'Dota 2 турниры';
  }

  function switchTab(tab) {
    if (tab === 'admin' && !(state.me && state.me.is_admin)) tab = 'home';
    if ((tab === 'team' || tab === 'search') && state.me && state.me.kind === 'VIEWER') tab = 'home';
    state.tab = tab;
    state.stack = [{ view: tab, params: {} }];
    render();
  }
  function go(view, params) { state.stack.push({ view, params: params || {} }); render(); }
  function goBack() {
    if (!$modal.hidden) { closeModal(null); return; }
    if (state.stack.length > 1) { state.stack.pop(); render(); }
  }
  function reload() { render(true); }

  async function refreshMe(force) {
    if (!force && Date.now() - state.meAt < 30000) return state.me;
    try {
      state.me = await api('/api/v2/me', { timeout: 10000 });
      state.meAt = Date.now();
      syncNav();
    } catch (e) { /* счётчики не критичны */ }
    return state.me;
  }

  function render(keepScroll) {
    clearTimers();
    if (state.ctrl) state.ctrl.abort();
    const ctrl = new AbortController();
    state.ctrl = ctrl;
    const seq = ++state.seq;
    const top = state.stack[state.stack.length - 1];
    const fn = VIEWS[top.view] || VIEWS.home;
    syncNav(); syncBackButton();
    const y = window.scrollY;
    $view.innerHTML = skeleton(3);
    if (!keepScroll) window.scrollTo(0, 0);
    const ctx = {
      params: top.params || {},
      signal: ctrl.signal,
      alive: () => seq === state.seq,
      set: (html) => { if (seq === state.seq) { $view.innerHTML = html; if (keepScroll) window.scrollTo(0, y); } },
      api: (path, o) => api(path, Object.assign({ signal: ctrl.signal }, o || {})),
    };
    Promise.resolve()
      .then(() => fn(ctx))
      .catch((err) => {
        if (!ctx.alive() || (err && err.status === -1)) return;
        if (err && err.status === 401) { showSessionError(err); return; }
        ctx.set(errorBox('Не удалось загрузить раздел', err, 'reload'));
      });
  }

  function showSessionError(err) {
    $nav.hidden = true;
    $view.innerHTML = '<div class="boot">' + errorBox('Нужна авторизация Telegram', err) +
      '<p class="muted small">Закройте Mini App и откройте её снова из бота TI:IT TOP.</p></div>';
  }

  // Делегирование событий: один обработчик на всё приложение.
  const ACTIONS = {};
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('#nav button[data-tab]');
    if (tab) { haptic(); switchTab(tab.dataset.tab); return; }
    const el = e.target.closest('[data-act]');
    if (!el || el.closest('#modal')) return;
    const fn = ACTIONS[el.dataset.act];
    if (!fn) return;
    e.preventDefault();
    Promise.resolve(fn(el, e)).catch((err) => { if (err && err.status !== -1) toast(err.message || String(err), 'bad'); });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    const el = e.target.closest && e.target.closest('[data-enter]');
    if (!el) return;
    const fn = ACTIONS[el.dataset.enter];
    if (!fn) return;
    e.preventDefault();
    Promise.resolve(fn(el, e)).catch((err) => { if (err && err.status !== -1) toast(err.message || String(err), 'bad'); });
  });
  document.getElementById('refreshBtn').addEventListener('click', () => { haptic(); refreshMe(true); reload(); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && state.me) refreshMe(false); });

  ACTIONS.back = () => goBack();
  ACTIONS.reload = () => reload();
  ACTIONS.tab = (el) => switchTab(el.dataset.tab);
  ACTIONS.open = (el) => go(el.dataset.view, JSON.parse(el.dataset.params || '{}'));
  const openAttr = (view, params) => 'data-act="open" data-view="' + esc(view) + '" data-params="' + esc(JSON.stringify(params || {})) + '"';

  // Выполняет мутацию: кнопка «занята», понятная ошибка, обновление раздела.
  async function mutate(btn, fn, okText, opts) {
    const o = opts || {};
    return withBusy(btn, async () => {
      try {
        const res = await fn();
        if (okText) toast(typeof okText === 'function' ? okText(res) : okText, 'ok');
        if (o.me) await refreshMe(true);
        if (o.reload !== false) reload();
        return res;
      } catch (err) {
        toast(err.message || 'Не удалось выполнить действие', 'bad');
        if (err && err.status === 401) showSessionError(err);
        return undefined;
      }
    });
  }

  const VIEWS = {};

  // =========================================================================
  // Главная
  // =========================================================================
  VIEWS.home = async (ctx) => {
    const me = state.me || {};
    const u = me.user || {};
    const mmr = me.mmr || {};
    const steps = [];
    if (!me.steam_linked) steps.push(['warn', 'Привяжите Steam-аккаунт', 'Без этого нельзя участвовать в турнирах.', 'profile']);
    else if (mmr.state === 'none') steps.push(['warn', 'Укажите точный MMR', 'Организатор сверит его с вашим аккаунтом.', 'profile']);
    else if (mmr.state === 'pending') steps.push(['info', 'MMR на проверке', 'Заявлено ' + mmr.declared + '. Ждите решения организатора.', 'profile']);
    else if (mmr.state === 'rejected') steps.push(['bad', 'MMR не подтверждён', 'Укажите актуальный MMR или напишите организаторам.', 'profile']);
    else if (mmr.state === 'confirmed') steps.push(['ok', 'MMR подтверждён', mmr.confirmed + ' MMR', 'profile']);
    if (me.team) steps.push(['ok', 'Команда: ' + me.team.name, me.team.is_captain ? 'Вы капитан' : 'Вы в составе', 'team']);
    else steps.push(['', 'Нет команды', 'Создайте свою или примите приглашение.', 'team']);
    const invites = me.counts ? Number(me.counts.pending_invites || 0) : 0;
    const unread = me.counts ? Number(me.counts.unread_notifications || 0) : 0;

    let html = '<div class="page-head"><div class="eyebrow">TI:IT TOP · DOTA 2</div><h1>Привет, ' + esc(u.first_name || u.name || 'игрок') + '!</h1>' +
      '<p>Профиль, команда, поиск игроков и турниры — всё здесь.</p></div>';
    if (invites) html += '<button class="item card hl" type="button" data-act="tab" data-tab="team"><span class="avatar">' + ic('mail') + '</span><span class="grow"><span class="title">' +
      esc(invites) + ' ' + plural(invites, 'приглашение', 'приглашения', 'приглашений') + ' в команду</span><span class="sub">Ответить в разделе «Команда»</span></span><span class="chev">›</span></button>';
    if (unread) html += '<button class="item" type="button" data-act="feed-notifications"><span class="avatar">' + ic('bell') + '</span><span class="grow"><span class="title">' +
      esc(unread) + ' ' + plural(unread, 'новое уведомление', 'новых уведомления', 'новых уведомлений') + '</span></span><span class="chev">›</span></button><div style="height:12px"></div>';
    html += '<div class="card"><div class="card-head"><h3>Мой статус</h3></div><div class="list">' + steps.map((s) =>
      '<button class="item" type="button" data-act="tab" data-tab="' + s[3] + '"><span class="grow"><span class="title">' + esc(s[1]) + '</span><span class="sub">' + esc(s[2]) + '</span></span>' +
      (s[0] ? pill(s[0] === 'ok' ? 'готово' : s[0] === 'info' ? 'ждём' : 'нужно', s[0] === 'info' ? 'info' : s[0]) : '') + '</button>').join('') + '</div></div>';
    html += '<div class="card"><div class="card-head"><h3>Турниры</h3><button class="link" type="button" data-act="tab" data-tab="tournaments">Все</button></div><div id="homeTours">' + skeleton(2) + '</div></div>';
    html += '<div class="grid2"><button class="btn block" type="button" data-act="tab" data-tab="search">' + ic('search') + ' Найти игроков</button><button class="btn block" type="button" ' + openAttr('support') + '>' + ic('chat') + ' Организаторам</button></div>';
    ctx.set(html);

    try {
      const data = await ctx.api('/api/v2/tournaments');
      if (!ctx.alive()) return;
      const items = arr(data.items).filter((t) => t.status === 'REGISTRATION' || t.status === 'ACTIVE').slice(0, 3);
      const box = document.getElementById('homeTours');
      if (box) box.innerHTML = items.length ? items.map(tourCard).join('') : '<div class="muted small">Сейчас нет открытых турниров.</div>';
    } catch (err) {
      const box = document.getElementById('homeTours');
      if (box && ctx.alive() && err.status !== -1) box.innerHTML = errorBox('Не удалось загрузить турниры', err, 'reload');
    }
  };
  ACTIONS['feed-notifications'] = () => { state.ui.feedMode = 'notifications'; switchTab('feed'); };

  // =========================================================================
  // Профиль
  // =========================================================================
  VIEWS.profile = async (ctx) => {
    const p = await ctx.api('/api/v2/profile');
    if (!ctx.alive()) return;
    const mmr = p.mmr || {};
    const steam = p.steam || {};
    const ver = p.verification || {};
    const stats = p.stats || {};
    const user = p.user || {};
    let mmrBlock;
    if (num(mmr.confirmed) !== null) mmrBlock = '<div class="mmr-value">' + esc(mmr.confirmed) + '</div><div class="mmr-tag">MMR подтверждён организатором</div>';
    else if (mmr.state === 'pending') mmrBlock = '<div class="mmr-value pending">' + esc(mmr.declared) + '</div><div class="mmr-tag">Заявлено вами · ждёт проверки организатором</div>';
    else if (mmr.state === 'rejected') mmrBlock = '<div class="mmr-value none">Не подтверждён</div><div class="mmr-tag">Заявка на ' + esc(mmr.declared) + ' MMR отклонена</div>';
    else mmrBlock = '<div class="mmr-value none">MMR не указан</div><div class="mmr-tag">Укажите точный MMR ниже</div>';
    const medal = p.medal;
    const medalText = medal ? esc(medal.name) + (medal.leaderboard_rank ? ' · #' + esc(medal.leaderboard_rank) : '') : 'Медаль не получена из источников (OpenDota, STRATZ, Dotabuff)';
    const range = p.medal_mmr_range ? '<div class="note">Справочный диапазон медали: ' + esc(p.medal_mmr_range.mmr_min) + '–' + esc(p.medal_mmr_range.mmr_max) + ' MMR. Решение принимает организатор.</div>' : '';

    let html = '<div class="card"><div class="row">' + personAvatar(user.avatar_url, user.name, 'lg') + '<div class="grow"><h2 class="ellipsis">' + esc(user.name || 'Игрок') + '</h2>' +
      '<div class="muted small">' + (user.username ? '@' + esc(user.username) + ' · ' : '') + 'ID ' + esc(user.telegram_id) + '</div></div>' + vPill(ver.status) + '</div>' +
      (p.team ? '<div class="divider"></div><button class="item" type="button" data-act="tab" data-tab="team"><span class="grow"><span class="title">' + ic('crown') + ' ' + esc(p.team.name) + '</span><span class="sub">' + (p.team.is_captain ? 'Капитан' : 'Игрок') + '</span></span><span class="chev">›</span></button>' : '') +
      '</div>';

    html += '<div class="card"><div class="card-head"><h3>MMR и ранг</h3></div><div class="mmr">' + medalHtml(medal) + '<div class="grow">' + mmrBlock + '</div></div>' +
      '<div class="note">Медаль: ' + medalText + '</div>' + range +
      (ver.admin_comment && mmr.state === 'rejected' ? '<div class="warnbox" style="margin-top:10px">Комментарий организатора: ' + esc(ver.admin_comment) + '</div>' : '');
    if (steam.linked) {
      html += '<div class="divider"></div><label class="field">Точный MMR (из клиента Dota 2)<div class="inline-form"><input class="input" id="mmrInput" inputmode="numeric" maxlength="4" placeholder="например, 3450" value="' +
        esc(mmr.declared !== null && mmr.declared !== undefined ? mmr.declared : '') + '" data-enter="profile-mmr"><button class="btn primary" type="button" data-act="profile-mmr">Отправить</button></div></label>' +
        '<div class="note">После отправки MMR проверяет организатор. До проверки он не участвует в среднем MMR команды.</div>';
    } else {
      html += '<div class="warnbox" style="margin-top:10px">Сначала привяжите Steam — затем можно будет указать MMR.</div>';
    }
    html += '</div>';
    if (steam.linked) {
      html += '<div class="card"><div class="card-head"><h3>Источники медали</h3><span class="muted small">OpenDota · STRATZ · Dotabuff</span></div>' + medalCheckHtml(p.medal_check, false) +
        '<div class="actions"><button class="btn small" type="button" data-act="profile-sources">' + ic('refresh') + ' Обновить данные источников</button></div>' +
        '<div class="note">Медаль сверяется по нескольким сайтам, выбирается самый достоверный вариант. Обновлять можно раз в 10 минут.</div></div>';
    }

    html += '<div class="card"><div class="card-head"><h3>Статистика</h3></div>';
    if (stats.public) {
      html += '<div class="grid3"><div class="stat"><span>Матчи</span><b>' + (num(stats.matches) !== null ? esc(stats.matches) : '—') + '</b></div>' +
        '<div class="stat"><span>Винрейт</span><b>' + (num(stats.winrate) !== null ? esc(stats.winrate) + '%' : '—') + '</b></div>' +
        '<div class="stat"><span>Часы</span><b>' + (num(stats.hours) !== null ? esc(stats.hours) : '—') + '</b></div></div><div class="note">«—» — источник не вернул значение.</div>';
    } else {
      html += '<div class="muted small">Публичная статистика недоступна: профиль скрыт в Dota 2 или источники её не вернули.</div>';
    }
    html += '</div>';

    html += '<div class="card"><div class="card-head"><h3>Steam / Dota 2</h3>' + (steam.linked ? pill('привязан', 'ok') : pill('не привязан', 'warn')) + '</div>';
    if (steam.linked) {
      const links = steam.links || {};
      html += '<div class="kv"><span>Ник</span><span>' + esc(steam.nickname || '—') + '</span></div>' +
        '<div class="kv"><span>Steam ID64</span><span>' + esc(steam.steam_id64) + '</span></div>' +
        '<div class="kv"><span>Dota Account ID</span><span>' + esc(steam.account_id !== null && steam.account_id !== undefined ? steam.account_id : '—') + '</span></div>' +
        '<div class="kv"><span>Источник проверки</span><span>' + esc(ver.source || '—') + '</span></div>' +
        '<div class="actions">' + ['steam', 'dotabuff', 'opendota', 'stratz'].filter((k) => links[k]).map((k) =>
          '<a class="btn small ghost" href="' + esc(links[k]) + '" target="_blank" rel="noopener" data-act="ext" data-url="' + esc(links[k]) + '">' + esc({ steam: 'Steam', dotabuff: 'Dotabuff', opendota: 'OpenDota', stratz: 'STRATZ' }[k]) + '</a>').join('') + '</div>' +
        '<div class="divider"></div><label class="field">Перепривязать Steam<div class="inline-form"><input class="input" id="steamInput" maxlength="300" placeholder="Steam ID64, ссылка или Dota ID" data-enter="profile-steam">' +
        '<button class="btn" type="button" data-act="profile-steam">Сменить</button></div></label><div class="note">При смене аккаунта MMR сбрасывается и снова проверяется организатором.</div>';
    } else {
      html += '<label class="field">Steam ID64, ссылка на профиль или Dota Account ID<div class="inline-form"><input class="input" id="steamInput" maxlength="300" placeholder="steamcommunity.com/profiles/7656…" data-enter="profile-steam">' +
        '<button class="btn primary" type="button" data-act="profile-steam">Привязать</button></div></label>' +
        '<div class="note">Проверка идёт через Steam → Dotabuff/OpenDota → STRATZ и может занять до 45 секунд.</div>';
    }
    html += '</div>';

    const pos = arr(p.positions);
    html += '<div class="card"><div class="card-head"><h3>Мои позиции</h3><span class="muted small">видят капитаны в поиске</span></div><div class="pos-pick" id="posPick">' +
      [1, 2, 3, 4, 5].map((x) => '<button type="button" data-act="toggle" class="' + (pos.includes(x) ? 'active' : '') + '" data-pos="' + x + '"><b>' + x + '</b><small>' + POS[x] + '</small></button>').join('') +
      '</div><div class="actions"><button class="btn primary" type="button" data-act="profile-positions">Сохранить позиции</button></div></div>';

    html += '<button class="item" type="button" ' + openAttr('support') + '><span class="avatar">' + ic('chat') + '</span><span class="grow"><span class="title">Связь с организаторами</span><span class="sub">Вопросы, проблемы с проверкой, жалобы</span></span><span class="chev">›</span></button>';
    ctx.set(html);
  };

  ACTIONS.toggle = (el) => { el.classList.toggle('active'); haptic(); };
  ACTIONS.ext = (el) => {
    const url = el.dataset.url;
    const app = tgApp();
    try { if (app && app.openLink) { app.openLink(url); return; } } catch (e) { /* ignore */ }
    window.open(url, '_blank', 'noopener');
  };
  ACTIONS['profile-mmr'] = async (el) => {
    const input = document.getElementById('mmrInput');
    const raw = input ? input.value.replace(/\s+/g, '') : '';
    if (!/^\d{1,4}$/.test(raw) || Number(raw) > 9500) { toast('Введите MMR целым числом от 0 до 9500', 'bad'); return; }
    const btn = el.tagName === 'BUTTON' ? el : document.querySelector('[data-act="profile-mmr"]');
    await mutate(btn, () => api('/api/v2/profile/mmr', { method: 'POST', body: { mmr: Number(raw) } }), (r) => r.message || 'MMR отправлен на проверку', { me: true });
  };
  ACTIONS['profile-steam'] = async (el) => {
    const input = document.getElementById('steamInput');
    const value = input ? input.value.trim() : '';
    if (!value) { toast('Укажите Steam ID64, ссылку или Dota Account ID', 'bad'); return; }
    const btn = el.tagName === 'BUTTON' ? el : document.querySelector('[data-act="profile-steam"]');
    const me = state.me || {};
    if (me.steam_linked) {
      const ok = await confirmDialog({ title: 'Перепривязать Steam?', text: 'Текущий MMR будет сброшен, новый аккаунт и MMR снова проверит организатор.', ok: 'Перепривязать', danger: true });
      if (!ok) return;
    }
    toast('Проверяю Steam… это может занять до 45 секунд');
    await mutate(btn, () => api('/api/v2/profile/steam', { method: 'POST', body: { steam: value }, timeout: 60000 }), (r) => r.message || 'Готово', { me: true });
  };
  ACTIONS['profile-sources'] = async (el) => {
    toast('Сверяю медаль по OpenDota, STRATZ и Dotabuff… до 30 секунд');
    await mutate(el, () => api('/api/v2/profile/sources/refresh', { method: 'POST', timeout: 60000 }),
      (r) => { const m = (r && r.medal_check && r.medal_check.medal) || {}; return m.name ? 'Медаль: ' + m.name : 'Источники не вернули медаль'; }, { me: true });
  };
  ACTIONS['profile-positions'] = async (el) => {
    const positions = Array.from(document.querySelectorAll('#posPick button.active')).map((b) => Number(b.dataset.pos));
    await mutate(el, () => api('/api/v2/profile/positions', { method: 'POST', body: { positions } }), 'Позиции сохранены');
  };

  // =========================================================================
  // Поддержка (обращения к организаторам)
  // =========================================================================
  VIEWS.support = async (ctx) => {
    const data = await ctx.api('/api/v2/support');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    let html = backBtn() + '<div class="page-head"><h1>Связь с организаторами</h1><p>Сообщение получат администраторы. Ответ придёт в бота и появится здесь.</p></div>' +
      '<div class="card"><label class="field">Ваше сообщение<textarea class="textarea" id="supportText" maxlength="3000" placeholder="Опишите вопрос или проблему"></textarea></label>' +
      '<button class="btn primary block" type="button" data-act="support-send">Отправить</button></div>' +
      '<div class="card"><div class="card-head"><h3>Мои обращения</h3><span class="muted small">' + items.length + '</span></div>';
    html += items.length ? '<div class="list">' + items.map((t) => '<div class="event"><div class="ei">' + (t.status === 'OPEN' ? ic('hourglass') : t.status === 'ANSWERED' ? ic('check') : ic('folder')) + '</div><div class="grow">' +
      '<div class="row between"><span class="et">№' + esc(t.id) + '</span>' + pill(S_STATUS[t.status] || t.status, t.status === 'ANSWERED' ? 'ok' : t.status === 'OPEN' ? 'warn' : '') + '</div>' +
      '<div class="eb">' + esc(t.message) + '</div>' + (t.reply ? '<div class="okbox" style="margin:8px 0 0">Ответ: ' + esc(t.reply) + '</div>' : '') +
      '<div class="ed">' + esc(fmtDate(t.created_at)) + '</div></div></div>').join('') + '</div>' : '<div class="muted small">Обращений пока нет.</div>';
    html += '</div>';
    ctx.set(html);
  };
  ACTIONS['support-send'] = async (el) => {
    const t = document.getElementById('supportText');
    const text = t ? t.value.trim() : '';
    if (!text) { toast('Напишите сообщение', 'bad'); return; }
    await mutate(el, () => api('/api/v2/support', { method: 'POST', body: { message: text } }), (r) => r.message || 'Отправлено');
  };

  // =========================================================================
  // Команда
  // =========================================================================
  function invitesBlock(invites) {
    if (!invites.length) return '';
    return '<div class="card hl"><div class="card-head"><h3>' + ic('mail') + ' Приглашения в команды</h3>' + pill(String(invites.length), 'warn') + '</div><div class="list">' +
      invites.map((i) => '<div class="item"><span>' + avatarHtml(i.avatar_url, i.team_name) + '</span><div class="grow"><div class="title">' + esc(i.team_name) + '</div>' +
        '<div class="sub">Капитан: ' + esc(i.captain || '—') + ' · до ' + esc(fmtDate(i.expires_at)) + '</div>' +
        '<div class="actions"><button class="btn small ok" type="button" data-act="invite-accept" data-id="' + esc(i.id) + '">Принять</button>' +
        '<button class="btn small danger" type="button" data-act="invite-decline" data-id="' + esc(i.id) + '">Отклонить</button>' +
        '<button class="btn small ghost" type="button" ' + openAttr('teamCard', { id: i.team_id }) + '>Состав</button></div></div></div>').join('') + '</div></div>';
  }

  function memberRow(m, t) {
    const canAll = t.positions_edit === 'all';
    const canSelf = t.positions_edit === 'self' && m.is_me;
    const editable = canAll || canSelf;
    let pos;
    if (editable) {
      pos = '<div class="pos-mini">' + [1, 2, 3, 4, 5].map((x) => '<button type="button" data-act="member-pos" data-user="' + esc(m.user_id) + '" data-pos="' + x + '" class="' +
        (m.position === x ? 'active' : '') + '" title="' + POS[x] + '">' + x + '</button>').join('') + '</div>';
    } else {
      pos = '<span class="small muted">Позиция: ' + (m.position ? esc(m.position + ' · ' + POS[m.position]) : 'не назначена') + '</span>';
    }
    const coachBtn = t.is_captain && m.role !== 'CAPTAIN' ? '<button class="btn small" type="button" data-act="member-coach" data-user="' + esc(m.user_id) + '" data-coach="' + (m.role === 'COACH' ? '0' : '1') + '">' + (m.role === 'COACH' ? 'Сделать игроком' : 'Сделать тренером') + '</button>' : '';
    const kick = coachBtn + (t.is_captain && !m.is_me && m.role !== 'CAPTAIN'
      ? '<button class="btn small danger" type="button" data-act="member-kick" data-user="' + esc(m.user_id) + '" data-name="' + esc(m.name) + '">Исключить</button>' : '');
    return '<div class="member' + (m.is_me ? ' me' : '') + '"><div class="row"><button class="avatar round" type="button" ' + openAttr('player', { id: m.user_id }) + '>' +
      (m.avatar_url ? '<img src="' + esc(m.avatar_url) + '" alt="" loading="lazy" data-fallback="' + initials(m.name) + '">' : initials(m.name)) + '</button>' +
      '<div class="grow"><div class="row wrap" style="gap:6px"><b class="ellipsis">' + esc(m.name) + '</b>' + (m.role === 'CAPTAIN' ? pill('капитан', 'gold') : '') + (m.is_me ? pill('вы', 'info') : '') + '</div>' +
      '<div class="small muted">' + (m.username ? '@' + esc(m.username) + ' · ' : '') + mmrInline(m.mmr) + '</div></div>' + medalHtml(m.medal, true) + '</div>' +
      '<div class="row between wrap">' + pos + kick + '</div></div>';
  }

  VIEWS.team = async (ctx) => {
    const data = await ctx.api('/api/v2/team');
    if (!ctx.alive()) return;
    const invites = arr(data.incoming_invites);
    const t = data.team;
    if (!t) {
      ctx.set('<div class="page-head"><div class="eyebrow">Команда</div><h1>Вы свободный игрок</h1><p>Создайте команду или примите приглашение капитана.</p></div>' +
        invitesBlock(invites) +
        '<div class="card"><div class="card-head"><h3>Создать команду</h3></div>' +
        '<label class="field">Название (до 64 символов)<input class="input" id="teamName" maxlength="64" placeholder="Например, Night Owls"></label>' +
        '<label class="field">Описание (необязательно)<textarea class="textarea" id="teamDesc" maxlength="500" placeholder="Кого ищете, когда играете, цели"></textarea></label>' +
        '<button class="btn primary block" type="button" data-act="team-create">Создать команду</button>' +
        '<div class="note">Вы станете капитаном. В составе до ' + esc(data.max_roster || 6) + ' игроков, для заявки на турнир нужно минимум 5.</div></div>' +
        '<div class="grid2"><button class="btn block" type="button" data-act="search-teams">Все команды</button><button class="btn block" type="button" data-act="tab" data-tab="search">Найти игроков</button></div>');
      return;
    }
    const roster = arr(t.roster);
    let html = invitesBlock(invites);
    html += '<div class="card"><div class="team-head">' + avatarHtml(t.avatar_url, t.name, 'lg') + '<div class="grow"><div class="eyebrow">Команда #' + esc(t.id) + '</div><h1 class="ellipsis">' + esc(t.name) + '</h1>' +
      '<div class="small muted">' + (t.is_captain ? '' + ic('crown') + ' Вы капитан' : 'Вы игрок состава') + ' · ' + (t.role_mode === 'CAPTAIN_CHOICE' ? 'позиции назначает капитан' : 'игроки выбирают позиции сами') + '</div></div></div>' +
      (t.description ? '<p class="desc">' + esc(t.description) + '</p>' : (t.is_captain ? '<p class="note">Добавьте описание — его увидят игроки в поиске команд.</p>' : ''));
    if (t.is_captain) {
      html += '<div class="actions"><button class="btn small" type="button" data-act="team-edit">' + ic('edit') + ' Изменить</button>' +
        '<label class="btn small" for="avatarFile">' + ic('image') + ' Аватар</label><input type="file" id="avatarFile" class="file-input" accept="image/*" data-change="team-avatar"></div>';
    }
    html += '</div>';
    if (t.roster_frozen) html += '<div class="warnbox">' + ic('lock') + ' ' + esc(t.roster_frozen) + '</div>';
    if (t.over_limit) html += '<div class="warnbox">В составе ' + esc(t.roster_count) + ' из ' + esc(t.max_roster) + ' игроков — лимит превышен. Подать заявку на турнир нельзя.</div>';

    html += '<div class="grid2" style="margin-bottom:12px"><div class="stat"><span>Состав</span><b>' + esc(t.roster_count) + ' / ' + esc(t.max_roster) + '</b><small>для заявки нужно ' + esc(t.min_to_apply) + '</small></div>' +
      '<div class="stat"><span>Средний MMR</span><b>' + (num(t.avg_mmr) !== null ? esc(t.avg_mmr) : '—') + '</b><small>подтверждено ' + esc(t.mmr_count) + ' из ' + esc(t.roster_count) + '</small></div></div>';

    html += '<div class="card"><div class="card-head"><h3>Состав</h3>' + (t.is_captain
      ? '<select class="select" style="width:auto;min-height:34px" data-change="team-mode"><option value="PLAYER_CHOICE"' + (t.role_mode === 'PLAYER_CHOICE' ? ' selected' : '') + '>Позиции: игроки</option><option value="CAPTAIN_CHOICE"' + (t.role_mode === 'CAPTAIN_CHOICE' ? ' selected' : '') + '>Позиции: капитан</option></select>'
      : '') + '</div><div class="list">' + roster.map((m) => memberRow(m, t)).join('') + '</div>' +
      (t.positions_edit === 'self' ? '<div class="note">Нажмите номер, чтобы выбрать свою позицию. Одна позиция — один игрок.</div>' : t.positions_edit === 'all' ? '<div class="note">Назначьте каждому игроку позицию 1–5.</div>' : '') + '</div>';

    if (t.is_captain) {
      const out = arr(t.outgoing_invites);
      html += '<div class="card"><div class="card-head"><h3>Исходящие приглашения</h3><button class="btn small primary" type="button" data-act="tab" data-tab="search">+ Найти игроков</button></div>' +
        (out.length ? '<div class="list">' + out.map((i) => '<div class="item"><div class="grow"><div class="title">' + esc(i.name) + '</div><div class="sub">' + (i.username ? '@' + esc(i.username) + ' · ' : '') + 'до ' + esc(fmtDate(i.expires_at)) + '</div></div>' +
          '<button class="btn small ghost" type="button" data-act="invite-cancel" data-id="' + esc(i.id) + '">Отозвать</button></div>').join('') + '</div>'
          : '<div class="muted small">Активных приглашений нет. Пригласить можно свободных игроков с подтверждённым MMR.</div>') + '</div>';
    }

    const apps = arr(t.applications);
    html += '<div class="card"><div class="card-head"><h3>Заявки на турниры</h3><button class="link" type="button" data-act="tab" data-tab="tournaments">Турниры</button></div>' +
      (apps.length ? '<div class="list">' + apps.map((a) => '<button class="item" type="button" ' + openAttr('tournament', { id: a.tournament_id }) + '><div class="grow"><div class="title">' + esc(a.tournament_name) + '</div>' +
        '<div class="sub">Заявка №' + esc(a.id) + ' · ' + esc(fmtDate(a.created_at)) + (a.comment ? ' · ' + esc(a.comment) : '') + '</div></div>' + aPill(a.status) + '</button>').join('') + '</div>'
        : '<div class="muted small">' + (t.is_captain ? 'Заявок пока нет. Подать заявку можно на странице турнира.' : 'Заявки подаёт капитан.') + '</div>') + '</div>';

    html += '<div class="card"><div class="card-head"><h3>Управление</h3></div><div class="actions" style="margin-top:0">' +
      '<button class="btn small ghost" type="button" data-act="search-teams">Все команды</button>';
    if (t.can_leave) html += '<button class="btn small danger" type="button" data-act="team-leave" data-name="' + esc(t.name) + '">Покинуть команду</button>';
    if (t.is_captain && t.can_delete) html += '<button class="btn small danger" type="button" data-act="team-delete" data-name="' + esc(t.name) + '">Расформировать</button>';
    html += '</div>' + (t.is_captain && t.delete_block_reason ? '<div class="note">' + esc(t.delete_block_reason) + '</div>' : '') + '</div>';
    ctx.set(html);
    state.ui.lastTeam = t;
  };

  // select/file: делегирование события change
  document.addEventListener('change', (e) => {
    const el = e.target.closest && e.target.closest('[data-change]');
    if (!el) return;
    const fn = ACTIONS[el.dataset.change];
    if (fn) Promise.resolve(fn(el, e)).catch((err) => toast(err.message || String(err), 'bad'));
  });

  ACTIONS['team-create'] = async (el) => {
    const name = (document.getElementById('teamName') || {}).value || '';
    const description = (document.getElementById('teamDesc') || {}).value || '';
    if (!name.trim()) { toast('Введите название команды', 'bad'); return; }
    await mutate(el, () => api('/api/v2/team', { method: 'POST', body: { name: name.trim(), description: description.trim() || null } }), 'Команда создана', { me: true });
  };
  ACTIONS['team-edit'] = async () => {
    const t = state.ui.lastTeam || {};
    const html = '<h3>Команда</h3><label class="field">Название<input class="input" data-field="name" maxlength="64" value="' + esc(t.name || '') + '"' + (t.can_rename ? '' : ' disabled') + '></label>' +
      (t.can_rename ? '' : '<div class="note" style="margin:-4px 0 10px">Название нельзя менять после допуска к турниру.</div>') +
      '<label class="field">Описание<textarea class="textarea" data-field="description" maxlength="500">' + esc(t.description || '') + '</textarea></label>' +
      '<div class="actions"><button class="btn ghost" type="button" data-modal="cancel">Отмена</button><button class="btn primary" type="button" data-modal="form">Сохранить</button></div>';
    const values = await openModal(html);
    if (!values || typeof values !== 'object') return;
    const body = { description: values.description || '' };
    const newName = String(values.name || '').trim();
    if (t.can_rename && newName && newName !== t.name) body.name = newName;
    await mutate(null, () => api('/api/v2/team', { method: 'PATCH', body }), 'Сохранено', { me: true });
  };
  ACTIONS['team-avatar'] = async (el) => {
    const f = el.files && el.files[0];
    el.value = '';
    if (!f) return;
    if (!/^image\//.test(f.type || '')) { toast('Выберите изображение', 'bad'); return; }
    if (f.size > 8 * 1024 * 1024) { toast('Файл больше 8 МБ', 'bad'); return; }
    const fd = new FormData(); fd.append('file', f);
    toast('Загружаю аватар…');
    await mutate(null, () => api('/api/v2/team/avatar', { method: 'POST', body: fd, timeout: 60000 }), 'Аватар обновлён');
  };
  ACTIONS['team-mode'] = async (el) => {
    await mutate(null, () => api('/api/v2/team/role-mode', { method: 'POST', body: { mode: el.value } }), 'Режим позиций изменён');
  };
  ACTIONS['member-pos'] = async (el) => {
    await mutate(el, () => api('/api/v2/team/members/' + el.dataset.user + '/position', { method: 'POST', body: { position: Number(el.dataset.pos) } }), 'Позиция назначена');
  };
  ACTIONS['member-coach'] = async (el) => {
    const on = el.dataset.coach === '1';
    await mutate(el, () => api('/api/v2/team/members/' + el.dataset.user + '/coach', { method: 'POST', body: { coach: on } }), on ? 'Назначен тренер' : 'Тренер снова игрок', { me: true });
  };
  ACTIONS['member-kick'] = async (el) => {
    if (!(await confirmDialog({ title: 'Исключить игрока?', text: el.dataset.name + ' будет удалён из состава и снова станет свободным игроком.', ok: 'Исключить', danger: true }))) return;
    await mutate(el, () => api('/api/v2/team/members/' + el.dataset.user, { method: 'DELETE' }), (r) => (r && r.pending ? r.message : 'Игрок исключён'));
  };
  ACTIONS['team-leave'] = async (el) => {
    if (!(await confirmDialog({ title: 'Покинуть команду?', text: 'Вы выйдете из «' + el.dataset.name + '». Вернуться можно только по новому приглашению капитана.', ok: 'Покинуть', danger: true }))) return;
    await mutate(el, () => api('/api/v2/team/leave', { method: 'POST' }), (r) => (r && r.pending ? r.message : 'Вы покинули команду'), { me: true });
  };
  ACTIONS['team-delete'] = async (el) => {
    const typed = await promptDialog({ title: 'Расформировать команду?', text: 'Все игроки станут свободными, приглашения и незавершённые заявки удалятся. Это нельзя отменить.\n\nВведите название команды для подтверждения:', placeholder: el.dataset.name, ok: 'Расформировать', danger: true, max: 64 });
    if (typed === null || typed === undefined) return;
    if (typed !== el.dataset.name) { toast('Название введено неверно — команда не удалена', 'bad'); return; }
    await mutate(el, () => api('/api/v2/team/delete', { method: 'POST', body: { confirm: true } }), 'Команда расформирована', { me: true });
  };
  ACTIONS['invite-accept'] = async (el) => {
    await mutate(el, () => api('/api/v2/invites/' + el.dataset.id + '/accept', { method: 'POST' }), (r) => (r && r.pending ? r.message : 'Вы в команде «' + (r.team_name || '') + '»'), { me: true });
  };
  ACTIONS['invite-decline'] = async (el) => {
    await mutate(el, () => api('/api/v2/invites/' + el.dataset.id + '/decline', { method: 'POST' }), 'Приглашение отклонено', { me: true });
  };
  ACTIONS['invite-cancel'] = async (el) => {
    await mutate(el, () => api('/api/v2/team/invites/' + el.dataset.id, { method: 'DELETE' }), 'Приглашение отозвано');
  };
  ACTIONS['search-teams'] = () => { state.ui.searchMode = 'teams'; switchTab('search'); };

  // Публичная карточка команды
  // ---------- 5.9: игрок/зритель, трансляция, матчи сегодня, болельщики, чат матча ----------
  VIEWS.welcome = async (ctx) => {
    ctx.set('<div class="page-head"><div class="eyebrow">TI:IT TOP · Dota 2</div><h1>Добро пожаловать!</h1><p>Выберите, как вы будете пользоваться приложением. Это можно поменять позже в профиле.</p></div>' +
      '<button class="kind-card" type="button" data-act="set-kind" data-kind="PLAYER"><span class="kc-ic">' + ic('gamepad') + '</span><span class="grow"><b>Я игрок</b><small>Участвую в турнирах: команда, заявки, проверка MMR, лобби и чаты матчей.</small></span></button>' +
      '<button class="kind-card" type="button" data-act="set-kind" data-kind="VIEWER"><span class="kc-ic">' + ic('users') + '</span><span class="grow"><b>Я зритель</b><small>Смотрю: сетка, расписание, результаты, трансляция. Могу болеть за команды и получать их уведомления.</small></span></button>');
  };
  ACTIONS['set-kind'] = async (el) => {
    await withBusy(el, async () => {
      try {
        await api('/api/v2/me/kind', { method: 'POST', body: { kind: el.dataset.kind } });
        await refreshMe(true);
        toast(el.dataset.kind === 'VIEWER' ? 'Вы зритель — приятного просмотра' : 'Вы игрок — заполните профиль', 'ok');
        switchTab(el.dataset.kind === 'VIEWER' ? 'home' : 'profile');
      } catch (err) { toast(err.message, 'bad'); }
    });
  };
  function streamCard() {
    const st = (state.me && state.me.stream) || {};
    if (!st.url) return '';
    return '<div class="card stream"><div class="row"><span class="avatar stream-ic">' + ic('bolt') + '</span><div class="grow"><b>Трансляция турниров</b><div class="small muted">twitch.tv/' + esc(st.channel) + '</div></div>' +
      '<button class="btn small primary" type="button" data-act="ext" data-url="' + esc(st.url) + '">Смотреть</button></div></div>';
  }
  // «MVP по выбору игроков»: задаёт админ (Админ-панель → MVP на главной) или голосование за MVP турнира
  function mvpCard() {
    const m = state.me && state.me.mvp;
    if (!m || !m.name) return '';
    return '<div class="card mvp"><div class="row"><span class="avatar mvp-ic">' + ic('crown') + '</span><div class="grow">' +
      '<div class="eyebrow">MVP матчей по выбору игроков</div><b class="mvp-name">' + esc(m.name) + '</b>' +
      (m.caption ? '<div class="small muted">' + esc(m.caption) + '</div>' : '') + '</div></div></div>';
  }
  function liveHtml(live) {
    const items = arr(live && live.items);
    if (!items.length) return '';
    return '<div class="card"><div class="card-head"><h3>' + ic('calendar') + ' Матчи сегодня</h3>' + pill(TZ_LABEL) + '</div>' + items.map((m) =>
      '<button class="srow" type="button" data-act="match-open" data-id="' + esc(m.id) + '"><span class="stime">' + esc(m.time) + '</span><span class="grow"><span class="sline">' + esc(m.team1 || 'TBD') + ' <span class="muted">vs</span> ' + esc(m.team2 || 'TBD') + '</span>' +
      '<span class="small muted">' + esc(m.round) + ' · BO' + esc(m.best_of) + (m.status === 'FINISHED' ? ' · ' + esc(m.score1) + ':' + esc(m.score2) : '') + '</span></span>' + mPill(m) + '</button>').join('') + '</div>';
  }
  const homePlayer = VIEWS.home;
  VIEWS.home = async (ctx) => {
    let live = null; try { live = await ctx.api('/api/v2/live'); } catch (e) { live = null; }
    if (!ctx.alive()) return;
    const top = mvpCard() + streamCard() + recordsItem() + liveHtml(live);
    if (state.me && state.me.kind === 'VIEWER') {
      const u = (state.me && state.me.user) || {};
      ctx.set('<div class="page-head"><div class="eyebrow">TI:IT TOP · зритель</div><h1>Привет, ' + esc(u.first_name || u.name || 'зритель') + '!</h1><p>Сетка, расписание, результаты и трансляция турниров.</p></div>' + top +
        '<button class="item" type="button" data-act="tab" data-tab="tournaments"><span class="avatar">' + ic('trophy') + '</span><span class="grow"><span class="title">Турниры и сетка</span><span class="sub">Расписание, результаты, статистика, MVP</span></span><span class="chev">›</span></button>' +
        '<button class="item" type="button" ' + openAttr('hall') + '><span class="avatar">' + ic('medal') + '</span><span class="grow"><span class="title">Зал славы</span><span class="sub">Победители прошлых турниров</span></span><span class="chev">›</span></button>' +
        '<div class="note">Откройте страницу команды и нажмите «Болеть» — придут уведомления о её матчах и результатах. Подписок: ' + esc(arr(state.me.follows).length) + '.</div>' +
        '<button class="btn block" type="button" data-act="set-kind" data-kind="PLAYER" style="margin-top:10px">' + ic('gamepad') + ' Стать игроком</button>');
      return;
    }
    const set = ctx.set;
    ctx.set = (h) => set(top + h);
    return homePlayer(ctx);
  };
  const profileBase = VIEWS.profile;
  VIEWS.profile = async (ctx) => {
    if (state.me && state.me.kind === 'VIEWER') {
      const u = (state.me && state.me.user) || {};
      ctx.set('<div class="card"><div class="row">' + personAvatar(u.avatar_url, u.name, 'lg') + '<div class="grow"><h2 class="ellipsis">' + esc(u.name || 'Зритель') + '</h2><div class="muted small">' + (u.username ? '@' + esc(u.username) : '') + '</div></div>' + pill('зритель', 'info') + '</div></div>' +
        '<div class="card"><div class="card-head"><h3>Вы зритель</h3></div><p class="small muted">Вам доступны турниры, сетка, расписание, результаты, статистика, зал славы, трансляция и общий чат. Болеете за команд: ' + esc(arr(state.me.follows).length) + '.</p>' +
        '<button class="btn primary block" type="button" data-act="set-kind" data-kind="PLAYER">' + ic('gamepad') + ' Стать игроком</button><div class="note">Игрок проходит регистрацию: Steam и точный MMR, затем может вступить в команду.</div></div>' +
        '<button class="item" type="button" ' + openAttr('support') + '><span class="avatar">' + ic('chat') + '</span><span class="grow"><span class="title">Связь с организаторами</span><span class="sub">Вопросы и предложения</span></span><span class="chev">›</span></button>');
      return;
    }
    return profileBase(ctx);
  };
  ACTIONS['team-follow'] = async (el) => {
    await mutate(el, () => api('/api/v2/teams/' + el.dataset.id + '/follow', { method: 'POST', body: { follow: el.dataset.on === '1' } }),
      el.dataset.on === '1' ? 'Теперь вы болеете за команду — придут уведомления о её матчах' : 'Вы больше не болеете за команду', { me: true });
  };
  // ---------- 5.15: записи трансляций ----------
  function recordsItem() {
    const n = Number(((state.me && state.me.stream) || {}).records || 0);
    if (!n && !(state.me && state.me.is_admin)) return '';
    return '<button class="item" type="button" ' + openAttr('records') + ' style="margin-bottom:12px"><span class="avatar">' + ic('image') + '</span><span class="grow"><span class="title">Записи трансляций</span><span class="sub">' +
      (n ? 'Видео: ' + n + ' · с переходом к нужному матчу' : 'Пока пусто — появятся после загрузки из OBS') + '</span></span><span class="chev">›</span></button>';
  }
  function fmtGb(bytes) { const b = Number(bytes || 0); return b >= 1e9 ? (b / 1e9).toFixed(1) + ' ГБ' : Math.max(1, Math.round(b / 1e6)) + ' МБ'; }
  function recCard(r) {
    return '<button class="rec-card" type="button" ' + openAttr('record', { id: r.id }) + '>' +
      '<span class="rec-poster">' + (r.poster_url ? '<img src="' + esc(r.poster_url) + '" alt="" loading="lazy">' : ic('image')) + (r.duration_text ? '<i>' + esc(r.duration_text) + '</i>' : '') + '</span>' +
      '<span class="rec-body"><b>' + esc(r.title) + '</b><span class="small muted">' + esc(r.when || '') + '</span>' +
      '<span class="meta">' + (r.tournament ? pill(r.tournament.name, 'info') : '') + (r.chapters_count ? pill('матчей: ' + r.chapters_count) : '') + (r.hidden ? pill('скрыта', 'warn') : '') + '</span></span></button>';
  }
  VIEWS.records = async (ctx) => {
    const d = await ctx.api('/api/v2/records');
    if (!ctx.alive()) return;
    const items = arr(d.items);
    let html = backBtn() + '<div class="page-head"><div class="eyebrow">TI:IT TOP · видео</div><h1>Записи трансляций</h1><p>Полные записи стримов турниров. Внутри — переход к началу каждого матча.</p></div>';
    if (d.disk) html += '<div class="note" style="margin:-4px 0 12px">Свободно на сервере: ' + esc(fmtGb(d.disk.free)) + ' из ' + esc(fmtGb(d.disk.total)) + '. Записи загружает скрипт с ПК стримера после закрытия OBS.</div>';
    html += items.length ? '<div class="rec-list">' + items.map(recCard).join('') + '</div>' : stateBox('Записей пока нет', 'После турнира здесь появятся записи трансляций.');
    ctx.set(html);
  };
  const REC_POS = 'tiit:recpos:';
  function recSaved(id) { try { return Number(localStorage.getItem(REC_POS + id) || 0); } catch (e) { return 0; } }
  function recSave(id, t) { try { if (t > 0) localStorage.setItem(REC_POS + id, String(Math.floor(t))); else localStorage.removeItem(REC_POS + id); } catch (e) { /* приватный режим */ } }
  function hms(sec) { const s = Math.max(0, Math.floor(sec)); return Math.floor(s / 3600) + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }
  VIEWS.record = async (ctx) => {
    const r = await ctx.api('/api/v2/records/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    const saved = recSaved(r.id);
    const chapters = arr(r.chapters);
    let html = backBtn() + '<div class="page-head"><div class="eyebrow">' + esc(r.when || 'Запись') + (r.duration_text ? ' · ' + esc(r.duration_text) : '') + '</div><h1>' + esc(r.title) + '</h1>' +
      (r.tournament ? '<p>' + esc(r.tournament.name) + '</p>' : '') + '</div>' +
      '<div class="rec-player"><video id="recVideo" controls playsinline preload="metadata"' + (r.poster_url ? ' poster="' + esc(r.poster_url) + '"' : '') + ' src="' + esc(r.video_url) + '"></video></div>';
    if (saved > 15 && (!r.duration || saved < r.duration - 30)) html += '<button class="btn small block" type="button" data-act="rec-seek" data-t="' + esc(saved) + '" id="recResume" style="margin-bottom:12px">' + ic('clock') + ' Продолжить с ' + esc(hms(saved)) + '</button>';
    html += '<div class="card"><div class="card-head"><h3>' + ic('flag') + ' Матчи в записи</h3>' + (chapters.some((c) => c.approx) ? pill('≈ по расписанию') : '') + '</div>' +
      (chapters.length ? chapters.map((c) => '<button class="srow rec-ch" type="button" data-act="rec-seek" data-t="' + esc(c.t) + '"><span class="stime">' + esc(c.at) + '</span><span class="grow"><span class="sline">' + esc(c.title) + '</span>' +
        (c.sub || c.approx ? '<span class="small muted">' + esc(c.sub || '') + (c.approx ? (c.sub ? ' · ' : '') + 'время примерное' : '') + '</span>' : '') + '</span></button>').join('')
        : '<div class="muted small">Матчи к записи не привязаны' + (state.me && state.me.is_admin ? ' — добавьте главы вручную ниже.' : '.') + '</div>') + '</div>' +
      '<div class="actions" style="justify-content:flex-start"><button class="btn small" type="button" data-act="ext" data-url="' + esc(new URL(r.video_url, location.href).href) + '">' + ic('download') + ' Открыть файл' + (r.size ? ' · ' + esc(fmtGb(r.size)) : '') + '</button></div>';
    if (state.me && state.me.is_admin) {
      html += '<div class="card" style="margin-top:12px"><div class="card-head"><h3>' + ic('settings') + ' Управление</h3>' + (r.hidden ? pill('скрыта от игроков', 'warn') : '') + '</div><div class="actions" style="justify-content:flex-start;flex-wrap:wrap">' +
        '<button class="btn small" type="button" data-act="rec-title" data-id="' + esc(r.id) + '" data-v="' + esc(r.title) + '">' + ic('edit') + ' Название</button>' +
        '<button class="btn small" type="button" data-act="rec-chapters" data-id="' + esc(r.id) + '">' + ic('flag') + ' Главы</button>' +
        '<button class="btn small" type="button" data-act="rec-tour" data-id="' + esc(r.id) + '">' + ic('trophy') + ' Турнир</button>' +
        '<button class="btn small" type="button" data-act="rec-hide" data-id="' + esc(r.id) + '" data-on="' + (r.hidden ? '0' : '1') + '">' + ic(r.hidden ? 'check' : 'lock') + (r.hidden ? ' Показать' : ' Скрыть') + '</button>' +
        '<button class="btn small danger" type="button" data-act="rec-delete" data-id="' + esc(r.id) + '">' + ic('trash') + ' Удалить</button></div>' +
        '<div class="note">Главы ' + (r.chapters_custom ? 'заданы вручную' : 'собраны автоматически по времени матчей («Идёт» / результат)') + '. ' + (r.tournament_fixed ? 'Турнир выбран вручную.' : r.tournament ? 'Турнир определён по матчам.' : 'Турнир не определён — выберите кнопкой «Турнир».') + '</div></div>';
      state.ui.recEdit = r;
    }
    ctx.set(html);
    const v = document.getElementById('recVideo');
    if (!v) return;
    let last = 0;
    v.addEventListener('timeupdate', () => {
      if (Math.abs(v.currentTime - last) < 5) return;
      last = v.currentTime;
      recSave(r.id, v.duration && v.currentTime > v.duration - 30 ? 0 : v.currentTime);
    });
    v.addEventListener('error', () => { if (ctx.alive()) toast('Видео не загрузилось. Проверьте интернет или попробуйте «Открыть файл».', 'bad'); });
  };
  ACTIONS['rec-seek'] = (el) => {
    const v = document.getElementById('recVideo');
    if (!v) return;
    const t = Number(el.dataset.t || 0);
    const jump = () => { v.currentTime = t; v.play().catch(() => {}); };
    if (v.readyState >= 1) jump(); else { v.addEventListener('loadedmetadata', jump, { once: true }); v.load(); }
    v.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const resume = document.getElementById('recResume'); if (resume) resume.remove();
  };
  async function recEdit(el, body, okText) {
    await mutate(el, () => api('/api/v2/admin/records/' + encodeURIComponent(el.dataset.id), { method: 'POST', body }), okText);
  }
  ACTIONS['rec-title'] = async (el) => {
    const v = await promptDialog({ title: 'Название записи', value: el.dataset.v, max: 120, placeholder: 'Например: TI:IT TOP #2 — 1/8 финала', ok: 'Сохранить' });
    if (v === null) return;
    await recEdit(el, { title: v }, 'Название сохранено');
  };
  ACTIONS['rec-chapters'] = async (el) => {
    const r = state.ui.recEdit || {};
    const v = await promptDialog({ title: 'Главы', text: 'По строке на матч: «0:12:30 Название». Пусто — снова автоматически по времени матчей.', value: r.chapters_text || '', multiline: true, max: 6000, ok: 'Сохранить' });
    if (v === null) return;
    await recEdit(el, { chapters: v }, v ? 'Главы сохранены' : 'Главы снова автоматические');
  };
  ACTIONS['rec-tour'] = async (el) => {
    let items = [];
    try { items = arr((await api('/api/v2/tournaments')).items); } catch (err) { toast(err.message, 'bad'); return; }
    const html = '<h3>Турнир записи</h3><p>От турнира зависит подпись в списке. «Авто» — по матчам внутри записи.</p><div class="chips">' +
      '<button class="chip" type="button" data-modal="form" data-intent="0">Авто</button>' +
      items.map((t) => '<button class="chip" type="button" data-modal="form" data-intent="' + esc(t.id) + '">' + esc(t.name) + '</button>').join('') +
      '</div><div class="actions"><button class="btn ghost" type="button" data-modal="cancel">Отмена</button></div>';
    const res = await openModal(html);
    if (!res) return;
    await recEdit(el, { tournament_id: Number(res.__intent) || null }, 'Турнир записи сохранён');
  };
  ACTIONS['rec-hide'] = async (el) => {
    await recEdit(el, { hidden: el.dataset.on === '1' }, el.dataset.on === '1' ? 'Запись скрыта — её видят только админы' : 'Запись снова видна всем');
  };
  ACTIONS['rec-delete'] = async (el) => {
    if (!(await confirmDialog({ title: 'Удалить запись?', text: 'Видео будет удалено с сервера насовсем. Оригинал на ПК стримера останется.', ok: 'Удалить', danger: true }))) return;
    const ok = await mutate(el, () => api('/api/v2/admin/records/' + encodeURIComponent(el.dataset.id), { method: 'DELETE', body: { confirm: true } }), 'Запись удалена', { reload: false });
    if (ok) { await refreshMe(true); goBack(); }
  };
  const chatState2 = { lastId: 0 };
  function mchatMsg(x) {
    return '<div class="msg' + (x.mine ? ' mine' : '') + (x.system ? ' sys' : '') + '"><div class="mh"><b>' + esc(x.system ? 'TI:IT TOP' : x.name) + '</b><span>' + esc(x.at || '') + '</span></div><div class="mt">' + esc(x.text) + '</div></div>';
  }
  VIEWS.matchChat = async (ctx) => {
    const d = await ctx.api('/api/v2/matches/' + encodeURIComponent(ctx.params.id) + '/chat');
    if (!ctx.alive()) return;
    let html = backBtn() + '<div class="page-head"><div class="eyebrow">Чат матча ' + esc(d.match_no) + ' · ' + esc(d.round || '') + '</div><h1>' + esc(d.team1 || 'TBD') + ' — ' + esc(d.team2 || 'TBD') + '</h1><p>' + esc(d.when || 'время не назначено') + '. Видят только игроки двух команд и организаторы.</p></div>';
    if (!d.open) { ctx.set(html + stateBox('Чат пока закрыт', d.reason || '')); return; }
    const msgs = arr(d.messages);
    chatState2.lastId = msgs.length ? msgs[msgs.length - 1].id : 0;
    html += '<div class="card"><div class="chat" id="mchat">' + (msgs.length ? msgs.map(mchatMsg).join('') : '<div class="muted small">Сообщений пока нет.</div>') + '</div>' +
      (d.can_write ? '<div class="inline-form" style="margin-top:10px"><input class="input" id="mchatInput" maxlength="1000" placeholder="Сообщение соперникам и своим" data-enter="mchat-send"><button class="btn primary" type="button" data-act="mchat-send" data-id="' + esc(d.match_id) + '">' + ic('send') + '</button></div>' : '<div class="note">Матч завершён — чат только для чтения.</div>') + '</div>';
    ctx.set(html);
    const box = document.getElementById('mchat'); if (box) box.scrollTop = box.scrollHeight;
    addTimer(async () => {
      if (!ctx.alive()) return;
      try {
        const n = await api('/api/v2/matches/' + encodeURIComponent(ctx.params.id) + '/chat?after_id=' + chatState2.lastId);
        const b = document.getElementById('mchat');
        if (b && arr(n.messages).length) {
          if (!chatState2.lastId) b.innerHTML = '';
          b.insertAdjacentHTML('beforeend', arr(n.messages).map(mchatMsg).join(''));
          chatState2.lastId = n.messages[n.messages.length - 1].id; b.scrollTop = b.scrollHeight;
        }
      } catch (e) { /* сеть моргнула — следующий опрос */ }
    }, 5000);
  };
  ACTIONS['mchat-send'] = async (el) => {
    const i = document.getElementById('mchatInput'); const text = i ? i.value.trim() : '';
    if (!text) return;
    const id = el.dataset.id || (state.stack[state.stack.length - 1].params || {}).id;
    try { await api('/api/v2/matches/' + id + '/chat', { method: 'POST', body: { text } }); i.value = ''; reload(); } catch (err) { toast(err.message, 'bad'); }
  };
  VIEWS.teamCard = async (ctx) => {
    const t = await ctx.api('/api/v2/teams/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    const roster = arr(t.roster);
    let html = backBtn() + '<div class="card"><div class="team-head">' + avatarHtml(t.avatar_url, t.name, 'lg') + '<div class="grow"><div class="eyebrow">Команда #' + esc(t.id) + '</div><h1 class="ellipsis">' + esc(t.name) + '</h1>' +
      '<div class="small muted">Капитан: ' + esc(t.captain ? t.captain.name : '—') + '</div></div></div>' + (t.description ? '<p class="desc">' + esc(t.description) + '</p>' : '') + '</div>' +
      '<div class="grid2" style="margin-bottom:12px"><div class="stat"><span>Состав</span><b>' + esc(t.roster_count) + ' / ' + esc(t.max_roster) + '</b></div>' +
      '<div class="stat"><span>Средний MMR</span><b>' + (num(t.avg_mmr) !== null ? esc(t.avg_mmr) : '—') + '</b><small>подтверждено ' + esc(t.mmr_count) + '</small></div></div>' +
      '<div class="card"><div class="card-head"><h3>Состав</h3></div><div class="list">' + roster.map((m) =>
        '<button class="item" type="button" ' + openAttr('player', { id: m.user_id }) + '>' + medalHtml(m.medal, true) + '<div class="grow"><div class="title">' + esc(m.name) + (m.role === 'CAPTAIN' ? ' ' + ic('crown') : m.role === 'COACH' ? ' ' + pill('тренер', 'info') : '') + '</div>' +
        '<div class="sub">' + (m.position ? 'Позиция ' + esc(m.position) + ' · ' : '') + mmrInline(m.mmr) + '</div></div><span class="chev">›</span></button>').join('') + '</div></div>';
    if (arr(t.tournaments).length) {
      html += '<div class="card"><div class="card-head"><h3>Допущена к турнирам</h3></div><div class="list">' + t.tournaments.map((x) =>
        '<button class="item" type="button" ' + openAttr('tournament', { id: x.id }) + '><div class="grow"><div class="title">' + esc(x.name) + '</div></div>' + tPill(x.status) + '</button>').join('') + '</div></div>';
    }
    ctx.set(html);
  };

  // Карточка игрока
  VIEWS.player = async (ctx) => {
    const p = await ctx.api('/api/v2/players/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    const stats = p.stats || {};
    const mmr = p.mmr || {};
    let html = backBtn() + '<div class="card"><div class="row">' + personAvatar(p.avatar_url, p.name, 'lg') + '<div class="grow"><h2 class="ellipsis">' + esc(p.name) + '</h2>' +
      '<div class="small muted">' + (p.username ? '@' + esc(p.username) : 'без username') + (p.steam_nickname ? ' · Steam: ' + esc(p.steam_nickname) : '') + '</div></div>' + vPill(p.verification) + '</div></div>';
    html += '<div class="card"><div class="mmr">' + medalHtml(p.medal) + '<div class="grow">' +
      (num(mmr.confirmed) !== null ? '<div class="mmr-value">' + esc(mmr.confirmed) + '</div><div class="mmr-tag">MMR подтверждён организатором</div>'
        : mmr.state === 'pending' ? '<div class="mmr-value pending">' + esc(mmr.declared) + '</div><div class="mmr-tag">Заявлен, ещё не проверен</div>'
          : '<div class="mmr-value none">Нет подтверждённого MMR</div>') +
      '</div></div><div class="note">Медаль: ' + (p.medal ? esc(p.medal.name) : 'нет данных источников') + '</div>' +
      '<div class="kv"><span>Позиции</span><span>' + esc(positionsText(p.positions)) + '</span></div>' +
      '<div class="kv"><span>Команда</span><span>' + (p.team ? '<button class="link" type="button" ' + openAttr('teamCard', { id: p.team.id }) + '>' + esc(p.team.name) + '</button>' : 'свободный игрок') + '</span></div>' +
      '<div class="kv"><span>Матчи / винрейт</span><span>' + (stats.public ? (num(stats.matches) !== null ? esc(stats.matches) : '—') + ' / ' + (num(stats.winrate) !== null ? esc(stats.winrate) + '%' : '—') : 'статистика скрыта') + '</span></div></div>';
    if (p.medal_check) html += '<div class="card"><div class="card-head"><h3>Источники медали</h3></div>' + medalCheckHtml(p.medal_check, false) + '</div>';
    if (p.can_invite) {
      html += p.invited ? '<div class="okbox">Приглашение уже отправлено и ждёт ответа.</div>'
        : '<button class="btn primary block" type="button" data-act="player-invite" data-user="' + esc(p.user_id) + '">Пригласить в мою команду</button>';
    }
    ctx.set(html);
  };
  ACTIONS['player-invite'] = async (el) => {
    await mutate(el, () => api('/api/v2/team/invites', { method: 'POST', body: { user_id: Number(el.dataset.user) } }), 'Приглашение отправлено');
  };

  // =========================================================================
  // Поиск: свободные игроки и команды
  // =========================================================================
  const searchState = { page: 1, items: [], total: 0, captainTeam: null, teamsPage: 1, teams: [], teamsTotal: 0 };

  VIEWS.search = async (ctx) => {
    const mode = state.ui.searchMode;
    const f = state.ui.players;
    let html = '<div class="page-head"><div class="eyebrow">Поиск</div><h1>' + (mode === 'teams' ? 'Команды' : 'Свободные игроки') + '</h1></div>' +
      '<div class="seg"><button type="button" data-act="search-mode" data-mode="players" class="' + (mode !== 'teams' ? 'active' : '') + '">Игроки</button>' +
      '<button type="button" data-act="search-mode" data-mode="teams" class="' + (mode === 'teams' ? 'active' : '') + '">Команды</button></div>';
    if (mode === 'teams') {
      html += '<div class="card"><div class="inline-form"><input class="input" id="teamsQ" maxlength="64" placeholder="Название команды" value="' + esc(state.ui.teamsQ) + '" data-enter="teams-search">' +
        '<button class="btn primary" type="button" data-act="teams-search">Найти</button></div></div><div id="results">' + skeleton(3) + '</div>';
      ctx.set(html);
      searchState.teamsPage = 1; searchState.teams = [];
      await loadTeams(ctx, false);
      return;
    }
    html += '<div class="card"><div class="inline-form"><input class="input" id="playersQ" maxlength="64" placeholder="Имя, @username или Steam-ник" value="' + esc(f.q) + '" data-enter="players-search">' +
      '<button class="btn primary" type="button" data-act="players-search">Найти</button></div>' +
      '<div class="divider"></div><div class="small muted" style="margin-bottom:6px">Позиция</div><div class="chips">' +
      '<button type="button" class="chip' + (!f.position ? ' active' : '') + '" data-act="players-pos" data-pos="">Любая</button>' +
      [1, 2, 3, 4, 5].map((x) => '<button type="button" class="chip' + (f.position === x ? ' active' : '') + '" data-act="players-pos" data-pos="' + x + '">' + x + ' · ' + POS[x] + '</button>').join('') + '</div>' +
      '<div class="grid3" style="margin-top:10px"><input class="input" id="minMmr" inputmode="numeric" maxlength="4" placeholder="MMR от" value="' + esc(f.min) + '" data-enter="players-search">' +
      '<input class="input" id="maxMmr" inputmode="numeric" maxlength="4" placeholder="MMR до" value="' + esc(f.max) + '" data-enter="players-search">' +
      '<select class="select" data-change="players-sort"><option value="mmr_desc"' + (f.sort === 'mmr_desc' ? ' selected' : '') + '>MMR ↓</option><option value="mmr_asc"' + (f.sort === 'mmr_asc' ? ' selected' : '') + '>MMR ↑</option><option value="new"' + (f.sort === 'new' ? ' selected' : '') + '>Новые</option></select></div>' +
      '<div class="note">Показаны игроки без команды с MMR, подтверждённым организатором.</div></div><div id="results">' + skeleton(4) + '</div>';
    ctx.set(html);
    searchState.page = 1; searchState.items = [];
    await loadPlayers(ctx, false);
  };

  function playersQuery(page) {
    const f = state.ui.players;
    const q = new URLSearchParams({ page: String(page), limit: '20', sort: f.sort || 'mmr_desc' });
    if (f.q) q.set('q', f.q);
    if (f.position) q.set('position', String(f.position));
    if (/^\d{1,4}$/.test(String(f.min))) q.set('min_mmr', String(f.min));
    if (/^\d{1,4}$/.test(String(f.max))) q.set('max_mmr', String(f.max));
    return '/api/v2/players?' + q.toString();
  }

  function playerRow(p) {
    const cap = searchState.captainTeam;
    let action = '';
    if (cap) {
      if (p.invited) action = pill('приглашён', 'info');
      else if (cap.can_invite) action = '<button class="btn small primary" type="button" data-act="search-invite" data-user="' + esc(p.user_id) + '">Пригласить</button>';
    }
    return '<div class="item">' + personAvatar(p.avatar_url, p.name) + medalHtml(p.medal, true) + '<button class="grow" type="button" style="background:none;border:0;padding:0;text-align:left;cursor:pointer;min-width:0" ' + openAttr('player', { id: p.user_id }) + '>' +
      '<div class="title">' + esc(p.name) + '</div><div class="sub">' + mmrInline(p.mmr) + ' · ' + esc(positionsText(p.positions)) + '</div></button>' + action + '</div>';
  }

  async function loadPlayers(ctx, append) {
    const box = document.getElementById('results');
    try {
      const data = await (ctx ? ctx.api(playersQuery(searchState.page)) : api(playersQuery(searchState.page)));
      if (ctx && !ctx.alive()) return;
      searchState.captainTeam = data.captain_team || null;
      searchState.total = Number(data.total || 0);
      searchState.items = append ? searchState.items.concat(arr(data.items)) : arr(data.items);
      if (!box || !box.isConnected) return;
      let html = '';
      if (searchState.captainTeam && !searchState.captainTeam.can_invite) html += '<div class="warnbox">В вашей команде нет свободных мест — приглашать нельзя.</div>';
      html += '<div class="small muted" style="margin-bottom:8px">Найдено: ' + esc(searchState.total) + '</div>';
      html += searchState.items.length ? '<div class="list">' + searchState.items.map(playerRow).join('') + '</div>' : stateBox('Никого не нашли', 'Измените фильтры или запрос.');
      if (data.has_more) html += '<div class="more"><button class="btn" type="button" data-act="players-more">Показать ещё</button></div>';
      box.innerHTML = html;
    } catch (err) {
      if (err.status === -1) return;
      if (box && box.isConnected) box.innerHTML = errorBox('Не удалось загрузить игроков', err, 'players-search');
    }
  }
  function readPlayerFilters() {
    const f = state.ui.players;
    const q = document.getElementById('playersQ'); if (q) f.q = q.value.trim();
    const mn = document.getElementById('minMmr'); if (mn) f.min = mn.value.trim();
    const mx = document.getElementById('maxMmr'); if (mx) f.max = mx.value.trim();
  }
  ACTIONS['search-mode'] = (el) => { state.ui.searchMode = el.dataset.mode; reload(); };
  ACTIONS['players-search'] = () => { readPlayerFilters(); reload(); };
  ACTIONS['players-pos'] = (el) => { readPlayerFilters(); state.ui.players.position = el.dataset.pos ? Number(el.dataset.pos) : null; reload(); };
  ACTIONS['players-sort'] = (el) => { readPlayerFilters(); state.ui.players.sort = el.value; reload(); };
  ACTIONS['players-more'] = async (el) => { await withBusy(el, async () => { searchState.page += 1; await loadPlayers(null, true); }); };
  ACTIONS['search-invite'] = async (el) => {
    await withBusy(el, async () => {
      try {
        await api('/api/v2/team/invites', { method: 'POST', body: { user_id: Number(el.dataset.user) } });
        toast('Приглашение отправлено', 'ok');
        const p = searchState.items.find((x) => String(x.user_id) === el.dataset.user);
        if (p) p.invited = true;
        el.outerHTML = pill('приглашён', 'info');
      } catch (err) { toast(err.message, 'bad'); }
    });
  };

  async function loadTeams(ctx, append) {
    const box = document.getElementById('results');
    const q = new URLSearchParams({ page: String(searchState.teamsPage), limit: '20' });
    if (state.ui.teamsQ) q.set('q', state.ui.teamsQ);
    try {
      const data = await (ctx ? ctx.api('/api/v2/teams?' + q) : api('/api/v2/teams?' + q));
      if (ctx && !ctx.alive()) return;
      searchState.teamsTotal = Number(data.total || 0);
      searchState.teams = append ? searchState.teams.concat(arr(data.items)) : arr(data.items);
      if (!box || !box.isConnected) return;
      let html = '<div class="small muted" style="margin-bottom:8px">Команд: ' + esc(searchState.teamsTotal) + '</div>';
      html += searchState.teams.length ? '<div class="list">' + searchState.teams.map((t) =>
        '<button class="item" type="button" ' + openAttr('teamCard', { id: t.id }) + '>' + avatarHtml(t.avatar_url, t.name) + '<div class="grow"><div class="title">' + esc(t.name) + '</div>' +
        '<div class="sub">' + esc(t.members) + '/' + esc(t.max_members) + ' · ' + (num(t.avg_mmr) !== null ? 'ср. ' + esc(t.avg_mmr) + ' MMR' : 'MMR нет данных') + ' · капитан ' + esc(t.captain ? t.captain.name : '—') + '</div></div>' +
        (t.open_slots > 0 ? pill(t.open_slots + ' ' + plural(t.open_slots, 'место', 'места', 'мест'), 'ok') : pill('полный')) + '</button>').join('') + '</div>' : stateBox('Команды не найдены', '');
      if (data.has_more) html += '<div class="more"><button class="btn" type="button" data-act="teams-more">Показать ещё</button></div>';
      box.innerHTML = html;
    } catch (err) {
      if (err.status === -1) return;
      if (box && box.isConnected) box.innerHTML = errorBox('Не удалось загрузить команды', err, 'teams-search');
    }
  }
  ACTIONS['teams-search'] = () => { const i = document.getElementById('teamsQ'); state.ui.teamsQ = i ? i.value.trim() : ''; reload(); };
  ACTIONS['teams-more'] = async (el) => { await withBusy(el, async () => { searchState.teamsPage += 1; await loadTeams(null, true); }); };

  // =========================================================================
  // Турниры
  // =========================================================================
  function tourCard(t) {
    const my = t.my_application ? aPill(t.my_application) : '';
    return '<button class="tour" type="button" ' + openAttr('tournament', { id: t.id }) + '><div class="row between"><b class="ellipsis">' + esc(t.name) + '</b>' + tPill(t.status, t.registration_open) + '</div>' +
      '<div class="small muted" style="margin-top:4px">Старт: ' + esc(fmtDate(t.start_date)) + '</div>' +
      '<div class="meta">' + pill('команд ' + t.approved_count + '/' + t.max_teams) + (t.registration_open ? pill('мест: ' + t.slots_left, t.slots_left ? 'ok' : 'bad') : '') + my + '</div></button>';
  }

  VIEWS.tournaments = async (ctx) => {
    const data = await ctx.api('/api/v2/tournaments');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    const active = items.filter((t) => t.status === 'REGISTRATION' || t.status === 'ACTIVE');
    const past = items.filter((t) => !(t.status === 'REGISTRATION' || t.status === 'ACTIVE'));
    let html = '<div class="page-head"><div class="eyebrow">Соревнования</div><h1>Турниры</h1><p>Регистрация, допущенные команды и сетка плей-офф.</p></div>' +
      '<button class="item" type="button" ' + openAttr('hall') + ' style="margin-bottom:12px"><span class="avatar">' + ic('trophy') + '</span><span class="grow"><span class="title">Зал славы</span><span class="sub">Победители и MVP прошлых турниров</span></span><span class="chev">›</span></button>';
    if (!items.length) html += stateBox('Турниров пока нет', 'Следите за объявлениями в боте.');
    if (active.length) html += '<h3 style="margin:4px 0 10px">Актуальные</h3>' + active.map(tourCard).join('');
    if (past.length) html += '<h3 style="margin:16px 0 10px">Прошедшие и отменённые</h3>' + past.map(tourCard).join('');
    ctx.set(html);
  };

  // ---------- Турнирный движок 5.2: сетка, расписание, окно матча ----------
  const M_STATUS_KIND = { WAITING: '', SCHEDULED: 'info', LIVE: 'ok', FINISHED: '', POSTPONED: 'warn' };
  function mPill(m) { return pill(m.status_text || m.status, M_STATUS_KIND[m.status] || ''); }
  function teamLine(team, placeholder, score, winnerId, avatars, bye) {
    if (!team) return '<div class="bt empty"><span class="seed">—</span><span class="nm">' + esc(placeholder || 'Ожидается') + '</span><span class="sc"></span></div>';
    const done = winnerId !== null && winnerId !== undefined;
    const cls = done ? (Number(winnerId) === Number(team.id) ? ' win' : ' lose') : '';
    return '<div class="bt' + cls + '"><span class="seed">' + (team.seed ? '#' + esc(team.seed) : '') + '</span>' +
      avatarHtml(team.avatar_url || (avatars && avatars[team.id]) || null, team.name) +
      '<span class="nm">' + esc(team.name) + '</span><span class="sc">' + (bye ? '<span class="bye" title="Проходит в этот раунд без игры: в сетке мест больше, чем команд">BYE</span>' : (score === null || score === undefined ? '' : esc(score))) + '</span></div>';
  }
  function matchCard(m, avatars, myTeam) {
    const mine = myTeam && m.team1 && m.team2 ? [m.team1.id, m.team2.id].map(Number).indexOf(Number(myTeam)) >= 0 : !!m.mine;
    return '<button class="bm' + (mine ? ' mine' : '') + (m.status === 'LIVE' ? ' live' : '') + '" type="button" data-act="match-open" data-id="' + esc(m.id) + '">' +
      '<div class="bh"><span>Матч ' + esc(m.no) + ' · BO' + esc(m.best_of) + '</span><span class="st st-' + esc(m.status) + '">' + esc(m.status_text) + '</span></div>' +
      teamLine(m.team1, m.team1_placeholder, m.score1, m.winner_team_id, avatars) +
      teamLine(m.team2, m.team2_placeholder, m.score2, m.winner_team_id, avatars) +
      '<div class="bmt">' + (m.scheduled_text ? ic('clock') + ' ' + esc(m.scheduled_text) + ' ' + TZ_LABEL : 'время не назначено') + '</div></button>';
  }
  function standingsHtml(st, avatars) {
    return arr(st).map((g) => '<div class="br-title">' + esc(g.title) + '</div><div class="tbl-wrap"><table class="standings"><tr><th>#</th><th>Команда</th><th>И</th><th>В</th><th>Н</th><th>П</th><th>Карты</th><th>Очки</th></tr>' +
      arr(g.rows).map((r) => '<tr class="' + (r.advancing ? 'adv' : r.status === 'eliminated' ? 'out' : '') + '"><td>' + esc(r.place) + '</td><td class="tn">' + avatarHtml(r.avatar_url || (avatars && avatars[r.team_id]) || null, r.name) + '<span>' + esc(r.name) + '</span></td><td>' + esc(r.played) + '</td><td>' + esc(r.wins) + '</td><td>' + esc(r.draws) + '</td><td>' + esc(r.losses) + '</td><td>' + esc(r.maps_won) + ':' + esc(r.maps_lost) + '</td><td><b>' + esc(r.points) + '</b></td></tr>').join('') +
      '</table></div>').join('');
  }
  function engineHtml(view, avatars, myTeam) {
    if (!view || !view.exists) return '';
    const stages = arr(view.all_stages).length ? view.all_stages : [view];
    if (stages.length > 1) return stages.map((st, i) => '<div class="stage-h">Этап ' + (i + 1) + ' · ' + esc(st.description || st.format_title) + '</div>' + engineStageHtml(st, avatars, myTeam)).join('');
    return engineStageHtml(view, avatars, myTeam);
  }
  // Команды с BYE показываем и в первом раунде — карточкой «Автопроход» на своём месте сетки,
  // чтобы у каждой команды была строка в 1/8 (1/4…), а не «появление» сразу во втором раунде.
  function withByeCards(r1, r2) {
    const cards = arr(r1.matches).slice();
    arr(r2.matches).forEach((m) => {
      [1, 2].forEach((slot) => {
        if (m['team' + slot + '_bye'] && m['team' + slot]) cards.push({ __bye: true, team: m['team' + slot], position: 2 * (m.position - 1) + slot, next: m.no });
      });
    });
    return cards.sort((a, b) => a.position - b.position);
  }
  function byeCard(c, avatars) {
    return '<div class="bm byecard"><div class="bh"><span>Автопроход</span><span class="st">BYE</span></div>' +
      teamLine(c.team, '', null, null, avatars) +
      '<div class="bt empty"><span class="seed">—</span><span class="nm">без соперника</span><span class="sc"></span></div>' +
      '<div class="bmt">сразу в матч ' + esc(c.next) + '</div></div>';
  }
  function engineStageHtml(view, avatars, myTeam) {
    let html = '';
    const byes = arr(view.brackets).reduce((n, b) => n + arr(b.rounds).reduce((k, r) => k + arr(r.matches).reduce((q, m) => q + (m.team1_bye ? 1 : 0) + (m.team2_bye ? 1 : 0), 0), 0), 0);
    if (byes) html += '<div class="note" style="margin:0 0 10px">Команд меньше, чем мест в сетке: у ' + byes + ' ' + plural(byes, 'команды', 'команд', 'команд') + ' с лучшим посевом в первом раунде нет соперника — это «Автопроход» (BYE), они сразу встречают победителя соседнего матча.</div>';
    if (arr(view.standings).length) html += standingsHtml(view.standings, avatars);
    if (view.champion) {
      html += '<div class="champion">' + ic('trophy') + ' Победитель: <b>' + esc(view.champion.name) + '</b></div>';
    }
    arr(view.brackets).forEach((b) => {
      if (arr(view.brackets).length > 1 || view.kind === 'table') html += '<div class="br-title">' + esc(view.kind === 'table' ? 'Матчи · ' + b.title : b.title) + '</div>';
      const rounds = arr(b.rounds);
      const firstCol = rounds.length > 1 && rounds[0].round_no === 1 && view.kind !== 'table' ? withByeCards(rounds[0], rounds[1]) : null;
      html += '<div class="bracket' + (view.kind === 'table' ? '' : ' tree') + '"><div class="bracket-board">' + rounds.map((r, i) =>
        '<div class="bracket-round"><div class="rt">' + esc(r.name) + '</div><div class="rms">' +
        (i === 0 && firstCol ? firstCol.map((m) => m.__bye ? byeCard(m, avatars) : matchCard(m, avatars, myTeam)) : arr(r.matches).map((m) => matchCard(m, avatars, myTeam))).join('') +
        '</div></div>'
      ).join('') + '</div></div>';
    });
    return html + '<div class="note">Время — по Москве (' + TZ_LABEL + '). Нажмите на матч, чтобы открыть подробности.</div>';
  }
  function scheduleHtml(data, filter, avatars, myTeam) {
    let items = arr(data.items);
    if (filter === 'mine') items = items.filter((m) => m.mine || (myTeam && [m.team1 && m.team1.id, m.team2 && m.team2.id].map(Number).indexOf(Number(myTeam)) >= 0));
    if (filter === 'today') { const today = fmtDate(new Date().toISOString(), false); items = items.filter((m) => m.day === today); }
    if (filter === 'upcoming') items = items.filter((m) => m.status !== 'FINISHED');
    if (!items.length) return '<div class="muted small">' + (filter === 'mine' ? 'У вашей команды нет матчей в сетке.' : filter === 'today' ? 'Сегодня матчей нет.' : 'Матчей пока нет.') + '</div>';
    let html = ''; let day = null;
    items.forEach((m) => {
      const d = m.day || 'Время не назначено';
      if (d !== day) { day = d; html += '<div class="day-h">' + esc(d) + '</div>'; }
      html += '<button class="srow' + (m.mine ? ' mine' : '') + '" type="button" data-act="match-open" data-id="' + esc(m.id) + '">' +
        '<span class="stime">' + esc(m.time || '—:—') + '</span><span class="grow"><span class="sline">' +
        esc(m.team1 ? m.team1.name : m.team1_placeholder) + ' <span class="muted">vs</span> ' + esc(m.team2 ? m.team2.name : m.team2_placeholder) + '</span>' +
        '<span class="small muted">' + esc(m.bracket_title) + ' · ' + esc(m.round_name) + ' · BO' + esc(m.best_of) +
        (m.status === 'FINISHED' ? ' · ' + esc(m.score1) + ':' + esc(m.score2) : '') + '</span></span>' + mPill(m) + '</button>';
    });
    return html;
  }

  const LOG_ACTION = { schedule: 'Назначено время', reschedule: 'Изменено время', postpone: 'Перенесён', result: 'Результат', result_correct: 'Исправлен результат',
    result_reset: 'Результат сброшен', cascade_reset: 'Сброшен каскадом', status: 'Статус', best_of: 'Формат серии', followup_removed: 'Матч удалён', reminder: 'Напоминание командам' };
  function scoreSelect(field, need, value) {
    let o = '';
    for (let i = 0; i <= need; i += 1) o += '<option value="' + i + '"' + (Number(value) === i ? ' selected' : '') + '>' + i + '</option>';
    return '<select class="select" data-field="' + field + '">' + o + '</select>';
  }
  function matchModalHtml(m) {
    const t1 = m.team1 ? m.team1.name : (m.team1_placeholder || 'Ожидается');
    const t2 = m.team2 ? m.team2.name : (m.team2_placeholder || 'Ожидается');
    let html = '<div class="eyebrow">' + esc(m.tournament_name || '') + ' · ' + esc(m.bracket_title || '') + '</div>' +
      '<h3>Матч ' + esc(m.no) + ' · ' + esc(m.round_name || '') + '</h3><div class="meta" style="margin-bottom:10px">' + mPill(m) + pill('BO' + m.best_of) +
      (m.result_type === 'forfeit' ? pill('тех. результат', 'warn') : '') + '</div>' +
      '<div class="bm static">' + teamLine(m.team1, m.team1_placeholder, m.score1, m.winner_team_id) + teamLine(m.team2, m.team2_placeholder, m.score2, m.winner_team_id) + '</div>' +
      '<div class="mm-info">' + ic('clock') + ' ' + (m.scheduled_text ? esc(m.scheduled_text) + ' ' + TZ_LABEL : 'Время не назначено') + '</div>' +
      ((m.next_winner || m.next_loser) ? '<div class="note">' + (m.next_winner ? 'Победитель → ' + esc(m.next_winner) : '') + (m.next_winner && m.next_loser ? ' · ' : '') + (m.next_loser ? 'Проигравший → ' + esc(m.next_loser) : '') + '</div>' : '');
    const ready = arr(m.ready_team_ids).map(Number);
    if (m.team1 && m.team2 && m.status !== 'FINISHED' && (ready.length || m.lobby)) {
      html += '<div class="note">Готовность: ' + [m.team1, m.team2].map((tm) => esc(tm.name) + ' — ' + (ready.indexOf(Number(tm.id)) >= 0 ? 'готова' : 'не отметилась')).join(' · ') + '</div>';
    }
    // 5.13: карты серии (ID матчей Dota) и прогнозы зрителей.
    if (arr(m.games).length && m.team1 && m.team2) {
      html += '<div class="note">' + arr(m.games).map((g) => 'Карта ' + esc(g.no) + ': ' + esc(Number(g.winner_team_id) === Number(m.team1.id) ? m.team1.name : m.team2.name) +
        (g.dota_match_id ? ' <button class="link small" type="button" data-act="ext" data-url="https://www.opendota.com/matches/' + esc(g.dota_match_id) + '">матч ' + esc(g.dota_match_id) + '</button>' : '')).join(' · ') + '</div>';
    }
    const pr = m.prediction;
    if (pr && m.team1 && m.team2) {
      const pct = (n) => (pr.total ? Math.round(n * 100 / pr.total) : 50);
      const mine = pr.mine ? (Number(pr.mine) === Number(m.team1.id) ? m.team1.name : m.team2.name) : null;
      html += '<div class="mm-sec"><div class="mm-t">' + ic('bolt') + ' Кто победит? <span class="small muted">· прогнозов: ' + esc(pr.total) + '</span></div>' +
        '<div class="pred-bar"><i style="width:' + pct(pr.team1) + '%"></i></div>' +
        '<div class="row between small"><span>' + esc(m.team1.name) + ' — ' + pct(pr.team1) + '%</span><span>' + pct(pr.team2) + '% — ' + esc(m.team2.name) + '</span></div>' +
        (mine ? '<div class="note">Ваш прогноз: <b>' + esc(mine) + '</b>' + (pr.correct === true ? ' — угадали!' : pr.correct === false ? ' — не угадали' : '') + '</div>' : '') +
        (pr.open ? '<div class="actions"><button class="btn small" type="button" data-modal="form" data-intent="predict1">' + esc(m.team1.name) + '</button><button class="btn small" type="button" data-modal="form" data-intent="predict2">' + esc(m.team2.name) + '</button></div>' +
          '<div class="note">Можно поменять до начала матча. Лучшие предсказатели — на странице турнира, вкладка «Прогнозы».</div>'
          : (pr.closed_reason ? '<div class="note">' + esc(pr.closed_reason) + '</div>' : '')) + '</div>';
    }
    if (m.lobby) {
      html += '<div class="actions" style="justify-content:flex-start"><button class="btn small primary" type="button" data-modal="form" data-intent="chat">' + ic('chat') + ' Чат матча</button></div>';
      html += '<div class="mm-sec"><div class="mm-t">' + ic('gamepad') + ' Лобби</div><div class="kv"><span>Название</span><span><b>' + esc(m.lobby.name) + '</b></span></div>' +
        '<div class="kv"><span>Пароль</span><span><b>' + esc(m.lobby.password) + '</b></span></div><div class="note">Лобби создаёт капитан «' + esc(m.team1 ? m.team1.name : '') + '». Не публикуйте пароль в общем чате.</div></div>';
    }
    const rep = m.report;
    if (rep) {
      const rs = { PENDING: 'ждёт подтверждения соперника', CONFIRMED: 'соперник подтвердил — ждёт организатора', DISPUTED: 'соперник оспорил — решает организатор' }[rep.status] || rep.status;
      html += '<div class="mm-sec"><div class="mm-t">' + ic('flag') + ' Результат от капитана</div><div><b>' + esc(m.team1 ? m.team1.name : '') + ' ' + esc(rep.score1) + ':' + esc(rep.score2) + ' ' + esc(m.team2 ? m.team2.name : '') + '</b></div>' +
        '<div class="note">' + esc(rs) + (rep.comment ? ' · ' + esc(rep.comment) : '') + '</div>' + (rep.screenshot_url ? '<img id="rsImg" class="shot" alt="скриншот">' : '') +
        dotaGamesHtml(rep.dota, m);
      if (rep.status === 'PENDING' && m.i_am_captain && Number(m.my_team_id) !== Number(rep.team_id)) {
        html += '<input class="input" data-field="rcomment" maxlength="300" placeholder="Комментарий (если оспариваете)" style="margin-top:8px"><div class="actions"><button class="btn small ok" type="button" data-modal="form" data-intent="report-confirm">Подтвердить</button>' +
          '<button class="btn small danger" type="button" data-modal="form" data-intent="report-dispute">Оспорить</button></div>';
      }
      if (m.can_edit) html += '<div class="actions"><button class="btn small primary" type="button" data-modal="form" data-intent="report-approve">Одобрить результат</button><button class="btn small danger" type="button" data-modal="form" data-intent="report-reject">Отклонить</button></div>';
      html += '</div>';
    } else if (m.i_am_captain && m.team1 && m.team2 && m.status !== 'FINISHED') {
      const need2 = m.wins_needed || 1;
      html += '<div class="mm-sec"><div class="mm-t">' + ic('flag') + ' Сообщить результат</div>' +
        '<label class="field">ID матчей Dota — быстрее и без споров<input class="input" data-field="rdota" inputmode="numeric" maxlength="200" placeholder="например 8012345678, 8012349999"></label>' +
        '<div class="note" style="margin-top:-6px">ID матча — в клиенте Dota (Профиль → Последние матчи) или ссылка Dotabuff/OpenDota. Бот сам узнает победителя, длительность и героев; если команды опознаны по Steam, подтверждать соперника не нужно. По одному ID на карту серии.</div>' +
        '<div class="small muted" style="margin:8px 0 4px">…или счёт вручную:</div>' +
        '<div class="grid2"><label class="field">' + esc(m.team1.name) + scoreSelect('rs1', need2, null) + '</label><label class="field">' + esc(m.team2.name) + scoreSelect('rs2', need2, null) + '</label></div>' +
        '<label class="field">Скриншот итога (необязательно)<input class="input" type="file" accept="image/*" data-field="rshot"></label>' +
        '<button class="btn small primary block" type="button" data-modal="form" data-intent="report">Отправить результат</button><div class="note">Соперник подтвердит, организатор одобрит — и сетка обновится.</div></div>';
    }
    if (m.can_edit) {
      const both = m.team1 && m.team2;
      const need = m.wins_needed || 1;
      const finished = m.status === 'FINISHED';
      const opts = arr(m.team_options);
      if (!finished && m.status !== 'LIVE' && opts.length) {
        const sel = (field, cur) => '<select class="select" data-field="' + field + '"><option value="">— пусто —</option>' + opts.map((o) => '<option value="' + esc(o.id) + '"' + (cur && Number(cur.id) === Number(o.id) ? ' selected' : '') + '>' + esc(o.name) + '</option>').join('') + '</select>';
        html += '<div class="mm-sec"><div class="mm-t">Кто играет</div><div class="grid2">' + sel('team1', m.team1) + sel('team2', m.team2) + '</div>' +
          '<div class="actions"><button class="btn small" type="button" data-modal="form" data-intent="teams">Сохранить пару</button>' +
          (m.manual ? '<button class="btn small danger" type="button" data-modal="form" data-intent="delete">Удалить матч</button>' : '') + '</div></div>';
      }
      if (!finished) {
        html += '<div class="mm-sec"><div class="mm-t">Время матча (' + TZ_LABEL + ')</div>' +
          '<input class="input" type="datetime-local" data-field="when" value="' + esc(m.scheduled_input || '') + '">' +
          '<div class="actions"><button class="btn small primary" type="button" data-modal="form" data-intent="schedule">Сохранить время</button>' +
          '<button class="btn small" type="button" data-modal="form" data-intent="postpone">Перенести</button>' +
          (m.scheduled_input ? '<button class="btn small ghost" type="button" data-modal="form" data-intent="unschedule">Убрать время</button>' : '') + '</div></div>';
      }
      if (both) {
        html += '<div class="mm-sec"><div class="mm-t">' + (finished ? 'Исправить результат' : 'Результат серии') + ' · BO' + esc(m.best_of) + ' (до ' + need + ' ' + plural(need, 'победы', 'побед', 'побед') + ')</div>' +
          '<div class="grid2"><label class="field">' + esc(t1) + scoreSelect('score1', need, m.score1) + '</label><label class="field">' + esc(t2) + scoreSelect('score2', need, m.score2) + '</label></div>' +
          '<label class="field">Техническая победа<select class="select" data-field="forfeit"><option value="">нет — обычный счёт</option><option value="1">' + esc(t1) + '</option><option value="2">' + esc(t2) + '</option></select></label>' +
          '<div class="note" style="margin-top:0">Допустимо: ' + esc(arr(m.allowed_scores).join(', ')) + '</div>' +
          '<div class="actions"><button class="btn small primary" type="button" data-modal="form" data-intent="result">Сохранить результат</button>' +
          (finished ? '<button class="btn small danger" type="button" data-modal="form" data-intent="reset">Сбросить результат</button>' : '') + '</div></div>';
      }
      if (!finished) {
        html += '<div class="mm-sec"><div class="mm-t">Статус и формат</div><div class="chips">' +
          (both ? '<button class="chip' + (m.status === 'LIVE' ? ' active' : '') + '" type="button" data-modal="form" data-intent="live">Идёт</button>' : '') +
          '<button class="chip' + (m.status === 'POSTPONED' ? ' active' : '') + '" type="button" data-modal="form" data-intent="postponed">Перенесён</button>' +
          '<button class="chip" type="button" data-modal="form" data-intent="auto">Авто (ожидается / запланирован)</button></div>' +
          '<div class="row" style="gap:8px;margin-top:10px"><select class="select" data-field="bo" style="max-width:110px">' + arr(m.best_of_options || [1, 3, 5]).map((b) => '<option value="' + b + '"' + (b === m.best_of ? ' selected' : '') + '>BO' + b + '</option>').join('') + '</select>' +
          '<button class="btn small" type="button" data-modal="form" data-intent="bo">Сменить формат серии</button></div></div>';
      }
      const log = arr(m.log);
      if (log.length) {
        html += '<div class="mm-sec"><div class="mm-t">История изменений</div>' + log.slice(0, 8).map((r) =>
          '<div class="small muted">' + esc(r.at) + ' — ' + esc(LOG_ACTION[r.action] || r.action) +
          (r.new && r.new.score1 !== null && r.new.score1 !== undefined ? ' ' + esc(r.new.score1) + ':' + esc(r.new.score2) : '') + '</div>').join('') + '</div>';
      }
    }
    return html + '<div class="actions"><button class="btn ghost" type="button" data-modal="cancel">Закрыть</button></div>';
  }
  // 5.13: карты серии по данным OpenDota — победитель, длительность, герои каждой стороны.
  function dotaGamesHtml(d, m) {
    if (!d || !arr(d.games).length) return '';
    const n1 = m && m.team1 ? m.team1.name : 'Команда 1';
    const n2 = m && m.team2 ? m.team2.name : 'Команда 2';
    return '<div class="' + (d.verified ? 'okbox' : 'warnbox') + '" style="margin-top:8px">' +
      (d.verified ? ic('check') + ' Команды опознаны по Steam — данные OpenDota' : 'Данные OpenDota — проверьте вручную') +
      arr(d.games).map((g) => '<div style="margin-top:8px"><b>Карта ' + esc(g.no) + '</b> · ' + esc(g.duration) + (g.start ? ' · ' + esc(g.start) : '') +
        ' · победа: <b>' + esc(Number(g.winner_team_id) === Number(m && m.team1 ? m.team1.id : 0) ? n1 : n2) + '</b>' +
        '<div class="small muted">' + esc(n1) + ': ' + esc(arr(g.heroes1).join(', ') || '—') + '</div>' +
        '<div class="small muted">' + esc(n2) + ': ' + esc(arr(g.heroes2).join(', ') || '—') + '</div>' +
        '<button class="link small" type="button" data-act="ext" data-url="https://www.opendota.com/matches/' + esc(g.dota_match_id) + '">матч ' + esc(g.dota_match_id) + '</button></div>').join('') +
      arr(d.warnings).map((w) => '<div class="small" style="margin-top:6px">' + esc(w) + '</div>').join('') + '</div>';
  }
  async function runMatchIntent(m, v, extra) {
    const base = '/api/v2/admin/matches/' + m.id;
    let path = base + '/schedule'; let body = {};
    const intent = v.__intent;
    if (intent === 'schedule' || intent === 'postpone' || intent === 'unschedule') {
      if (intent === 'schedule' && !v.when) { toast('Выберите дату и время', 'bad'); return; }
      body = { when: intent === 'unschedule' ? '' : (v.when || ''), postpone: intent === 'postpone' };
    } else if (intent === 'result') {
      path = base + '/result';
      body = v.forfeit ? { forfeit_winner: Number(v.forfeit), version: m.version } : { score1: Number(v.score1), score2: Number(v.score2), version: m.version };
    } else if (intent === 'reset') {
      if (!(await confirmDialog({ title: 'Сбросить результат?', text: 'Команды в следующих матчах будут пересчитаны.', ok: 'Сбросить', danger: true }))) return;
      path = base + '/reset';
    } else if (intent === 'live' || intent === 'postponed' || intent === 'auto') {
      path = base + '/status';
      body = { status: intent === 'live' ? 'LIVE' : intent === 'postponed' ? 'POSTPONED' : 'WAITING' };
    } else if (intent === 'chat') {
      go('matchChat', { id: m.id });
      return;
    } else if (intent === 'predict1' || intent === 'predict2') {
      const team = intent === 'predict1' ? m.team1 : m.team2;
      try { await api('/api/v2/matches/' + m.id + '/predict', { method: 'POST', body: { team_id: team.id } }); toast('Прогноз принят: ' + team.name, 'ok'); } catch (err) { toast(err.message, 'bad'); }
      return;
    } else if (intent === 'report') {
      const fd = new FormData();
      if (v.rdota && v.rdota.trim()) { fd.append('dota_ids', v.rdota.trim()); toast('Запрашиваю матчи в OpenDota…'); } else { fd.append('score1', v.rs1); fd.append('score2', v.rs2); }
      if (v.rshot) fd.append('screenshot', v.rshot);
      try {
        const r = await api('/api/v2/matches/' + m.id + '/report', { method: 'POST', body: fd, timeout: 90000 });
        toast(r.verified ? 'Счёт ' + r.score1 + ':' + r.score2 + ' подтверждён OpenDota — ждёт одобрения организатора' : 'Результат отправлен сопернику и организаторам', 'ok');
        reload();
      } catch (err) { toast(err.message, 'bad'); }
      return;
    } else if (intent === 'report-confirm' || intent === 'report-dispute') {
      path = '/api/v2/match-reports/' + m.report.id + '/respond'; body = { confirm: intent === 'report-confirm', comment: v.rcomment || null };
    } else if (intent === 'report-approve' || intent === 'report-reject') {
      path = '/api/v2/admin/match-reports/' + m.report.id; body = { approve: intent === 'report-approve' };
    } else if (intent === 'teams') {
      path = base + '/teams'; body = { team1_id: Number(v.team1) || null, team2_id: Number(v.team2) || null };
    } else if (intent === 'delete') {
      if (!(await confirmDialog({ title: 'Удалить матч?', text: 'Матч исчезнет из таблицы и расписания.', ok: 'Удалить', danger: true }))) return;
      try { await api(base, { method: 'DELETE' }); toast('Матч удалён', 'ok'); reload(); } catch (err) { toast(err.message, 'bad'); }
      return;
    } else if (intent === 'bo') {
      path = base + '/best-of'; body = { best_of: Number(v.bo) };
    } else return;
    Object.assign(body, extra || {});
    try {
      await api(path, { method: 'POST', body });
      toast('Сохранено', 'ok');
      reload();
    } catch (err) {
      if (err && err.status === 409 && err.data && err.data.need_confirm) {
        const lines = arr(err.data.warnings).map((w) => '• ' + w)
          .concat(arr(err.data.affected).map((a) => '• Матч ' + a.no + ' (' + a.round_name + ') — ' + a.status_text));
        const cascade = intent === 'result' || intent === 'reset' || intent === 'report-approve';
        const ok = await confirmDialog({ title: err.message, text: lines.join('\n'), ok: cascade ? 'Пересчитать' : 'Всё равно сохранить', danger: cascade });
        if (ok) return runMatchIntent(m, v, cascade ? { cascade: true } : { force: true });
        return;
      }
      toast((err && err.message) || 'Не удалось сохранить', 'bad');
    }
  }
  // Скриншот требует авторизации — грузим через fetch с заголовком Telegram и показываем blob.
  async function loadShot(url, id) {
    try {
      const res = await fetch(url, { headers: { 'X-Telegram-Init-Data': INIT_DATA } });
      if (!res.ok) return;
      const img = document.getElementById(id);
      if (img) img.src = URL.createObjectURL(await res.blob());
    } catch (e) { /* скриншот не критичен */ }
  }
  ACTIONS['match-open'] = async (el) => {
    let m;
    try { m = await api('/api/v2/matches/' + encodeURIComponent(el.dataset.id)); } catch (err) { toast(err.message, 'bad'); return; }
    const pending = openModal(matchModalHtml(m));
    if (m.report && m.report.screenshot_url) loadShot(m.report.screenshot_url, 'rsImg');
    const v = await pending;
    if (v && typeof v === 'object' && v.__intent) await runMatchIntent(m, v);
  };

  function bracketHtml(bracket, avatars, myTeam) {
    if (bracket && bracket.engine) return engineHtml(bracket.engine, avatars, myTeam);
    if (!bracket || !bracket.exists || !bracket.bracket) {
      return '<div class="muted small">' + esc((bracket && bracket.error) || 'Сетка ещё не опубликована. Её формирует организатор после одобрения команд.') + '</div>';
    }
    const teamRow = (team, seed, placeholder) => {
      if (!team) return '<div class="bt empty"><span class="seed">—</span><span class="nm">' + esc(placeholder) + '</span></div>';
      return '<div class="bt"><span class="seed">#' + esc(team.seed || seed || '') + '</span>' + avatarHtml(avatars[team.id] || null, team.name) +
        '<span class="nm">' + esc(team.name) + '</span>' + (num(team.avg_mmr) !== null ? '<span class="small muted">' + esc(team.avg_mmr) + '</span>' : '') + '</div>';
    };
    return '<div class="bracket"><div class="bracket-board">' + arr(bracket.bracket.rounds).map((r) =>
      '<div class="bracket-round"><div class="rt">' + esc(r.name) + '</div>' + arr(r.matches).map((m) => {
        const bye = m.status === 'BYE';
        return '<div class="bm"><div class="bh"><span>' + esc(m.label || m.id) + '</span><span>' + (bye ? 'BYE' : m.status === 'FINISHED' ? 'завершён' : 'ожидание') + '</span></div>' +
          teamRow(m.team_a, m.seed_a, bye ? 'автопроход' : 'ожидается') + teamRow(m.team_b, m.seed_b, bye ? 'автопроход' : 'ожидается') + '</div>';
      }).join('') + '</div>').join('') + '</div></div>';
  }

  VIEWS.tournament = async (ctx) => {
    const t = await ctx.api('/api/v2/tournaments/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    const teams = arr(t.teams);
    const avatars = {};
    teams.forEach((x) => { avatars[x.id] = x.avatar_url; });
    let html = backBtn() + '<div class="card"><div class="eyebrow">Турнир #' + esc(t.id) + '</div><div class="row between" style="align-items:flex-start"><h1>' + esc(t.name) + '</h1>' + tPill(t.status, t.registration_open) + '</div>' +
      (t.description ? '<p class="desc">' + esc(t.description) + '</p>' : '') +
      '<div class="grid2" style="margin-top:12px"><div class="stat"><span>Старт</span><b style="font-size:16px">' + esc(fmtDate(t.start_date)) + '</b><small>по московскому времени</small></div>' +
      '<div class="stat"><span>Команды</span><b>' + esc(t.approved_count) + ' / ' + esc(t.max_teams) + '</b><small>' + (t.registration_open ? 'свободно мест: ' + esc(t.slots_left) : 'регистрация закрыта') + '</small></div></div></div>';
    const canWithdraw = t.my_team && t.my_team.is_captain && ['DRAFT', 'PENDING', 'APPROVED'].includes(t.my_application) && !['FINISHED', 'CANCELLED'].includes(t.status);
    if (t.my_application && !t.can_apply) {
      html += '<div class="' + (t.my_application === 'APPROVED' ? 'okbox' : t.my_application === 'REJECTED' ? 'warnbox' : 'okbox') + '">Заявка вашей команды: <b>' + esc(A_STATUS[t.my_application] || t.my_application) + '</b>' +
        (canWithdraw ? '<div class="actions" style="margin-top:8px"><button class="btn small danger" type="button" data-act="tour-withdraw" data-id="' + esc(t.id) + '" data-name="' + esc(t.name) + '" data-started="' + (t.status === 'ACTIVE' ? '1' : '') + '">Сняться с турнира</button></div>' : '') + '</div>';
    } else if (t.can_apply) {
      html += '<button class="btn primary block" type="button" data-act="tour-apply" data-id="' + esc(t.id) + '" data-name="' + esc(t.name) + '" style="margin-bottom:12px">Подать заявку от команды «' + esc(t.my_team ? t.my_team.name : '') + '»</button>';
    } else if (t.apply_block_reason && (t.status === 'REGISTRATION')) {
      html += '<div class="warnbox">' + esc(t.apply_block_reason) + '</div>';
    }
    const eng = t.bracket && t.bracket.engine;
    const myTeam = t.my_team ? t.my_team.id : null;
    const sec = ctx.params.sec || state.ui.tourSec || 'bracket';
    html += '<div class="chips seg" style="margin:4px 0 12px">' + [['bracket', 'Сетка'], ['schedule', 'Расписание'], ['teams', 'Команды'], ['predict', 'Прогнозы'], ['stats', 'Статистика'], ['mvp', 'MVP']].map(([k, n]) =>
      '<button class="chip' + (sec === k ? ' active' : '') + '" type="button" data-act="tour-sec" data-sec="' + k + '">' + n + '</button>').join('') + '</div>';
    if (sec === 'bracket') {
      html += '<div class="card"><div class="card-head"><h3>' + (eng ? esc(eng.format === 'double_elim' ? 'Сетка: верхняя и нижняя' : 'Сетка плей-офф') : 'Сетка плей-офф') + '</h3>' +
        (t.bracket && t.bracket.locked ? pill('зафиксирована', 'ok') : t.bracket && t.bracket.exists ? pill('предварительная', 'warn') : '') + '</div>' +
        (eng && eng.description ? '<div class="small muted" style="margin:-4px 0 10px">' + esc(eng.description) + '</div>' : '') +
        (t.bracket && t.bracket.stale ? '<div class="warnbox">Состав одобренных команд изменился после генерации — организатор обновит сетку.</div>' : '') + bracketHtml(t.bracket, avatars, myTeam) + '</div>';
    }
    if (sec === 'schedule') {
      const filter = state.ui.schedFilter || 'all';
      let sched = null; let schedErr = null;
      try { sched = await ctx.api('/api/v2/tournaments/' + encodeURIComponent(t.id) + '/schedule'); } catch (e) { schedErr = e; }
      if (!ctx.alive()) return;
      html += '<div class="card"><div class="card-head"><h3>Расписание матчей</h3>' + pill(TZ_LABEL) + '</div>' +
        '<div class="chips" style="margin-bottom:10px">' + [['all', 'Все'], ['upcoming', 'Предстоящие'], ['mine', 'Мои матчи'], ['today', 'Сегодня']].map(([k, n]) =>
          '<button class="chip' + (filter === k ? ' active' : '') + '" type="button" data-act="sched-filter" data-f="' + k + '">' + n + '</button>').join('') + '</div>' +
        (schedErr ? '<div class="inline-error">' + esc(schedErr.message) + '</div>' : sched && sched.exists ? scheduleHtml(sched, filter, avatars, myTeam)
          : '<div class="muted small">Расписание появится, когда организатор сформирует сетку.</div>') + '</div>';
    }
    if (sec === 'predict') {
      let pd = null; try { pd = await ctx.api('/api/v2/tournaments/' + encodeURIComponent(t.id) + '/predictions'); } catch (e) { pd = null; }
      if (!ctx.alive()) return;
      html += '<div class="card"><div class="card-head"><h3>' + ic('bolt') + ' Лучшие предсказатели</h3>' + (pd && pd.pending ? pill('ждут исхода: ' + pd.pending) : '') + '</div>' +
        '<div class="note" style="margin-top:0">Откройте любой матч в сетке и выберите, кто победит. Прогноз можно менять до начала матча; игроки за свой матч не голосуют.</div>' +
        (pd && pd.me ? '<div class="okbox">Вы на ' + esc(pd.me.place) + ' месте: угадано ' + esc(pd.me.correct) + ' из ' + esc(pd.me.total) + '</div>' : '') +
        (pd && arr(pd.items).length ? '<div class="list">' + arr(pd.items).map((x) => '<div class="item"><b style="width:28px">' + esc(x.place) + '</b><div class="grow"><div class="title">' + esc(x.name) + '</div>' +
          '<div class="sub">угадано ' + esc(x.correct) + ' из ' + esc(x.total) + ' · ' + esc(x.accuracy) + '%</div></div></div>').join('') + '</div>'
          : '<div class="muted small">Пока никто не угадал исход сыгранного матча.</div>') + '</div>';
    }
    if (sec === 'stats') {
      let st = null; try { st = await ctx.api('/api/v2/tournaments/' + encodeURIComponent(t.id) + '/stats'); } catch (e) { st = null; }
      if (!ctx.alive()) return;
      html += st ? '<div class="card"><div class="card-head"><h3>Статистика турнира</h3></div><div class="grid3"><div class="stat"><span>Серий сыграно</span><b>' + esc(st.series_played) + '/' + esc(st.series_total) + '</b></div>' +
        '<div class="stat"><span>Карт сыграно</span><b>' + esc(st.maps_played) + '</b></div><div class="stat"><span>Тех. результаты</span><b>' + esc(st.forfeits) + '</b></div></div>' +
        (arr(st.best_teams).length ? '<div class="mm-t" style="margin-top:12px">Лучший винрейт серий</div>' + arr(st.best_teams).map((x) => '<div class="kv"><span>' + esc(x.team) + '</span><span>' + esc(x.series_w) + '–' + esc(x.series_l) + ' · ' + esc(x.series_winrate) + '%</span></div>').join('') : '') +
        (arr(st.most_maps).length ? '<div class="mm-t" style="margin-top:12px">Больше всего выигранных карт</div>' + arr(st.most_maps).map((x) => '<div class="kv"><span>' + esc(x.team) + '</span><span>' + esc(x.maps_w) + ':' + esc(x.maps_l) + '</span></div>').join('') : '') +
        (arr(st.longest).length ? '<div class="mm-t" style="margin-top:12px">Самые долгие серии</div>' + arr(st.longest).map((x) => '<div class="kv"><span>Матч ' + esc(x.match_no) + ' · ' + esc(x.round) + '</span><span>' + esc(x.text) + '</span></div>').join('') : '') +
        (arr(st.upsets).length ? '<div class="mm-t" style="margin-top:12px">Сенсации (победил посев ниже)</div>' + arr(st.upsets).map((x) => '<div class="kv"><span>#' + esc(x.winner_seed) + ' обыграл #' + esc(x.loser_seed) + '</span><span>' + esc(x.text) + '</span></div>').join('') : '') +
        (!st.series_played ? '<div class="muted small" style="margin-top:10px">Статистика появится после первых результатов.</div>' : '') + '</div>'
        : '<div class="inline-error">Статистика временно недоступна.</div>';
    }
    if (sec === 'mvp') {
      let mv = null; try { mv = await ctx.api('/api/v2/tournaments/' + encodeURIComponent(t.id) + '/mvp'); } catch (e) { mv = null; }
      if (!ctx.alive()) return;
      html += '<div class="card"><div class="card-head"><h3>' + ic('trophy') + ' MVP турнира</h3>' + (mv ? pill('голосов ' + mv.total_votes) : '') + '</div>';
      if (!mv) html += '<div class="inline-error">Голосование временно недоступно.</div>';
      else {
        html += '<div class="note" style="margin-top:0">' + (mv.open ? (mv.can_vote ? 'Голосуют игроки команд-участниц, один голос, за себя нельзя. Голос можно поменять.' : 'Голосуют только игроки команд-участниц.') : 'Голосование откроется после финала.') + '</div>';
        html += '<div class="list" style="margin-top:8px">' + arr(mv.candidates).slice(0, 60).map((c) => '<div class="item"><div class="grow"><div class="title">' + esc(c.name) + '</div><div class="sub">' + esc(c.team) + ' · голосов: ' + esc(c.votes) + '</div></div>' +
          (mv.can_vote ? (mv.my_vote === c.user_id ? pill('ваш голос', 'ok') : '<button class="btn small" type="button" data-act="mvp-vote" data-t="' + esc(t.id) + '" data-user="' + esc(c.user_id) + '">Голосовать</button>') : '') + '</div>').join('') + '</div>';
      }
      html += '</div>';
      const au = mv && mv.audience;
      if (au) {
        html += '<div class="card"><div class="card-head"><h3>' + ic('users') + ' Приз зрительских симпатий</h3>' + pill('голосов ' + au.total_votes) + '</div>' +
          '<div class="note" style="margin-top:0">' + esc(au.reason || 'Голосуют все — зрители и игроки: один голос с аккаунта, за себя нельзя. Голос можно поменять.') + '</div>' +
          '<div class="list" style="margin-top:8px">' + arr(au.candidates).slice(0, 60).map((c) => '<div class="item"><div class="grow"><div class="title">' + esc(c.name) + '</div><div class="sub">' + esc(c.team) + ' · голосов: ' + esc(c.votes) + '</div></div>' +
            (au.can_vote ? (au.my_vote === c.user_id ? pill('ваш голос', 'ok') : '<button class="btn small" type="button" data-act="aud-vote" data-t="' + esc(t.id) + '" data-user="' + esc(c.user_id) + '">Голосовать</button>') : '') + '</div>').join('') + '</div></div>';
      }
    }
    if (sec !== 'teams') { ctx.set(html); return; }
    html += '<div class="card"><div class="card-head"><h3>Допущенные команды</h3>' + pill(String(teams.length)) + '</div>' +
      (teams.length ? '<div class="list">' + teams.map((x) => '<button class="item" type="button" ' + openAttr('teamCard', { id: x.id }) + '>' + avatarHtml(x.avatar_url, x.name) +
        '<div class="grow"><div class="title">' + esc(x.name) + '</div><div class="sub">' + esc(x.members) + ' игроков · ' + (num(x.avg_mmr) !== null ? 'ср. ' + esc(x.avg_mmr) + ' MMR (' + esc(x.mmr_count) + ' подтв.)' : 'MMR нет данных') + '</div></div><span class="chev">›</span></button>').join('') + '</div>'
        : '<div class="muted small">Пока нет одобренных команд.</div>') + '</div>';
    ctx.set(html);
  };
  ACTIONS['aud-vote'] = async (el) => {
    await mutate(el, () => api('/api/v2/tournaments/' + el.dataset.t + '/audience-vote', { method: 'POST', body: { candidate_user_id: Number(el.dataset.user) } }), 'Голос зрителей учтён');
  };
  ACTIONS['mvp-vote'] = async (el) => {
    await mutate(el, () => api('/api/v2/tournaments/' + el.dataset.t + '/mvp', { method: 'POST', body: { candidate_user_id: Number(el.dataset.user) } }), 'Голос учтён');
  };
  VIEWS.hall = async (ctx) => {
    const data = await ctx.api('/api/v2/hall-of-fame');
    if (!ctx.alive()) return;
    const medal = { 1: 'gold', 2: 'silver', 3: 'bronze' };
    const items = arr(data.items);
    ctx.set(backBtn() + '<div class="page-head"><div class="eyebrow">История</div><h1>Зал славы</h1><p>Победители и призёры завершённых турниров TI:IT TOP.</p></div>' +
      (items.length ? items.map((t) => '<div class="card hall"><div class="card-head"><h3>' + ic('trophy') + ' ' + esc(t.name) + '</h3>' + (t.date ? pill(t.date.split(' ')[0]) : '') + '</div>' +
        arr(t.places).map((p) => '<div class="place ' + (medal[p.place] || '') + '"><span class="pn">' + esc(p.place) + '</span><span class="grow">' + esc(p.team) + '</span></div>').join('') +
        (t.mvp ? '<div class="note">MVP: <b>' + esc(t.mvp.name) + '</b> · голосов ' + esc(t.mvp.votes) + '</div>' : '') +
        (t.audience ? '<div class="note">Приз зрительских симпатий: <b>' + esc(t.audience.name) + '</b> · голосов ' + esc(t.audience.votes) + '</div>' : '') + '</div>').join('')
        : stateBox('Пока пусто', 'Здесь появятся победители после первого завершённого турнира.')));
  };
  ACTIONS['tour-sec'] = (el) => { state.ui.tourSec = el.dataset.sec; setParam('sec', el.dataset.sec); };
  ACTIONS['sched-filter'] = (el) => { state.ui.schedFilter = el.dataset.f; reload(); };
  ACTIONS['tour-apply'] = async (el) => {
    if (!(await confirmDialog({ title: 'Подать заявку?', text: 'Команда будет заявлена на «' + el.dataset.name + '». Заявку рассмотрит организатор.', ok: 'Подать заявку' }))) return;
    await mutate(el, () => api('/api/v2/tournaments/' + el.dataset.id + '/apply', { method: 'POST' }), 'Заявка отправлена организаторам');
  };
  ACTIONS['tour-withdraw'] = async (el) => {
    const text = el.dataset.started
      ? 'Турнир уже идёт: все несыгранные матчи вашей команды будут засчитаны соперникам как технические поражения. Отменить это нельзя.'
      : 'Команда уйдёт из сетки, её место займут другие. Пока регистрация открыта, заявку можно подать снова.';
    if (!(await confirmDialog({ title: 'Сняться с «' + el.dataset.name + '»?', text, ok: 'Сняться', danger: true }))) return;
    await mutate(el, () => api('/api/v2/tournaments/' + el.dataset.id + '/withdraw', { method: 'POST', body: { confirm: true } }), (r) => r.message || 'Команда снята с турнира');
  };

  // =========================================================================
  // Лента: чат, события, уведомления
  // =========================================================================
  const chatState = { lastId: 0, firstId: 0, busy: false };
  const EVENT_ICON = { team_created: ic('crown'), team_invite: ic('mail'), invite_accepted: ic('handshake'), invite_declined: ic('close'), application_submitted: ic('clipboard'), application_decision: ic('clipboard'),
    mmr_decision: ic('search'), support_reply: ic('chat'), tournament_created: ic('trophy'), tournament_status: ic('trophy'), tournament_bracket: ic('folder'), team_deleted: ic('trash'), team_member_removed: ic('door'),
    team_member_left: ic('door'), steam_linked: ic('gamepad'), steam_rebind: ic('gamepad'), team_renamed: ic('edit'), match_result: ic('flag'), match_scheduled: ic('calendar'), match_postponed: ic('alarm'), match_reminder: ic('alarm') };

  VIEWS.feed = async (ctx) => {
    const mode = state.ui.feedMode;
    const unread = state.me && state.me.counts ? Number(state.me.counts.unread_notifications || 0) : 0;
    const head = '<div class="page-head"><div class="eyebrow">Сообщество</div><h1>Лента</h1></div><div class="seg">' +
      '<button type="button" data-act="feed-mode" data-mode="chat" class="' + (mode === 'chat' ? 'active' : '') + '">' + ic('chat') + ' Чат</button>' +
      '<button type="button" data-act="feed-mode" data-mode="events" class="' + (mode === 'events' ? 'active' : '') + '">' + ic('news') + ' События</button>' +
      '<button type="button" data-act="feed-mode" data-mode="notifications" class="' + (mode === 'notifications' ? 'active' : '') + '">' + ic('bell') + ' Уведомления' + (unread ? ' (' + unread + ')' : '') + '</button></div>';
    if (mode === 'events') return feedEvents(ctx, head);
    if (mode === 'notifications') return feedNotifications(ctx, head);
    return feedChat(ctx, head);
  };
  ACTIONS['feed-mode'] = (el) => { state.ui.feedMode = el.dataset.mode; reload(); };

  function chatMsgHtml(m) {
    const canDelete = state.me && state.me.is_admin;
    return '<div class="msg' + (m.mine ? ' mine' : '') + '" data-msg="' + esc(m.id) + '"><div class="mh">' + (m.mine ? '' : personAvatar(m.avatar_url, m.name, 'xs')) + '<b>' + esc(m.name) + '</b><span>' + esc(fmtRel(m.created_at)) + '</span>' +
      (canDelete ? '<button class="link" type="button" data-act="chat-delete" data-id="' + esc(m.id) + '" style="margin-left:auto;font-size:12px">удалить</button>' : '') +
      '</div><div class="mt">' + esc(m.text) + '</div></div>';
  }
  function chatNearBottom(box) { return box.scrollHeight - box.scrollTop - box.clientHeight < 90; }

  async function feedChat(ctx, head) {
    const data = await ctx.api('/api/v2/chat?limit=40');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    chatState.lastId = items.length ? items[items.length - 1].id : 0;
    chatState.firstId = items.length ? items[0].id : 0;
    ctx.set(head + '<div class="card"><div id="chatBox" class="chat">' +
      (data.has_more ? '<div class="more" style="margin:0 0 4px"><button class="btn small ghost" type="button" data-act="chat-older">Загрузить ранее</button></div>' : '') +
      (items.length ? items.map(chatMsgHtml).join('') : '<div class="muted small" id="chatEmpty">Сообщений пока нет — напишите первым.</div>') + '</div>' +
      '<div class="composer"><textarea class="textarea" id="chatInput" maxlength="1000" placeholder="Сообщение…" data-enter="chat-send"></textarea>' +
      '<button class="btn primary" type="button" data-act="chat-send">' + ic('send') + '</button></div><div class="note">Не публикуйте личные данные. Администраторы могут удалять сообщения.</div></div>');
    const box = document.getElementById('chatBox');
    if (box) box.scrollTop = box.scrollHeight;
    // Один опрос на всё приложение; очищается при уходе из раздела (clearTimers).
    addTimer(() => { if (document.visibilityState === 'visible') pollChat(); }, 7000);
  }

  async function pollChat() {
    if (chatState.busy) return;
    const box = document.getElementById('chatBox');
    if (!box) return;
    chatState.busy = true;
    try {
      const data = await api('/api/v2/chat?after_id=' + chatState.lastId + '&limit=100', { timeout: 10000 });
      const stick = chatNearBottom(box);
      arr(data.deleted_ids).forEach((id) => { const el = box.querySelector('[data-msg="' + id + '"]'); if (el) el.remove(); });
      const fresh = arr(data.items).filter((m) => m.id > chatState.lastId && !box.querySelector('[data-msg="' + m.id + '"]'));
      if (fresh.length) {
        const empty = document.getElementById('chatEmpty'); if (empty) empty.remove();
        box.insertAdjacentHTML('beforeend', fresh.map(chatMsgHtml).join(''));
        chatState.lastId = fresh[fresh.length - 1].id;
        if (stick) box.scrollTop = box.scrollHeight;
      }
    } catch (err) { /* тихо: следующий опрос повторит */ }
    finally { chatState.busy = false; }
  }
  ACTIONS['chat-send'] = async (el) => {
    const input = document.getElementById('chatInput');
    const text = input ? input.value.trim() : '';
    if (!text) return;
    const btn = el.tagName === 'BUTTON' ? el : document.querySelector('[data-act="chat-send"]');
    await withBusy(btn, async () => {
      try {
        const r = await api('/api/v2/chat', { method: 'POST', body: { text } });
        input.value = '';
        const box = document.getElementById('chatBox');
        if (box && r.item && !box.querySelector('[data-msg="' + r.item.id + '"]')) {
          const empty = document.getElementById('chatEmpty'); if (empty) empty.remove();
          box.insertAdjacentHTML('beforeend', chatMsgHtml(r.item));
          box.scrollTop = box.scrollHeight;
        }
        haptic();
      } catch (err) { toast(err.message, 'bad'); }
    });
  };
  ACTIONS['chat-older'] = async (el) => {
    await withBusy(el, async () => {
      const box = document.getElementById('chatBox');
      try {
        const data = await api('/api/v2/chat?before_id=' + chatState.firstId + '&limit=40');
        const items = arr(data.items);
        const holder = el.parentElement;
        const prevHeight = box.scrollHeight;
        if (items.length) {
          chatState.firstId = items[0].id;
          holder.insertAdjacentHTML('afterend', items.map(chatMsgHtml).join(''));
        }
        if (!data.has_more) holder.remove();
        box.scrollTop = box.scrollHeight - prevHeight;
      } catch (err) { toast(err.message, 'bad'); }
    });
  };
  ACTIONS['chat-delete'] = async (el) => {
    if (!(await confirmDialog({ title: 'Удалить сообщение?', ok: 'Удалить', danger: true }))) return;
    try {
      await api('/api/v2/chat/' + el.dataset.id, { method: 'DELETE' });
      const m = document.querySelector('[data-msg="' + el.dataset.id + '"]'); if (m) m.remove();
      toast('Сообщение удалено', 'ok');
    } catch (err) { toast(err.message, 'bad'); }
  };

  function eventHtml(e, unread, act) {
    return '<div class="event' + (unread ? ' unread' : '') + '"' + (act || '') + '><div class="ei">' + (EVENT_ICON[e.kind] || ic('bolt')) + '</div><div class="grow">' +
      '<div class="et">' + esc(e.title) + '</div>' + (e.body ? '<div class="eb">' + esc(e.body) + '</div>' : '') + '<div class="ed">' + esc(fmtDate(e.created_at)) + '</div></div></div>';
  }

  async function feedEvents(ctx, head) {
    const data = await ctx.api('/api/v2/events?limit=30');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    ctx.set(head + (items.length ? '<div class="list" id="eventsList">' + items.map((e) => eventHtml(e)).join('') + '</div>' : stateBox('Событий пока нет', '')) +
      (data.has_more && items.length ? '<div class="more"><button class="btn" type="button" data-act="events-more" data-before="' + esc(items[items.length - 1].id) + '">Показать ещё</button></div>' : ''));
  }
  ACTIONS['events-more'] = async (el) => {
    await withBusy(el, async () => {
      try {
        const data = await api('/api/v2/events?limit=30&before_id=' + el.dataset.before);
        const items = arr(data.items);
        const list = document.getElementById('eventsList');
        if (list) list.insertAdjacentHTML('beforeend', items.map((e) => eventHtml(e)).join(''));
        if (data.has_more && items.length) el.dataset.before = items[items.length - 1].id; else el.parentElement.remove();
      } catch (err) { toast(err.message, 'bad'); }
    });
  };

  const NOTIFY_TARGET = { team_invite: 'team', invite_accepted: 'team', invite_declined: 'team', team_member_left: 'team', team_member_removed: 'team', team_deleted: 'team',
    mmr_decision: 'profile', application_decision: 'tournaments', support_reply: 'support', match_result: 'tournaments', match_scheduled: 'tournaments', match_postponed: 'tournaments', match_reminder: 'tournaments' };

  async function feedNotifications(ctx, head) {
    const data = await ctx.api('/api/v2/notifications?limit=30');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    if (state.me && state.me.counts) { state.me.counts.unread_notifications = Number(data.unread || 0); syncNav(); }
    ctx.set(head + (data.unread ? '<div class="actions" style="margin:0 0 10px"><button class="btn small" type="button" data-act="notif-read-all">Прочитать все (' + esc(data.unread) + ')</button></div>' : '') +
      (items.length ? '<div class="list" id="notifList">' + items.map((n) => eventHtml(n, !n.read, ' data-act="notif-open" data-id="' + esc(n.id) + '" data-kind="' + esc(n.kind) + '" data-read="' + (n.read ? '1' : '0') + '" role="button" tabindex="0"')).join('') + '</div>'
        : stateBox('Уведомлений нет', 'Здесь появятся приглашения, решения по MMR и заявкам, ответы поддержки.')) +
      (data.has_more && items.length ? '<div class="more"><button class="btn" type="button" data-act="notif-more" data-before="' + esc(items[items.length - 1].id) + '">Показать ещё</button></div>' : ''));
  }
  ACTIONS['notif-read-all'] = async (el) => {
    await mutate(el, () => api('/api/v2/notifications/read-all', { method: 'POST' }), 'Все уведомления прочитаны', { me: true });
  };
  ACTIONS['notif-open'] = async (el) => {
    if (el.dataset.read !== '1') {
      try { await api('/api/v2/notifications/' + el.dataset.id + '/read', { method: 'POST' }); } catch (err) { /* не критично */ }
      el.classList.remove('unread'); el.dataset.read = '1';
      if (state.me && state.me.counts) { state.me.counts.unread_notifications = Math.max(0, Number(state.me.counts.unread_notifications || 0) - 1); syncNav(); }
    }
    const target = NOTIFY_TARGET[el.dataset.kind];
    if (target === 'support') go('support');
    else if (target) switchTab(target);
  };
  ACTIONS['notif-more'] = async (el) => {
    await withBusy(el, async () => {
      try {
        const data = await api('/api/v2/notifications?limit=30&before_id=' + el.dataset.before);
        const items = arr(data.items);
        const list = document.getElementById('notifList');
        if (list) list.insertAdjacentHTML('beforeend', items.map((n) => eventHtml(n, !n.read, ' data-act="notif-open" data-id="' + esc(n.id) + '" data-kind="' + esc(n.kind) + '" data-read="' + (n.read ? '1' : '0') + '" role="button" tabindex="0"')).join(''));
        if (data.has_more && items.length) el.dataset.before = items[items.length - 1].id; else el.parentElement.remove();
      } catch (err) { toast(err.message, 'bad'); }
    });
  };

  // =========================================================================
  // Админка: каждый раздел — отдельный запрос, большие списки — с пагинацией
  // =========================================================================
  const ADMIN_SECTIONS = [
    ['aVerification', ic('search'), 'Проверка MMR', 'verification_pending'],
    ['aDigest', ic('news'), 'Сводка дня', null],
    ['aJournal', ic('clock'), 'Журнал действий', null],
    ['aRoster', ic('lock'), 'Замены в составах', null],
    ['aSuspicious', ic('shield'), 'Подозрительные', null],
    ['aBans', ic('ban'), 'Заблокированные', null],
    ['aReports', ic('flag'), 'Результаты капитанов', null],
    ['aApplications', ic('clipboard'), 'Заявки', 'applications_pending'],
    ['aSupport', ic('chat'), 'Поддержка', 'support_open'],
    ['aTournaments', ic('trophy'), 'Турниры', 'tournaments'],
    ['aTeams', ic('crown'), 'Команды', 'teams'],
    ['aUsers', ic('users'), 'Пользователи', 'users'],
    ['aBroadcast', ic('megaphone'), 'Рассылка', null],
    ['aMvp', ic('crown'), 'MVP на главной', null],
    ['aAdmins', ic('shield'), 'Админы', null],
    ['aAI', ic('bot'), 'AI-помощник', null],
    ['aDiagnostics', ic('globe'), 'Диагностика', null],
  ];
  const COUNT_LABELS = { users: 'Пользователи', registered: 'Зарегистрированы', teams: 'Команды', free_agents: 'Свободные игроки', tournaments: 'Турниры', tournaments_open: 'Открыта регистрация',
    applications_pending: 'Заявки ждут', applications_approved: 'Заявки одобрены', verification_pending: 'Ждут проверки', verified_players: 'Проверены', rejected_players: 'Отклонены',
    support_open: 'Открытые обращения', chat_messages: 'Сообщения чата', events: 'События' };

  VIEWS.admin = async (ctx) => {
    ctx.set('<div class="page-head"><div class="eyebrow">Control center</div><h1>Админ-панель</h1><p>Все действия дополнительно проверяются на сервере.</p></div>' +
      '<div class="tiles">' + ADMIN_SECTIONS.map((s) => '<button class="tile" type="button" ' + openAttr(s[0]) + '><span>' + s[1] + '</span><b>' + esc(s[2]) + '</b><small data-count="' + esc(s[3] || '') + '"></small></button>').join('') + '</div>' +
      '<div class="card"><div class="card-head"><h3>Статистика</h3></div><div id="adminStats">' + skeleton(2) + '</div></div>');
    try {
      const s = await ctx.api('/api/v2/admin/stats');
      if (!ctx.alive()) return;
      const c = s.counts || {};
      document.querySelectorAll('[data-count]').forEach((el) => {
        const k = el.dataset.count;
        if (k && c[k] !== undefined) el.textContent = (k === 'verification_pending' || k === 'applications_pending' || k === 'support_open') ? (c[k] ? 'ждут: ' + c[k] : 'пусто') : 'всего: ' + c[k];
      });
      const box = document.getElementById('adminStats');
      if (box) box.innerHTML = '<div class="grid3">' + Object.keys(COUNT_LABELS).filter((k) => c[k] !== undefined).map((k) =>
        '<div class="stat"><span>' + esc(COUNT_LABELS[k]) + '</span><b>' + esc(c[k]) + '</b></div>').join('') + '</div>' +
        '<div class="note">Уведомления в Telegram: ' + (s.telegram_notifications ? 'включены' : 'отключены на этом сервере') + '. Диапазон медали при вводе MMR: ' + (s.mmr_range_hard_block ? 'жёсткая проверка' : 'только подсказка') + '.</div>';
    } catch (err) {
      const box = document.getElementById('adminStats');
      if (box && ctx.alive() && err.status !== -1) box.innerHTML = errorBox('Статистика не загрузилась', err, 'reload');
    }
  };

  function pager(data, act) {
    if (!data || (!data.has_more && Number(data.page || 1) <= 1)) return '';
    const page = Number(data.page || 1);
    const pages = Math.max(1, Math.ceil(Number(data.total || 0) / Number(data.limit || 20)));
    return '<div class="row between" style="margin-top:10px"><button class="btn small" type="button" data-act="' + act + '" data-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>← Назад</button>' +
      '<span class="small muted">стр. ' + page + ' из ' + pages + '</span><button class="btn small" type="button" data-act="' + act + '" data-page="' + (page + 1) + '"' + (!data.has_more ? ' disabled' : '') + '>Вперёд →</button></div>';
  }
  function setParam(key, value) { const top = state.stack[state.stack.length - 1]; top.params = Object.assign({}, top.params, { [key]: value }); reload(); }

  // ---------- Проверка MMR ----------
  VIEWS.aVerification = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const data = await ctx.api('/api/v2/admin/verification?limit=20&page=' + page);
    if (!ctx.alive()) return;
    const items = arr(data.items);
    let html = backBtn() + '<div class="page-head"><h1>Проверка MMR</h1><p>Очередь игроков со статусом «на проверке»: ' + esc(data.total) + '. Сверьте MMR на аккаунте и примите решение.</p></div>';
    html += items.length ? items.map((p) => {
      const links = p.links || {};
      const st = p.stats || {};
      return '<div class="card"><div class="row">' + personAvatar(p.user.avatar_url, p.user.name) + medalHtml(p.medal, true) + '<div class="grow"><b class="ellipsis">' + esc(p.user.name) + '</b><div class="small muted">' + (p.user.username ? '@' + esc(p.user.username) + ' · ' : '') + 'TG ' + esc(p.user.telegram_id) + '</div></div>' +
        '<button class="btn small ghost" type="button" ' + openAttr('aUser', { id: p.user.user_id }) + '>Профиль</button></div>' +
        '<div class="kv"><span>Steam</span><span>' + esc(p.nickname || '—') + ' · ' + esc(p.steam_id64 || '—') + '</span></div>' +
        '<div class="kv"><span>Заявленный MMR</span><span>' + (num(p.mmr.declared) !== null ? '<b>' + esc(p.mmr.declared) + '</b>' : 'не указан') + '</span></div>' +
        '<div class="kv"><span>Медаль (профиль)</span><span>' + (p.medal ? esc(p.medal.name) : 'нет данных') + (p.medal_mmr_range ? ' · ' + esc(p.medal_mmr_range.mmr_min) + '–' + esc(p.medal_mmr_range.mmr_max) : '') + '</span></div>' +
        '<div class="kv"><span>Матчи / винрейт</span><span>' + (st.public ? (num(st.matches) !== null ? esc(st.matches) : '—') + ' / ' + (num(st.winrate) !== null ? esc(st.winrate) + '%' : '—') : 'скрыто') + '</span></div>' +
        '<div class="kv"><span>Источник</span><span>' + esc(p.source || '—') + ' · ' + esc(fmtDate(p.updated_at)) + '</span></div>' +
        '<div class="actions">' + ['steam', 'dotabuff', 'opendota', 'stratz'].filter((k) => links[k]).map((k) => '<a class="btn small ghost" href="' + esc(links[k]) + '" data-act="ext" data-url="' + esc(links[k]) + '">' + esc(k) + '</a>').join('') + '</div>' +
        '<div class="inline-form" style="margin-top:10px"><input class="input" id="vm' + esc(p.profile_id) + '" inputmode="numeric" maxlength="4" placeholder="MMR" value="' + esc(num(p.mmr.declared) !== null ? p.mmr.declared : '') + '">' +
        '<button class="btn ok" type="button" data-act="ver-approve" data-id="' + esc(p.profile_id) + '">' + ic('check') + ' Подтвердить</button></div>' +
        '<div class="actions"><button class="btn small danger" type="button" data-act="ver-reject" data-id="' + esc(p.profile_id) + '">' + ic('close') + ' Отклонить</button>' +
        '<button class="btn small" type="button" data-act="user-recheck" data-user="' + esc(p.user.user_id) + '">' + ic('refresh') + ' Перепроверить</button>' +
        '<button class="btn small" type="button" data-act="player-check" data-user="' + esc(p.user.user_id) + '">' + ic('bot') + ' Проверка на смурфа</button></div>' +
        '<details class="check-box"' + (p.check && p.check.risk && (p.check.risk.level === 'high' || p.check.risk.level === 'medium') ? ' open' : '') + '><summary>Источники медали и риск' +
        (p.check && p.check.risk ? ' · ' + (RISK[p.check.risk.level] || RISK.unknown)[0] : ' · не проверялся') + '</summary>' + medalCheckHtml(p.check, true) + '</details></div>';
    }).join('') : stateBox('Очередь пуста', 'Новых заявок на проверку нет.');
    html += pager(data, 'ver-page');
    ctx.set(html);
  };
  ACTIONS['ver-page'] = (el) => setParam('page', Number(el.dataset.page));

  // ---------- 5.13: сводка дня и замены в составах ----------
  VIEWS.aDigest = async (ctx) => {
    const d = await ctx.api('/api/v2/admin/digest');
    if (!ctx.alive()) return;
    // Сводка — HTML, собранный сервером из экранированных данных (тот же текст уходит в Telegram в 09:00 МСК).
    const safe = String(d.html || '').replace(/<(?!\/?b>)[^>]*>/g, '');
    ctx.set(backBtn() + '<div class="page-head"><h1>Сводка дня</h1><p>Каждый день в 09:00 по Москве она сама приходит админам в Telegram.</p></div>' +
      '<div class="card"><div style="white-space:pre-wrap;line-height:1.5">' + safe + '</div></div>' +
      '<button class="btn block" type="button" data-act="reload">' + ic('refresh') + ' Обновить</button>');
  };
  VIEWS.aJournal = async (ctx) => {
    const d = await ctx.api('/api/v2/admin/journal');
    if (!ctx.alive()) return;
    const items = arr(d.items);
    ctx.set(backBtn() + '<div class="page-head"><h1>Журнал действий</h1><p>Кто из организаторов, что и когда менял: результаты, пары, время, сетка, замены, блокировки.</p></div>' +
      (items.length ? '<div class="card"><div class="list">' + items.map((e) => '<div class="item"><div class="grow"><div class="title">' + esc(e.who) + ' — ' + esc(e.what) + '</div>' +
        '<div class="sub">' + esc(e.where) + ' · ' + esc(e.at) + '</div></div></div>').join('') + '</div></div>' : stateBox('Пока пусто', 'Здесь появятся действия организаторов.')));
  };
  VIEWS.aRoster = async (ctx) => {
    const d = await ctx.api('/api/v2/admin/roster-requests');
    if (!ctx.alive()) return;
    const items = arr(d.items);
    let html = backBtn() + '<div class="page-head"><h1>Замены в составах</h1><p>После закрытия регистрации состав меняется только с вашего согласия. Решить можно здесь или кнопкой в Telegram.</p></div>';
    html += items.length ? items.map((r) => '<div class="card"><div class="row between"><b>' + esc(r.team.name) + '</b>' + pill(esc(r.kind_text), r.kind === 'ADD' ? 'warn' : 'info') + '</div>' +
      '<div class="kv"><span>Игрок</span><span><button class="link" type="button" ' + openAttr('aUser', { id: r.target.user_id }) + '>' + esc(r.target.name) + '</button></span></div>' +
      '<div class="kv"><span>Кто просит</span><span>' + esc(r.requested_by) + '</span></div>' +
      '<div class="kv"><span>Турнир</span><span>' + esc(r.tournament || '—') + '</span></div>' +
      '<div class="kv"><span>Об игроке</span><span>' + esc(r.facts) + '</span></div>' +
      '<div class="actions"><button class="btn small ok" type="button" data-act="roster-decide" data-id="' + esc(r.id) + '" data-ok="1">' + ic('check') + ' Одобрить</button>' +
      '<button class="btn small danger" type="button" data-act="roster-decide" data-id="' + esc(r.id) + '">' + ic('close') + ' Отклонить</button></div></div>').join('')
      : stateBox('Запросов нет', 'Когда капитан или игрок попробует изменить замороженный состав, запрос появится здесь.');
    ctx.set(html);
  };
  ACTIONS['roster-decide'] = async (el) => {
    const approve = el.dataset.ok === '1';
    await mutate(el, () => api('/api/v2/admin/roster-requests/' + el.dataset.id, { method: 'POST', body: { approve } }), approve ? 'Изменение состава одобрено' : 'Запрос отклонён');
  };

  // ---------- Подозрительные игроки (смурфы/твинки) ----------
  VIEWS.aSuspicious = async (ctx) => {
    const level = state.ui.suspLevel || 'risky';
    const [data, dup] = await Promise.all([ctx.api('/api/v2/admin/players/suspicious?level=' + level), ctx.api('/api/v2/admin/duplicates').catch(() => null)]);
    if (!ctx.alive()) return;
    const c = data.counts || {}; const sw = data.sweep || {};
    let html = backBtn() + '<div class="page-head"><h1>Подозрительные игроки</h1><p>Последняя проверка каждого игрока: медаль по OpenDota, STRATZ и Dotabuff и риск смурфа или твинка. Это подсказка — решение за организатором.</p></div>';
    if (dup && dup.total) {
      html += '<div class="card"><div class="card-head"><h3>Дубли аккаунтов</h3>' + pill(String(dup.total), 'bad') + '</div>' +
        arr(dup.steam).map((g) => '<div class="warnbox" style="margin-bottom:8px"><b>Один Steam на ' + esc(arr(g.users).length) + ' аккаунта</b> · ' + esc(g.steam_id64) + '<div class="small" style="margin-top:4px">' +
          arr(g.users).map((u) => '<button class="link" type="button" ' + openAttr('aUser', { id: u.user_id }) + '>' + esc(u.name) + '</button>' + (u.team ? ' (' + esc(u.team) + ')' : '')).join(', ') + '</div></div>').join('') +
        arr(dup.multi_team).map((u) => '<div class="warnbox" style="margin-bottom:8px"><b>' + esc(u.name) + '</b> числится в командах: ' + esc(arr(u.teams).join(', ')) + '</div>').join('') +
        '<div class="note">Часто это смурф или твинк: один человек под двумя Telegram-аккаунтами. Проверьте и при необходимости заблокируйте.</div></div>';
    }
    html += '<div class="grid3"><div class="stat"><span>Высокий риск</span><b>' + esc(c.high || 0) + '</b></div><div class="stat"><span>Средний</span><b>' + esc(c.medium || 0) + '</b></div><div class="stat"><span>Не проверены</span><b>' + esc(c.unchecked || 0) + '</b></div></div>';
    html += '<div class="card" style="margin-top:12px">' + (sw.running
      ? '<div class="row between"><b>Идёт автопроверка</b>' + pill(esc(sw.done) + ' из ' + esc(sw.total), 'info') + '</div><div class="progress"><i style="width:' + (sw.total ? Math.round(sw.done * 100 / sw.total) : 0) + '%"></i></div><div class="note">Игроки проверяются по одному раз в ~12 секунд, чтобы не упереться в бесплатные лимиты. AI подключается только к подозрительным. Найдено: ' + esc(sw.found) + '.</div>'
      : '<div class="note" style="margin-top:0">Автопроверка идёт сама раз в сутки. Можно запустить сейчас — проверятся все, кого не проверяли больше недели' + (sw.finished_at ? ' (прошлый запуск: ' + esc(fmtDate(sw.finished_at)) + ', найдено ' + esc(sw.found) + ')' : '') + '.</div>' +
        '<button class="btn primary block" type="button" data-act="sweep-start">' + ic('refresh') + ' Проверить всех непроверенных (' + esc(c.unchecked || 0) + ')</button>') + '</div>';
    html += '<div class="chips" style="margin:12px 0">' + [['risky', 'Подозрительные'], ['high', 'Высокий риск'], ['all', 'Все проверенные (' + (c.checked || 0) + ')']].map(([k, n]) =>
      '<button class="chip' + (level === k ? ' active' : '') + '" type="button" data-act="susp-level" data-level="' + k + '">' + esc(n) + '</button>').join('') + '</div>';
    const items = arr(data.items);
    html += items.length ? items.map((it) => {
      const chk = it.check || {}; const r = chk.risk || {}; const md = chk.medal || {};
      const k = RISK[r.level] || RISK.unknown;
      return '<div class="card"><div class="row">' + personAvatar(it.user.avatar_url, it.user.name) + medalHtml(md.tier ? { tier: md.tier, name: md.name } : null, true) +
        '<div class="grow"><b class="ellipsis">' + esc(it.user.name) + '</b><div class="small muted">' + (it.user.username ? '@' + esc(it.user.username) + ' · ' : '') + esc(it.team || 'без команды') + ' · ' + esc(md.name || 'медаль ?') + '</div></div>' +
        pill(k[0] + (num(r.score) !== null ? ' · ' + r.score : ''), k[1]) + '</div>' +
        (arr(r.reasons).length ? '<ul class="reasons">' + arr(r.reasons).slice(0, 4).map((x) => '<li>' + esc(x.text) + '</li>').join('') + '</ul>' : '') +
        (chk.ai && chk.ai.summary ? '<div class="ai-box">' + esc(chk.ai.summary) + '</div>' : '') +
        '<div class="actions"><button class="btn small" type="button" ' + openAttr('aUser', { id: it.user.user_id }) + '>Профиль</button>' +
        '<button class="btn small" type="button" data-act="player-check" data-user="' + esc(it.user.user_id) + '">' + ic('bot') + ' Перепроверить с AI</button>' +
        '<button class="btn small danger" type="button" data-act="user-ban" data-user="' + esc(it.user.user_id) + '" data-name="' + esc(it.user.name) + '">Заблокировать</button></div>' +
        '<div class="note">Проверено ' + esc(fmtDate(chk.created_at)) + '</div></div>';
    }).join('') : stateBox(level === 'all' ? 'Проверок ещё нет' : 'Подозрительных нет', level === 'all' ? 'Запустите автопроверку выше.' : 'Среди проверенных игроков признаков смурфа или твинка не найдено.');
    ctx.set(html);
    if (sw.running) addTimer(() => { if (ctx.alive()) reload(); }, 15000);
  };
  ACTIONS['susp-level'] = (el) => { state.ui.suspLevel = el.dataset.level; reload(); };
  ACTIONS['sweep-start'] = async (el) => {
    await mutate(el, () => api('/api/v2/admin/players/check-all', { method: 'POST', body: { max_age_days: 7 } }),
      (r) => (r.sweep && r.sweep.total ? 'Автопроверка запущена: ' + r.sweep.total + ' игроков' : 'Все игроки уже проверены за последнюю неделю'));
  };
  ACTIONS['ver-approve'] = async (el) => {
    const input = document.getElementById('vm' + el.dataset.id);
    const raw = input ? input.value.trim() : '';
    if (!/^\d{1,4}$/.test(raw) || Number(raw) > 9500) { toast('Укажите MMR числом от 0 до 9500', 'bad'); return; }
    await mutate(el, () => api('/api/v2/admin/verification/' + el.dataset.id, { method: 'POST', body: { decision: 'approve', mmr: Number(raw) } }), 'MMR подтверждён, игрок уведомлён');
  };
  ACTIONS['ver-reject'] = async (el) => {
    const comment = await promptDialog({ title: 'Отклонить MMR?', text: 'Комментарий увидит игрок (необязательно).', placeholder: 'Причина отказа', multiline: true, ok: 'Отклонить', danger: true, max: 500 });
    if (comment === null || comment === undefined) return;
    await mutate(el, () => api('/api/v2/admin/verification/' + el.dataset.id, { method: 'POST', body: { decision: 'reject', comment: comment || null } }), 'MMR отклонён, игрок уведомлён');
  };
  ACTIONS['player-check'] = async (el) => {
    const noAi = el.dataset.noai === '1';
    toast(noAi ? 'Сверяю OpenDota, STRATZ, Dotabuff и Steam… до 30 секунд' : 'Сверяю источники и спрашиваю AI… до минуты');
    await mutate(el, () => api('/api/v2/admin/players/' + el.dataset.user + '/check', { method: 'POST', body: { use_ai: !noAi }, timeout: 90000 }),
      (r) => { const c = (r && r.check) || {}; const m = c.medal || {}; const k = c.risk ? (RISK[c.risk.level] || RISK.unknown)[0] : '—'; return 'Медаль: ' + (m.name || 'нет данных') + ' · риск: ' + k + (r && r.ai_left !== undefined ? ' · AI-проверок осталось: ' + r.ai_left : ''); });
  };
  ACTIONS['player-avatars-sync'] = async (el) => {
    toast('Загружаю аватарки из Steam и Telegram… до минуты');
    await mutate(el, () => api('/api/v2/admin/players/avatars/sync', { method: 'POST', body: {}, timeout: 90000 }),
      (r) => 'Аватарки: Steam ' + (r.steam || 0) + ', Telegram ' + (r.telegram || 0) + ', без фото ' + (r.without_photo || 0) + (r.skipped ? ', осталось ' + r.skipped + ' — нажмите ещё раз' : ''));
  };
  ACTIONS['user-recheck'] = async (el) => {
    toast('Перепроверяю через внешние сервисы… до 45 секунд');
    await mutate(el, () => api('/api/v2/admin/users/' + el.dataset.user + '/recheck', { method: 'POST', timeout: 60000 }), (r) => 'Проверка: ' + r.status + ' (' + (r.source || '—') + ')');
  };

  // ---------- Пользователи ----------
  VIEWS.aUsers = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const q = new URLSearchParams({ page: String(page), limit: '20', status: state.ui.usersStatus });
    if (state.ui.usersQ) q.set('q', state.ui.usersQ);
    const data = await ctx.api('/api/v2/admin/users?' + q);
    if (!ctx.alive()) return;
    const statuses = [['ALL', 'Все'], ['VERIFIED', 'Проверены'], ['PENDING', 'На проверке'], ['REJECTED', 'Отклонены'], ['UNLINKED', 'Без Steam']];
    let html = backBtn() + '<div class="page-head"><h1>Пользователи</h1><p>Всего по фильтру: ' + esc(data.total) + '</p></div>' +
      '<div class="card"><div class="inline-form"><input class="input" id="usersQ" maxlength="64" placeholder="Имя, @username, Telegram ID, Steam" value="' + esc(state.ui.usersQ) + '" data-enter="users-search">' +
      '<button class="btn primary" type="button" data-act="users-search">Найти</button></div><div class="chips" style="margin-top:10px">' +
      statuses.map((s) => '<button class="chip' + (state.ui.usersStatus === s[0] ? ' active' : '') + '" type="button" data-act="users-status" data-status="' + s[0] + '">' + esc(s[1]) + '</button>').join('') + '</div></div>';
    const items = arr(data.items);
    html += '<div class="row between wrap" style="margin:-4px 0 10px"><span class="small muted grow">Аватарки игроков берутся из Steam, иначе из Telegram, и хранятся на сервере.</span><button class="btn small" type="button" data-act="player-avatars-sync">' + ic('download') + ' Обновить аватарки</button></div>';
    html += items.length ? '<div class="list">' + items.map((u) => '<button class="item" type="button" ' + openAttr('aUser', { id: u.user_id }) + '>' + personAvatar(u.avatar_url, u.name) + '<div class="grow"><div class="title">' + esc(u.name) + '</div>' +
      '<div class="sub">' + (u.username ? '@' + esc(u.username) + ' · ' : '') + 'TG ' + esc(u.telegram_id) + ' · ' + (u.team ? esc(u.team.name) : 'без команды') + ' · ' + mmrInline(u.mmr) + '</div></div>' + vPill(u.status) + '</button>').join('') + '</div>'
      : stateBox('Никого не нашли', '');
    html += pager(data, 'users-page');
    ctx.set(html);
  };
  ACTIONS['user-ban'] = async (el) => {
    const reason = await promptDialog({ title: 'Заблокировать ' + el.dataset.name + '?', text: 'Пользователь не сможет пользоваться ботом и Mini App. Причину увидит он сам (необязательно).', placeholder: 'Причина', multiline: true, ok: 'Заблокировать', danger: true, max: 300 });
    if (reason === null || reason === undefined) return;
    await mutate(el, () => api('/api/v2/admin/users/' + el.dataset.user + '/ban', { method: 'POST', body: { reason: reason || null } }), 'Доступ заблокирован');
  };
  ACTIONS['user-unban'] = async (el) => {
    if (!(await confirmDialog({ title: 'Разблокировать?', text: 'Пользователь снова сможет пользоваться ботом и Mini App.', ok: 'Разблокировать' }))) return;
    await mutate(el, () => api('/api/v2/admin/users/' + el.dataset.user + '/ban', { method: 'DELETE' }), 'Доступ восстановлен');
  };
  VIEWS.aReports = async (ctx) => {
    const data = await ctx.api('/api/v2/admin/match-reports');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    const st = { PENDING: ['ждёт соперника', 'warn'], CONFIRMED: ['подтверждён', 'ok'], DISPUTED: ['оспорен', 'bad'] };
    ctx.set(backBtn() + '<div class="page-head"><h1>Результаты капитанов</h1><p>Капитан присылает счёт и скриншот, соперник подтверждает, вы одобряете — сетка обновится сама.</p></div>' +
      (items.length ? items.map((r) => '<div class="card"><div class="row between"><b>Матч ' + esc(r.match_no) + ' · ' + esc(r.round || '') + '</b>' + pill((st[r.status] || [r.status])[0], (st[r.status] || [])[1]) + '</div>' +
        '<div style="margin-top:6px">' + esc(r.team1) + ' <b>' + esc(r.score1) + ':' + esc(r.score2) + '</b> ' + esc(r.team2) + '</div><div class="note">Прислал капитан «' + esc(r.reporter) + '» · ' + esc(r.created_at) + (r.comment ? ' · ' + esc(r.comment) : '') + '</div>' +
        (r.screenshot_url ? '<img class="shot" data-shot="' + esc(r.screenshot_url) + '" alt="скриншот">' : '') +
        dotaGamesHtml(r.dota, { team1: { id: r.team1_id, name: r.team1 }, team2: { id: r.team2_id, name: r.team2 } }) +
        '<div class="actions"><button class="btn small ok" type="button" data-act="rep-decide" data-id="' + esc(r.id) + '" data-ok="1">Одобрить</button><button class="btn small danger" type="button" data-act="rep-decide" data-id="' + esc(r.id) + '" data-ok="0">Отклонить</button></div></div>').join('')
        : stateBox('Нет результатов на проверке', 'Когда капитаны пришлют счёт, он появится здесь.')));
    document.querySelectorAll('img[data-shot]').forEach((img, i) => { img.id = 'shot' + i; loadShot(img.dataset.shot, img.id); });
  };
  ACTIONS['rep-decide'] = async (el, e, extra) => {
    const approve = el.dataset.ok === '1';
    try {
      await api('/api/v2/admin/match-reports/' + el.dataset.id, { method: 'POST', body: Object.assign({ approve }, extra || {}) });
      toast(approve ? 'Результат внесён в сетку' : 'Результат отклонён', 'ok'); reload();
    } catch (err) {
      if (err && err.status === 409 && err.data && err.data.need_confirm) {
        const lines = arr(err.data.affected).map((a) => '• Матч ' + a.no + ' — ' + a.status_text);
        if (await confirmDialog({ title: err.message, text: lines.join('\n'), ok: 'Пересчитать', danger: true })) return ACTIONS['rep-decide'](el, e, { cascade: true });
        return;
      }
      toast(err.message, 'bad');
    }
  };
  VIEWS.aBans = async (ctx) => {
    const data = await ctx.api('/api/v2/admin/bans');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    ctx.set(backBtn() + '<div class="page-head"><h1>Заблокированные</h1><p>Эти пользователи не могут пользоваться ботом и Mini App.</p></div>' +
      (items.length ? '<div class="list">' + items.map((b) => '<div class="item">' + personAvatar(b.avatar_url, b.name) + '<div class="grow"><div class="title">' + esc(b.name) + '</div><div class="sub">' +
        (b.username ? '@' + esc(b.username) + ' · ' : '') + esc(fmtDate(b.created_at)) + (b.reason ? ' · ' + esc(b.reason) : '') + '</div></div>' +
        '<button class="btn small" type="button" data-act="user-unban" data-user="' + esc(b.user_id) + '">Разблокировать</button></div>').join('') + '</div>'
        : stateBox('Никто не заблокирован', 'Заблокировать можно в карточке пользователя.')));
  };
  ACTIONS['users-search'] = () => { const i = document.getElementById('usersQ'); state.ui.usersQ = i ? i.value.trim() : ''; setParam('page', 1); };
  ACTIONS['users-status'] = (el) => { state.ui.usersStatus = el.dataset.status; setParam('page', 1); };
  ACTIONS['users-page'] = (el) => setParam('page', Number(el.dataset.page));

  VIEWS.aUser = async (ctx) => {
    const u = await ctx.api('/api/v2/admin/users/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    const p = u.profile;
    let html = backBtn() + '<div class="card"><div class="row">' + personAvatar(u.avatar_url, u.name, 'lg') + '<div class="grow"><h2 class="ellipsis">' + esc(u.name) + '</h2>' +
      '<div class="small muted">' + (u.username ? '@' + esc(u.username) + ' · ' : '') + 'TG ' + esc(u.telegram_id) + ' · #' + esc(u.user_id) + '</div></div>' + (u.is_admin ? pill('админ', 'gold') : '') + '</div>' +
      '<div class="kv"><span>Регистрация в боте</span><span>' + (u.registered ? 'да' : 'нет') + ' · ' + esc(fmtDate(u.created_at)) + '</span></div>' +
      '<div class="kv"><span>Команда</span><span>' + (u.team ? '<button class="link" type="button" ' + openAttr('aTeam', { id: u.team.id }) + '>' + esc(u.team.name) + '</button>' + (u.team.is_captain ? ' ' + ic('crown') : '') : '—') + '</span></div>' +
      '<div class="kv"><span>Обращений</span><span>' + esc(u.support_tickets) + '</span></div></div>';
    if (p) {
      html += '<div class="card"><div class="card-head"><h3>Профиль игрока</h3>' + vPill(p.status) + '</div><div class="mmr">' + medalHtml(p.medal) + '<div class="grow"><div>' + mmrInline(p.mmr) + '</div>' +
        '<div class="small muted">Медаль: ' + (p.medal ? esc(p.medal.name) : 'нет данных') + '</div></div></div>' +
        '<div class="kv"><span>Steam</span><span>' + esc(p.nickname || '—') + ' · ' + esc(p.steam_id64 || '—') + '</span></div>' +
        '<div class="kv"><span>Account ID</span><span>' + esc(p.account_id !== null && p.account_id !== undefined ? p.account_id : '—') + '</span></div>' +
        '<div class="kv"><span>Позиции</span><span>' + esc(positionsText(p.positions)) + '</span></div>' +
        '<div class="kv"><span>Источник</span><span>' + esc(p.source || '—') + '</span></div>' +
        (p.admin_comment ? '<div class="kv"><span>Комментарий</span><span>' + esc(p.admin_comment) + '</span></div>' : '') +
        '<div class="actions"><button class="btn small" type="button" data-act="user-recheck" data-user="' + esc(u.user_id) + '">' + ic('refresh') + ' Перепроверить через API</button>' +
        (p.status === 'PENDING' ? '<button class="btn small primary" type="button" ' + openAttr('aVerification') + '>К очереди проверки</button>' : '') + '</div></div>';
      html += '<div class="card"><div class="card-head"><h3>Источники медали и риск смурфа</h3></div>' + medalCheckHtml(u.check, true) +
        '<div class="actions"><button class="btn small primary" type="button" data-act="player-check" data-user="' + esc(u.user_id) + '">' + ic('bot') + ' Проверить с AI</button>' +
        '<button class="btn small" type="button" data-act="player-check" data-noai="1" data-user="' + esc(u.user_id) + '">Проверить без AI</button></div></div>';
    } else {
      html += stateBox('Профиль игрока не создан', 'Пользователь ещё не привязывал Steam.');
    }
    const hist = arr(u.history);
    if (hist.length) {
      html += '<div class="card"><div class="card-head"><h3>История проверок</h3></div><div class="list">' + hist.map((r) => '<div class="event"><div class="ei">' + ic('search') + '</div><div class="grow"><div class="et">' + esc(V_STATUS[r.status] || r.status) + ' · ' + esc(r.source || '—') + '</div>' +
        (r.comment ? '<div class="eb">' + esc(r.comment) + '</div>' : '') + '<div class="ed">' + esc(fmtDate(r.created_at)) + (r.performed_by ? ' · админ ' + esc(r.performed_by) : '') + '</div></div></div>').join('') + '</div></div>';
    }
    if (u.ban) {
      html += '<div class="warnbox" style="margin-bottom:10px"><b>' + ic('ban') + ' Доступ заблокирован</b><div class="small">' + esc(fmtDate(u.ban.created_at)) + (u.ban.reason ? ' · ' + esc(u.ban.reason) : '') + '</div>' +
        '<div class="actions"><button class="btn small" type="button" data-act="user-unban" data-user="' + esc(u.user_id) + '">Разблокировать</button></div></div>';
    } else if (!u.is_admin) {
      html += '<button class="btn block danger" type="button" data-act="user-ban" data-user="' + esc(u.user_id) + '" data-name="' + esc(u.name) + '" style="margin-bottom:10px">' + ic('ban') + ' Заблокировать доступ к боту</button>';
    }
    if (!u.is_admin) html += '<button class="btn block" type="button" data-act="admin-add" data-tg="' + esc(u.telegram_id) + '" data-name="' + esc(u.name) + '">' + ic('shield') + ' Назначить администратором</button>';
    ctx.set(html);
  };

  // ---------- Команды ----------
  VIEWS.aTeams = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const q = new URLSearchParams({ page: String(page), limit: '20' });
    if (state.ui.adminTeamsQ) q.set('q', state.ui.adminTeamsQ);
    const data = await ctx.api('/api/v2/admin/teams?' + q);
    if (!ctx.alive()) return;
    const items = arr(data.items);
    const tgOnly = items.filter((t) => t.telegram_avatar_only).length;
    let html = backBtn() + '<div class="page-head"><h1>Команды</h1><p>Всего: ' + esc(data.total) + '</p></div>' +
      '<div class="card"><div class="inline-form"><input class="input" id="aTeamsQ" maxlength="64" placeholder="Название команды" value="' + esc(state.ui.adminTeamsQ) + '" data-enter="ateams-search">' +
      '<button class="btn primary" type="button" data-act="ateams-search">Найти</button></div>' +
      '<div class="divider"></div><div class="row between wrap"><div class="small muted grow">Аватары, сохранённые только в Telegram, не загружаются при просмотре страниц. Их можно один раз скопировать на сервер.</div>' +
      '<button class="btn small" type="button" data-act="avatars-sync">' + ic('download') + ' Аватары из Telegram' + (tgOnly ? ' (' + tgOnly + ' на стр.)' : '') + '</button></div><div id="syncResult"></div></div>';
    html += items.length ? '<div class="list">' + items.map((t) => '<button class="item" type="button" ' + openAttr('aTeam', { id: t.id }) + '>' + avatarHtml(t.avatar_url, t.name) + '<div class="grow"><div class="title">' + esc(t.name) + '</div>' +
      '<div class="sub">' + esc(t.members) + '/' + esc(t.max_members) + ' · капитан ' + esc(t.captain ? t.captain.name : '—') + ' · ' + (num(t.avg_mmr) !== null ? 'ср. ' + esc(t.avg_mmr) : 'MMR —') + '</div></div>' +
      (t.over_limit ? pill('перебор', 'bad') : '') + (t.telegram_avatar_only ? pill('аватар в TG', 'warn') : '') + '</button>').join('') + '</div>' : stateBox('Команд нет', '');
    html += pager(data, 'ateams-page');
    ctx.set(html);
  };
  ACTIONS['ateams-search'] = () => { const i = document.getElementById('aTeamsQ'); state.ui.adminTeamsQ = i ? i.value.trim() : ''; setParam('page', 1); };
  ACTIONS['ateams-page'] = (el) => setParam('page', Number(el.dataset.page));
  ACTIONS['avatars-sync'] = async (el) => {
    await withBusy(el, async () => {
      const box = document.getElementById('syncResult');
      try {
        const r = await api('/api/v2/admin/avatars/sync', { method: 'POST', timeout: 70000 });
        const lines = [];
        lines.push('Проверено: ' + r.checked + ', сохранено: ' + arr(r.saved).length + ', ошибок: ' + arr(r.failed).length + (arr(r.skipped).length ? ', отложено: ' + r.skipped.length : ''));
        arr(r.failed).forEach((f) => lines.push(f.name + ': ' + f.error));
        if (arr(r.skipped).length) lines.push('Часть команд не успела — нажмите ещё раз.');
        if (box) box.innerHTML = '<pre class="pre" style="margin-top:10px">' + esc(lines.join('\n')) + '</pre>';
        toast(arr(r.saved).length ? 'Аватары скопированы' : 'Готово', 'ok');
      } catch (err) {
        if (box) box.innerHTML = '<div class="inline-error">' + esc(err.message) + '</div>';
        toast(err.message, 'bad');
      }
    });
  };

  VIEWS.aTeam = async (ctx) => {
    const t = await ctx.api('/api/v2/admin/teams/' + encodeURIComponent(ctx.params.id));
    if (!ctx.alive()) return;
    let html = backBtn() + '<div class="card"><div class="team-head">' + avatarHtml(t.avatar_url, t.name, 'lg') + '<div class="grow"><div class="eyebrow">Команда #' + esc(t.id) + '</div><h1 class="ellipsis">' + esc(t.name) + '</h1>' +
      '<div class="small muted">Капитан: ' + (t.captain ? esc(t.captain.name) + ' · TG ' + esc(t.captain.telegram_id) : '—') + '</div></div></div>' + (t.description ? '<p class="desc">' + esc(t.description) + '</p>' : '') +
      '<div class="grid2" style="margin-top:10px"><div class="stat"><span>Состав</span><b class="' + (t.over_limit ? 'mmr-inline pending' : '') + '">' + esc(t.roster_count) + '/' + esc(t.max_roster) + '</b></div>' +
      '<div class="stat"><span>Средний MMR</span><b>' + (num(t.avg_mmr) !== null ? esc(t.avg_mmr) : '—') + '</b><small>подтверждено ' + esc(t.mmr_count) + '</small></div></div></div>';
    html += '<div class="card"><div class="card-head"><h3>Состав</h3></div><div class="list">' + arr(t.roster).map((m) => '<div class="item">' + medalHtml(m.medal, true) + '<div class="grow"><div class="title">' + esc(m.name) + (m.role === 'CAPTAIN' ? ' ' + ic('crown') : m.role === 'COACH' ? ' ' + pill('тренер', 'info') : '') + '</div>' +
      '<div class="sub">' + (m.position ? 'поз. ' + esc(m.position) + ' · ' : '') + mmrInline(m.mmr) + '</div></div>' +
      (m.role === 'CAPTAIN' ? '' : '<button class="btn small danger" type="button" data-act="aremove-member" data-team="' + esc(t.id) + '" data-user="' + esc(m.user_id) + '" data-name="' + esc(m.name) + '">Удалить</button>') + '</div>').join('') + '</div></div>';
    const apps = arr(t.applications);
    if (apps.length) html += '<div class="card"><div class="card-head"><h3>Заявки</h3></div><div class="list">' + apps.map((a) => '<div class="item"><div class="grow"><div class="title">' + esc(a.tournament_name) + '</div><div class="sub">№' + esc(a.id) + ' · ' + esc(fmtDate(a.created_at)) + '</div>' +
      (['APPROVED', 'PENDING', 'DRAFT'].includes(a.status) && a.tournament_id ? '<div class="actions"><button class="btn small danger" type="button" data-act="a-withdraw" data-tour="' + esc(a.tournament_id) + '" data-team="' + esc(t.id) + '" data-name="' + esc(t.name) + '">Снять с турнира</button></div>' : '') +
      '</div>' + aPill(a.status) + '</div>').join('') + '</div></div>';
    html += '<div class="card"><div class="card-head"><h3>Расформирование</h3></div><div class="small muted" style="margin-bottom:10px">Команда снимается со всех турниров (если игры уже идут — её матчи засчитываются соперникам), игроки становятся свободными, название остаётся в истории матчей.</div>' +
      '<button class="btn danger block" type="button" data-act="a-disband" data-team="' + esc(t.id) + '" data-name="' + esc(t.name) + '">Расформировать команду</button></div>';
    ctx.set(html);
  };
  ACTIONS['a-withdraw'] = async (el) => {
    const reason = await promptDialog({ title: 'Снять «' + el.dataset.name + '» с турнира?', text: 'Если игры уже идут, несыгранные матчи команды засчитаются соперникам. Капитан получит уведомление.\n\nПричина (увидит капитан):', placeholder: 'например, не явились', ok: 'Снять', danger: true, max: 300 });
    if (reason === null || reason === undefined) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.tour + '/teams/' + el.dataset.team + '/withdraw', { method: 'POST', body: { confirm: true, reason } }), 'Команда снята с турнира');
  };
  ACTIONS['a-disband'] = async (el) => {
    const reason = await promptDialog({ title: 'Расформировать «' + el.dataset.name + '»?', text: 'Это нельзя отменить. Игроки получат уведомление.\n\nПричина:', placeholder: 'например, смурф в составе', ok: 'Расформировать', danger: true, max: 300 });
    if (reason === null || reason === undefined) return;
    const ok = await mutate(el, () => api('/api/v2/admin/teams/' + el.dataset.team + '/disband', { method: 'POST', body: { confirm: true, reason } }), 'Команда расформирована', { reload: false });
    if (ok) goBack();
  };
  ACTIONS['aremove-member'] = async (el) => {
    if (!(await confirmDialog({ title: 'Удалить игрока из команды?', text: el.dataset.name + ' станет свободным игроком. Игрок получит уведомление.', ok: 'Удалить', danger: true }))) return;
    await mutate(el, () => api('/api/v2/admin/teams/' + el.dataset.team + '/members/' + el.dataset.user, { method: 'DELETE' }), 'Игрок удалён из состава');
  };

  // ---------- Турниры ----------
  function nextWeekday(weekday, hour) {
    const d = new Date(); d.setHours(hour, 0, 0, 0);
    let delta = (weekday - d.getDay() + 7) % 7;
    if (delta === 0 && d <= new Date()) delta = 7;
    d.setDate(d.getDate() + delta);
    return d;
  }

  // 5.11: расстановка пар — все «ручные» места этапа на одном экране, выбор команды меняет её местами с прежним местом.
  let PAIRS = null;
  VIEWS.aPairs = async (ctx) => {
    const id = encodeURIComponent(ctx.params.id);
    const d = await ctx.api('/api/v2/admin/tournaments/' + id + '/engine/pairs');
    if (!ctx.alive()) return;
    PAIRS = d;
    const teams = arr(d.teams);
    const busy = new Set(arr(d.busy).map(Number));
    const label = (x) => (x.seed ? '#' + x.seed + ' ' : '') + x.name;
    const tname = (tid) => { const x = teams.find((t) => Number(t.id) === Number(tid)); return x ? label(x) : 'Команда #' + tid; };
    const matches = arr(d.matches);
    const saveBtn = '<button class="btn primary block" type="button" data-act="pairs-save" data-id="' + id + '">Сохранить расстановку</button>';
    let html = backBtn() + '<div class="page-head"><h1>Расстановка пар</h1><p>' + esc(d.tournament.name) + ' · ' + esc(d.format_title) + '</p></div>' +
      '<div class="card"><div class="small muted">Выберите команду в любом месте — если она уже стоит в другом матче, команды поменяются местами. Места «Победитель / Проигравший матча N» заполняются результатами, их не меняют. Изменения применяются только после «Сохранить».</div>' +
      (d.table ? '' : '<div id="pairsFree" style="margin:10px 0"></div>') + (matches.length ? saveBtn : '') + '</div>' +
      '<div class="card"><div class="card-head"><h3>Автодобор пар</h3></div><div class="small muted" style="margin-bottom:10px">Делается сам при закрытии регистрации. Все команды без соперника (на автопроходе, одни в матче, не попавшие в сетку) встают друг против друга — сильный против слабого. При нечётном числе сильнейшая получает BYE. Готовые пары, их время и BO сохраняются, капитанам приходит уведомление.</div>' +
      '<button class="btn block" type="button" data-act="autopair" data-id="' + id + '">Добрать пары сейчас</button></div>';
    if (!matches.length) html += '<div class="card muted">Вписывать вручную нечего: все матчи сыграны или заполняются результатами других матчей.</div>';
    let last = '';
    matches.forEach((m) => {
      const head = m.bracket_title + ' · ' + m.round_name;
      if (head !== last) html += '<div class="eyebrow" style="margin:14px 4px 6px">' + esc(head) + '</div>';
      last = head;
      const slot = (s) => {
        if (!s.editable) {
          return '<div class="pair-fixed">' + (s.team_id ? esc(tname(s.team_id)) + (s.source ? ' <span class="small muted">· ' + esc(s.source) + '</span>' : '') : '<span class="muted">' + esc(s.source || 'Ожидается') + '</span>') + '</div>';
        }
        const opts = teams.filter((x) => !busy.has(Number(x.id)) || Number(x.id) === Number(s.team_id));
        return '<select class="select pair-sel" data-change="pair-pick" data-match="' + m.id + '" data-slot="' + s.slot + '" data-scope="' + esc(m.scope) + '" data-prev="' + esc(s.team_id || '') + '" data-orig="' + esc(s.team_id || '') + '">' +
          '<option value="">— пусто —</option>' + opts.map((x) => '<option value="' + esc(x.id) + '"' + (Number(x.id) === Number(s.team_id) ? ' selected' : '') + '>' + esc(label(x)) + '</option>').join('') + '</select>';
      };
      html += '<div class="card pair-card"><div class="row between" style="margin-bottom:8px"><b>Матч ' + esc(m.no) + '</b><span class="small muted">BO' + esc(m.best_of) + '</span></div>' +
        slot(m.slots[0]) + '<div class="pair-vs">против</div>' + slot(m.slots[1]) + '</div>';
    });
    if (matches.length) html += '<div style="margin:14px 0">' + saveBtn + '<button class="btn ghost block" type="button" data-act="reload" style="margin-top:8px">Сбросить изменения</button></div>';
    ctx.set(html);
    pairsRefresh();
  };
  function pairsRefresh() {
    const sels = [...document.querySelectorAll('select.pair-sel')];
    const changed = sels.filter((s) => (s.value || '') !== (s.dataset.orig || ''));
    sels.forEach((s) => s.classList.toggle('changed', changed.includes(s)));
    const n = new Set(changed.map((s) => s.dataset.match)).size;
    document.querySelectorAll('[data-act="pairs-save"]').forEach((b) => { b.textContent = n ? 'Сохранить расстановку (' + n + ' ' + plural(n, 'матч', 'матча', 'матчей') + ')' : 'Сохранить расстановку'; });
    const box = document.getElementById('pairsFree');
    if (!box || !PAIRS) return;
    const busy = new Set(arr(PAIRS.busy).map(Number));
    const used = new Set(sels.map((s) => Number(s.value)).filter(Boolean));
    const free = arr(PAIRS.teams).filter((x) => !busy.has(Number(x.id)) && !used.has(Number(x.id)));
    box.innerHTML = free.length
      ? '<div class="warnbox">Без пары (' + free.length + '): ' + esc(free.map((x) => x.name).join(', ')) + '</div>'
      : '<div class="okbox">Все команды расставлены.</div>';
  }
  ACTIONS['pair-pick'] = (el) => {
    const val = el.value;
    const prev = el.dataset.prev || '';
    if (val) {
      document.querySelectorAll('select.pair-sel').forEach((o) => {
        if (o === el || o.value !== val || o.dataset.scope !== el.dataset.scope) return;
        o.value = prev; // команда уходит отсюда — на её место встаёт прежняя команда выбранного места
        o.dataset.prev = o.value;
      });
    }
    el.dataset.prev = val;
    haptic();
    pairsRefresh();
  };
  ACTIONS['t-publish'] = async (el) => {
    if (!(await confirmDialog({ title: 'Разослать итоги?', text: 'Картинка с призёрами, MVP и призом зрителей уйдёт админам, всем игрокам команд турнира и их болельщикам. При завершении турнира это происходит само.', ok: 'Разослать' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/publish-results', { method: 'POST' }), (r) => 'Отправляется: получателей — ' + r.sent, { reload: false });
  };
  ACTIONS['t-recheck'] = async (el) => {
    if (!(await confirmDialog({ title: 'Перепроверить всех игроков?', text: 'Свежая проверка медали по OpenDota, STRATZ и Dotabuff и риска смурфа для каждого игрока одобренных команд. Идёт фоном, по игроку раз в ~12 секунд; AI — только для подозрительных. Результаты — в «Подозрительных». В день турнира это делается само утром.', ok: 'Запустить' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/recheck-rosters', { method: 'POST' }),
      (r) => 'Проверка запущена: игроков — ' + ((r.sweep || {}).total || 0), { reload: false });
  };
  ACTIONS.autopair = async (el) => {
    const url = '/api/v2/admin/tournaments/' + el.dataset.id + '/engine/autopair';
    let plan;
    try { plan = await api(url, { method: 'POST', body: { dry_run: true } }); } catch (err) { toast(err.message, 'bad'); return; }
    if (plan.action !== 'rebuilt' && plan.action !== 'filled') { toast(plan.text || 'Добирать некого'); return; }
    const lines = arr(plan.pairs).map((p) => '• ' + p.team1 + ' — ' + p.team2)
      .concat(arr(plan.byes).map((b) => '• BYE: ' + b.team))
      .concat(arr(plan.left).length ? ['Не поместились: ' + arr(plan.left).join(', ')] : []);
    if (!(await confirmDialog({ title: 'Добрать пары?', text: plan.text + (lines.length ? '\n\n' + lines.join('\n') : ''), ok: 'Добрать' }))) return;
    await mutate(el, () => api(url, { method: 'POST', body: { dry_run: false } }), (res) => res.text || 'Готово');
  };
  ACTIONS['pairs-save'] = async (el) => {
    const by = {};
    document.querySelectorAll('select.pair-sel').forEach((s) => {
      if ((s.value || '') === (s.dataset.orig || '')) return;
      const p = by[s.dataset.match] || (by[s.dataset.match] = { match_id: Number(s.dataset.match) });
      p['team' + s.dataset.slot + '_id'] = Number(s.value) || null;
    });
    const pairs = Object.values(by);
    if (!pairs.length) { toast('Изменений нет'); return; }
    el.disabled = true;
    try {
      const r = await api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/pairs', { method: 'POST', body: { pairs } });
      toast('Сохранено, изменено матчей: ' + r.changed, 'ok');
      reload();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      el.disabled = false;
    }
  };

  VIEWS.aTournaments = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const data = await ctx.api('/api/v2/admin/tournaments?limit=20&page=' + page);
    if (!ctx.alive()) return;
    let html = backBtn() + '<div class="page-head"><h1>Турниры</h1><p>Создание, регистрация, заявки и сетка.</p></div>' +
      '<div class="card"><div class="card-head"><h3>Новый турнир</h3></div>' +
      '<label class="field">Название<input class="input" id="ntName" maxlength="128" placeholder="IT:TOP Dota Cup"></label>' +
      '<div class="grid2"><label class="field">Команд (2–1024)<input class="input" id="ntMax" inputmode="numeric" value="16"></label>' +
      '<label class="field">Старт (' + TZ_LABEL + ')<input class="input" id="ntStart" type="datetime-local"></label></div>' +
      '<div class="chips" style="margin-bottom:10px"><button class="chip" type="button" data-act="nt-preset" data-day="1">Ближайший понедельник 20:00</button><button class="chip" type="button" data-act="nt-preset" data-day="5">Ближайшая пятница 20:00</button></div>' +
      '<label class="field">Описание<textarea class="textarea" id="ntDesc" maxlength="4000" placeholder="Формат, призы, правила"></textarea></label>' +
      '<label class="row" style="gap:8px;margin-bottom:10px"><input type="checkbox" id="ntOpen"> <span>Сразу открыть регистрацию</span></label>' +
      '<button class="btn primary block" type="button" data-act="nt-create">Создать турнир</button></div>';
    const items = arr(data.items);
    html += items.length ? items.map((t) => '<button class="tour" type="button" ' + openAttr('aTournament', { id: t.id }) + '><div class="row between"><b class="ellipsis">#' + esc(t.id) + ' ' + esc(t.name) + '</b>' + tPill(t.status, t.registration_open) + '</div>' +
      '<div class="small muted" style="margin-top:4px">Старт: ' + esc(fmtDate(t.start_date)) + '</div><div class="meta">' + pill('одобрено ' + t.approved_count + '/' + t.max_teams) + (t.pending_count ? pill('ждут ' + t.pending_count, 'warn') : '') + '</div></button>').join('')
      : stateBox('Турниров нет', 'Создайте первый турнир.');
    html += pager(data, 'at-page');
    ctx.set(html);
  };
  ACTIONS['at-page'] = (el) => setParam('page', Number(el.dataset.page));
  ACTIONS['nt-preset'] = (el) => {
    const d = nextWeekday(Number(el.dataset.day), 20);
    const s = document.getElementById('ntStart'); if (s) s.value = localDateInput(d);
    const n = document.getElementById('ntName');
    if (n && !n.value.trim()) n.value = 'IT:TOP Dota Cup — ' + d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
  };
  ACTIONS['nt-create'] = async (el) => {
    const name = (document.getElementById('ntName') || {}).value || '';
    const max = Number((document.getElementById('ntMax') || {}).value);
    const start = (document.getElementById('ntStart') || {}).value || '';
    const description = (document.getElementById('ntDesc') || {}).value || '';
    const open = !!(document.getElementById('ntOpen') || {}).checked;
    if (!name.trim()) { toast('Введите название турнира', 'bad'); return; }
    if (!Number.isInteger(max) || max < 2 || max > 1024) { toast('Количество команд: от 2 до 1024', 'bad'); return; }
    const startIso = start || null; // московское время без зоны — сервер переводит в UTC
    await mutate(el, () => api('/api/v2/admin/tournaments', { method: 'POST', body: { name: name.trim(), max_teams: max, start_date: startIso, description, open_registration: open } }), 'Турнир создан');
  };

  VIEWS.aTournament = async (ctx) => {
    const id = encodeURIComponent(ctx.params.id);
    const [t, b] = await Promise.all([ctx.api('/api/v2/admin/tournaments/' + id), ctx.api('/api/v2/admin/tournaments/' + id + '/bracket').catch((e) => ({ error: e.message }))]);
    if (!ctx.alive()) return;
    const apps = arr(t.applications);
    const statuses = ['DRAFT', 'REGISTRATION', 'ACTIVE', 'FINISHED', 'CANCELLED'];
    let html = backBtn() + '<div class="card"><div class="eyebrow">Турнир #' + esc(t.id) + '</div><div class="row between" style="align-items:flex-start"><h1>' + esc(t.name) + '</h1>' + tPill(t.status, t.registration_open) + '</div>' +
      '<div class="small muted" style="margin-top:4px">Одобрено ' + esc(t.approved_count) + '/' + esc(t.max_teams) + ' · ждут ' + esc(t.pending_count) + '</div>' +
      '<div class="divider"></div><div class="small muted" style="margin-bottom:6px">Статус (регистрация открыта только в статусе «Регистрация»)</div><div class="chips">' +
      statuses.map((s) => '<button class="chip' + (t.status === s ? ' active' : '') + '" type="button" data-act="at-status" data-id="' + esc(t.id) + '" data-status="' + s + '">' + esc(T_STATUS[s]) + '</button>').join('') + '</div></div>';
    html += '<div class="card"><div class="card-head"><h3>Параметры</h3></div>' +
      '<label class="field">Название<input class="input" id="etName" maxlength="128" value="' + esc(t.name) + '"></label>' +
      '<div class="grid2"><label class="field">Команд<input class="input" id="etMax" inputmode="numeric" value="' + esc(t.max_teams) + '"></label>' +
      '<label class="field">Старт (' + TZ_LABEL + ')<input class="input" id="etStart" type="datetime-local" value="' + esc(mskInput(t.start_date)) + '"></label></div>' +
      '<div class="chips" style="margin-bottom:10px"><button class="chip" type="button" data-act="et-preset" data-day="1">Пн 20:00</button><button class="chip" type="button" data-act="et-preset" data-day="5">Пт 20:00</button><button class="chip" type="button" data-act="et-clear">Без даты</button></div>' +
      '<label class="field">Описание<textarea class="textarea" id="etDesc" maxlength="4000">' + esc(t.description || '') + '</textarea></label>' +
      '<button class="btn primary block" type="button" data-act="et-save" data-id="' + esc(t.id) + '">Сохранить</button></div>';
    html += '<div class="card"><div class="card-head"><h3>Заявки команд</h3>' + pill(String(apps.length)) + '</div>' + (apps.length ? '<div class="list">' + apps.map((a) =>
      '<div class="item">' + avatarHtml(a.avatar_url, a.name) + '<div class="grow"><div class="title">' + esc(a.name) + '</div><div class="sub">' + esc(a.members) + ' игроков · ' + (num(a.avg_mmr) !== null ? 'ср. ' + esc(a.avg_mmr) + ' (' + esc(a.mmr_count) + ' подтв.)' : 'MMR —') + ' · №' + esc(a.application_id) + '</div>' +
      (a.status === 'PENDING' || a.status === 'DRAFT' ? '<div class="actions"><button class="btn small ok" type="button" data-act="app-decide" data-id="' + esc(a.application_id) + '" data-status="APPROVED">Одобрить</button><button class="btn small danger" type="button" data-act="app-decide" data-id="' + esc(a.application_id) + '" data-status="REJECTED">Отклонить</button></div>' : '') +
      (a.status === 'APPROVED' && !['FINISHED', 'CANCELLED'].includes(t.status) ? '<div class="actions"><button class="btn small ghost" type="button" data-act="a-withdraw" data-tour="' + esc(t.id) + '" data-team="' + esc(a.id) + '" data-name="' + esc(a.name) + '">Снять с турнира</button></div>' : '') +
      '</div>' + aPill(a.status) + '</div>').join('') + '</div>' : '<div class="muted small">Заявок нет.</div>') + '</div>';

    const eng = b.engine || null;
    html += '<div class="card"><div class="card-head"><h3>Сетка и расписание</h3>' + (b.locked ? pill('зафиксирована', 'ok') : b.exists ? pill('черновик', 'warn') : pill('нет')) + '</div>';
    if (b.error) html += '<div class="inline-error">' + esc(b.error) + '</div>';
    else {
      const teams = arr(b.teams);
      const opt = (sel) => '<option value="">— команда —</option>' + teams.map((x) => '<option value="' + esc(x.id) + '"' + (Number(sel) === Number(x.id) ? ' selected' : '') + '>' + esc(x.name) + (num(x.avg_mmr) !== null ? ' · ' + esc(x.avg_mmr) : ' · MMR неполный') + '</option>').join('');
      const incomplete = teams.filter((x) => !x.mmr_complete);
      const st = (eng && eng.settings) || {};
      const fmtKey = eng ? eng.format : 'double_elim';
      const boSel = (id, val) => '<select class="select" id="' + id + '">' + (id === 'engBo' ? [1, 2, 3, 5] : [1, 3, 5]).map((x) => '<option value="' + x + '"' + (Number(val) === x ? ' selected' : '') + '>BO' + x + '</option>').join('') + '</select>';
      const legacyOnly = !eng && b.exists && b.bracket;
      if (eng) html += '<div class="small muted" style="margin:-4px 0 8px">' + esc(eng.description || eng.format_title) + ' · матчей: ' + esc(eng.total) + ', сыграно: ' + esc((eng.counts || {}).FINISHED || 0) + '</div>';
      if (eng) html += '<button class="btn block" type="button" style="margin-bottom:10px" ' + openAttr('aPairs', { id: t.id }) + '>Расставить пары вручную — кто против кого</button>';
      html += '<button class="btn block" type="button" style="margin-bottom:10px" data-act="t-recheck" data-id="' + esc(t.id) + '">' + ic('shield') + ' Перепроверить ранги всех игроков турнира</button>';
      if (eng && t.status !== 'DRAFT') {
        const boardUrl = location.origin + '/v2/static/board.html?t=' + encodeURIComponent(t.id);
        html += '<div class="okbox" style="margin-bottom:10px"><b>Табло для проектора и стрима</b><div class="small muted" style="margin:4px 0 8px">Открывается в любом браузере без Telegram, само обновляется каждые 30 секунд. Адрес: <span style="word-break:break-all">' + esc(boardUrl) + '</span></div>' +
          '<div class="actions" style="margin-top:0"><button class="btn small primary" type="button" data-act="ext" data-url="' + esc(boardUrl) + '">Открыть табло</button>' +
          '<button class="btn small" type="button" data-act="ext" data-url="' + esc(boardUrl + '&mode=stage') + '">Сцена для OBS</button>' +
          '<button class="btn small" type="button" data-act="ext" data-url="' + esc(location.origin + '/api/v2/public/tournaments/' + encodeURIComponent(t.id) + '/results.png') + '">Картинка итогов</button>' +
          '<button class="btn small" type="button" data-act="t-publish" data-id="' + esc(t.id) + '">Разослать итоги</button></div>' +
          '<div class="small muted" style="margin-top:6px">Сцена — крупно идущий матч и счёт по картам. В OBS: источник «Браузер», адрес ' + esc(boardUrl) + '&amp;mode=stage&amp;bg=none (прозрачный фон).</div></div>';
      }
      if (teams.length < 2) html += '<div class="warnbox" style="margin-top:10px">Для сетки нужно минимум 2 одобренные команды (сейчас ' + teams.length + ').</div>';
      if (b.stale) html += '<div class="warnbox" style="margin-top:10px">Состав одобренных команд изменился после генерации — пересоберите сетку.</div>';
      if (incomplete.length) html += '<div class="warnbox" style="margin-top:10px">Неполный MMR (для фиксации нужно 5 подтверждённых): ' + esc(incomplete.map((x) => x.name).join(', ')) + '</div>';
      if (legacyOnly) {
        html += '<div class="okbox" style="margin-top:10px">Есть опубликованная сетка старого формата. Перенесите её в новый движок — пары первого раунда сохранятся, появятся счёт, BO, время и нижняя сетка.</div>';
      }
      if (!b.locked && teams.length >= 2) {
        html += '<div class="grid2" style="margin-top:10px"><label class="field">Формат<select class="select" id="engFormat">' +
          [['double_elim', 'Double elimination (с нижней сеткой)'], ['single_elim', 'Single elimination'], ['round_robin', 'Круговая (все со всеми)'], ['groups_rr', 'Группы (все со всеми) → плей-офф'], ['groups_gsl', 'Группы GSL → плей-офф'], ['swiss', 'Швейцарская система'], ['manual', 'Ручные матчи (пары ставлю сам)']].map(([k, n]) => '<option value="' + k + '"' + (fmtKey === k ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>' +
          '<label class="field">Посев<select class="select" id="engSeeding"><option value="opening"' + (st.seeding !== 'standard' && st.seeding !== 'everyone' ? ' selected' : '') + '>Открывающий матч вручную</option><option value="standard"' + (st.seeding === 'standard' ? ' selected' : '') + '>Стандартный по MMR</option><option value="everyone"' + (st.seeding === 'everyone' ? ' selected' : '') + '>Все играют в 1-м раунде (BYE только лишней)</option></select></label></div>' +
          '<div class="grid2"><label class="field">Все матчи' + boSel('engBo', st.bo_default || 3) + '</label><label class="field">Финал / гранд-финал' + boSel('engBoFinal', st.bo_final || 1) + '</label></div>' +
          '<div class="small muted" style="margin:2px 0 6px">Для групп, круговой и швейцарки (BO2 — ничья 1:1, только в группах):</div><div class="grid2">' +
          '<label class="field">Групп (0 — авто)<input class="input" id="engGroups" inputmode="numeric" value="' + esc(st.groups != null ? st.groups : 2) + '"></label>' +
          '<label class="field">Выходят из группы<input class="input" id="engAdvance" inputmode="numeric" value="' + esc(st.advance || 2) + '"></label>' +
          '<label class="field">Плей-офф после<select class="select" id="engPlayoff">' + [['single_elim', 'Single elimination'], ['double_elim', 'Double elimination'], ['none', 'Без плей-офф']].map(([k, n]) => '<option value="' + k + '"' + ((st.playoff_format || 'single_elim') === k ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>' +
          '<label class="field">BO плей-офф / финал<div class="row" style="gap:6px">' + boSel('engPoBo', st.playoff_bo_default || 3) + boSel('engPoBoF', st.playoff_bo_final || 3) + '</div></label>' +
          '<label class="field">Швейцарка: побед до выхода (0 — авто)<input class="input" id="engSwW" inputmode="numeric" value="' + esc(st.wins_to_advance || 0) + '"></label>' +
          '<label class="field">Поражений до вылета (0 — авто)<input class="input" id="engSwL" inputmode="numeric" value="' + esc(st.losses_to_eliminate || 0) + '"></label></div>' +
          '<label class="row" style="gap:8px;margin-bottom:6px"><input type="checkbox" id="engReset"' + (st.grand_final_reset ? ' checked' : '') + '> <span class="small">Перезапуск гранд-финала (double elimination)</span></label>' +
          '<label class="row" style="gap:8px;margin-bottom:10px"><input type="checkbox" id="engThird"' + (st.third_place ? ' checked' : '') + '> <span class="small">Матч за 3-е место (single elimination)</span></label>' +
          '<div class="grid2"><label class="field">Матч 1 — команда A<select class="select" id="openA">' + opt(b.opening_team_a) + '</select></label>' +
          '<label class="field">Матч 1 — команда B<select class="select" id="openB">' + opt(b.opening_team_b) + '</select></label></div>' +
          '<div class="note" style="margin-top:0">«Открывающий матч»: команды A и B играют матч №1, остальные посеяны по среднему подтверждённому MMR. «Стандартный»: 1-й против последнего; BYE получают сильнейшие, фиктивных матчей нет. Пересборка сбрасывает расписание.</div>' +
          '<div class="actions">' +
          (legacyOnly ? '<button class="btn primary" type="button" data-act="eng-import" data-id="' + esc(t.id) + '">Перенести текущую сетку</button>' : '') +
          '<button class="btn' + (legacyOnly ? '' : ' primary') + '" type="button" data-act="eng-generate" data-id="' + esc(t.id) + '">' + (eng ? 'Пересобрать' : 'Сгенерировать') + '</button>' +
          (eng ? '<button class="btn" type="button" data-act="eng-lock" data-id="' + esc(t.id) + '">Зафиксировать</button>' : '') + '</div>';
      }
      if (eng && eng.format === 'manual') {
        const tsel = (id) => '<select class="select" id="' + id + '"><option value="">— команда —</option>' + teams.map((x) => '<option value="' + esc(x.id) + '">' + esc(x.name) + '</option>').join('') + '</select>';
        html += '<div class="mm-sec"><div class="mm-t">' + ic('plus') + ' Добавить матч</div><div class="grid2">' + tsel('mmA') + tsel('mmB') + '</div>' +
          '<div class="grid2" style="margin-top:8px"><label class="field">BO<select class="select" id="mmBo">' + [1, 2, 3, 5].map((x) => '<option value="' + x + '"' + (x === 3 ? ' selected' : '') + '>BO' + x + '</option>').join('') + '</select></label>' +
          '<label class="field">Тур<input class="input" id="mmRound" inputmode="numeric" placeholder="авто"></label></div>' +
          '<label class="field">Время (' + TZ_LABEL + ', можно позже)<input class="input" id="mmWhen" type="datetime-local"></label>' +
          '<button class="btn primary block" type="button" data-act="eng-add-match" data-id="' + esc(t.id) + '">Добавить матч</button></div>';
      }
      const firstSt = eng ? (arr(eng.all_stages).length ? eng.all_stages[0] : eng) : null;
      if (firstSt && firstSt.playoffs_ready) html += '<div class="okbox" style="margin-top:10px">Групповой этап сыгран. <button class="btn small primary" type="button" data-act="eng-playoffs" data-id="' + esc(t.id) + '">Сформировать плей-офф</button></div>';
      if (eng) {
        html += '<div class="actions"><button class="btn small" type="button" ' + openAttr('tournament', { id: t.id, sec: 'schedule' }) + '>Расписание турнира</button>' +
          '<button class="btn small danger" type="button" data-act="eng-reset" data-id="' + esc(t.id) + '">Сбросить сетку</button></div>';
      }
      const avatars = {}; teams.forEach((x) => { avatars[x.id] = x.avatar_url; });
      if (eng) html += '<div class="divider"></div><div class="note" style="margin-top:0">Нажмите на матч: время (' + TZ_LABEL + '), результат серии, статус, BO.</div>' + engineHtml(eng, avatars);
      else if (b.bracket) html += '<div class="divider"></div>' + bracketHtml({ exists: true, bracket: b.bracket }, avatars);
    }
    html += '</div>';
    ctx.set(html);
  };
  ACTIONS['at-status'] = async (el) => {
    if (el.classList.contains('active')) return;
    const cur = document.querySelector('[data-act="at-status"].active');
    const closing = cur && cur.dataset.status === 'REGISTRATION' && (el.dataset.status === 'ACTIVE' || el.dataset.status === 'DRAFT');
    const text = 'Новый статус: ' + (T_STATUS[el.dataset.status] || el.dataset.status) +
      (closing ? '\n\nРегистрация закроется: все команды без соперника встанут в пары друг против друга, лишняя (при нечётном числе) получит автопроход BYE. Готовые пары и их время сохранятся, капитанам придут уведомления.' : '');
    if (!(await confirmDialog({ title: 'Сменить статус?', text, ok: 'Сменить' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/status', { method: 'POST', body: { status: el.dataset.status } }),
      (res) => 'Статус изменён' + (res && res.autopair && res.autopair.text ? '. ' + res.autopair.text : ''));
  };
  ACTIONS['et-preset'] = (el) => { const s = document.getElementById('etStart'); if (s) s.value = localDateInput(nextWeekday(Number(el.dataset.day), 20)); };
  ACTIONS['et-clear'] = () => { const s = document.getElementById('etStart'); if (s) s.value = ''; };
  ACTIONS['et-save'] = async (el) => {
    const name = ((document.getElementById('etName') || {}).value || '').trim();
    const max = Number((document.getElementById('etMax') || {}).value);
    const start = (document.getElementById('etStart') || {}).value || '';
    const description = (document.getElementById('etDesc') || {}).value || '';
    if (!name) { toast('Название не может быть пустым', 'bad'); return; }
    if (!Number.isInteger(max) || max < 2 || max > 1024) { toast('Количество команд: от 2 до 1024', 'bad'); return; }
    // Значение поля — московское время без зоны; сервер переводит его в UTC.
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id, { method: 'PATCH', body: { name, max_teams: max, description, start_date: start || null } }), 'Сохранено');
  };
  function engForm() {
    const val = (id) => (document.getElementById(id) || {}).value;
    const chk = (id) => !!(document.getElementById(id) || {}).checked;
    return {
      format: val('engFormat') || 'double_elim', seeding: val('engSeeding') || 'standard',
      bo_default: Number(val('engBo') || 3), bo_final: Number(val('engBoFinal') || 1),
      grand_final_reset: chk('engReset'), third_place: chk('engThird'),
      opening_team_a: Number(val('openA')) || null, opening_team_b: Number(val('openB')) || null,
      groups: val('engGroups'), advance: val('engAdvance'), playoff_format: val('engPlayoff'),
      playoff_bo_default: Number(val('engPoBo') || 3), playoff_bo_final: Number(val('engPoBoF') || 3),
      wins_to_advance: val('engSwW'), losses_to_eliminate: val('engSwL'),
    };
  }
  ACTIONS['eng-generate'] = async (el) => {
    const f = engForm();
    if (f.seeding === 'opening') {
      if (!f.opening_team_a || !f.opening_team_b) { toast('Выберите обе команды первого матча или стандартный посев', 'bad'); return; }
      if (f.opening_team_a === f.opening_team_b) { toast('Команда не может играть сама с собой', 'bad'); return; }
    }
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/generate', { method: 'POST', body: f }), 'Сетка сгенерирована');
  };
  ACTIONS['eng-import'] = async (el) => {
    const f = engForm();
    if (!(await confirmDialog({ title: 'Перенести сетку?', text: 'Пары первого раунда останутся как опубликованы. Формат: ' + (f.format === 'double_elim' ? 'double elimination (с нижней сеткой)' : 'single elimination') + ', BO' + f.bo_default + ', финал BO' + f.bo_final + '.', ok: 'Перенести' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/import-legacy', { method: 'POST', body: f }), 'Сетка перенесена');
  };
  ACTIONS['eng-add-match'] = async (el) => {
    const val = (id) => (document.getElementById(id) || {}).value || '';
    const a = Number(val('mmA')); const b = Number(val('mmB'));
    if (!a || !b) { toast('Выберите обе команды', 'bad'); return; }
    if (a === b) { toast('Команда не может играть сама с собой', 'bad'); return; }
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/matches', { method: 'POST', body: { team1_id: a, team2_id: b, best_of: Number(val('mmBo')) || 1, round_no: val('mmRound') || null, when: val('mmWhen') || null } }), 'Матч добавлен');
  };
  ACTIONS['eng-playoffs'] = async (el) => {
    if (!(await confirmDialog({ title: 'Сформировать плей-офф?', text: 'Лучшие команды выйдут в плей-офф: победители групп против вторых мест других групп.', ok: 'Сформировать' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/playoffs', { method: 'POST' }), 'Плей-офф сформирован');
  };
  ACTIONS['eng-lock'] = async (el) => {
    if (!(await confirmDialog({ title: 'Зафиксировать сетку?', text: 'После фиксации пересобрать сетку можно только через сброс.', ok: 'Зафиксировать' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/lock', { method: 'POST' }), 'Сетка зафиксирована');
  };
  ACTIONS['eng-reset'] = async (el) => {
    if (!(await confirmDialog({ title: 'Сбросить сетку?', text: 'Будут удалены все матчи, расписание и результаты этой сетки.', ok: 'Сбросить', danger: true }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/engine/reset', { method: 'POST', body: { force: true } }), 'Сетка сброшена');
  };
  ACTIONS['br-generate'] = async (el) => {
    const a = Number((document.getElementById('openA') || {}).value);
    const b = Number((document.getElementById('openB') || {}).value);
    if (!a || !b) { toast('Выберите обе команды первого матча', 'bad'); return; }
    if (a === b) { toast('Команда не может играть сама с собой', 'bad'); return; }
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/bracket/generate', { method: 'POST', body: { opening_team_a: a, opening_team_b: b, lock: false } }), 'Сетка сгенерирована');
  };
  ACTIONS['br-lock'] = async (el) => {
    if (!(await confirmDialog({ title: 'Зафиксировать сетку?', text: 'После фиксации распределение не будет меняться автоматически.', ok: 'Зафиксировать' }))) return;
    await mutate(el, () => api('/api/v2/admin/tournaments/' + el.dataset.id + '/bracket/lock', { method: 'POST' }), 'Сетка зафиксирована');
  };

  // ---------- Заявки ----------
  VIEWS.aApplications = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const status = state.ui.appStatus;
    const data = await ctx.api('/api/v2/admin/applications?limit=20&page=' + page + '&status=' + status);
    if (!ctx.alive()) return;
    const counts = data.counts || {};
    const tabs = [['PENDING', 'Ждут'], ['APPROVED', 'Одобрены'], ['REJECTED', 'Отклонены'], ['CANCELLED', 'Отменены'], ['ALL', 'Все']];
    let html = backBtn() + '<div class="page-head"><h1>Заявки на турниры</h1></div><div class="seg">' + tabs.map((x) => '<button type="button" data-act="app-filter" data-status="' + x[0] + '" class="' + (status === x[0] ? 'active' : '') + '">' +
      esc(x[1]) + (x[0] !== 'ALL' && counts[x[0]] ? ' ' + counts[x[0]] : '') + '</button>').join('') + '</div>';
    const items = arr(data.items);
    html += items.length ? '<div class="list">' + items.map((a) => '<div class="item">' + avatarHtml(a.team && a.team.avatar_url, a.team ? a.team.name : '?') + '<div class="grow"><div class="title">№' + esc(a.id) + ' · ' + esc(a.team ? a.team.name : 'команда удалена') + '</div>' +
      '<div class="sub">' + esc(a.tournament.name) + ' · капитан ' + esc(a.captain ? a.captain.name : '—') + ' · ' + esc(fmtDate(a.created_at)) + '</div>' + (a.comment ? '<div class="sub">' + ic('chat') + ' ' + esc(a.comment) + '</div>' : '') +
      (a.status === 'PENDING' || a.status === 'DRAFT' ? '<div class="actions"><button class="btn small ok" type="button" data-act="app-decide" data-id="' + esc(a.id) + '" data-status="APPROVED">Одобрить</button>' +
        '<button class="btn small danger" type="button" data-act="app-decide" data-id="' + esc(a.id) + '" data-status="REJECTED">Отклонить</button>' +
        '<button class="btn small ghost" type="button" data-act="app-decide" data-id="' + esc(a.id) + '" data-status="CANCELLED">Отменить</button>' +
        (a.team ? '<button class="btn small ghost" type="button" ' + openAttr('aTeam', { id: a.team.id }) + '>Состав</button>' : '') + '</div>' : '') +
      '</div>' + aPill(a.status) + '</div>').join('') + '</div>' : stateBox('Заявок нет', '');
    html += pager(data, 'app-page');
    ctx.set(html);
  };
  ACTIONS['app-filter'] = (el) => { state.ui.appStatus = el.dataset.status; setParam('page', 1); };
  ACTIONS['app-page'] = (el) => setParam('page', Number(el.dataset.page));
  ACTIONS['app-decide'] = async (el) => {
    const st = el.dataset.status;
    let comment = null;
    if (st === 'APPROVED') {
      if (!(await confirmDialog({ title: 'Одобрить заявку №' + el.dataset.id + '?', text: 'Капитан получит уведомление.', ok: 'Одобрить' }))) return;
    } else {
      comment = await promptDialog({ title: (st === 'REJECTED' ? 'Отклонить' : 'Отменить') + ' заявку №' + el.dataset.id + '?', text: 'Комментарий получит капитан (необязательно).', multiline: true, ok: st === 'REJECTED' ? 'Отклонить' : 'Отменить заявку', danger: true, max: 1000 });
      if (comment === null || comment === undefined) return;
    }
    await mutate(el, () => api('/api/v2/admin/applications/' + el.dataset.id + '/decision', { method: 'POST', body: { status: st, comment: comment || null } }), 'Решение сохранено, капитан уведомлён');
  };

  // ---------- Поддержка ----------
  VIEWS.aSupport = async (ctx) => {
    const page = Number(ctx.params.page || 1);
    const status = state.ui.ticketStatus;
    const data = await ctx.api('/api/v2/admin/support?limit=20&page=' + page + '&status=' + status);
    if (!ctx.alive()) return;
    const counts = data.counts || {};
    const tabs = [['OPEN', 'Открытые'], ['ANSWERED', 'Отвеченные'], ['CLOSED', 'Закрытые'], ['ALL', 'Все']];
    let html = backBtn() + '<div class="page-head"><h1>Поддержка</h1><p>Ответ уйдёт пользователю в бота и появится в Mini App.</p></div><div class="seg">' + tabs.map((x) => '<button type="button" data-act="sup-filter" data-status="' + x[0] + '" class="' + (status === x[0] ? 'active' : '') + '">' +
      esc(x[1]) + (x[0] !== 'ALL' && counts[x[0]] ? ' ' + counts[x[0]] : '') + '</button>').join('') + '</div>';
    const items = arr(data.items);
    html += items.length ? items.map((t) => '<div class="card"><div class="row between"><b>№' + esc(t.id) + ' · ' + esc(t.user ? t.user.name : '—') + '</b>' + pill(S_STATUS[t.status] || t.status, t.status === 'OPEN' ? 'warn' : t.status === 'ANSWERED' ? 'ok' : '') + '</div>' +
      '<div class="small muted">' + (t.user && t.user.username ? '@' + esc(t.user.username) + ' · ' : '') + (t.user ? 'TG ' + esc(t.user.telegram_id) + ' · ' : '') + esc(fmtDate(t.created_at)) + '</div>' +
      '<p class="desc">' + esc(t.message) + '</p>' + (t.reply ? '<div class="okbox" style="margin:8px 0 0">Ответ: ' + esc(t.reply) + '</div>' : '') +
      (t.status !== 'CLOSED' ? '<div class="composer"><textarea class="textarea" id="rep' + esc(t.id) + '" maxlength="3000" placeholder="' + (t.reply ? 'Дополнить ответ' : 'Ответ пользователю') + '"></textarea></div>' +
        '<div class="actions"><button class="btn small primary" type="button" data-act="sup-reply" data-id="' + esc(t.id) + '">Ответить</button><button class="btn small ghost" type="button" data-act="sup-close" data-id="' + esc(t.id) + '">Закрыть</button>' +
        (t.user ? '<button class="btn small ghost" type="button" ' + openAttr('aUser', { id: t.user.user_id }) + '>Профиль</button>' : '') + '</div>' : '') + '</div>').join('')
      : stateBox('Обращений нет', '');
    html += pager(data, 'sup-page');
    ctx.set(html);
  };
  ACTIONS['sup-filter'] = (el) => { state.ui.ticketStatus = el.dataset.status; setParam('page', 1); };
  ACTIONS['sup-page'] = (el) => setParam('page', Number(el.dataset.page));
  ACTIONS['sup-reply'] = async (el) => {
    const t = document.getElementById('rep' + el.dataset.id);
    const reply = t ? t.value.trim() : '';
    if (!reply) { toast('Введите ответ', 'bad'); return; }
    await mutate(el, () => api('/api/v2/admin/support/' + el.dataset.id + '/reply', { method: 'POST', body: { reply } }), 'Ответ отправлен');
  };
  ACTIONS['sup-close'] = async (el) => {
    if (!(await confirmDialog({ title: 'Закрыть обращение №' + el.dataset.id + '?', ok: 'Закрыть' }))) return;
    await mutate(el, () => api('/api/v2/admin/support/' + el.dataset.id + '/close', { method: 'POST' }), 'Обращение закрыто');
  };

  // ---------- Рассылка ----------
  function broadcastProgress(s) {
    if (!s || s.total === undefined) return '';
    const done = Number(s.sent || 0) + Number(s.blocked || 0) + Number(s.failed || 0);
    const pct = s.total ? Math.round(done * 100 / s.total) : 100;
    return '<div class="card"><div class="card-head"><h3>' + (s.running ? 'Идёт рассылка…' : 'Последняя рассылка') + '</h3>' + (s.running ? pill(pct + '%', 'warn') : pill('готово', 'ok')) + '</div>' +
      '<div class="progress"><i style="width:' + pct + '%"></i></div><div class="grid3" style="margin-top:10px"><div class="stat"><span>Доставлено</span><b>' + esc(s.sent || 0) + '</b></div>' +
      '<div class="stat"><span>Заблокировали</span><b>' + esc(s.blocked || 0) + '</b></div><div class="stat"><span>Ошибки</span><b>' + esc(s.failed || 0) + '</b></div></div>' +
      '<div class="note">Всего получателей: ' + esc(s.total) + (s.error ? ' · ошибка: ' + esc(s.error) : '') + '</div></div>';
  }
  VIEWS.aBroadcast = async (ctx) => {
    const [st, hist] = await Promise.all([ctx.api('/api/v2/admin/broadcast/status'), ctx.api('/api/v2/admin/broadcast/history')]);
    if (!ctx.alive()) return;
    const s = st.state || {};
    let html = backBtn() + '<div class="page-head"><h1>Рассылка</h1><p>Текст получат все пользователи бота. Отправка идёт в фоне — окно можно закрыть.</p></div>' +
      '<div id="bcProgress">' + broadcastProgress(s) + '</div>' +
      '<div class="card"><label class="field">Текст сообщения<textarea class="textarea" id="bcText" maxlength="4000" placeholder="Объявление для всех игроков"></textarea></label>' +
      '<button class="btn primary block" type="button" data-act="bc-send"' + (s.running ? ' disabled' : '') + '>Отправить всем</button></div>';
    const items = arr(hist.items);
    html += '<div class="card"><div class="card-head"><h3>История</h3></div>' + (items.length ? '<div class="list">' + items.map((x) => '<div class="item"><div class="grow"><div class="title">' + esc(x.type) + '</div><div class="sub">' + esc(fmtDate(x.created_at)) + '</div></div>' +
      pill(x.sent + '/' + x.total, 'ok') + (x.blocked ? pill('заблок. ' + x.blocked) : '') + (x.failed ? pill('ошибок ' + x.failed, 'bad') : '') + '</div>').join('') + '</div>' : '<div class="muted small">Рассылок ещё не было.</div>') + '</div>';
    ctx.set(html);
    if (s.running) addTimer(pollBroadcast, 2000);
  };
  async function pollBroadcast() {
    try {
      const st = await api('/api/v2/admin/broadcast/status', { timeout: 8000 });
      const box = document.getElementById('bcProgress');
      if (box) box.innerHTML = broadcastProgress(st.state || {});
      if (!(st.state && st.state.running)) { clearTimers(); reload(); }
    } catch (e) { /* следующий тик */ }
  }
  ACTIONS['bc-send'] = async (el) => {
    const t = document.getElementById('bcText');
    const text = t ? t.value.trim() : '';
    if (!text) { toast('Введите текст рассылки', 'bad'); return; }
    if (!(await confirmDialog({ title: 'Отправить всем пользователям?', text: text.length > 300 ? text.slice(0, 300) + '…' : text, ok: 'Отправить' }))) return;
    await mutate(el, () => api('/api/v2/admin/broadcast', { method: 'POST', body: { text } }), 'Рассылка запущена');
  };

  // ---------- MVP на главной ----------
  VIEWS.aMvp = async (ctx) => {
    const d = await ctx.api('/api/v2/admin/mvp-banner');
    if (!ctx.alive()) return;
    const m = d.manual || {};
    const cur = d.current;
    ctx.set(backBtn() + '<div class="page-head"><h1>MVP на главной</h1><p>Плашка «MVP матчей по выбору игроков» вверху главного экрана и в приветствии /start. ' +
      'Если ручную убрать, покажется победитель голосования за MVP последнего турнира.</p></div>' +
      '<div class="card"><div class="card-head"><h3>Сейчас на главной</h3>' + (cur ? pill(cur.source === 'manual' ? 'вручную' : 'по голосованию', cur.source === 'manual' ? 'info' : 'ok') : '') + '</div>' +
      (cur ? '<b>' + esc(cur.name) + '</b>' + (cur.caption ? '<div class="small muted">' + esc(cur.caption) + '</div>' : '') : '<div class="muted small">Плашки нет: голосований ещё не было, вручную не задано.</div>') + '</div>' +
      '<div class="card"><label class="field">Имя<input class="input" id="mvpName" maxlength="40" placeholder="Витёк" value="' + esc(m.name || '') + '"></label>' +
      '<label class="field">Подпись<input class="input" id="mvpCaption" maxlength="120" placeholder="Босс, легенда, начальник" value="' + esc(m.caption || '') + '"></label>' +
      '<button class="btn primary block" type="button" data-act="mvp-save">Поставить на главную</button>' +
      (d.manual ? '<button class="btn block" type="button" data-act="mvp-clear" style="margin-top:8px">Убрать ручную плашку</button>' : '') + '</div>' +
      '<div class="note">То же из бота: <code>/mvp Имя | подпись</code>, убрать — <code>/mvp off</code>.</div>');
  };
  ACTIONS['mvp-save'] = async (el) => {
    const name = (document.getElementById('mvpName') || {}).value || '';
    const caption = (document.getElementById('mvpCaption') || {}).value || '';
    if (!name.trim()) { toast('Укажите имя', 'bad'); return; }
    await mutate(el, () => api('/api/v2/admin/mvp-banner', { method: 'POST', body: { name: name.trim(), caption: caption.trim() } }), 'MVP на главной обновлён', { me: true });
  };
  ACTIONS['mvp-clear'] = async (el) => {
    await mutate(el, () => api('/api/v2/admin/mvp-banner', { method: 'DELETE' }), 'Ручная плашка убрана', { me: true });
  };

  // ---------- Администраторы ----------
  VIEWS.aAdmins = async (ctx) => {
    const data = await ctx.api('/api/v2/admin/admins');
    if (!ctx.alive()) return;
    const items = arr(data.items);
    ctx.set(backBtn() + '<div class="page-head"><h1>Администраторы</h1><p>ADMIN_IDS из .env — стартовый список; добавленные здесь хранятся в базе.</p></div>' +
      '<div class="card"><label class="field">Telegram ID нового администратора<div class="inline-form"><input class="input" id="newAdmin" inputmode="numeric" maxlength="16" placeholder="например, 123456789" data-enter="admin-add-input">' +
      '<button class="btn primary" type="button" data-act="admin-add-input">Добавить</button></div></label><div class="note">Узнать ID можно в карточке пользователя (раздел «Пользователи»).</div></div>' +
      '<div class="list">' + items.map((a) => '<div class="item"><span class="avatar round">' + ic('shield') + '</span><div class="grow"><div class="title">' + esc(a.name) + '</div><div class="sub">' + (a.username ? '@' + esc(a.username) + ' · ' : '') + 'TG ' + esc(a.telegram_id) + (a.bootstrap ? ' · из .env' : '') + '</div></div>' +
        (a.is_me ? pill('вы', 'info') : '<button class="btn small danger" type="button" data-act="admin-remove" data-tg="' + esc(a.telegram_id) + '" data-name="' + esc(a.name) + '">Удалить</button>') + '</div>').join('') + '</div>');
  };
  ACTIONS['admin-add-input'] = async (el) => {
    const i = document.getElementById('newAdmin');
    const v = i ? i.value.trim() : '';
    if (!/^\d{3,16}$/.test(v)) { toast('Введите числовой Telegram ID', 'bad'); return; }
    const btn = el.tagName === 'BUTTON' ? el : document.querySelector('[data-act="admin-add-input"]');
    await mutate(btn, () => api('/api/v2/admin/admins', { method: 'POST', body: { telegram_id: Number(v) } }), 'Администратор добавлен');
  };
  ACTIONS['admin-add'] = async (el) => {
    if (!(await confirmDialog({ title: 'Назначить администратором?', text: el.dataset.name + ' получит полный доступ к админ-панели.', ok: 'Назначить' }))) return;
    await mutate(el, () => api('/api/v2/admin/admins', { method: 'POST', body: { telegram_id: Number(el.dataset.tg) } }), 'Администратор добавлен');
  };
  ACTIONS['admin-remove'] = async (el) => {
    if (!(await confirmDialog({ title: 'Удалить администратора?', text: el.dataset.name + ' потеряет доступ к админ-панели.', ok: 'Удалить', danger: true }))) return;
    await mutate(el, () => api('/api/v2/admin/admins/' + el.dataset.tg, { method: 'DELETE' }), (r) => r.warning || 'Администратор удалён');
  };

  // ---------- AI-помощник ----------
  VIEWS.aAI = async (ctx) => {
    let status = null;
    try { status = await ctx.api('/api/v2/admin/ai/status'); } catch (err) { if (err.status === -1) return; }
    if (!ctx.alive()) return;
    const providers = status ? arr(status.providers) : [];
    ctx.set(backBtn() + '<div class="page-head"><h1>AI-помощник</h1><p>Отвечает по данным проекта через read-only запросы к базе. Ключи и токены AI не получает.</p></div>' +
      '<div class="card"><div class="chips">' + (status ? providers.map((p) => pill(p.name + ' · ' + p.model + (p.configured ? '' : ' · нет ключа'), p.configured ? 'ok' : 'bad')).join('') : pill('статус недоступен', 'bad')) + '</div>' +
      (status && !status.configured ? '<div class="warnbox" style="margin-top:10px">AI не настроен: укажите AI_API_KEY в .env сервера.</div>' : '') +
      (status && status.usage ? '<div class="note">Сегодня: вопросов в боте ' + esc(status.usage.bot_total) + ' из ' + esc(status.usage.bot_limit) + ' (на человека ' + esc(status.usage.bot_user_limit) +
        '), AI-проверок игроков ' + esc(status.usage.check_total) + ' из ' + esc(status.usage.check_limit) + '.</div>' : '') + '</div>' +
      '<div class="card"><div class="ai-log" id="aiLog"></div><div id="aiErr"></div><textarea class="textarea" id="aiQ" maxlength="4000" placeholder="Например: какие команды выше всех по среднему подтверждённому MMR?" data-enter="ai-send"></textarea>' +
      '<div class="actions"><button class="btn ghost" type="button" data-act="ai-clear">Очистить</button><button class="btn primary" type="button" data-act="ai-send">Спросить</button></div></div>');
    renderAi();
  };
  function renderAi() {
    const log = document.getElementById('aiLog');
    if (!log) return;
    const h = state.ui.aiHistory;
    log.innerHTML = h.length ? h.map((m) => '<div class="ai-msg' + (m.role === 'user' ? ' user' : '') + '"><div class="who">' + (m.role === 'user' ? 'Вы' : 'AI') + '</div>' + esc(m.content) + '</div>').join('') : '<div class="muted small">Задайте вопрос по текущим данным TI:IT TOP.</div>';
    log.scrollTop = log.scrollHeight;
  }
  ACTIONS['ai-clear'] = () => { state.ui.aiHistory = []; renderAi(); };
  ACTIONS['ai-send'] = async (el) => {
    const q = document.getElementById('aiQ');
    const question = q ? q.value.trim() : '';
    if (!question) { toast('Введите вопрос', 'bad'); return; }
    const btn = el.tagName === 'BUTTON' ? el : document.querySelector('[data-act="ai-send"]');
    const err = document.getElementById('aiErr');
    await withBusy(btn, async () => {
      const history = state.ui.aiHistory.slice(-8);
      state.ui.aiHistory.push({ role: 'user', content: question });
      renderAi();
      if (err) err.innerHTML = '';
      try {
        const r = await api('/api/v2/admin/ai/ask', { method: 'POST', body: { question, history }, timeout: 110000 });
        const meta = [r.provider, r.model].filter(Boolean).join(' · ');
        state.ui.aiHistory.push({ role: 'assistant', content: (r.answer || 'AI не вернул текст ответа.') + (meta ? '\n\n[' + meta + ']' : '') });
        if (q) q.value = '';
      } catch (e) {
        state.ui.aiHistory.pop();
        if (err) err.innerHTML = '<div class="inline-error">' + esc(e.message) + '</div>';
      }
      renderAi();
    });
  };

  // ---------- Диагностика ----------
  VIEWS.aDiagnostics = async (ctx) => {
    ctx.set(backBtn() + '<div class="page-head"><h1>Диагностика</h1><p>Проверка Steam, Dotabuff/OpenDota, STRATZ, прокси и внешнего IP. Запускается только по кнопке, до 60 секунд.</p></div>' +
      '<div class="card"><button class="btn primary block" type="button" data-act="diag-run">Запустить проверку</button><div id="diagOut" style="margin-top:10px"></div></div>');
  };
  ACTIONS['diag-run'] = async (el) => {
    const out = document.getElementById('diagOut');
    await withBusy(el, async () => {
      if (out) out.innerHTML = '<div class="muted small">Проверяю внешние сервисы…</div>';
      try {
        const r = await api('/api/v2/admin/diagnostics', { timeout: 75000 });
        if (out) out.innerHTML = (r.ok ? '' : '<div class="warnbox">Проверка завершилась с ошибкой.</div>') + '<pre class="pre">' + esc(r.text || '—') + '</pre>' +
          '<div class="note">Время: ' + esc(r.elapsed) + ' с · Mini App ' + esc(r.miniapp ? r.miniapp.version : VERSION) + ' · уведомления Telegram: ' + (r.miniapp && r.miniapp.telegram_notifications ? 'вкл' : 'выкл') + '</div>';
      } catch (e) {
        if (out) out.innerHTML = '<div class="inline-error">' + esc(e.message) + '</div>';
      }
    });
  };

  // =========================================================================
  // Старт: один минимальный запрос /api/v2/me, остальное — по разделам
  // =========================================================================
  const teamCardBase = VIEWS.teamCard;
  VIEWS.teamCard = async (ctx) => {
    const id = Number(ctx.params.id);
    const on = arr(state.me && state.me.follows).map(Number).indexOf(id) >= 0;
    const btn = '<button class="btn block' + (on ? '' : ' primary') + '" type="button" data-act="team-follow" data-id="' + esc(id) + '" data-on="' + (on ? '0' : '1') + '" style="margin-bottom:12px">' +
      ic('bell') + (on ? ' Вы болеете за команду — отписаться' : ' Болеть за команду') + '</button>';
    const set = ctx.set;
    ctx.set = (h) => set(h.replace(backBtn(), backBtn() + btn));
    return teamCardBase(ctx);
  };

  async function boot() {
    if (!INIT_DATA) {
      // SDK мог ещё не загрузиться — ждём до 3 секунд.
      for (let i = 0; i < 30 && !INIT_DATA; i += 1) {
        await new Promise((r) => setTimeout(r, 100));
        INIT_DATA = readInitData();
      }
    }
    if (!INIT_DATA) {
      $view.innerHTML = '<div class="boot">' + stateBox('Откройте Mini App из Telegram', 'Приложение работает внутри Telegram: нажмите кнопку «TI:IT TOP» в боте.') + '</div>';
      return;
    }
    try {
      state.me = await api('/api/v2/me', { timeout: 15000 });
      state.meAt = Date.now();
    } catch (err) {
      if (err.status === 401) { showSessionError(err); return; }
      $view.innerHTML = '<div class="boot">' + errorBox('Не удалось открыть TI:IT TOP', err, 'boot-retry') + '</div>';
      return;
    }
    const qs = new URLSearchParams(location.search);
    const start = qs.get('tab');
    if (!state.me.kind_chosen) { state.tab = 'home'; state.stack = [{ view: 'welcome', params: {} }]; render(); return; }
    switchTab(start && TAB_TITLES[start] ? start : 'home');
    const matchId = Number(qs.get('match'));
    if (matchId) go('matchChat', { id: matchId });
    const recId = qs.get('record');
    if (recId) go('record', { id: recId });
  }
  ACTIONS['boot-retry'] = () => { $view.innerHTML = '<div class="boot"><div class="spinner"></div><p>Подключаюсь…</p></div>'; boot(); };

  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    if (r && r.status === -1) { e.preventDefault(); return; }
    console.error('[TI:IT TOP] unhandled', r);
  });

  boot();
})();
