/* Капелька входа: после входа, регистрации или нового пароля показывает под шапкой
   аватар, «Успешный вход», ник и галочку, и играет звук. Около 3 секунд.

   Слушает: zento:login (auth.js, после успешного входа) — показать;
            zento:session со state 'out' (выход) — скрыть сразу.
   Звук: браузер пускает его только после нажатия пользователя, поэтому
   на кнопках «Войти» и «Создать аккаунт» мы один раз разрешаем звук. */
(function () {
  'use strict';

  var SHOW_MS = 2600;   // сколько держим капельку
  var LEAVE_MS = 400;   // уход
  var SOUND_SRC = 'assets/sounds/login-success.mp3';

  var el = document.getElementById('capsule');
  if (!el) return;
  var initialsEl = document.getElementById('capsule-initials');
  var nickEl = document.getElementById('capsule-nick');
  var live = document.getElementById('capsule-live');
  var reduceMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var timers = [];

  // Один объект звука на страницу. Если браузер не поддерживает Audio, звука просто не будет
  var audio = null;
  try {
    audio = new Audio(SOUND_SRC);
    audio.preload = 'auto';
  } catch (e) {
    audio = null;
  }

  // Разрешаем звук на жесте: play() и сразу pause(). Ошибку молча игнорируем
  function unlockSound() {
    if (!audio) return;
    try {
      var p = audio.play();
      if (p && typeof p.then === 'function') {
        p.then(function () { audio.pause(); audio.currentTime = 0; }, function () { /* молча */ });
      }
    } catch (e) { /* молча */ }
  }

  function playSound() {
    if (!audio) return;
    try {
      audio.currentTime = 0;
      var p = audio.play();
      if (p && typeof p.catch === 'function') p.catch(function () { /* звук не дали: визуал всё равно показываем */ });
    } catch (e) { /* молча */ }
  }

  // Инициалы из ника, как в шапке: «Ivan_99» -> «IV»
  function initialsOf(nick) {
    var words = String(nick || '').trim().split(/[\s_]+/).filter(Boolean);
    var out = words[0] ? words[0].charAt(0) : '';
    if (words[1]) out += words[1].charAt(0);
    else if (words[0] && words[0].length > 1) out += words[0].charAt(1);
    return out.toUpperCase() || '?';
  }

  function clearTimers() {
    while (timers.length) clearTimeout(timers.pop());
  }

  function later(fn, ms) {
    timers.push(setTimeout(fn, ms));
  }

  // Капелька стоит под шапкой: её низ + 8 px. Шапка липкая, поэтому положение меняется только при изменении окна
  function placeCapsule() {
    var header = document.querySelector('.site-header');
    if (!header) return;
    el.style.top = Math.round(header.getBoundingClientRect().bottom + 8) + 'px';
  }

  // Ник берём через textContent: в нике может быть что угодно, HTML не разбираем
  function fill(user) {
    var nick = user && typeof user.nick === 'string' ? user.nick.trim() : '';
    if (initialsEl) initialsEl.textContent = initialsOf(nick);
    if (nickEl) nickEl.textContent = nick ? '«' + nick + '»' : '';
    paintPhoto(nick);
    return nick;
  }

  // Фото из профиля вместо инициалов. Нет фото или не загрузилось — остаются инициалы.
  var photo = null;
  function paintPhoto(nick) {
    if (!initialsEl || !nick) return;
    if (!photo) {
      photo = document.createElement('img');
      photo.className = 'capsule-photo';
      photo.alt = '';
      photo.decoding = 'async';
      photo.hidden = true;
      photo.addEventListener('error', function () { photo.hidden = true; });
      initialsEl.appendChild(photo);
    }
    photo.hidden = true;
    fetch('/api/profile/me', { headers: { Accept: 'application/json' } }).then(function (res) {
      return res.ok ? res.json() : null;
    }).then(function (d) {
      if (!d || d.ok !== true || !d.user || !d.user.has_avatar) return;
      photo.src = '/api/users/' + encodeURIComponent(nick) + '/avatar?v=' + encodeURIComponent(d.user.avatar_v || 0);
      photo.hidden = false;
    }).catch(function () { /* без фото остаются инициалы */ });
  }

  function hide() {
    clearTimers();
    el.classList.remove('is-on', 'is-leaving');
  }

  function leave() {
    el.classList.add('is-leaving');
  }

  // Показываем заново: если капелька уже видна, таймеры перезапускаются, копии не появляются
  var shownOnce = false;
  function show(user) {
    shownOnce = true;
    clearTimers();
    var nick = fill(user);
    placeCapsule();
    // Вращение обводки только там, где работает @property (иначе градиент статичный)
    el.classList.toggle('has-angle', !!(window.CSS && CSS.registerProperty));
    el.classList.remove('is-on', 'is-leaving');
    void el.offsetWidth; // перезапуск CSS-анимаций
    el.classList.add('is-on');

    // Одна живая область для программ чтения экрана
    if (live) {
      live.textContent = '';
      later(function () {
        live.textContent = 'Успешный вход' + (nick ? ', ' + nick : '');
      }, 80);
    }

    if (reduceMotion && reduceMotion.matches) {
      // Без движения: статичная капелька 3 секунды, потом скрываем сразу
      later(hide, SHOW_MS + LEAVE_MS);
    } else {
      later(leave, SHOW_MS);
      later(hide, SHOW_MS + LEAVE_MS);
    }
  }

  document.addEventListener('zento:login', function (e) {
    var user = e && e.detail ? e.detail.user : null;
    show(user);
    playSound();
  });

  // Перезагрузка страницы с активным входом: капелька без звука (браузер звук без нажатия всё равно не даст)
  function onSession(detail) {
    var state = detail ? detail.state : '';
    if (state === 'out') { shownOnce = false; hide(); return; }
    if (state === 'in' && detail.user && !shownOnce) show(detail.user);
  }
  document.addEventListener('zento:session', function (e) { onSession(e && e.detail ? e.detail : null); });
  if (window.zentoSession) onSession(window.zentoSession);

  window.addEventListener('resize', function () {
    if (el.classList.contains('is-on')) placeCapsule();
  });

  ['login-submit', 'register-submit', 'reset-confirm-submit'].forEach(function (id) {
    var btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', unlockSound);
  });
})();
