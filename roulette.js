/* ZENTO: колесо «Испытай удачу!» (главная страница).
   Призы и лимит (одна прокрутка в сутки) считает сервер. Клиент только рисует колесо
   и поворачивает его к призу из ответа.
   GET  /api/roulette/status → {ok, can_spin, next_at, prizes:[{id, label}]}
   POST /api/roulette/spin   → {ok, prize:{id, label}}; ошибки: 401 login_required, 429 already_spun.
   Без входа колесо не запрашиваем: состояние приходит событием zento:session (auth.js). */
(function () {
  'use strict';

  var SPIN_MS = 4600;             // длительность вращения (CSS: .wheel-rotor)
  var GUARD_MS = SPIN_MS + 1200;  // страховка: разблокируем кнопку, даже если transitionend не придёт
  var REQUEST_MS = 15000;         // запрос к серверу не дольше 15 с
  var TURNS = 5;                  // полных оборотов до остановки
  var LABEL_MAX = 10;             // подпись приза на колесе не длиннее 10 символов
  var NS = 'http://www.w3.org/2000/svg';
  // Сегменты: два цвета по очереди, у нечётного числа призов последний берёт третий цвет
  var FILLS = ['#1E40AF', '#BFDBFE', '#4338CA'];
  var LIGHT = { '#BFDBFE': true };

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var rotor = document.getElementById('wheel-rotor');
  var btn = document.getElementById('spin-btn');
  var statusBox = document.getElementById('spin-status');
  if (!rotor || !btn || !statusBox) return; // на странице нет колеса

  var prizes = [];        // [{id, label}]
  var prizesKey = '';     // чтобы не перерисовывать колесо без нужды
  var canSpin = false;
  var nextAt = null;
  var notice = '';        // итог последней попытки: «Ваш приз: …» или «уже крутили»
  var busy = false;       // идёт запрос или вращение: кнопка заблокирована
  var loading = false;
  var angle = 0;          // накопленный угол поворота, градусы
  var guardTimer = 0;

  /* ---------- Помощники ---------- */
  function svgEl(name, attrs) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
    return node;
  }

  function shortLabel(text) {
    var s = String(text || '');
    return s.length > LABEL_MAX ? s.slice(0, LABEL_MAX - 1) + '…' : s;
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    try {
      return d.toLocaleString('uk-UA', {
        timeZone: 'Europe/Kyiv', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'
      });
    } catch (e) {
      return d.toLocaleString('uk-UA');
    }
  }

  function nextText() {
    return 'Завтра можно крутить снова' + (nextAt && formatDate(nextAt) ? ': ' + formatDate(nextAt) : '');
  }

  /* ---------- Колесо ---------- */
  // Точка на окружности радиуса r; угол отсчитывается по часовой стрелке от верха
  function pointAt(deg, r) {
    var rad = deg * Math.PI / 180;
    return (100 + r * Math.sin(rad)).toFixed(3) + ' ' + (100 - r * Math.cos(rad)).toFixed(3);
  }

  function buildWheel() {
    var key = JSON.stringify(prizes);
    if (key === prizesKey) return;
    prizesKey = key;
    while (rotor.firstChild) rotor.removeChild(rotor.firstChild);
    var n = prizes.length;
    if (!n) return;
    var step = 360 / n;

    // Обод задаёт габарит колеса. Он симметричен, поэтому центр вращения (fill-box center) — центр колеса
    rotor.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 96, 'class': 'wheel-rim' }));

    prizes.forEach(function (p, i) {
      var a1 = i * step;
      var a2 = (i + 1) * step;
      var fill = FILLS[i % 2];
      if (n % 2 === 1 && i === n - 1) fill = FILLS[2];
      var large = step > 180 ? 1 : 0;
      // Один приз: сегмент — весь круг (дуга из одной точки не рисуется)
      var d = n === 1
        ? 'M100 5 A95 95 0 1 1 100 195 A95 95 0 1 1 100 5 Z'
        : 'M100 100 L' + pointAt(a1, 95) + ' A95 95 0 ' + large + ' 1 ' + pointAt(a2, 95) + ' Z';
      rotor.appendChild(svgEl('path', { d: d, fill: fill, stroke: '#0F172A', 'stroke-width': 1.5, 'stroke-linejoin': 'round' }));

      // Подпись вдоль радиуса: в системе, где биссектриса смотрит вверх, текст читается снизу вверх
      var mid = (i + 0.5) * step;
      var g = svgEl('g', { transform: 'rotate(' + mid.toFixed(3) + ' 100 100)' });
      var text = svgEl('text', {
        transform: 'translate(100 12) rotate(-90)',
        x: 0, y: 4, 'text-anchor': 'end',
        'class': 'wheel-label',
        fill: LIGHT[fill] ? '#0F172A' : '#FFFFFF'
      });
      text.textContent = shortLabel(p.label);
      g.appendChild(text);
      rotor.appendChild(g);
    });
  }

  function indexOfPrize(id) {
    for (var i = 0; i < prizes.length; i++) {
      if (String(prizes[i].id) === String(id)) return i;
    }
    return -1;
  }

  // Угол, при котором сегмент index окажется под указателем (сверху), с небольшим случайным сдвигом внутри сегмента
  function targetAngle(index) {
    var n = prizes.length;
    var step = 360 / n;
    var center = (index + 0.5) * step;
    var jitter = (Math.random() * 2 - 1) * Math.max(0, step / 2 - 4);
    var want = (((-center + jitter) % 360) + 360) % 360;
    var cur = ((angle % 360) + 360) % 360;
    var delta = (((want - cur) % 360) + 360) % 360;
    return angle + TURNS * 360 + delta;
  }

  // Поворот к сегменту. done вызывается один раз: по концу перехода или по страховочному таймеру
  function animateTo(index, done) {
    var target = targetAngle(index);
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      clearTimeout(guardTimer);
      rotor.removeEventListener('transitionend', onEnd);
      angle = target;
      done();
    }
    function onEnd(e) {
      if (e.target === rotor && e.propertyName === 'transform') finish();
    }

    if (reduceMotion.matches) {
      // Без движения: сразу встаём на приз, переход не запускаем (transitionend при нём не придёт)
      rotor.style.transform = 'rotate(' + target + 'deg)';
      finish();
      return;
    }
    rotor.addEventListener('transitionend', onEnd);
    guardTimer = setTimeout(finish, GUARD_MS);
    rotor.style.transform = 'rotate(' + target + 'deg)';
  }

  /* ---------- Состояние кнопки и текста ---------- */
  function refreshUI() {
    btn.disabled = busy || !canSpin || prizes.length === 0;
    btn.textContent = busy ? 'Крутим…' : 'Крутить';
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function unlock() {
    busy = false;
    refreshUI();
  }

  // Текст под кнопкой: итог последней попытки плюс когда можно снова
  function setIdleText() {
    if (busy) return;
    if (!prizes.length) { statusBox.textContent = 'Призы скоро появятся'; return; }
    if (canSpin) { statusBox.textContent = 'Одна прокрутка в сутки'; return; }
    statusBox.textContent = (notice ? notice + ' ' : '') + nextText();
  }

  /* ---------- Сеть ---------- */
  function loadStatus() {
    if (loading || busy) return;
    loading = true;
    fetch('/api/roulette/status', { headers: { Accept: 'application/json' } }).then(function (res) {
      if (res.status === 401) return { needLogin: true };
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      loading = false;
      if (!data || data.needLogin) {
        canSpin = false;
        statusBox.textContent = 'Войдите, чтобы крутить колесо';
        refreshUI();
        return;
      }
      prizes = Array.isArray(data.prizes)
        ? data.prizes.filter(function (p) { return p && p.id != null && typeof p.label === 'string'; })
        : [];
      canSpin = data.can_spin === true && prizes.length > 0;
      nextAt = typeof data.next_at === 'string' ? data.next_at : null;
      if (canSpin) notice = ''; // новый день: старый итог уже не показываем
      buildWheel();
      refreshUI();
      setIdleText();
    }).catch(function () {
      loading = false;
      canSpin = false;
      statusBox.textContent = 'Колесо сейчас недоступно. Обновите страницу';
      refreshUI();
    });
  }

  function spin() {
    if (busy || !canSpin) return;
    busy = true;
    statusBox.textContent = 'Крутим колесо…';
    refreshUI();

    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var abortTimer = setTimeout(function () { if (ctl) ctl.abort(); }, REQUEST_MS);

    fetch('/api/roulette/spin', {
      method: 'POST',
      headers: { Accept: 'application/json' },
      signal: ctl ? ctl.signal : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { status: res.status, body: body || {} };
      });
    }).then(function (r) {
      clearTimeout(abortTimer);
      if (r.status === 401) {
        unlock();
        document.dispatchEvent(new CustomEvent('zento:need-login'));
        return;
      }
      if (r.status === 429) {
        canSpin = false;
        notice = 'Сегодня колесо уже крутили.';
        unlock();
        setIdleText();
        loadStatus(); // подтянем дату следующей попытки
        return;
      }
      var prize = r.body.prize;
      if ((r.status !== 200 && r.status !== 201) || !prize) {
        unlock();
        statusBox.textContent = 'Не получилось крутануть. Попробуйте ещё раз';
        return;
      }
      var index = indexOfPrize(prize.id);
      var won = function () {
        canSpin = false;
        notice = 'Ваш приз: «' + String(prize.label) + '».';
        unlock();
        setIdleText();
        loadStatus();
      };
      if (index < 0) {
        // Приза нет в списке колеса: показываем результат текстом, без вращения
        won();
        return;
      }
      animateTo(index, won);
    }).catch(function () {
      clearTimeout(abortTimer);
      unlock();
      statusBox.textContent = 'Нет связи с сервером. Попробуйте ещё раз';
    });
  }

  /* ---------- События ---------- */
  btn.addEventListener('click', spin);

  // Состояние входа приходит от auth.js: 'in' — грузим колесо, 'out' — сбрасываем
  document.addEventListener('zento:session', function (e) {
    var state = e.detail && e.detail.state;
    if (state === 'in') {
      loadStatus();
    } else {
      canSpin = false;
      notice = '';
      refreshUI();
      statusBox.textContent = '';
    }
  });

  refreshUI();
})();
