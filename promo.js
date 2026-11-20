/* ZENTO: новости на главной (рулетка, Telegram-бот, приложение). Карточки лежат в index.html.
   Ряд листается пальцем (scroll-snap). Точки показывают текущую карточку и переключают её.
   Автосмена каждые 5 с, только если карточки не помещаются в ширину. Пауза: мышь над рядом, фокус, касание
   (продолжаем через 5 с), скрытая вкладка. prefers-reduced-motion: без автосмены. */
(function () {
  'use strict';

  var STEP_MS = 5000;
  var HOLD_MS = 5000;

  var row = document.getElementById('promo-row');
  var dotsBox = document.getElementById('promo-dots');
  var section = document.getElementById('promo');
  if (!row || !dotsBox || !section) return;

  var cards = Array.prototype.slice.call(row.querySelectorAll('.promo-card'));
  if (!cards.length) { section.hidden = true; return; }

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var dots = [];
  var index = 0;
  var hovering = false, focused = false, touching = false;
  var holdUntil = 0, acc = 0, last = 0, timer = 0;

  function scrollable() { return row.scrollWidth - row.clientWidth > 8; }

  function cardLeft(card) { return card.offsetLeft - row.offsetLeft - (parseFloat(getComputedStyle(row).paddingLeft) || 0); }

  cards.forEach(function (card, k) {
    card.setAttribute('aria-label', 'Карточка ' + (k + 1) + ' из ' + cards.length);
    var dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'promo-dot press';
    dot.setAttribute('aria-label', 'Карточка ' + (k + 1));
    dot.addEventListener('click', function () { go(k); acc = 0; });
    dotsBox.appendChild(dot);
    dots.push(dot);
  });

  function mark(k) {
    index = k;
    dots.forEach(function (d, i) {
      if (i === k) d.setAttribute('aria-current', 'true'); else d.removeAttribute('aria-current');
    });
  }

  function go(k) {
    var n = cards.length;
    k = ((k % n) + n) % n;
    row.scrollTo({ left: cardLeft(cards[k]), behavior: reduceMotion.matches ? 'auto' : 'smooth' });
    mark(k);
  }

  // Какая карточка ближе к левому краю: по ней рисуем точку (при ручной прокрутке)
  var raf = 0;
  row.addEventListener('scroll', function () {
    if (raf) return;
    raf = window.requestAnimationFrame(function () {
      raf = 0;
      var best = 0, dist = Infinity;
      cards.forEach(function (c, i) {
        var d = Math.abs(c.offsetLeft - row.offsetLeft - row.scrollLeft - (parseFloat(getComputedStyle(row).paddingLeft) || 0));
        if (d < dist) { dist = d; best = i; }
      });
      if (best !== index) mark(best);
    });
  }, { passive: true });

  function running() {
    return !reduceMotion.matches && !hovering && !focused && !touching && !document.hidden &&
      scrollable() && Date.now() >= holdUntil;
  }

  function tick() {
    var t = Date.now();
    var dt = last ? t - last : 0;
    last = t;
    if (dt > 1000) dt = 0;
    if (!running()) return;
    acc += dt;
    if (acc >= STEP_MS) { acc = 0; go(index + 1); }
  }

  row.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') hovering = true; });
  row.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') hovering = false; });
  row.addEventListener('focusin', function () { focused = true; });
  row.addEventListener('focusout', function (e) { if (!row.contains(e.relatedTarget)) focused = false; });
  row.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight') { e.preventDefault(); go(index + 1); acc = 0; }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(index - 1); acc = 0; }
  });
  row.addEventListener('touchstart', function () { touching = true; }, { passive: true });
  function release() { touching = false; holdUntil = Date.now() + HOLD_MS; acc = 0; }
  row.addEventListener('touchend', release, { passive: true });
  row.addEventListener('touchcancel', release, { passive: true });
  document.addEventListener('visibilitychange', function () { last = 0; acc = 0; });

  // Если все карточки помещаются в ширину (компьютер), точки не нужны
  function syncDots() { dotsBox.hidden = !scrollable(); }
  syncDots();
  window.addEventListener('resize', syncDots);
  mark(0);
  timer = setInterval(tick, 200);
  window.addEventListener('pagehide', function () { clearInterval(timer); timer = 0; });
})();
