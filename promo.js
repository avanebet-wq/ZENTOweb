/* ZENTO: полоса карточек «Акции / Новости / Новинки» (главная страница).
   Данные: assets/promo.json. Смена слайда каждые 5 с.
   Пауза: кнопкой, при наведении мыши, при фокусе и при касании (продолжаем через 5 с после последнего касания),
   когда вкладка скрыта. Вручную: точки, стрелки, свайп. prefers-reduced-motion: без авто-смены.
   Один таймер создаётся один раз и останавливается при уходе со страницы. */
(function () {
  'use strict';

  var STEP_MS = 5000;   // сколько показываем слайд
  var TICK_MS = 200;    // шаг таймера; копим только время, когда карусель идёт
  var HOLD_MS = 5000;   // после касания ждём 5 с
  var SWIPE_PX = 40;    // минимальный сдвиг пальцем для свайпа

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var section = document.getElementById('promo');
  var track = document.getElementById('promo-track');
  var viewport = document.getElementById('promo-viewport');
  var dotsBox = document.getElementById('promo-dots');
  var pauseBtn = document.getElementById('promo-pause');
  if (!section || !track || !viewport || !dotsBox || !pauseBtn) return;

  var slides = [];
  var dots = [];
  var index = 0;
  var userPaused = false;   // нажата кнопка «Пауза»
  var hovering = false;     // мышь над полосой
  var focused = false;      // фокус внутри полосы
  var touching = false;     // палец на полосе
  var holdUntil = 0;        // до этого момента после касания не идём
  var hiddenNow = document.hidden;
  var acc = 0;              // сколько мс слайд уже показан
  var last = 0;
  var timer = 0;

  function now() { return Date.now(); }
  function autoOK() { return !reduceMotion.matches; }
  function running() {
    return autoOK() && !userPaused && !hovering && !focused && !touching && !hiddenNow &&
      slides.length > 1 && now() >= holdUntil;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ---------- Построение ---------- */
  function validSlide(s) {
    return s && typeof s.title === 'string' && Array.isArray(s.items) && s.items.length > 0;
  }

  function buildSlide(data, k, total) {
    var slide = el('div', 'promo-slide');
    slide.setAttribute('role', 'group');
    slide.setAttribute('aria-roledescription', 'slide');
    slide.setAttribute('aria-label', 'Слайд ' + (k + 1) + ' из ' + total + ': ' + data.title);

    var card = el('article', 'promo-card');
    card.appendChild(el('p', 'promo-tag', 'Испытай удачу!'));
    card.appendChild(el('h3', 'promo-card-title', data.title));
    var list = el('ul', 'promo-list');
    data.items.forEach(function (item) {
      var li = el('li');
      li.appendChild(el('span', 'promo-item-title', String(item.title || '')));
      li.appendChild(el('span', 'promo-item-text', String(item.text || '')));
      list.appendChild(li);
    });
    card.appendChild(list);
    slide.appendChild(card);
    return slide;
  }

  function build(data) {
    var list = data && Array.isArray(data.slides) ? data.slides.filter(validSlide) : [];
    if (!list.length) { section.hidden = true; return; }
    list.forEach(function (s, k) {
      var slide = buildSlide(s, k, list.length);
      track.appendChild(slide);
      slides.push(slide);

      var dot = el('button', 'promo-dot press');
      dot.type = 'button';
      dot.setAttribute('aria-label', 'Слайд ' + (k + 1));
      dot.addEventListener('click', function () { go(k); });
      dotsBox.appendChild(dot);
      dots.push(dot);
    });
  }

  /* ---------- Переключение ---------- */
  function show(k) {
    index = k;
    slides.forEach(function (s, i) { s.classList.toggle('is-active', i === k); });
    dots.forEach(function (d, i) {
      if (i === k) d.setAttribute('aria-current', 'true');
      else d.removeAttribute('aria-current');
    });
  }

  // Ручной переход (точки, стрелки, свайп) или переход по таймеру: отсчёт начинается заново
  function go(k) {
    var n = slides.length;
    if (!n) return;
    show(((k % n) + n) % n);
    acc = 0;
  }

  function renderPause() {
    pauseBtn.hidden = !autoOK();
    pauseBtn.textContent = userPaused ? 'Продолжить' : 'Пауза';
    pauseBtn.setAttribute('aria-label', userPaused ? 'Продолжить смену карточек' : 'Остановить смену карточек');
  }

  /* ---------- Один таймер ---------- */
  function tick() {
    var t = now();
    var dt = last ? t - last : 0;
    last = t;
    // Если вкладку усыпили или таймер задержался, это время не засчитываем
    if (dt > 1000) dt = 0;
    if (!running()) return;
    acc += dt;
    if (acc >= STEP_MS) go(index + 1);
  }

  /* ---------- Пауза и ручное управление ---------- */
  pauseBtn.addEventListener('click', function () {
    userPaused = !userPaused;
    renderPause();
  });

  viewport.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') hovering = true; });
  viewport.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') hovering = false; });

  // Фокус внутри области карточек (стрелками) тоже останавливает смену.
  // Кнопки «Пауза» и точки сюда не входят, иначе «Продолжить» не смогло бы запустить смену
  viewport.addEventListener('focusin', function () { focused = true; });
  viewport.addEventListener('focusout', function (e) {
    if (!viewport.contains(e.relatedTarget)) focused = false;
  });

  viewport.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight') { e.preventDefault(); go(index + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(index - 1); }
  });

  var startX = 0;
  var startY = 0;
  viewport.addEventListener('touchstart', function (e) {
    touching = true;
    var t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
  }, { passive: true });

  viewport.addEventListener('touchend', function (e) {
    touching = false;
    holdUntil = now() + HOLD_MS;
    var t = e.changedTouches && e.changedTouches[0];
    if (!t) return;
    var dx = t.clientX - startX;
    var dy = t.clientY - startY;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy)) go(index + (dx < 0 ? 1 : -1));
  }, { passive: true });

  viewport.addEventListener('touchcancel', function () {
    touching = false;
    holdUntil = now() + HOLD_MS;
  }, { passive: true });

  // Вкладка скрыта: не листаем. Вернулись: отсчёт заново
  document.addEventListener('visibilitychange', function () {
    hiddenNow = document.hidden;
    last = 0;
    acc = 0;
  });

  if (reduceMotion.addEventListener) reduceMotion.addEventListener('change', renderPause);

  /* ---------- Запуск ---------- */
  fetch('assets/promo.json', { headers: { Accept: 'application/json' } }).then(function (res) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }).then(function (data) {
    build(data);
    if (!slides.length) return;
    show(0);
    renderPause();
    timer = setInterval(tick, TICK_MS);
  }).catch(function () {
    section.hidden = true;
  });

  // Уход со страницы: останавливаем таймер
  window.addEventListener('pagehide', function () {
    clearInterval(timer);
    timer = 0;
  });
})();
