/* ZENTO: капли с фигурками вокруг аватара в профиле (вариант «Стекло»).
   Подключается к шапке профиля (profile.js): две прозрачные канвы, одна под аватаром, другая над ним.
   Капли тянутся и вращаются по орбитам, внутри пузырьки и фигурка, свечение берётся из цвета фигурки,
   на кромке аватара играют блики: чем ближе капля, тем ярче. Без внешних библиотек и inline-скриптов.
   Свечение и цветные блики здесь сделаны по просьбе пользователя (исключение из правил дизайна).

   API (window.ZentoDrops):
     mount({ hero, back, front, avatar, who, onPick })  один раз при загрузке страницы
     setFigures([{ src, name, rarity, tint, data }])     фигурки (полученные достижения)
     refreshAvatar()                                       аватар сменился: обновить отражение и блики
     setActive(true|false)                                 запустить / остановить анимацию (пока профиль открыт)
     resize()                                              пересчитать размеры (после показа профиля) */
(function () {
  'use strict';
  var TAU = Math.PI * 2;
  var WHITE = [255, 255, 255];
  var reduceMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  // Вариант «Стекло»: крупные стеклянные капли, линза с перевёрнутым аватаром, радужная дисперсия по краю
  var CFG = { rx: [1.45, 2.15], flat: [0.30, 0.70], size: [32, 42], speed: [0.040, 0.070], lens: 0.95, lensM: 1.65, stretch: 0.36, tail: 0.14, bubbles: 5, aura: 0.55 };
  // Запасные оттенки по редкости (если не вышло взять цвет из самой картинки)
  var TIER_TINT = ['#B8BCC8', '#6596E2', '#4D69CD', '#8947FF', '#D42BE6', '#EB4B4B', '#CAAB05', '#B8A66B'];

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function rgb(h) { var n = parseInt(h.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
  function rgba(c, a) { return 'rgba(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ',' + a + ')'; }
  function mixc(a, b, t) { return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)]; }
  function angDiff(a, b) { var d = (a - b) % TAU; if (d > Math.PI) d -= TAU; if (d < -Math.PI) d += TAU; return d; }
  function mulberry(seed) { var s = seed >>> 0; return function () { s += 0x6D2B79F5; var t = s; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }

  var hero, cvB, cvF, ctxB, ctxF, avatarEl, whoEl, onPick;
  var W = 0, H = 0, dpr = 1;
  var ava = { cx: 0, cy: 0, R: 0 };
  var textBox = { l: 0, r: 0, t: 0, b: 0 };
  var scCv = document.createElement('canvas');   // аватар для отражения в каплях
  var rimCv = document.createElement('canvas');  // грани аватара (статичные)
  var state = { t: 0, light: -2.2, lightT: -2.2, active: false };
  var figs = [], drops = [], states = [];
  var raf = 0, last = 0, mounted = false, swallow = false;

  /* ---------- Размеры и статичные слои ---------- */
  function measure() {
    if (!mounted) return false;
    var w = hero.clientWidth, h = hero.clientHeight;
    if (!w || !h) return false;
    W = w; H = h;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    ava.R = avatarEl.offsetWidth / 2;
    ava.cx = avatarEl.offsetLeft + avatarEl.offsetWidth / 2;
    ava.cy = avatarEl.offsetTop + avatarEl.offsetHeight / 2;
    if (whoEl) {
      textBox.l = whoEl.offsetLeft + whoEl.offsetWidth / 2 - 104; textBox.r = whoEl.offsetLeft + whoEl.offsetWidth / 2 + 104;
      textBox.t = whoEl.offsetTop - 6; textBox.b = whoEl.offsetTop + whoEl.offsetHeight + 6;
    }
    [cvB, cvF, scCv, rimCv].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
    renderRim();
    renderAvatarSprite();
    buildDrops();
    return true;
  }

  function renderRim() {
    var c = rimCv.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, W, H);
    var seg = 28;
    for (var i = 0; i < seg; i++) {
      var a0 = i / seg * TAU, a1 = (i + 0.92) / seg * TAU;
      c.beginPath(); c.arc(ava.cx, ava.cy, ava.R - 1, a0, a1);
      c.strokeStyle = 'rgba(255,255,255,' + (i % 2 ? 0.10 : 0.26) + ')'; c.lineWidth = 2.2; c.stroke();
    }
    c.beginPath(); c.arc(ava.cx, ava.cy, ava.R + 2.5, 0, TAU); c.strokeStyle = 'rgba(255,255,255,.08)'; c.lineWidth = 1; c.stroke();
  }

  function avatarSource() {
    var node = avatarEl.querySelector('.pf-view > img, .pf-view > canvas');
    if (node && node.tagName === 'IMG') { return node.complete && node.naturalWidth ? node : null; }
    if (node && node.tagName === 'CANVAS') return node;
    return null;
  }

  function renderAvatarSprite() {
    if (!W) return;
    var c = scCv.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, W, H);
    var R = ava.R, cx = ava.cx, cy = ava.cy, src = avatarSource();
    c.save();
    c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.clip();
    if (src) {
      var sw = src.naturalWidth || src.width, sh = src.naturalHeight || src.height;
      var s = Math.max(2 * R / sw, 2 * R / sh);
      c.drawImage(src, cx - sw * s / 2, cy - sh * s / 2, sw * s, sh * s);
    } else {
      c.fillStyle = '#3A64D8'; c.fillRect(cx - R, cy - R, R * 2, R * 2);
      var ini = avatarEl.querySelector('.pf-initials');
      if (ini && ini.textContent) {
        c.fillStyle = '#fff'; c.font = '600 ' + Math.round(R * 0.7) + 'px -apple-system, system-ui, sans-serif';
        c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(ini.textContent, cx, cy + 1);
      }
    }
    c.restore();
  }

  /* ---------- Фигурки и капли ---------- */
  // Цвет фигурки: усреднённый насыщенный цвет картинки; если не вышло, цвет редкости
  function sampleTint(img, fallback) {
    try {
      var c = document.createElement('canvas'); c.width = 24; c.height = 24;
      var x = c.getContext('2d'); x.drawImage(img, 0, 0, 24, 24);
      var data = x.getImageData(0, 0, 24, 24).data, r = 0, g = 0, b = 0, wsum = 0;
      for (var i = 0; i < data.length; i += 4) {
        var a = data[i + 3] / 255; if (a < 0.5) continue;
        var mx = Math.max(data[i], data[i + 1], data[i + 2]), mn = Math.min(data[i], data[i + 1], data[i + 2]);
        var sat = mx ? (mx - mn) / mx : 0, wt = 0.15 + sat * sat;
        r += data[i] * wt; g += data[i + 1] * wt; b += data[i + 2] * wt; wsum += wt;
      }
      if (wsum > 0) {
        var out = [r / wsum, g / wsum, b / wsum], mxv = Math.max(out[0], out[1], out[2]);
        if (mxv < 110) out = out.map(function (v) { return Math.min(255, v * 110 / Math.max(mxv, 1)); });
        return out;
      }
    } catch (e) { /* картинка с другого источника или ещё не готова */ }
    return fallback;
  }

  function setFigures(list) {
    figs = (list || []).slice(0, 8).map(function (f) {
      var tier = clamp(parseInt(f.tier, 10) || 1, 1, 8);
      var fb = rgb(f.tint && /^#[0-9a-f]{6}$/i.test(f.tint) ? f.tint : TIER_TINT[tier - 1]);
      var fig = { name: String(f.name || ''), rarity: String(f.rarity || ''), data: f.data, img: new Image(), tint: fb, loaded: false };
      fig.img.onload = function () { fig.tint = sampleTint(fig.img, fb); fig.loaded = true; applyTints(); };
      fig.img.src = f.src;
      return fig;
    });
    buildDrops();
  }
  function applyTints() { drops.forEach(function (d) { d.tint = d.fig.tint; d.lightTint = mixc(d.fig.tint, WHITE, 0.55); }); }

  function buildDrops() {
    drops = [];
    if (!W || !figs.length) return;
    var n = figs.length, rnd = mulberry(2027 + n * 131);
    for (var i = 0; i < n; i++) {
      var fig = figs[i], k = n > 1 ? i / (n - 1) : 0.5;
      var d = {
        fig: fig, tint: fig.tint, lightTint: mixc(fig.tint, WHITE, 0.55),
        R0: lerp(CFG.size[0], CFG.size[1], rnd()) * (W < 380 ? 0.92 : 1),
        rx: ava.R * lerp(CFG.rx[0], CFG.rx[1], clamp(k * 0.75 + rnd() * 0.25, 0, 1)), ry: 0,
        tilt: rnd() * TAU, prec: (rnd() - 0.5) * 0.10, phase: i / n * TAU + rnd() * 0.5,
        speed: lerp(CFG.speed[0], CFG.speed[1], rnd()), dir: (i % 3 === 2) ? -1 : 1,
        bob: 3 + rnd() * 4, bobF: 0.6 + rnd() * 0.8, bobP: rnd() * TAU,
        w1: 0.9 + rnd() * 0.8, w2: 0.7 + rnd() * 0.9, w3: 1.2 + rnd(), p1: rnd() * TAU, p2: rnd() * TAU,
        swing: 0.15 + rnd() * 0.18, pop: 0, bubbles: [], rnd: mulberry(1000 + i * 17)
      };
      d.ry = d.rx * lerp(CFG.flat[0], CFG.flat[1], rnd());
      // орбита должна помещаться в шапку профиля
      var ext = function (a, b, t) { return Math.sqrt(Math.pow(a * Math.cos(t), 2) + Math.pow(b * Math.sin(t), 2)); };
      var exX = ext(d.rx, d.ry, d.tilt), exY = ext(d.rx, d.ry, d.tilt + Math.PI / 2);
      var kx = (Math.min(ava.cx, W - ava.cx) - d.R0 * 1.15 - 4) / Math.max(exX, 1);
      var ky = (Math.min(ava.cy, H - ava.cy) - d.R0 * 1.15 - 6) / Math.max(exY, 1);
      var kk = Math.min(1, kx, ky);
      d.rx *= kk; d.ry *= kk;
      // капля не должна врезаться в сам аватар
      d.ry = Math.max(d.ry, ava.R * 1.12 + d.R0 * 0.4);
      d.rx = Math.max(d.rx, d.ry);
      for (var b = 0; b < CFG.bubbles; b++) d.bubbles.push(newBubble(d, true));
      drops.push(d);
    }
  }
  function newBubble(d, init) {
    var r = d.rnd;
    return { u0: (r() - 0.5) * 1.0, u: 0, v: init ? (r() - 0.5) * 1.5 : 0.78, r: 0.045 + r() * 0.06, sp: 0.10 + r() * 0.20, f: 1 + r() * 2, ph: r() * TAU };
  }
  function orbit(d, tt) {
    var th = d.phase + d.dir * d.speed * tt * TAU;
    var u = d.rx * Math.cos(th), v = d.ry * Math.sin(th);
    var ph = d.tilt + d.prec * tt, c = Math.cos(ph), s = Math.sin(ph);
    return { x: ava.cx + u * c - v * s + Math.sin(tt * d.bobF + d.bobP) * d.bob, y: ava.cy + u * s + v * c + Math.cos(tt * d.bobF * 0.8 + d.bobP) * d.bob, z: Math.sin(th) };
  }

  var N = 36;
  function blobPts(d, x, y, R, s, beta, t, tail) {
    var pts = [], ca = Math.cos(beta), sa = Math.sin(beta), sx = 1 + s, sy = 1 / Math.sqrt(1 + s);
    for (var k = 0; k < N; k++) {
      var a = k / N * TAU;
      var r = 1 + 0.055 * Math.cos(2 * a + d.w1 * t + d.p1) + 0.045 * Math.cos(3 * a - d.w2 * t + d.p2) + 0.03 * Math.cos(5 * a + d.w3 * t);
      var back = Math.cos(a - (beta + Math.PI));
      if (back > 0) r += tail * s * Math.pow(back, 3) * 0.9;
      var lx = Math.cos(a) * r, ly = Math.sin(a) * r;
      var par = lx * ca + ly * sa, per = -lx * sa + ly * ca;
      var px = par * sx, py = per * sy;
      pts.push([x + (px * ca - py * sa) * R, y + (px * sa + py * ca) * R]);
    }
    return pts;
  }
  function pathFrom(pts) {
    var p = new Path2D(), n = pts.length;
    p.moveTo((pts[n - 1][0] + pts[0][0]) / 2, (pts[n - 1][1] + pts[0][1]) / 2);
    for (var i = 0; i < n; i++) {
      var a = pts[i], b = pts[(i + 1) % n];
      p.quadraticCurveTo(a[0], a[1], (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    }
    p.closePath();
    return p;
  }
  function rimPoly(ctx, S, target, width, inset) {
    var best = 0, bd = 9, pts = S.pts, n = pts.length, ds = [];
    for (var k = 0; k < n; k++) {
      var dd = Math.abs(angDiff(Math.atan2(pts[k][1] - S.y, pts[k][0] - S.x), target));
      ds.push(dd); if (dd < bd) { bd = dd; best = k; }
    }
    var from = best, to = best;
    while (ds[(from - 1 + n) % n] < width && best - from > -n / 2) from--;
    while (ds[(to + 1) % n] < width && to - best < n / 2) to++;
    ctx.beginPath();
    for (var i = from; i <= to; i++) {
      var q = pts[((i % n) + n) % n], x = S.x + (q[0] - S.x) * inset, y = S.y + (q[1] - S.y) * inset;
      if (i === from) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
  }
  // Капля над ником и значком становится прозрачнее, чтобы текст читался
  function textFade(x, y, R) {
    var dx = Math.max(textBox.l - x, 0, x - textBox.r), dy = Math.max(textBox.t - y, 0, y - textBox.b);
    return lerp(0.38, 1, clamp(Math.hypot(dx, dy) / (R * 0.9), 0, 1));
  }

  function drawDrop(ctx, d, S, src) {
    var x = S.x, y = S.y, R = S.R, tint = d.tint, lt = d.lightTint, L = state.light, lx = Math.cos(L), ly = Math.sin(L), t = state.t;
    ctx.save();
    ctx.globalAlpha = S.alpha * (S.z >= 0 ? textFade(x, y, R) : 1);
    // свечение оттенком фигурки
    ctx.globalCompositeOperation = 'lighter';
    var g = ctx.createRadialGradient(x, y, R * 0.5, x, y, R * (2.0 + 0.4 * CFG.aura));
    g.addColorStop(0, rgba(tint, 0.34 * CFG.aura)); g.addColorStop(0.5, rgba(tint, 0.10 * CFG.aura)); g.addColorStop(1, rgba(tint, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, R * 2.5, 0, TAU); ctx.fill();
    ctx.globalCompositeOperation = 'source-over';
    // тело капли
    g = ctx.createRadialGradient(x - lx * R * 0.25, y - ly * R * 0.25, R * 0.05, x, y, R * 1.06);
    g.addColorStop(0, rgba(tint, 0.10)); g.addColorStop(0.55, 'rgba(12,16,32,.20)'); g.addColorStop(0.88, rgba(tint, 0.30)); g.addColorStop(1, rgba(tint, 0.55));
    ctx.fillStyle = g; ctx.fill(S.path);
    // линза: перевёрнутое увеличенное отражение аватара
    if (src && Math.hypot(x - ava.cx, y - ava.cy) < ava.R + R * 1.3) {
      ctx.save(); ctx.clip(S.path);
      ctx.globalAlpha = S.alpha * CFG.lens * 0.9;
      ctx.translate(x, y); ctx.scale(-CFG.lensM, -CFG.lensM); ctx.translate(-x, -y);
      ctx.drawImage(src, 0, 0, W, H);
      ctx.restore();
      ctx.fillStyle = rgba(tint, 0.14); ctx.fill(S.path);
    }
    // тень внутри края и пузырьки
    ctx.save(); ctx.clip(S.path);
    ctx.lineJoin = 'round';
    ctx.lineWidth = R * 0.30; ctx.strokeStyle = 'rgba(4,6,16,.28)'; ctx.stroke(S.path);
    ctx.lineWidth = R * 0.34; ctx.strokeStyle = rgba(tint, 0.26); ctx.stroke(S.path);
    for (var i = 0; i < d.bubbles.length; i++) {
      var b = d.bubbles[i], bx = x + b.u * R, by = y + b.v * R, br = Math.max(0.8, b.r * R);
      ctx.beginPath(); ctx.arc(bx, by, br, 0, TAU);
      ctx.fillStyle = 'rgba(255,255,255,.10)'; ctx.fill();
      ctx.lineWidth = 0.7; ctx.strokeStyle = 'rgba(255,255,255,.62)'; ctx.stroke();
      ctx.beginPath(); ctx.arc(bx - br * 0.35, by - br * 0.35, Math.max(0.5, br * 0.28), 0, TAU);
      ctx.fillStyle = 'rgba(255,255,255,.92)'; ctx.fill();
    }
    ctx.restore();
    // фигурка
    var fs = R * 1.32 * (1 - 0.10 * S.s);
    ctx.save();
    ctx.translate(x, y + Math.sin(t * 1.3 + d.bobP) * R * 0.04);
    ctx.rotate(S.beta); ctx.scale(1 + 0.30 * S.s, 1 / (1 + 0.30 * S.s)); ctx.rotate(-S.beta + Math.sin(t * 0.8 + d.bobP) * d.swing);
    if (d.fig.img.complete && d.fig.img.naturalWidth) ctx.drawImage(d.fig.img, -fs / 2, -fs / 2, fs, fs);
    ctx.restore();
    // каустика
    ctx.globalCompositeOperation = 'lighter';
    g = ctx.createRadialGradient(x - lx * R * 0.45, y - ly * R * 0.45, 0, x - lx * R * 0.45, y - ly * R * 0.45, R * 0.55);
    g.addColorStop(0, rgba(lt, 0.42)); g.addColorStop(1, rgba(lt, 0));
    ctx.save(); ctx.clip(S.path); ctx.fillStyle = g; ctx.fillRect(x - R * 1.6, y - R * 1.6, R * 3.2, R * 3.2); ctx.restore();
    // дисперсия по краю
    var cols = [[255, 70, 90], [70, 255, 140], [90, 130, 255]];
    for (var c = 0; c < 3; c++) {
      ctx.save(); ctx.translate(-lx * (c - 1) * 1.5, -ly * (c - 1) * 1.5);
      ctx.lineWidth = 1.5; ctx.strokeStyle = rgba(cols[c], 0.55); ctx.stroke(S.path); ctx.restore();
    }
    // контур, ободок света и блик
    ctx.globalCompositeOperation = 'source-over';
    var rg = ctx.createLinearGradient(x + lx * R, y + ly * R, x - lx * R, y - ly * R);
    rg.addColorStop(0, 'rgba(255,255,255,.92)'); rg.addColorStop(0.5, rgba(tint, 0.34)); rg.addColorStop(1, rgba(lt, 0.85));
    ctx.lineWidth = 1.3; ctx.strokeStyle = rg; ctx.stroke(S.path);
    ctx.globalCompositeOperation = 'lighter';
    rimPoly(ctx, S, Math.atan2(-ly, -lx), 0.75, 0.97);
    ctx.lineWidth = 2.6; ctx.lineCap = 'round'; ctx.strokeStyle = rgba(lt, 0.62); ctx.stroke();
    rimPoly(ctx, S, Math.atan2(ly, lx), 0.55, 0.88);
    ctx.lineWidth = 1.8; ctx.strokeStyle = 'rgba(255,255,255,.72)'; ctx.stroke();
    var hx = x + lx * R * 0.46, hy = y + ly * R * 0.46;
    ctx.save(); ctx.translate(hx, hy); ctx.rotate(L + Math.PI / 2); ctx.scale(1, 0.38);
    g = ctx.createRadialGradient(0, 0, 0, 0, 0, R * 0.32);
    g.addColorStop(0, 'rgba(255,255,255,.88)'); g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R * 0.32, 0, TAU); ctx.fill(); ctx.restore();
    ctx.beginPath(); ctx.arc(x + lx * R * 0.2 - ly * R * 0.28, y + ly * R * 0.2 + lx * R * 0.28, Math.max(1, R * 0.06), 0, TAU);
    ctx.fillStyle = 'rgba(255,255,255,.95)'; ctx.fill();
    ctx.restore();
  }

  // Блики на гранях аватара: чем ближе капля, тем ярче
  function drawGlints(ctx) {
    var hasConic = typeof ctx.createConicGradient === 'function';
    for (var i = 0; i < states.length; i++) {
      var S = states[i], dx = S.x - ava.cx, dy = S.y - ava.cy, D = Math.hypot(dx, dy);
      var gap = D - ava.R - S.R * 0.5;
      var I = clamp(1 - gap / (ava.R * 1.65), 0, 1);
      if (I <= 0.01) continue;
      I = Math.pow(I, 1.45) * (S.z < 0 ? 0.78 : 1);
      var a = Math.atan2(dy, dx), col = mixc(S.d.tint, WHITE, 0.5);
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      var w = lerp(0.22, 1.05, I), f = w / TAU;
      if (hasConic) {
        var gr = ctx.createConicGradient(a - Math.PI, ava.cx, ava.cy);
        gr.addColorStop(0, rgba(col, 0)); gr.addColorStop(clamp(0.5 - f, 0.01, 0.49), rgba(col, 0));
        gr.addColorStop(0.5 - f * 0.4, rgba(col, I * 0.55)); gr.addColorStop(0.5, 'rgba(255,255,255,' + I + ')');
        gr.addColorStop(0.5 + f * 0.4, rgba(col, I * 0.55)); gr.addColorStop(clamp(0.5 + f, 0.51, 0.99), rgba(col, 0)); gr.addColorStop(1, rgba(col, 0));
        ctx.strokeStyle = gr;
      } else { ctx.strokeStyle = rgba(col, I * 0.5); }
      ctx.lineWidth = 2 + 5 * I; ctx.beginPath(); ctx.arc(ava.cx, ava.cy, ava.R - 1, 0, TAU); ctx.stroke();
      // свет, ложащийся на сам аватар у кромки
      ctx.save(); ctx.beginPath(); ctx.arc(ava.cx, ava.cy, ava.R, 0, TAU); ctx.clip();
      var px = ava.cx + Math.cos(a) * ava.R, py = ava.cy + Math.sin(a) * ava.R;
      var sp = ctx.createRadialGradient(px, py, 0, px, py, ava.R * 0.95);
      sp.addColorStop(0, rgba(col, 0.38 * I)); sp.addColorStop(1, rgba(col, 0));
      ctx.fillStyle = sp; ctx.fillRect(ava.cx - ava.R, ava.cy - ava.R, ava.R * 2, ava.R * 2);
      ctx.restore();
      // звёздочка в самой яркой точке
      if (I > 0.45) {
        var q = (I - 0.45) / 0.55, len = 5 + 18 * q;
        ctx.lineCap = 'round'; ctx.lineWidth = 1.3; ctx.strokeStyle = 'rgba(255,255,255,' + (0.85 * q) + ')';
        for (var k = 0; k < 2; k++) {
          var ang = a + Math.PI / 2 * k + 0.5;
          ctx.beginPath(); ctx.moveTo(px - Math.cos(ang) * len, py - Math.sin(ang) * len); ctx.lineTo(px + Math.cos(ang) * len, py + Math.sin(ang) * len); ctx.stroke();
        }
        var gl = ctx.createRadialGradient(px, py, 0, px, py, 12 * q + 3);
        gl.addColorStop(0, 'rgba(255,255,255,' + (0.9 * q) + ')'); gl.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = gl; ctx.beginPath(); ctx.arc(px, py, 12 * q + 3, 0, TAU); ctx.fill();
      }
      ctx.restore();
    }
  }

  /* ---------- Кадр ---------- */
  function step(ts) {
    if (!state.active) { raf = 0; return; }
    var dt = Math.min(0.05, (ts - (last || ts)) / 1000); last = ts;
    state.t += dt * (reduceMotion.matches ? 0.12 : 1);
    var t = state.t;
    state.light += angDiff(state.lightT, state.light) * Math.min(1, dt * 6);
    states = [];
    for (var i = 0; i < drops.length; i++) {
      var d = drops[i], p0 = orbit(d, t), p1 = orbit(d, t + 0.03);
      var vx = (p1.x - p0.x) / 0.03, vy = (p1.y - p0.y) / 0.03, sp = Math.hypot(vx, vy);
      var beta = Math.atan2(vy, vx), s = clamp(sp / 230, 0, 1) * CFG.stretch;
      d.pop *= Math.pow(0.02, dt);
      var sc = (1 + 0.28 * p0.z) * (1 + 0.28 * d.pop) * (1 + 0.04 * Math.sin(t * 1.7 + d.bobP));
      var R = d.R0 * sc, pts = blobPts(d, p0.x, p0.y, R, s, beta, t, CFG.tail);
      states.push({ d: d, x: p0.x, y: p0.y, z: p0.z, R: R, s: s, beta: beta, pts: pts, path: pathFrom(pts), alpha: p0.z < 0 ? 0.80 + 0.2 * (1 + p0.z) : 1 });
      for (var b = 0; b < d.bubbles.length; b++) {
        var bb = d.bubbles[b];
        bb.v -= bb.sp * dt * (reduceMotion.matches ? 0.3 : 1); bb.u = bb.u0 + Math.sin(t * bb.f + bb.ph) * 0.07;
        if (bb.v < -0.82 || bb.u * bb.u + bb.v * bb.v > 0.74) d.bubbles[b] = newBubble(d, false);
      }
    }
    ctxB.setTransform(dpr, 0, 0, dpr, 0, 0); ctxF.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctxB.clearRect(0, 0, W, H); ctxF.clearRect(0, 0, W, H);
    var order = states.slice().sort(function (a, b) { return a.z - b.z; });
    for (var k = 0; k < order.length; k++) {
      if (order[k].z < 0) drawDrop(ctxB, order[k].d, order[k], null);
    }
    ctxF.drawImage(rimCv, 0, 0, W, H);
    drawGlints(ctxF);
    for (var m = 0; m < order.length; m++) {
      if (order[m].z >= 0) drawDrop(ctxF, order[m].d, order[m], scCv);
    }
    raf = requestAnimationFrame(step);
  }

  function setActive(on) {
    on = !!on;
    if (on === state.active && (on === !!raf || !on)) return;
    state.active = on;
    if (on) {
      if (!W) measure();
      last = 0;
      if (!raf) raf = requestAnimationFrame(step);
    } else if (raf) {
      cancelAnimationFrame(raf); raf = 0;
      if (ctxB) { ctxB.clearRect(0, 0, cvB.width, cvB.height); ctxF.clearRect(0, 0, cvF.width, cvF.height); }
    }
  }

  /* ---------- Подключение ---------- */
  function pointer(e) { var r = hero.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

  function mount(o) {
    if (mounted || !o || !o.hero || !o.back || !o.front || !o.avatar) return;
    hero = o.hero; cvB = o.back; cvF = o.front; avatarEl = o.avatar; whoEl = o.who || null; onPick = o.onPick || null;
    ctxB = cvB.getContext('2d'); ctxF = cvF.getContext('2d');
    mounted = true;
    hero.addEventListener('pointermove', function (e) { var p = pointer(e); state.lightT = Math.atan2(p.y - ava.cy, p.x - ava.cx); });
    // нажатие на каплю открывает фигурку; нажатие мимо работает как раньше (например, смена фото)
    hero.addEventListener('pointerdown', function (e) {
      var p = pointer(e), hit = null, best = 1e9;
      state.lightT = Math.atan2(p.y - ava.cy, p.x - ava.cx);
      for (var i = states.length - 1; i >= 0; i--) {
        var S = states[i], dd = Math.hypot(S.x - p.x, S.y - p.y);
        if (dd < S.R * 1.2 && dd < best) { best = dd; hit = S; }
      }
      swallow = false;
      if (!hit) return;
      hit.d.pop = 1;
      swallow = true;
      if (onPick) onPick(hit.d.fig.data);
    }, true);
    hero.addEventListener('click', function (e) { if (swallow) { swallow = false; e.stopPropagation(); e.preventDefault(); } }, true);
    // картинка аватара подгрузилась или заменилась: обновляем отражение
    avatarEl.addEventListener('load', refreshAvatar, true);
    if (window.MutationObserver) new MutationObserver(refreshAvatar).observe(avatarEl, { childList: true, subtree: true });
    if (window.ResizeObserver) new ResizeObserver(function () { if (state.active) measure(); }).observe(hero);
  }

  function refreshAvatar() { if (W) renderAvatarSprite(); }
  function resize() { measure(); }

  // Для проверок: где сейчас капли (координаты внутри шапки профиля)
  function positions() { return states.map(function (S) { return { x: S.x, y: S.y, r: S.R, z: S.z }; }); }

  window.ZentoDrops = { mount: mount, setFigures: setFigures, refreshAvatar: refreshAvatar, setActive: setActive, resize: resize, positions: positions };
})();
