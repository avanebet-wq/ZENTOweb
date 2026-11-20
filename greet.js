/* ZENTO: приветствие на главной. «Привет» из пикселей превращается в «Hello» и обратно,
   рядом ник с «!», ниже «Вы, <ранг>».
   Слушает zento:session (auth.js) и zento:user-updated (profile.js). Данные: GET /api/profile/me.
   Без внешних библиотек, без inline-скриптов. */
(function () {
  'use strict';

  var root = document.getElementById('greet');
  var canvas = document.getElementById('greet-canvas');
  if (!root || !canvas || !canvas.getContext) return;

  var els = {
    name: document.getElementById('greet-name'),
    sr: document.getElementById('greet-sr'),
    badge: document.getElementById('rank-badge'),
    lvl: document.getElementById('rank-lvl'),
    rankName: document.getElementById('rank-name'),
    line: root.querySelector('.greet-line')
  };
  var ctx = canvas.getContext('2d');
  var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  var WORD_A = 'Привет';
  var WORD_B = 'Hello';
  var FAMILY = '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", system-ui, "Segoe UI", Roboto, sans-serif';
  var WHITE = [255, 255, 255];
  var BLUE = [64, 156, 255]; // --tint-text
  // Этапы анимации, мс: текст, в пиксели, Привет→Hello, пауза, Hello→Привет, в текст
  var T_HOLD = 700, T_PIX = 300, T_MORPH = 1000, T_STAY = 1300, T_BACK = 1000, T_TEXT = 300;

  var state = { user: null, size: 48, w: 0, h: 0, dpr: 1, cell: 4, a: [], b: [], raf: 0, done: false, token: 0 };

  function tierOf(level) { return Math.min(5, Math.ceil(level / 3)); } // 15 уровней = 5 ступеней по 3

  /* ---------- Ранг ---------- */
  function paintRank(rank) {
    if (!rank) return;
    els.badge.setAttribute('data-tier', String(tierOf(rank.level)));
    els.lvl.textContent = String(rank.level);
    els.rankName.textContent = rank.name;
  }

  function paintName(nick) {
    els.name.textContent = nick + '!';
    els.sr.textContent = 'Привет, ' + nick + '!';
  }

  /* ---------- Пиксельная анимация ---------- */
  function fontCss(px) { return '700 ' + px + 'px ' + FAMILY; }

  // Точки пикселей слова: клетки cell × cell, закрашенные в тексте
  function sampleWord(text, px) {
    var off = document.createElement('canvas');
    off.width = state.w; off.height = state.h;
    var c = off.getContext('2d', { willReadFrequently: true });
    c.font = fontCss(px);
    var width = c.measureText(text).width;
    if (width > state.w) { px = px * state.w / width; c.font = fontCss(px); width = state.w; }
    c.textBaseline = 'alphabetic';
    c.fillStyle = '#000';
    c.fillText(text, (state.w - width) / 2, state.h * 0.78);
    var data = c.getImageData(0, 0, state.w, state.h).data;
    var pts = [];
    var cell = state.cell;
    for (var x = 0; x + cell <= state.w; x += cell) {
      for (var y = 0; y + cell <= state.h; y += cell) {
        var cx = Math.floor(x + cell / 2), cy = Math.floor(y + cell / 2);
        if (data[(cy * state.w + cx) * 4 + 3] > 110) pts.push({ x: x, y: y });
      }
    }
    return pts; // порядок: слева направо, сверху вниз
  }

  function measure() {
    var cs = window.getComputedStyle(els.line);
    var px = parseFloat(cs.fontSize) || 48;
    var probe = document.createElement('canvas').getContext('2d');
    probe.font = fontCss(px);
    var width = Math.ceil(probe.measureText(WORD_A).width) + 4;
    state.size = px;
    state.w = width;
    state.h = Math.ceil(px * 1.2);
    state.cell = Math.max(3, Math.round(px / 13));
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(state.w * state.dpr);
    canvas.height = Math.round(state.h * state.dpr);
    canvas.style.width = state.w + 'px';
    canvas.style.height = state.h + 'px';
    // Базовая линия canvas-слова совпадает с базовой линией ника рядом
    canvas.style.transform = 'translateY(' + Math.round(state.h * 0.22) + 'px)';
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    state.a = sampleWord(WORD_A, px);
    state.b = sampleWord(WORD_B, px);
  }

  function drawText(alpha) {
    ctx.clearRect(0, 0, state.w, state.h);
    ctx.globalAlpha = alpha;
    ctx.font = fontCss(state.size);
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(WORD_A, 2, state.h * 0.78);
    ctx.globalAlpha = 1;
  }

  function ease(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function mix(t) {
    return 'rgb(' + Math.round(lerp(WHITE[0], BLUE[0], t)) + ',' + Math.round(lerp(WHITE[1], BLUE[1], t)) + ',' +
      Math.round(lerp(WHITE[2], BLUE[2], t)) + ')';
  }

  // Пары «откуда → куда» для морфинга: точки слева направо переходят в точки слева направо
  var jit = [];
  function pairs(from, to) {
    var n = Math.max(from.length, to.length), out = [];
    for (var i = 0; i < n; i++) {
      var f = from[Math.floor(i * from.length / n)], t = to[Math.floor(i * to.length / n)];
      if (!jit[i]) jit[i] = { d: Math.random() * 0.4, o: (Math.random() - 0.5) * 16 };
      out.push({ f: f, t: t, d: jit[i].d, o: jit[i].o });
    }
    return out;
  }

  function drawDots(list, progress, colorT, alpha) {
    ctx.clearRect(0, 0, state.w, state.h);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = mix(colorT);
    var s = state.cell - 1;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var q = ease(Math.max(0, Math.min(1, (progress - p.d) / (1 - p.d))));
      var x = lerp(p.f.x, p.t.x, q);
      var y = lerp(p.f.y, p.t.y, q) + Math.sin(Math.PI * q) * p.o;
      ctx.fillRect(Math.round(x), Math.round(y), s, s);
    }
    ctx.globalAlpha = 1;
  }

  function stop() { if (state.raf) window.cancelAnimationFrame(state.raf); state.raf = 0; }

  function play() {
    stop();
    state.done = false;
    measure();
    if (reduce && reduce.matches) { drawText(1); state.done = true; return; }
    var toB = pairs(state.a, state.b);
    var toA = pairs(state.b, state.a);
    var still = state.a.map(function (p) { return { f: p, t: p, d: 0, o: 0 }; });
    var token = ++state.token;
    var t1 = T_HOLD, t2 = t1 + T_PIX, t3 = t2 + T_MORPH, t4 = t3 + T_STAY, t5 = t4 + T_BACK, t6 = t5 + T_TEXT;
    var start = null;
    function frame(now) {
      if (token !== state.token) return;
      if (start === null) start = now;
      var t = now - start;
      if (t < t1) drawText(1);
      else if (t < t2) { var k = (t - t1) / T_PIX; drawText(1 - k); drawDots(still, 1, 0, k); drawTextOver(1 - k); }
      else if (t < t3) drawDots(toB, (t - t2) / T_MORPH, ease((t - t2) / T_MORPH), 1);
      else if (t < t4) drawDots(toB, 1, 1, 1);
      else if (t < t5) drawDots(toA, (t - t4) / T_BACK, 1 - ease((t - t4) / T_BACK), 1);
      else if (t < t6) { var m = (t - t5) / T_TEXT; drawDots(toA, 1, 0, 1 - m); drawTextOver(m); }
      else { drawText(1); state.done = true; state.raf = 0; return; }
      state.raf = window.requestAnimationFrame(frame);
    }
    state.raf = window.requestAnimationFrame(frame);
  }

  // Текст поверх точек (переход «текст ⇄ пиксели»), без очистки холста
  function drawTextOver(alpha) {
    ctx.globalAlpha = alpha;
    ctx.font = fontCss(state.size);
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(WORD_A, 2, state.h * 0.78);
    ctx.globalAlpha = 1;
  }

  /* ---------- Данные и показ ---------- */
  function startWhenCapsuleGone(then) {
    // Капелька входа закрывает верх страницы на ~3 с: ждём, пока она уйдёт (не дольше 4 с)
    var cap = document.getElementById('capsule');
    var waited = 0;
    (function check() {
      if (!cap || !cap.classList.contains('is-on') || waited > 4000) { then(); return; }
      waited += 200;
      window.setTimeout(check, 200);
    })();
  }

  function show(user) {
    state.user = user;
    paintName(user.nick);
    paintRank(user.rank);
    root.hidden = false;
    root.classList.remove('is-ready');
    window.requestAnimationFrame(function () { root.classList.add('is-ready'); });
    var mine = state.token + 1;
    measure();
    drawText(1); // до старта анимации слово уже на месте, макет не прыгает
    startWhenCapsuleGone(function () { if (state.user === user && mine > 0) play(); });
  }

  function load() {
    var request = ++state.token;
    window.fetch('/api/profile/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (d) {
        if (request !== state.token) return;
        if (!d || d.ok !== true || !d.user) { hide(); return; }
        show(d.user);
      })
      .catch(function () { /* без данных блок просто не показываем */ });
  }

  function hide() {
    stop();
    state.token++;
    state.user = null;
    root.hidden = true;
    root.classList.remove('is-ready');
  }

  document.addEventListener('zento:session', function (e) {
    var d = e && e.detail;
    if (d && d.state === 'in') load(); else hide();
  });
  document.addEventListener('zento:user-updated', function (e) {
    var u = e && e.detail && e.detail.user;
    if (u && u.nick && state.user) { state.user.nick = u.nick; paintName(u.nick); }
  });
  var resizeTimer = 0;
  window.addEventListener('resize', function () {
    if (!state.user || !state.done) return;
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () { measure(); drawText(1); }, 150);
  });
  if (window.zentoSession && window.zentoSession.state === 'in') load();
})();
