/* ZENTO: настоящее «жидкое стекло» для кнопок (Liquid Glass как в iOS).
   Перенос компонента apple-tahoe-liquid-glass-button (React) на обычный JavaScript, без библиотек.
   Что делает: линза с преломлением фона (SVG-фильтр в backdrop-filter, только Chromium), блик по краю
   по форме «сквиркла», упругое нажатие, свет под пальцем. В Safari и Firefox вместо преломления матовое стекло.
   Включается само для кнопок из SELECTOR, в том числе для созданных позже (корзина, профиль).
   Без inline-скриптов (CSP script-src 'self'). */
(function () {
  'use strict';

  var SELECTOR = '.btn, .icon-btn, .cart-btn, .promo-btn';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var HOVER_QUERY = '(hover: hover) and (pointer: fine)';

  // Если в системе отключена прозрачность, остаются обычные кнопки
  if (window.matchMedia && window.matchMedia('(prefers-reduced-transparency: reduce)').matches) return;
  if (typeof ResizeObserver === 'undefined') return;

  /* ---------- Среда ---------- */
  var detectedEngine = null;
  // Chromium умеет SVG-фильтры в backdrop-filter, остальные показывают матовое стекло
  function detectEngine() {
    if (detectedEngine) return detectedEngine;
    var ua = navigator.userAgent || '';
    if (/Firefox\//.test(ua)) return (detectedEngine = 'gecko');
    var brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (brands && brands.length) {
      var ch = brands.some(function (b) { return /Chromium|Google Chrome|Microsoft Edge/i.test(b.brand); });
      return (detectedEngine = ch ? 'chromium' : 'webkit');
    }
    var chromium = /(Chrome|Chromium|Edg|SamsungBrowser)\/\d+/.test(ua);
    return (detectedEngine = chromium ? 'chromium' : 'webkit');
  }
  function detectMode() { return detectEngine() === 'chromium' ? 'refract' : 'frost'; }

  var reducedQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function motionLevel() { return reducedQuery && reducedQuery.matches ? 'reduced' : 'full'; }

  /* ---------- Пружины ---------- */
  var springs = {
    press: { duration: 0.24, bounce: 0.2 },
    release: { duration: 0.5, bounce: 0.42 },
    glow: { duration: 0.42, bounce: 0 },
    track: { duration: 0.18, bounce: 0 }
  };

  function Spring(value, options) {
    this.value = value;
    this.velocity = 0;
    this.target = value;
    this.stiffness = 0;
    this.damping = 0;
    this.configure(options);
  }
  Spring.prototype.configure = function (o) {
    var omega = (2 * Math.PI) / o.duration;
    this.stiffness = omega * omega;
    this.damping = o.bounce >= 0
      ? (4 * Math.PI * (1 - o.bounce)) / o.duration
      : (4 * Math.PI) / (o.duration * (1 + o.bounce));
    return this;
  };
  Object.defineProperty(Spring.prototype, 'settled', {
    get: function () { return this.value === this.target && this.velocity === 0; }
  });
  Spring.prototype.step = function (dt) {
    if (this.settled || !(dt > 0)) return this;
    var omega = Math.sqrt(this.stiffness);
    var zeta = this.damping / (2 * omega);
    var x0 = this.value - this.target;
    var v0 = this.velocity;
    var x, v;
    if (zeta < 1) {
      var wd = omega * Math.sqrt(1 - zeta * zeta);
      var decay = Math.exp(-zeta * omega * dt);
      var a = x0;
      var b = (v0 + zeta * omega * x0) / wd;
      var cos = Math.cos(wd * dt), sin = Math.sin(wd * dt);
      x = decay * (a * cos + b * sin);
      v = decay * ((b * wd - zeta * omega * a) * cos - (a * wd + zeta * omega * b) * sin);
    } else if (zeta === 1) {
      var dc = Math.exp(-omega * dt);
      var bb = v0 + omega * x0;
      x = dc * (x0 + bb * dt);
      v = dc * (bb - omega * (x0 + bb * dt));
    } else {
      var root = omega * Math.sqrt(zeta * zeta - 1);
      var r1 = -zeta * omega + root, r2 = -zeta * omega - root;
      var c2 = (v0 - r1 * x0) / (r2 - r1);
      var c1 = x0 - c2;
      var e1 = Math.exp(r1 * dt), e2 = Math.exp(r2 * dt);
      x = c1 * e1 + c2 * e2;
      v = c1 * r1 * e1 + c2 * r2 * e2;
    }
    var scale = Math.max(1, Math.abs(this.target), Math.abs(x0));
    if (Math.abs(x) < 1e-4 * scale && Math.abs(v) < 1e-3 * scale) {
      this.value = this.target;
      this.velocity = 0;
    } else {
      this.value = this.target + x;
      this.velocity = v;
    }
    return this;
  };

  function rubberBand(offset, limit, coefficient) {
    if (limit <= 0) return 0;
    var k = coefficient == null ? 0.55 : coefficient;
    return Math.sign(offset) * limit * (1 - 1 / ((Math.abs(offset) * k) / limit + 1));
  }

  /* ---------- Материалы и тон ---------- */
  var MATERIALS = {
    clear: {
      light: { shadowLift: 0.18, highlightRolloff: 0 },
      dark: { shadowLift: 0.15, highlightRolloff: 0.2 }
    },
    regular: {
      blur: 4.5,
      light: { fillSigma: 15, fillOpacity: 0.34, tone: [0.36, 0.67, -0.1113, 1.1031] },
      dark: { fillSigma: 14, fillOpacity: 0.65, tone: [0.105, 0.83, -0.2091, 1.0648] }
    }
  };

  var toneCache = {};
  function toneTable(material, appearance, nested) {
    var key = material + '|' + appearance + '|' + (nested ? 'n' : '');
    if (toneCache[key]) return toneCache[key];
    var dark = appearance === 'dark';
    var values = [];
    var n, v;
    if (material === 'regular') {
      var t = (dark ? MATERIALS.regular.dark : MATERIALS.regular.light).tone;
      var weight = dark ? 0.9 : 0.8;
      for (n = 0; n < 33; n++) {
        v = n / 32;
        values.push(weight * (t[0] + t[1] * v + t[2] * v * v) + (1 - weight) * v);
      }
    } else {
      var spec = dark ? MATERIALS.clear.dark : MATERIALS.clear.light;
      for (n = 0; n < 33; n++) {
        v = n / 32;
        var tone = v + spec.shadowLift * Math.pow(1 - v, 3) - spec.highlightRolloff * Math.pow(v, 3);
        var tinted = dark ? 0.38 * v + 0.2 : 0.78 * v + 0.26;
        var tintWeight = dark ? 0.14 : 0.06;
        var layerLight = dark && nested ? 0.08 * (1 - v) : 0;
        values.push(Math.max(0, Math.min(1, (1 - tintWeight) * tone + tintWeight * tinted + layerLight)));
      }
    }
    return (toneCache[key] = values.map(function (x) { return x.toFixed(5); }).join(' '));
  }

  function tintChannels(tint) {
    if (!tint || !/^#[\da-f]{6}$/i.test(tint)) return null;
    return [parseInt(tint.slice(1, 3), 16) / 255, parseInt(tint.slice(3, 5), 16) / 255, parseInt(tint.slice(5, 7), 16) / 255];
  }

  /* ---------- Контур «сквиркл» ---------- */
  var SMOOTHING = 0.8125;
  function rad(d) { return (d * Math.PI) / 180; }
  function sat(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

  function cornerParams(radius, budget) {
    var e = radius;
    var p = (1 + SMOOTHING) * e;
    var o = 90 * (1 - SMOOTHING);
    var i = Math.sin(rad(o / 2)) * e * Math.SQRT2;
    var g = e * Math.tan(rad((90 - o) / 4));
    var c = g * Math.cos(rad(45 * SMOOTHING));
    var d = c * Math.tan(rad(45 * SMOOTHING));
    var u = (p - i - c - d) / 3;
    var a = 2 * u;
    if (p > budget) {
      var rem = budget - d - i - c;
      var m = rem / 6;
      var l = rem - m;
      u = Math.min(u, l);
      a = rem - u;
      p = budget;
    }
    return { a: a, b: u, c: c, d: d, p: p, i: i };
  }

  var outlineCache = {};

  function bezier(out, x0, y0, x1, y1, x2, y2, x3, y3, steps) {
    for (var s = 1; s <= steps; s++) {
      var t = s / steps, w = 1 - t;
      out.push(
        x0 * w * w * w + 3 * w * w * t * x1 + 3 * w * t * t * x2 + t * t * t * x3,
        y0 * w * w * w + 3 * w * w * t * y1 + 3 * w * t * t * y2 + t * t * t * y3
      );
    }
  }

  function squircleOutline(w, h, radius) {
    var key = w + '|' + h + '|' + radius;
    if (outlineCache[key]) return outlineCache[key];
    var hw = w / 2, hh = h / 2;
    var budget = Math.min(hw, hh);
    var r = Math.min(radius, budget);
    var outline;
    if (r < 0.5) {
      outline = {
        corners: [new Float64Array(0), new Float64Array(0), new Float64Array(0), new Float64Array(0)],
        extent: 0,
        polygon: 'polygon(0px 0px, ' + w + 'px 0px, ' + w + 'px ' + h + 'px, 0px ' + h + 'px)',
        width: w, height: h, radius: 0
      };
      return (outlineCache[key] = outline);
    }
    var cp = cornerParams(r, budget);
    var a = cp.a, b = cp.b, c = cp.c, d = cp.d, p = cp.p, i = cp.i;
    var pts = [];
    var x0 = hw - p, y0 = -hh;
    bezier(pts, x0, y0, x0 + a, y0, x0 + a + b, y0, x0 + a + b + c, y0 + d, 24);
    var ax = x0 + a + b + c, ay = y0 + d;
    var chord = i * Math.SQRT2;
    var dx = i, dy = i;
    var offset = Math.sqrt(Math.max(0, r * r - (chord * chord) / 4));
    var cx = ax + dx / 2 - (dy / chord) * offset;
    var cy = ay + dy / 2 + (dx / chord) * offset;
    var angle = Math.atan2(ay - cy, ax - cx);
    var extent = 2 * Math.asin(Math.min(1, chord / (2 * r)));
    for (var s = 1; s <= 8; s++) {
      var theta = angle + (extent * s) / 8;
      pts.push(cx + r * Math.cos(theta), cy + r * Math.sin(theta));
    }
    var qx = ax + dx, qy = ay + dy;
    bezier(pts, qx, qy, qx + d, qy + c, qx + d, qy + b + c, qx + d, qy + a + b + c, 24);

    var withAnchors = [x0, y0].concat(pts, [hw, -hh + p]);
    var count = withAnchors.length / 2;
    var trPts = new Float64Array(count * 2);
    var trNormals = new Float64Array(count * 2);
    for (var k = 0; k < count; k++) {
      var px = withAnchors[2 * k], py = withAnchors[2 * k + 1];
      var pk = Math.max(0, k - 1), nk = Math.min(count - 1, k + 1);
      var tdx = withAnchors[2 * nk] - withAnchors[2 * pk];
      var tdy = withAnchors[2 * nk + 1] - withAnchors[2 * pk + 1];
      if (k === 0) { tdx = 1; tdy = 0; }
      if (k === count - 1) { tdx = 0; tdy = 1; }
      var len = Math.hypot(tdx, tdy) || 1;
      trPts[2 * k] = px; trPts[2 * k + 1] = py;
      trNormals[2 * k] = tdy / len; trNormals[2 * k + 1] = -tdx / len;
    }
    function mirror(mx, my) {
      var run = new Float64Array(count * 4);
      var sx = mx ? -1 : 1, sy = my ? -1 : 1;
      for (var q = 0; q < count; q++) {
        run[4 * q] = sx * trPts[2 * q];
        run[4 * q + 1] = sy * trPts[2 * q + 1];
        run[4 * q + 2] = sx * trNormals[2 * q];
        run[4 * q + 3] = sy * trNormals[2 * q + 1];
      }
      return run;
    }
    var tr = mirror(false, false), br = mirror(false, true), bl = mirror(true, true), tl = mirror(true, false);
    var poly = [];
    function push(run, step) {
      for (var q = step > 0 ? 0 : count - 1; step > 0 ? q < count : q >= 0; q += step) {
        poly.push((run[4 * q] + hw).toFixed(2) + 'px ' + (run[4 * q + 1] + hh).toFixed(2) + 'px');
      }
    }
    push(tr, 1); push(br, -1); push(bl, 1); push(tl, -1);
    outline = { corners: [tr, br, bl, tl], extent: p, polygon: 'polygon(' + poly.join(', ') + ')', width: w, height: h, radius: r };
    var keys = Object.keys(outlineCache);
    if (keys.length > 256) delete outlineCache[keys[0]];
    return (outlineCache[key] = outline);
  }

  // Расстояние со знаком и внешняя нормаль контура в центрированных координатах
  function outlineField(px, py, outline) {
    var hw = outline.width / 2, hh = outline.height / 2, p = outline.extent;
    var ax = Math.abs(px), ay = Math.abs(py);
    var qx = ax - (hw - p), qy = ay - (hh - p);
    if (qx > 0 && qy > 0) {
      var run = px >= 0 ? (py < 0 ? outline.corners[0] : outline.corners[1]) : (py < 0 ? outline.corners[3] : outline.corners[2]);
      if (run.length === 0) {
        var sdx = ax - hw, sdy = ay - hh;
        var dist = Math.hypot(sdx, sdy);
        return { d: dist, nx: sdx / (dist || 1), ny: sdy / (dist || 1) };
      }
      var best2 = Infinity, bx = 0, by = 0, bnx = 0, bny = 0;
      var n = run.length / 4;
      for (var k = 0; k < n - 1; k++) {
        var x1 = run[4 * k], y1 = run[4 * k + 1];
        var ex = run[4 * k + 4] - x1, ey = run[4 * k + 5] - y1;
        var len2 = ex * ex + ey * ey;
        var t = len2 > 1e-12 ? sat(((px - x1) * ex + (py - y1) * ey) / len2) : 0;
        var cqx = x1 + ex * t, cqy = y1 + ey * t;
        var ddx = px - cqx, ddy = py - cqy;
        var d2 = ddx * ddx + ddy * ddy;
        if (d2 < best2) { best2 = d2; bx = cqx; by = cqy; bnx = run[4 * k + 2]; bny = run[4 * k + 3]; }
      }
      var distance = Math.sqrt(best2);
      var delta = distance > 1e-5 ? 1 / distance : 0;
      var ddx2 = px - bx, ddy2 = py - by;
      var direction = ddx2 * bnx + ddy2 * bny >= 0 ? 1 : -1;
      return {
        d: distance * direction,
        nx: distance > 1e-5 ? ddx2 * delta * direction : bnx,
        ny: distance > 1e-5 ? ddy2 * delta * direction : bny
      };
    }
    if (qx > qy) return { d: qx - p, nx: Math.sign(px) || 1, ny: 0 };
    return { d: qy - p, nx: 0, ny: Math.sign(py) || 1 };
  }

  function resolveRadius(value, w, h) {
    var n = parseFloat(value);
    if (!isFinite(n)) return 0;
    var r = String(value).trim().slice(-1) === '%' ? (n / 100) * Math.min(w, h) : n;
    return Math.max(0, Math.min(r, w / 2, h / 2));
  }

  /* ---------- Карты: смещение (field) и блик по краю (overlay) ---------- */
  var COS_SPREAD = Math.cos(1.08);
  var COS_DIFFUSE = Math.cos(1.08 * 0.65);
  var COS_SIDE = Math.cos(0.98);

  function pixelScale(w, h) {
    return w * h > 250000 ? 0.5 : Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
  }

  function renderCanvases(w, h, radius, bezel, dark, tint) {
    try {
      var outline = squircleOutline(w, h, radius);
      var hw = w / 2, hh = h / 2, p = outline.extent;
      var bz = Math.max(1, Math.min(bezel, hw, hh));
      var k = pixelScale(w, h);
      var mw = Math.max(1, Math.round(w * k));
      var mh = Math.max(1, Math.round(h * k));
      var fieldCanvas = document.createElement('canvas');
      fieldCanvas.width = mw; fieldCanvas.height = mh;
      var fctx = fieldCanvas.getContext('2d');
      if (!fctx) return { field: null, overlay: null };
      var fimg = fctx.createImageData(mw, mh);

      var pad = 2;
      var bw = w + pad * 2, bh = h + pad * 2;
      var ok = pixelScale(bw, bh);
      var omw = Math.max(1, Math.round(bw * ok));
      var omh = Math.max(1, Math.round(bh * ok));
      var overlayCanvas = document.createElement('canvas');
      overlayCanvas.width = omw; overlayCanvas.height = omh;
      var octx = overlayCanvas.getContext('2d');
      if (!octx) return { field: null, overlay: null };
      var oimg = octx.createImageData(omw, omh);

      for (var pass = 0; pass < 2; pass++) {
        var isField = pass === 0;
        var cw = isField ? w : bw, ch = isField ? h : bh;
        var scale = isField ? k : ok;
        var width = isField ? mw : omw, height = isField ? mh : omh;
        var data = (isField ? fimg : oimg).data;
        var aa = 1;
        for (var j = 0; j < height; j++) {
          var py = (j + 0.5) / scale - ch / 2;
          for (var i = 0; i < width; i++) {
            var px = (i + 0.5) / scale - cw / 2;
            var f = outlineField(px, py, outline);
            var d = f.d, nx = f.nx, ny = f.ny;
            var o = (j * width + i) * 4;
            if (isField) {
              var coverage = sat(0.5 - d / aa);
              if (d < -(bz + 4)) {
                data[o] = 128; data[o + 1] = 128; data[o + 2] = 128; data[o + 3] = 255;
                continue;
              }
              var depth = sat(-d / bz);
              var edge = 1 - sat(Math.sqrt((2 - depth) * depth));
              data[o] = Math.round(255 * (0.5 - nx * edge * 0.5));
              data[o + 1] = Math.round(255 * (0.5 - ny * edge * 0.5));
              data[o + 2] = 128;
              data[o + 3] = Math.round(255 * coverage);
            } else {
              var qx = Math.abs(px) - (hw - p), qy = Math.abs(py) - (hh - p);
              if (Math.max(qx, qy) - p > pad + 6) continue;
              var cov = sat(0.5 - d / aa);
              var directional = Math.pow(sat((Math.abs(ny) - COS_SPREAD) / (1 - COS_SPREAD)), 1.25);
              var diffuseDir = Math.pow(sat((Math.abs(ny) - COS_DIFFUSE) / (1 - COS_DIFFUSE)), 1.25);
              var stroke = sat((d + 0.9) / aa + 0.5) * sat(0.5 - d / aa);
              var specular = directional * stroke;
              specular = specular / (1 + 0.5 * (1 - specular));
              var diffuse = diffuseDir * 0.18 * Math.pow(1 - sat(-d / 4.5), 2) * cov;
              var highlight = sat(specular * (dark ? 0.82 : 1) + diffuse * (dark ? 0.4 : 1));
              var adjusted = d - 0.5;
              var falloff = 1 - sat(-adjusted / 0.5);
              var ramp = (falloff > 0 ? 1 : 0) * 0.25 + falloff * 0.75;
              var band = ramp * sat((adjusted + 0.5) / aa + 0.5) * sat(-adjusted / aa + 0.5);
              var wx = sat((nx - COS_SIDE) / (1 - COS_SIDE)) * band;
              var wy = sat((-nx - COS_SIDE) / (1 - COS_SIDE)) * band;
              var shade = (wx / (1 + 0.4 * (1 - wx)) + wy / (1 + 0.4 * (1 - wy))) * (dark ? 0.6 : 0.48);
              var alpha = highlight + shade * (1 - highlight);
              if (alpha <= 0.002) continue;
              if (tint) {
                var base = highlight / Math.max(alpha, 1e-5);
                data[o] = Math.round(255 * Math.min(1, base * (0.45 + 0.55 * tint[0])));
                data[o + 1] = Math.round(255 * Math.min(1, base * (0.45 + 0.55 * tint[1])));
                data[o + 2] = Math.round(255 * Math.min(1, base * (0.45 + 0.55 * tint[2])));
                data[o + 3] = Math.round(255 * alpha);
              } else {
                var light = ((dark ? 0.82 : 1) * highlight) / Math.max(alpha, 1e-5);
                data[o] = data[o + 1] = data[o + 2] = Math.round(255 * Math.min(1, light));
                data[o + 3] = Math.round(255 * alpha);
              }
            }
          }
        }
        (isField ? fctx : octx).putImageData(isField ? fimg : oimg, 0, 0);
      }
      return { field: fieldCanvas, overlay: overlayCanvas };
    } catch (e) {
      return { field: null, overlay: null };
    }
  }

  /* Широкие капсулы: концы запекаем один раз, растягиваем только середину */
  var bakeCache = {};
  function capsuleShape(w, h, radius) { return radius >= Math.min(w, h) / 2 - 0.75 && w >= h * 2; }

  function slicedURLs(baked, w, h) {
    var cap = h / 2;
    function bake(source, sourcePad, destPad) {
      var cw = w + destPad * 2, ch = h + destPad * 2;
      var scale = pixelScale(cw, ch);
      var canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(cw * scale));
      canvas.height = Math.max(1, Math.round(ch * scale));
      var ctx = canvas.getContext('2d');
      var sh = source.height;
      var sk = sh / (h + sourcePad * 2);
      var startX = sourcePad * sk;
      var capPx = cap * sk;
      var destCap = cap * scale;
      var startDest = destPad * scale;
      var capSrc = startX + capPx;
      var capDest = Math.round(startDest + destCap);
      var midDest = Math.max(1, canvas.width - 2 * capDest);
      ctx.drawImage(source, 0, 0, capSrc, sh, 0, 0, capDest, canvas.height);
      ctx.drawImage(source, capSrc, 0, Math.max(1, source.width - 2 * capSrc), sh, capDest, 0, midDest, canvas.height);
      ctx.drawImage(source, source.width - capSrc, 0, capSrc, sh, canvas.width - capDest, 0, capDest, canvas.height);
      return canvas;
    }
    return {
      field: baked.field ? bake(baked.field, 0, 0).toDataURL('image/png') : '',
      overlay: baked.overlay ? bake(baked.overlay, 2, 2).toDataURL('image/png') : ''
    };
  }

  var fieldCache = {}, overlayCache = {};
  var CACHE_LIMIT = 64;
  function remember(cache, key, make) {
    if (cache[key]) return cache[key];
    var value = make();
    if (!value) return value;
    cache[key] = value;
    var keys = Object.keys(cache);
    if (keys.length > CACHE_LIMIT) delete cache[keys[0]];
    return value;
  }

  function capsuleBake(h, radius, o) {
    var key = 'bake|h' + h + '|r' + radius + '|' + (o.dark ? 'd' : 'l') + '|' + (o.tint || '');
    if (!bakeCache[key]) {
      bakeCache[key] = renderCanvases(h * 3, h, radius, o.bezel, o.dark, o.tint);
      var keys = Object.keys(bakeCache);
      if (keys.length > 16) delete bakeCache[keys[0]];
    }
    return bakeCache[key];
  }

  function fieldURL(w, h, radius, o) {
    return remember(fieldCache, w + 'x' + h + 'r' + radius + 'b' + o.bezel, function () {
      if (capsuleShape(w, h, radius)) {
        var baked = capsuleBake(h, radius, o);
        if (baked.field) return slicedURLs(baked, w, h).field;
      }
      var c = renderCanvases(w, h, radius, o.bezel, o.dark, o.tint).field;
      return c ? c.toDataURL('image/png') : '';
    });
  }

  function overlayURL(w, h, radius, o) {
    return remember(overlayCache, w + 'x' + h + 'r' + radius + (o.dark ? 'd' : 'l') + (o.tintKey || ''), function () {
      if (capsuleShape(w, h, radius)) {
        var baked = capsuleBake(h, radius, o);
        if (baked.overlay) return slicedURLs(baked, w, h).overlay;
      }
      var c = renderCanvases(w, h, radius, o.bezel, o.dark, o.tint).overlay;
      return c ? c.toDataURL('image/png') : '';
    });
  }

  /* ---------- Общий SVG-фильтр: одинаковые кнопки делят один фильтр ---------- */
  var defsHost = null;
  var filterRefs = {};

  function host() {
    if (defsHost) return defsHost;
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    svg.style.cssText = 'position:fixed;width:0;height:0;overflow:hidden;pointer-events:none';
    document.body.appendChild(svg);
    return (defsHost = svg);
  }

  function acquireFilter(id, markup) {
    if (filterRefs[id]) { filterRefs[id].count++; return; }
    var filter = document.createElementNS(SVG_NS, 'filter');
    filter.setAttribute('id', id);
    filter.setAttribute('color-interpolation-filters', 'sRGB');
    filter.innerHTML = markup;
    host().appendChild(filter);
    filterRefs[id] = { el: filter, count: 1 };
  }

  function releaseFilter(id) {
    var entry = filterRefs[id];
    if (!entry) return;
    if (--entry.count <= 0) { entry.el.remove(); delete filterRefs[id]; }
  }

  function filterMarkup(o) {
    var dark = o.appearance === 'dark';
    var spec = o.material === 'regular' ? (dark ? MATERIALS.regular.dark : MATERIALS.regular.light) : null;
    var parts = ['<feImage href="' + o.field + '" x="0" y="0" width="' + o.w + '" height="' + o.h + '" preserveAspectRatio="none" result="field"/>'];
    var base = 'SourceGraphic';
    if (spec) {
      parts.push(
        '<feGaussianBlur in="SourceGraphic" stdDeviation="' + MATERIALS.regular.blur + '" result="frost"/>',
        '<feGaussianBlur in="SourceGraphic" stdDeviation="' + spec.fillSigma + '" result="fill"/>',
        '<feBlend in="frost" in2="fill" mode="' + (dark ? 'darken' : 'lighten') + '" result="blend"/>',
        '<feComposite in="blend" in2="frost" operator="arithmetic" k2="' + spec.fillOpacity + '" k3="' + (1 - spec.fillOpacity) + '" result="mix"/>'
      );
      base = 'mix';
    }
    parts.push('<feDisplacementMap in="' + base + '" in2="field" scale="' + (o.refraction * 2) + '" xChannelSelector="R" yChannelSelector="G" result="refracted"/>');
    var table = toneTable(o.material, o.appearance, o.nested);
    parts.push('<feComponentTransfer in="refracted" result="tone"><feFuncR type="table" tableValues="' + table + '"/><feFuncG type="table" tableValues="' + table + '"/><feFuncB type="table" tableValues="' + table + '"/></feComponentTransfer>');
    var color = 'tone';
    if (o.material === 'regular') {
      parts.push('<feColorMatrix in="tone" type="saturate" values="' + spec.tone[3] + '" result="color"/>');
      color = 'color';
    }
    if (o.tint) {
      var body = o.material === 'regular' ? 0.78 : 0.68;
      var rest = 1 - body;
      parts.push(
        '<feColorMatrix in="' + color + '" type="saturate" values="0" result="luminance"/>',
        '<feComponentTransfer in="luminance" result="tinted">',
        '<feFuncR type="linear" slope="' + o.tint[0] * rest + '" intercept="' + o.tint[0] * body + '"/>',
        '<feFuncG type="linear" slope="' + o.tint[1] * rest + '" intercept="' + o.tint[1] * body + '"/>',
        '<feFuncB type="linear" slope="' + o.tint[2] * rest + '" intercept="' + o.tint[2] * body + '"/>',
        '<feFuncA type="linear" slope="' + rest + '" intercept="' + body + '"/>',
        '</feComponentTransfer>'
      );
      color = 'tinted';
    }
    parts.push('<feComposite in="' + color + '" in2="field" operator="in"/>');
    return parts.join('');
  }

  /* ---------- Нажатие: пружины, «набухание», свет под пальцем ---------- */
  var claimed = typeof WeakSet === 'function' ? new WeakSet() : { has: function () { return false; }, add: function () {} };

  function isDisabled(element) {
    return element.matches(':disabled, [data-disabled], [aria-disabled="true"]');
  }

  var GLOW_VARS = ['--lg-glow', '--lg-glow-x', '--lg-glow-y', '--lg-glow-size'];

  function attachInteraction(element) {
    var view = element.ownerDocument.defaultView;
    var saved = { translate: element.style.translate, scale: element.style.scale };
    var tx = new Spring(0, springs.track), ty = new Spring(0, springs.track);
    var press = new Spring(0, springs.press), glow = new Spring(0, springs.glow);
    var pointer = null, keyboard = false, hovering = false, previous = 0, written = false;
    var glowX = 0, glowY = 0, raf = 0, awake = false;

    function writeStyle(name, value) {
      if (element.style.getPropertyValue(name) !== value) element.style.setProperty(name, value);
    }
    function size() { return { width: element.offsetWidth || 1, height: element.offsetHeight || 1 }; }
    function layout() {
      var rect = element.getBoundingClientRect();
      var s = size();
      return { cx: rect.left + rect.width / 2 - tx.value, cy: rect.top + rect.height / 2 - ty.value, width: s.width, height: s.height, rect: rect };
    }
    function light(x, y) {
      var l = layout();
      glowX = ((x - l.rect.left) * l.width) / (l.rect.width || 1);
      glowY = ((y - l.rect.top) * l.height) / (l.rect.height || 1);
    }
    function set(spring, target, options) { spring.configure(options).target = target; }

    function move(e) {
      if (!pointer || e.pointerId !== pointer.id) return;
      pointer.clientX = e.clientX; pointer.clientY = e.clientY;
    }
    function release(e) {
      if (e && (!pointer || e.pointerId !== pointer.id)) return;
      pointer = null;
      view.removeEventListener('pointermove', move);
      view.removeEventListener('pointerup', release);
      view.removeEventListener('pointercancel', release);
      set(press, 0, springs.release);
      set(tx, 0, springs.release);
      set(ty, 0, springs.release);
      set(glow, hovering ? 0.35 : 0, springs.glow);
    }
    function down(e) {
      if (claimed.has(e) || e.button !== 0 || isDisabled(element)) return;
      claimed.add(e);
      var l = layout();
      pointer = { id: e.pointerId, x: e.clientX, y: e.clientY, cx: l.cx, cy: l.cy, clientX: e.clientX, clientY: e.clientY };
      light(e.clientX, e.clientY);
      set(press, 1, springs.press);
      set(glow, 1, springs.press);
      var s = size();
      var follow = { duration: springs.track.duration + 0.0005 * Math.max(s.width, s.height), bounce: 0 };
      set(tx, 0, follow);
      set(ty, 0, follow);
      view.addEventListener('pointermove', move);
      view.addEventListener('pointerup', release);
      view.addEventListener('pointercancel', release);
    }
    function hover(e) {
      if (e.pointerType === 'touch' || !view.matchMedia(HOVER_QUERY).matches) return;
      hovering = e.type !== 'pointerleave';
      if (hovering) light(e.clientX, e.clientY);
      if (!pointer && !keyboard) set(glow, hovering ? 0.35 : 0, springs.glow);
    }
    function key(e) {
      if (e.key !== ' ' && e.key !== 'Enter') return;
      if (e.type === 'keydown') {
        if (e.repeat || claimed.has(e) || isDisabled(element) || pointer) return;
        claimed.add(e);
        keyboard = true;
        var s = size();
        glowX = s.width / 2; glowY = s.height / 2;
        set(press, 1, springs.press);
        set(glow, 1, springs.press);
      } else if (keyboard) {
        keyboard = false;
        set(press, 0, springs.release);
        set(glow, hovering ? 0.35 : 0, springs.glow);
      }
    }
    function drag(e) {
      if (!pointer) return;
      var origin = e.target;
      var source = origin && origin.nodeType === 1 ? origin : origin && origin.parentElement;
      if (!source || !source.closest('[draggable="true"]')) e.preventDefault();
    }
    function blur() {
      if (keyboard) {
        keyboard = false;
        set(press, 0, springs.release);
        set(glow, 0, springs.glow);
      }
    }

    function frame(now) {
      var dt = previous ? Math.max(0, (now - previous) / 1000) : 1 / 60;
      previous = now;
      var level = motionLevel();
      var s = size();
      var width = s.width, height = s.height;
      if (pointer) {
        var l = layout();
        var small = Math.max(width, height) <= 72;
        var ox = small
          ? pointer.clientX - pointer.x - (l.cx - pointer.cx)
          : pointer.clientX - Math.max(l.cx - width / 2, Math.min(l.cx + width / 2, pointer.clientX));
        var oy = small
          ? pointer.clientY - pointer.y - (l.cy - pointer.cy)
          : pointer.clientY - Math.max(l.cy - height / 2, Math.min(l.cy + height / 2, pointer.clientY));
        var limit = small ? 4 + 0.15 * Math.max(width, height) : Math.min(10, 3 + 0.04 * Math.max(width, height));
        tx.target = rubberBand(ox, limit);
        ty.target = rubberBand(oy, limit);
        light(pointer.clientX, pointer.clientY);
      }
      [tx, ty, press, glow].forEach(function (sp) { sp.step(dt); });
      var elastic = level === 'full';
      var grow = elastic ? Math.min(0.16, 8 / Math.max(width, height)) * press.value : 0;
      var cap = Math.max(width, height) <= 72 ? 0.2 : 0.12;
      var ex = elastic ? Math.min(cap, (0.5 * Math.abs(tx.value)) / width + 0.00007 * Math.abs(tx.velocity)) : 0;
      var ey = elastic ? Math.min(cap, (0.5 * Math.abs(ty.value)) / height + 0.00007 * Math.abs(ty.velocity)) : 0;
      var sx = ((1 + grow) * (1 + ex)) / Math.sqrt(1 + ey);
      var sy = ((1 + grow) * (1 + ey)) / Math.sqrt(1 + ex);
      var moved = elastic && (Math.abs(tx.value) > 0.01 || Math.abs(ty.value) > 0.01);
      var scaled = Math.abs(sx - 1) > 1e-4 || Math.abs(sy - 1) > 1e-4;
      var lit = glow.value > 0.001;
      if (moved || scaled || lit || pointer || keyboard) {
        written = true;
        writeStyle('translate', moved ? tx.value.toFixed(3) + 'px ' + ty.value.toFixed(3) + 'px' : saved.translate);
        writeStyle('scale', scaled ? sx.toFixed(5) + ' ' + sy.toFixed(5) : saved.scale);
        writeStyle('--lg-glow', lit ? glow.value.toFixed(4) : '0');
        writeStyle('--lg-glow-x', glowX.toFixed(2) + 'px');
        writeStyle('--lg-glow-y', glowY.toFixed(2) + 'px');
        writeStyle('--lg-glow-size', Math.max(36, Math.min(180, Math.max(width, height) * 0.9)).toFixed(1) + 'px');
        if (pointer || keyboard) element.setAttribute('data-glass-pressed', '');
        else element.removeAttribute('data-glass-pressed');
      } else if (written) {
        written = false;
        element.style.translate = saved.translate;
        element.style.scale = saved.scale;
        GLOW_VARS.forEach(function (n) { element.style.removeProperty(n); });
        element.removeAttribute('data-glass-pressed');
      }
      var active = Boolean(pointer) || [tx, ty, press, glow].some(function (sp) { return !sp.settled; });
      if (!active) previous = 0;
      return active;
    }
    function loop(now) {
      if (frame(now)) raf = view.requestAnimationFrame(loop);
      else awake = false;
    }
    function wake() {
      if (!awake) { awake = true; raf = view.requestAnimationFrame(loop); }
    }

    element.addEventListener('pointerdown', down);
    element.addEventListener('dragstart', drag);
    element.addEventListener('pointerenter', hover);
    element.addEventListener('pointermove', hover);
    element.addEventListener('pointerleave', hover);
    element.addEventListener('keydown', key);
    element.addEventListener('keyup', key);
    element.addEventListener('focusout', blur);
    element.addEventListener('pointerdown', wake);
    element.addEventListener('pointerenter', wake);
    element.addEventListener('keydown', wake);

    return {
      dispose: function () {
        release();
        view.cancelAnimationFrame(raf);
        awake = false;
        element.removeEventListener('pointerdown', down);
        element.removeEventListener('dragstart', drag);
        element.removeEventListener('pointerenter', hover);
        element.removeEventListener('pointermove', hover);
        element.removeEventListener('pointerleave', hover);
        element.removeEventListener('keydown', key);
        element.removeEventListener('keyup', key);
        element.removeEventListener('focusout', blur);
        element.removeEventListener('pointerdown', wake);
        element.removeEventListener('pointerenter', wake);
        element.removeEventListener('keydown', wake);
        element.style.translate = saved.translate;
        element.style.scale = saved.scale;
        GLOW_VARS.forEach(function (n) { element.style.removeProperty(n); });
        element.removeAttribute('data-glass-pressed');
      }
    };
  }

  /* ---------- Стекло на одном элементе ---------- */
  function layer(css) {
    var span = document.createElement('span');
    span.setAttribute('aria-hidden', 'true');
    span.setAttribute('data-glass-layer', '');
    span.style.cssText = 'position:absolute;z-index:-1;pointer-events:none;' + css;
    return span;
  }

  function attachGlass(element, opts) {
    var material = opts.material || 'clear';
    var appearance = opts.appearance || 'dark';
    var dark = appearance === 'dark';
    var tintStr = opts.tint || '';
    var tintRGB = tintChannels(tintStr);
    var blur = opts.blur == null ? 1 : opts.blur;
    var saturation = opts.saturation || 1.2;
    var brightness = opts.brightness || 1.05;
    var fallbackBlur = opts.fallbackBlur || 10;
    var mode = detectMode();
    var engine = detectEngine();
    var spec = material === 'regular' ? (dark ? MATERIALS.regular.dark : MATERIALS.regular.light) : null;

    var lens = layer('inset:0;border-radius:inherit;');
    var overlay = layer('inset:-2px;background-size:100% 100%;background-repeat:no-repeat;');
    var fill = layer('inset:0;border-radius:inherit;');
    var glowEl = layer('inset:0;border-radius:inherit;');
    glowEl.style.setProperty('--lg-glow-color', dark ? 'rgb(255 255 255 / 0.16)' : 'rgb(255 255 255 / 0.3)');
    glowEl.style.backgroundImage = 'radial-gradient(var(--lg-glow-size, 64px) circle at var(--lg-glow-x, 50%) var(--lg-glow-y, 50%), color-mix(in srgb, var(--lg-glow-color) calc(var(--lg-glow, 0) * 100%), transparent), transparent 65%)';
    fill.hidden = true;
    element.insertBefore(glowEl, element.firstChild);
    element.insertBefore(fill, element.firstChild);
    element.insertBefore(overlay, element.firstChild);
    element.insertBefore(lens, element.firstChild);
    element.setAttribute('data-liquid-glass', 'frost');

    var interaction = attachInteraction(element);
    var held = null;
    var lastKey = '';
    var raf = 0;
    var disposed = false;

    function paint(w, h, radius) {
      var nested = !!(element.parentElement && element.parentElement.closest('[data-liquid-glass]'));
      var refraction = opts.refraction != null ? opts.refraction : (nested ? 8 : 18);
      var bz = Math.round(opts.bezel != null ? opts.bezel : Math.min(32, Math.min(w, h) / 2));
      var planes = { bezel: bz, dark: dark, tint: tintRGB, tintKey: tintStr };
      var outline = squircleOutline(w, h, radius);
      var ov = overlayURL(w, h, radius, planes);
      var field = '';
      var filterId = '';
      if (mode === 'refract') {
        field = fieldURL(w, h, radius, planes);
        if (field) {
          filterId = ('lgf' + w + 'x' + h + 'r' + radius + 'b' + bz + 'f' + Math.round(refraction * 10) + (dark ? 'd' : 'l') +
            (material === 'regular' ? 'R' : 'C') + tintStr + (nested ? 'n' : '')).replace(/[^a-zA-Z0-9-]/g, '');
        }
      }
      var key = [w, h, radius, filterId, nested].join('|');
      if (key === lastKey) return;
      lastKey = key;

      if (held && held !== filterId) { releaseFilter(held); held = null; }
      if (filterId && held !== filterId) {
        acquireFilter(filterId, filterMarkup({
          w: w, h: h, field: field, material: material, appearance: appearance, refraction: refraction, tint: tintRGB, nested: nested
        }));
        held = filterId;
      }
      var refracting = !!filterId;
      element.setAttribute('data-liquid-glass', refracting ? 'refract' : 'frost');

      var cssTone = 'saturate(' + saturation + ') brightness(' + (dark ? brightness * 0.88 : brightness) + ')';
      var engineBlur = engine === 'webkit' ? Math.min(fallbackBlur, 16) : fallbackBlur;
      var backdrop = refracting
        ? 'url(#' + filterId + ')' + (blur > 0 ? ' blur(' + blur + 'px)' : '')
        : (material === 'regular'
          ? 'blur(' + Math.max(2, engineBlur * 0.5) + 'px) ' + cssTone
          : 'blur(' + engineBlur + 'px) ' + cssTone);
      lens.style.backdropFilter = backdrop;
      lens.style.webkitBackdropFilter = backdrop;
      lens.style.clipPath = outline.polygon;
      overlay.style.backgroundImage = ov ? 'url(' + ov + ')' : '';

      // Заливка там, где фильтр не работает (матовое стекло) и тёмная вуаль для прозрачного
      if (!refracting && (material === 'regular' || dark)) {
        fill.hidden = false;
        fill.style.backgroundColor = material === 'regular'
          ? (tintRGB
            ? 'rgb(' + tintRGB.map(function (c) { return Math.round(c * 255); }).join(' ') + ' / ' + spec.fillOpacity + ')'
            : (dark ? 'rgb(16 16 18 / ' + spec.fillOpacity + ')' : 'rgb(255 255 255 / ' + spec.fillOpacity + ')'))
          : 'rgb(8 8 10 / 0.16)';
      } else if (refracting && material === 'clear' && dark) {
        fill.hidden = false;
        fill.style.backgroundColor = 'rgb(8 8 10 / 0.08)';
      } else {
        fill.hidden = true;
      }
    }

    function measure() {
      raf = 0;
      if (disposed) return;
      var w = Math.round(lens.offsetWidth);
      var h = Math.round(lens.offsetHeight);
      if (w < 2 || h < 2) return;
      var radius = Math.round(resolveRadius(getComputedStyle(lens).borderTopLeftRadius, w, h));
      paint(w, h, radius);
    }
    var observer = new ResizeObserver(function () { if (!raf) raf = requestAnimationFrame(measure); });
    observer.observe(lens);
    measure();

    // Скрипты сайта меняют текст кнопки через textContent и стирают слои стекла: возвращаем их на место
    var layers = [glowEl, fill, overlay, lens];
    var restoring = false;
    var guard = new MutationObserver(function () {
      if (restoring || disposed || lens.parentNode === element) return;
      restoring = true;
      layers.forEach(function (n) { element.insertBefore(n, element.firstChild); });
      restoring = false;
      if (!raf) raf = requestAnimationFrame(measure);
    });
    guard.observe(element, { childList: true });

    return {
      dispose: function () {
        disposed = true;
        observer.disconnect();
        guard.disconnect();
        cancelAnimationFrame(raf);
        interaction.dispose();
        if (held) releaseFilter(held);
        [lens, overlay, fill, glowEl].forEach(function (n) { n.remove(); });
        element.removeAttribute('data-liquid-glass');
      }
    };
  }

  /* ---------- Какие кнопки и с какими настройками ---------- */
  function optionsFor(el) {
    if (el.classList.contains('btn-primary')) {
      // Основная: синее матовое стекло (белый текст остаётся читаемым)
      return { material: 'regular', appearance: 'dark', tint: '#0A84FF', refraction: 10, bezel: 14 };
    }
    var small = el.classList.contains('icon-btn') || el.classList.contains('cart-btn');
    return { material: 'clear', appearance: 'dark', refraction: small ? 8 : 12, bezel: small ? 16 : 18 };
  }

  var instances = typeof WeakMap === 'function' ? new WeakMap() : null;
  if (!instances) return;

  function attach(el) {
    if (instances.has(el)) return;
    instances.set(el, attachGlass(el, optionsFor(el)));
  }
  function detach(el) {
    var inst = instances.get(el);
    if (inst) { inst.dispose(); instances.delete(el); }
  }
  function eachMatch(node, fn) {
    if (node.nodeType !== 1) return;
    if (node.matches(SELECTOR)) fn(node);
    var list = node.querySelectorAll ? node.querySelectorAll(SELECTOR) : [];
    for (var i = 0; i < list.length; i++) fn(list[i]);
  }

  function start() {
    eachMatch(document.body, attach);
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        for (var i = 0; i < r.addedNodes.length; i++) eachMatch(r.addedNodes[i], attach);
        for (var j = 0; j < r.removedNodes.length; j++) {
          var n = r.removedNodes[j];
          // Узел могли просто перенести: отцепляем только тех, кого уже нет на странице
          eachMatch(n, function (el) { if (!document.body.contains(el)) detach(el); });
        }
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  window.ZentoGlass = { attach: attach, detach: detach };
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
