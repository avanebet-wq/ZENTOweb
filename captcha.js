/* captcha.js: виджет капчи-пазла ZENTO. Без сборки и библиотек.
 * Открытие: window.ZentoCaptcha.open({ onTicket(ticket), onCancel() })
 * Данные с сервера в HTML не вставляются: только textContent и createElement. */
(function () {
  'use strict';

  if (window.ZentoCaptcha) return;

  const API_NEW = '/api/captcha/new';
  const API_VERIFY = '/api/captcha/verify';
  const IMAGE_RE = /^\/assets\/captcha\/\d{2}\.svg$/;
  const CELL_RATIO = 0.35;      // магнит: отпустили ближе, чем 35% ячейки
  const MOVE_MS = 260;          // длительность движения кусочка
  const SUCCESS_MS = 600;       // сколько показываем галочку до закрытия
  const DEFAULT_EXPIRES = 120;  // секунд, если сервер не прислал expires_in
  const POS = ['0 0', '100% 0', '0 100%', '100% 100%'];
  const CELL_NAMES = [
    'верхний левый угол', 'верхний правый угол',
    'нижний левый угол', 'нижний правый угол'
  ];
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const M = {
    close: 'Закрыть',
    title: 'Соберите картинку',
    hint: 'Перетащите кусочки на свои места',
    keys: 'Без мыши: Tab к кусочку, Enter взять, стрелки выбрать место, Enter положить, Esc отменить.',
    loading: 'Загружаем картинку…',
    newImage: 'Другая картинка',
    retryNew: 'Попробовать ещё раз',
    retryVerify: 'Проверить ещё раз',
    network: 'Нет связи, попробуйте ещё раз',
    apiDown: 'Сейчас не получается загрузить картинку. Попробуйте позже.',
    tooManyRequests: 'Слишком часто. Подождите немного и попробуйте снова.',
    tooManyAttempts: 'Слишком много попыток. Подождите немного и попробуйте снова.',
    expired: 'Время вышло. Загрузили новую картинку.',
    wrong: 'Кусочки стоят не на тех местах. Загрузили новую картинку.',
    generic: 'Что-то пошло не так. Попробуйте ещё раз.',
    incomplete: 'Положите все четыре кусочка на места.',
    wrongPlace: 'Не туда. Попробуйте другое место.',
    cancelled: 'Выбор отменён.',
    checking: 'Проверяем…',
    success: 'Готово! Картинка собрана.'
  };

  let active = null; // открытый сейчас виджет (на странице один)

  function noop() {}

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function svgEl(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs).forEach((k) => node.setAttribute(k, attrs[k]));
    return node;
  }

  function icon(d) {
    const s = svgEl('svg', {
      viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
      'stroke-linecap': 'round', 'aria-hidden': 'true', focusable: 'false'
    });
    s.appendChild(svgEl('path', { d }));
    return s;
  }

  function checkBadge() {
    const wrap = el('span', 'zc-check');
    wrap.setAttribute('aria-hidden', 'true');
    const s = svgEl('svg', {
      viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.6',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round'
    });
    s.appendChild(svgEl('path', { d: 'M5 12.5l4.5 4.5L19 7.5', pathLength: '1' }));
    wrap.appendChild(s);
    return wrap;
  }

  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function shuffle(list) {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      const t = list[i];
      list[i] = list[j];
      list[j] = t;
    }
    return list;
  }

  function centerOf(node) {
    const r = node.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function dist(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // Запрос к API. Сетевой сбой приходит как отказ промиса.
  function request(url, body) {
    const init = { cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.method = 'POST';
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return fetch(url, init).then((res) => res.text().then((text) => {
      let data = null;
      try { data = JSON.parse(text); } catch (e) { data = null; }
      return { status: res.status, data };
    }));
  }

  // Проверяем ответ /api/captcha/new, прежде чем что-то рисовать.
  function parseChallenge(d) {
    if (!d || d.ok !== true) return null;
    if (typeof d.challenge_id !== 'string' || !d.challenge_id || d.challenge_id.length > 256) return null;
    if (typeof d.image !== 'string' || !IMAGE_RE.test(d.image)) return null;
    if (!Array.isArray(d.tiles) || d.tiles.length !== 4) return null;
    const seenPiece = [false, false, false, false];
    const seenToken = new Set();
    const tiles = [];
    for (const t of d.tiles) {
      if (!t || typeof t.token !== 'string' || !t.token || t.token.length > 256) return null;
      if (!Number.isInteger(t.piece) || t.piece < 0 || t.piece > 3 || seenPiece[t.piece]) return null;
      if (seenToken.has(t.token)) return null;
      seenPiece[t.piece] = true;
      seenToken.add(t.token);
      tiles.push({ token: t.token, piece: t.piece });
    }
    const expires = Number(d.expires_in) > 0 ? Number(d.expires_in) : DEFAULT_EXPIRES;
    return { id: d.challenge_id, image: d.image, tiles, expires };
  }

  // ----- Сборка диалога -----

  function build(st) {
    const dlg = el('dialog', 'zc-dialog');
    dlg.setAttribute('aria-labelledby', 'zc-title');
    dlg.setAttribute('aria-describedby', 'zc-hint');

    const card = el('div', 'zc-card');

    const head = el('div', 'zc-head');
    const title = el('h2', 'zc-title', M.title);
    title.id = 'zc-title';
    const closeBtn = el('button', 'zc-icon-btn');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', M.close);
    closeBtn.appendChild(icon('M6 6l12 12M18 6L6 18'));
    head.append(title, closeBtn);

    const hint = el('p', 'zc-hint', M.hint);
    hint.id = 'zc-hint';
    const keys = el('p', 'zc-keys', M.keys);
    const status = el('p', 'zc-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    const puzzle = el('div', 'zc-puzzle');
    const loadingEl = el('p', 'zc-loading', M.loading);
    const board = el('div', 'zc-board');
    board.setAttribute('role', 'group');
    board.setAttribute('aria-label', 'Доска 2 на 2');
    board.tabIndex = -1;
    const cells = [];
    for (let i = 0; i < 4; i++) {
      const cell = el('div', 'zc-cell');
      board.appendChild(cell);
      cells.push(cell);
    }
    const tray = el('div', 'zc-tray');
    tray.setAttribute('role', 'group');
    tray.setAttribute('aria-label', 'Кусочки картинки');
    const slots = [];
    for (let i = 0; i < 4; i++) {
      const slot = el('div', 'zc-slot');
      tray.appendChild(slot);
      slots.push(slot);
    }
    puzzle.append(loadingEl, board, tray);

    const foot = el('div', 'zc-foot');
    const newBtn = el('button', 'zc-btn zc-btn-ghost', M.newImage);
    newBtn.type = 'button';
    const retryBtn = el('button', 'zc-btn zc-btn-primary');
    retryBtn.type = 'button';
    retryBtn.hidden = true;
    foot.append(newBtn, retryBtn);

    card.append(head, hint, keys, status, puzzle, foot);
    dlg.appendChild(card);

    Object.assign(st, { dlg, closeBtn, status, loadingEl, board, tray, cells, slots, newBtn, retryBtn });

    closeBtn.addEventListener('click', () => {
      if (st.phase !== 'done') st.dlg.close();
    });
    newBtn.addEventListener('click', () => {
      if (st.phase === 'ready') loadChallenge(st, '');
    });
    retryBtn.addEventListener('click', () => {
      if (st.retryMode === 'verify') submit(st);
      else if (st.retryMode === 'new') loadChallenge(st, '');
    });
    dlg.addEventListener('cancel', (e) => {
      // Esc сначала отменяет выбранный кусочек, а не закрывает окно
      if (st.selected) {
        e.preventDefault();
        clearSelection(st, M.cancelled);
      } else if (st.phase === 'done') {
        e.preventDefault();
      }
    });
    dlg.addEventListener('close', () => onClosed(st));
  }

  function onClosed(st) {
    if (st.gone) return;
    st.gone = true;
    if (active === st) active = null;
    clearTimeout(st.expTimer);
    clearTimeout(st.okTimer);
    st.dlg.remove();
    const opener = st.opener;
    if (opener && opener.isConnected && typeof opener.focus === 'function') {
      opener.focus();
    }
    if (!st.resolved) st.onCancel();
  }

  // ----- Открытие и задание -----

  function open(options) {
    if (active) return;
    const opts = options || {};
    const st = {
      onTicket: typeof opts.onTicket === 'function' ? opts.onTicket : noop,
      onCancel: typeof opts.onCancel === 'function' ? opts.onCancel : noop,
      opener: document.activeElement,
      phase: 'loading',   // loading | ready | checking | done
      retryMode: null,    // null | 'new' | 'verify'
      challenge: null,
      tiles: [],          // {token, piece, index, slot, node, placed, moving, drag}
      cells: [],
      slots: [],
      selected: null,
      target: 0,
      placed: 0,
      dragging: null,
      seq: 0,
      expTimer: null,
      okTimer: null,
      resolved: false,
      gone: false
    };
    active = st;
    build(st);
    document.body.appendChild(st.dlg);
    st.dlg.showModal();
    st.closeBtn.focus();
    loadChallenge(st, '');
  }

  function setStatus(st, text, isError) {
    st.status.textContent = text || '';
    st.status.classList.toggle('is-error', !!isError);
  }

  function updateUi(st) {
    const busy = st.phase !== 'ready';
    st.newBtn.disabled = busy;
    st.retryBtn.hidden = !st.retryMode;
    st.retryBtn.textContent = st.retryMode === 'verify' ? M.retryVerify : M.retryNew;
    st.retryBtn.disabled = busy;
    st.loadingEl.hidden = st.phase !== 'loading';
    const showPuzzle = !!st.challenge && st.phase !== 'loading';
    st.board.hidden = !showPuzzle;
    st.tray.hidden = !showPuzzle;
  }

  function loadChallenge(st, msg) {
    if (st.gone || st.phase === 'done') return;
    clearTimeout(st.expTimer);
    const id = ++st.seq;
    st.phase = 'loading';
    st.retryMode = null;
    resetBoard(st);
    setStatus(st, msg, !!msg);
    updateUi(st);
    request(API_NEW).then((r) => {
      if (id !== st.seq || st.gone) return;
      const c = r.status === 200 ? parseChallenge(r.data) : null;
      if (!c) {
        failNew(st, r.status === 429 ? M.tooManyRequests : M.apiDown);
        return;
      }
      setupChallenge(st, c);
    }, () => {
      if (id !== st.seq || st.gone) return;
      failNew(st, M.network);
    });
  }

  function failNew(st, message) {
    st.phase = 'ready';
    st.challenge = null;
    st.retryMode = 'new';
    setStatus(st, message, true);
    updateUi(st);
  }

  function resetBoard(st) {
    st.challenge = null;
    st.tiles = [];
    st.placed = 0;
    st.selected = null;
    st.target = 0;
    st.dragging = null;
    st.cells.forEach((c) => {
      clearChildren(c);
      c.classList.remove('is-target', 'is-near', 'is-shake');
    });
    st.slots.forEach(clearChildren);
    st.board.classList.remove('is-success');
  }

  function setupChallenge(st, c) {
    st.challenge = c;
    shuffle(c.tiles.slice()).forEach((t, i) => {
      const p = {
        token: t.token, piece: t.piece, index: i, slot: st.slots[i],
        node: null, placed: false, moving: false, drag: null
      };
      p.node = makePiece(st, p, c.image);
      p.slot.appendChild(p.node);
      st.tiles.push(p);
    });
    st.phase = 'ready';
    updateUi(st);
    if (!st.dlg.contains(document.activeElement) && st.tiles[0]) {
      st.tiles[0].node.focus({ preventScroll: true });
    }
    st.expTimer = setTimeout(() => {
      if (st.phase === 'ready' && st.challenge) loadChallenge(st, M.expired);
    }, c.expires * 1000);
  }

  function makePiece(st, p, image) {
    const node = el('div', 'zc-piece');
    node.setAttribute('role', 'button');
    node.setAttribute('aria-label', 'Кусочек ' + (p.index + 1) + ' из 4');
    node.setAttribute('aria-pressed', 'false');
    node.tabIndex = 0;
    node.style.backgroundImage = 'url("' + image + '")';
    node.style.backgroundPosition = POS[p.piece];
    node.addEventListener('pointerdown', (e) => onPointerDown(st, p, e));
    node.addEventListener('pointermove', (e) => onPointerMove(st, p, e));
    node.addEventListener('pointerup', (e) => onPointerEnd(st, p, e, false));
    node.addEventListener('pointercancel', (e) => onPointerEnd(st, p, e, true));
    node.addEventListener('keydown', (e) => onKey(st, p, e));
    node.addEventListener('blur', () => {
      if (st.selected === p) clearSelection(st, '');
    });
    node.addEventListener('dragstart', (e) => e.preventDefault());
    return node;
  }

  // ----- Перетаскивание мышью и пальцем -----

  function onPointerDown(st, p, e) {
    if (st.phase !== 'ready' || p.placed || p.moving || st.dragging) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    st.dragging = p;
    p.drag = { id: e.pointerId, x: e.clientX, y: e.clientY };
    if (st.selected) clearSelection(st, '');
    try { p.node.setPointerCapture(e.pointerId); } catch (err) { /* не критично */ }
    p.node.focus({ preventScroll: true });
    p.node.classList.add('is-dragging');
    p.node.style.transition = 'none';
  }

  function onPointerMove(st, p, e) {
    if (!p.drag || e.pointerId !== p.drag.id) return;
    const dx = e.clientX - p.drag.x;
    const dy = e.clientY - p.drag.y;
    p.node.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(1.06)';
    st.cells[p.piece].classList.toggle('is-near', isNear(st, p));
  }

  function onPointerEnd(st, p, e, cancelled) {
    if (!p.drag || e.pointerId !== p.drag.id) return;
    p.drag = null;
    st.dragging = null;
    try { p.node.releasePointerCapture(e.pointerId); } catch (err) { /* уже снят */ }
    p.node.classList.remove('is-dragging');
    st.cells.forEach((c) => c.classList.remove('is-near'));

    if (!cancelled && isNear(st, p)) {
      p.node.style.transition = '';
      place(st, p);
      return;
    }
    const wrong = !cancelled ? nearWrongCell(st, p) : -1;
    p.moving = true;
    moveNode(p.node, p.slot, true, () => { p.moving = false; });
    if (wrong >= 0) {
      shake(st.cells[wrong]);
      setStatus(st, M.wrongPlace, true);
    }
  }

  function isNear(st, p) {
    const target = st.cells[p.piece];
    const r = target.getBoundingClientRect();
    return dist(centerOf(p.node), centerOf(target)) < r.width * CELL_RATIO;
  }

  function nearWrongCell(st, p) {
    const center = centerOf(p.node);
    for (let i = 0; i < 4; i++) {
      if (i === p.piece) continue;
      const r = st.cells[i].getBoundingClientRect();
      if (dist(center, centerOf(st.cells[i])) < r.width * CELL_RATIO) return i;
    }
    return -1;
  }

  // Переносит кусочек в новый контейнер; при animate плавно от старого места.
  function moveNode(node, parent, animate, done) {
    const from = node.getBoundingClientRect();
    parent.appendChild(node);
    node.style.transition = 'none';
    node.style.transform = '';
    const to = node.getBoundingClientRect();
    if (!animate || reduced() || !from.width || !to.width) {
      node.style.transition = '';
      done();
      return;
    }
    const scale = from.width / to.width;
    const dx = from.left - to.left;
    const dy = from.top - to.top;
    node.style.transformOrigin = '0 0';
    node.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + scale + ')';
    void node.offsetWidth; // перерисовка, чтобы переход сработал
    node.style.transition = 'transform ' + MOVE_MS + 'ms cubic-bezier(0.16, 1, 0.3, 1)';
    node.style.transform = 'translate(0px, 0px) scale(1)';
    setTimeout(() => {
      node.style.transition = '';
      node.style.transform = '';
      node.style.transformOrigin = '';
      done();
    }, MOVE_MS + 30);
  }

  function place(st, p) {
    p.placed = true;
    p.moving = true;
    p.node.classList.add('is-placed');
    p.node.tabIndex = -1;
    p.node.setAttribute('aria-pressed', 'false');
    p.node.setAttribute('aria-label', 'Кусочек на месте');
    st.placed += 1;
    moveNode(p.node, st.cells[p.piece], true, () => { p.moving = false; });
    if (st.placed === 4) {
      setStatus(st, 'Есть! Все четыре кусочка на месте.');
      setTimeout(() => submit(st), MOVE_MS);
    } else {
      setStatus(st, 'Есть! На месте: ' + st.placed + ' из 4.');
    }
    focusNext(st);
  }

  function focusNext(st) {
    const next = st.tiles.find((t) => !t.placed);
    if (next && st.phase === 'ready') next.node.focus({ preventScroll: true });
    else st.board.focus({ preventScroll: true });
  }

  function shake(node) {
    node.classList.remove('is-shake');
    void node.offsetWidth;
    node.classList.add('is-shake');
    setTimeout(() => node.classList.remove('is-shake'), 400);
  }

  // ----- Клавиатура -----

  function occupiedCells(st) {
    const arr = [false, false, false, false];
    st.tiles.forEach((t) => { if (t.placed) arr[t.piece] = true; });
    return arr;
  }

  function select(st, p) {
    if (st.selected) clearSelection(st, '');
    st.selected = p;
    p.node.classList.add('is-selected');
    p.node.setAttribute('aria-pressed', 'true');
    const free = occupiedCells(st).indexOf(false);
    st.target = free >= 0 ? free : 0;
    showTarget(st);
  }

  function showTarget(st) {
    st.cells.forEach((c, i) => c.classList.toggle('is-target', !!st.selected && i === st.target));
    if (st.selected) {
      setStatus(st, 'Кусочек ' + (st.selected.index + 1) + ' из 4. Место: ' +
        CELL_NAMES[st.target] + '. Enter положить, Esc отменить.');
    }
  }

  function clearSelection(st, msg) {
    if (!st.selected) return;
    const p = st.selected;
    st.selected = null;
    p.node.classList.remove('is-selected');
    p.node.setAttribute('aria-pressed', 'false');
    st.cells.forEach((c) => c.classList.remove('is-target'));
    if (msg) setStatus(st, msg);
  }

  function moveTarget(st, key) {
    let row = st.target >> 1;
    let col = st.target & 1;
    if (key === 'ArrowLeft') col = 0;
    else if (key === 'ArrowRight') col = 1;
    else if (key === 'ArrowUp') row = 0;
    else row = 1;
    st.target = row * 2 + col;
    showTarget(st);
  }

  function placeSelected(st) {
    const p = st.selected;
    if (!p) return;
    if (st.target === p.piece) {
      clearSelection(st, '');
      place(st, p);
    } else {
      shake(st.cells[st.target]);
      setStatus(st, M.wrongPlace, true);
    }
  }

  function onKey(st, p, e) {
    if (st.phase !== 'ready' || p.placed || p.moving) return;
    const k = e.key;
    const isEnter = k === 'Enter' || k === ' ' || k === 'Spacebar';
    if (!st.selected) {
      if (isEnter) {
        e.preventDefault();
        select(st, p);
      }
      return;
    }
    if (st.selected !== p) return;
    if (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'ArrowUp' || k === 'ArrowDown') {
      e.preventDefault();
      moveTarget(st, k);
    } else if (isEnter) {
      e.preventDefault();
      placeSelected(st);
    } else if (k === 'Escape' || k === 'Esc') {
      e.preventDefault();
      e.stopPropagation();
      clearSelection(st, M.cancelled);
    }
  }

  // ----- Проверка и результат -----

  function submit(st) {
    if (st.gone || st.phase !== 'ready' || st.placed !== 4 || !st.challenge) return;
    st.phase = 'checking';
    st.retryMode = null;
    const challengeId = st.challenge.id;
    // Все кусочки стоят на своих местах: ячейка = номер части
    const placements = Object.create(null);
    st.tiles.forEach((t) => { placements[t.token] = t.piece; });
    const seq = ++st.seq;
    setStatus(st, M.checking);
    updateUi(st);
    request(API_VERIFY, { challenge_id: challengeId, placements }).then((r) => {
      if (seq !== st.seq || st.gone) return;
      const d = r.data;
      if (r.status === 200 && d && d.ok === true && typeof d.ticket === 'string' && d.ticket) {
        success(st, d.ticket);
        return;
      }
      verifyFailed(st, r);
    }, () => {
      if (seq !== st.seq || st.gone) return;
      st.phase = 'ready';
      st.retryMode = 'verify';
      setStatus(st, M.network, true);
      updateUi(st);
    });
  }

  function verifyFailed(st, r) {
    const code = r.data && typeof r.data.error === 'string' ? r.data.error : '';
    if (code === 'captcha_incomplete') {
      st.phase = 'ready';
      setStatus(st, M.incomplete, true);
      updateUi(st);
      return;
    }
    let msg = M.generic;
    if (code === 'captcha_wrong') msg = M.wrong;
    else if (code === 'captcha_expired') msg = M.expired;
    else if (code === 'too_many_attempts' || r.status === 429) msg = M.tooManyAttempts;
    loadChallenge(st, msg);
  }

  function success(st, ticket) {
    st.phase = 'done';
    st.retryMode = null;
    clearTimeout(st.expTimer);
    st.board.classList.add('is-success');
    st.board.appendChild(checkBadge());
    setStatus(st, M.success);
    updateUi(st);
    st.okTimer = setTimeout(() => {
      st.resolved = true;
      st.dlg.close();
      st.onTicket(ticket);
    }, SUCCESS_MS);
  }

  window.ZentoCaptcha = { open };
})();
