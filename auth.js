/* ZENTO: вход и регистрация. Аватар в шапке, диалог #auth-dialog, капча — window.ZentoCaptcha (captcha.js).
   Данные с сервера выводим только через textContent. Правила полей совпадают с договором (CONTRACT.md). */
(function () {
  'use strict';

  var NET_MSG = 'Нет связи с сервером, попробуйте ещё раз';
  var TICKET_MS = 290 * 1000; // билет живёт 300 с, держим небольшой запас
  var LETTER_RE = /[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]/;
  var LOWER_RE = /[a-zа-яёіїєґ]/;
  var UPPER_RE = /[A-ZА-ЯЁІЇЄҐ]/;
  var SYMBOL_RE = /[^A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]/;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function $(id) { return document.getElementById(id); }

  var els = {
    btn: $('auth-btn'),
    guest: $('avatar-guest'),
    initials: $('avatar-initials'),
    menu: $('acc-menu'),
    accName: $('acc-name'),
    accMail: $('acc-mail'),
    logout: $('logout-btn'),
    dialog: $('auth-dialog'),
    tabs: $('auth-tabs'),
    tabLogin: $('tab-login'),
    tabRegister: $('tab-register'),
    panelLogin: $('panel-login'),
    panelRegister: $('panel-register'),
    loginForm: $('login-form'),
    registerForm: $('register-form'),
    loginError: $('login-error'),
    registerError: $('register-error'),
    loginSubmit: $('login-submit'),
    registerSubmit: $('register-submit'),
    phone: $('r-phone'),
    pwMeter: $('pw-meter'),
    pwHint: $('pw-hint'),
    toast: $('toast')
  };

  // Ключ поля -> [id поля ввода, id его ошибки]
  var F = {
    login: ['l-login', 'le-login'],
    lpass: ['l-pass', 'le-pass'],
    name: ['r-name', 're-name'],
    phone: ['r-phone', 're-phone'],
    email: ['r-email', 're-email'],
    pass: ['r-pass', 're-pass'],
    pass2: ['r-pass2', 're-pass2'],
    consent: ['r-consent', 're-consent']
  };
  var LOGIN_KEYS = ['login', 'lpass'];
  var REG_KEYS = ['name', 'phone', 'email', 'pass', 'pass2', 'consent'];
  // Ключи полей, которые приходят с сервера (fields), -> наши ключи
  var SERVER_KEY = { name: 'name', phone: 'phone', email: 'email', password: 'pass', consent: 'consent' };

  var user = null;
  var busy = false;
  var ticket = '';
  var ticketExp = 0;
  var notifyTimer = 0;

  /* ---------- Сеть ---------- */
  // Возвращает {status, data}. Сетевой сбой — отклонённый промис (сообщение NET_MSG).
  function request(method, url, body) {
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, 20000) : 0;
    var opts = {
      method: method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' }
    };
    if (ctl) opts.signal = ctl.signal;
    if (method !== 'GET') opts.body = JSON.stringify(body || {});
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (txt) {
        var data = {};
        try { data = JSON.parse(txt) || {}; } catch (e) { data = {}; }
        return { status: res.status, data: data };
      });
    }).then(function (r) {
      clearTimeout(timer);
      return r;
    }, function () {
      clearTimeout(timer);
      throw new Error('network');
    });
  }

  /* ---------- Помощники ---------- */
  function notify(msg) {
    var box = els.toast;
    if (!box) return;
    box.textContent = msg;
    box.classList.add('is-on');
    clearTimeout(notifyTimer);
    notifyTimer = setTimeout(function () { box.classList.remove('is-on'); }, 2800);
  }

  function restart(node, cls) {
    if (!node || reduceMotion.matches) return;
    node.classList.remove(cls);
    void node.offsetWidth;
    node.classList.add(cls);
  }

  function input(key) { return $(F[key][0]); }
  function errBox(key) { return $(F[key][1]); }

  // Ошибка у поля: текст в своём блоке и рамка; msg пустой — ошибку снимаем
  function setErr(key, msg) {
    var inp = input(key);
    var box = errBox(key);
    if (!inp || !box) return;
    if (!msg) {
      inp.removeAttribute('aria-invalid');
      box.textContent = '';
      box.hidden = true;
      return;
    }
    inp.setAttribute('aria-invalid', 'true');
    box.textContent = msg;
    box.hidden = false;
    restart(inp, 'is-flash');
  }

  function clearKeys(keys) { keys.forEach(function (k) { setErr(k, ''); }); }

  function formErr(box, msg) {
    box.textContent = msg || '';
    box.hidden = !msg;
  }

  function focusFirstInvalid(keys) {
    for (var i = 0; i < keys.length; i++) {
      var inp = input(keys[i]);
      if (inp && inp.getAttribute('aria-invalid') === 'true') { inp.focus(); return; }
    }
  }

  function setBusy(on) {
    busy = on;
    els.loginSubmit.disabled = on;
    els.registerSubmit.disabled = on;
  }

  /* ---------- Аватар ---------- */
  function initialsOf(name) {
    var words = String(name || '').trim().split(/\s+/).filter(Boolean);
    var out = '';
    if (words[0]) out += words[0].charAt(0);
    if (words[1]) out += words[1].charAt(0);
    return out.toUpperCase() || '?';
  }

  function swapAnim() {
    restart(els.btn, 'is-swap');
  }

  function setUser(u) {
    user = u && typeof u === 'object' ? u : null;
    if (user) {
      var name = String(user.name || '');
      els.guest.hidden = true;
      els.initials.hidden = false;
      els.initials.textContent = initialsOf(name);
      els.btn.classList.add('is-user');
      els.btn.setAttribute('aria-label', 'Аккаунт: ' + name + '. Открыть меню');
      els.btn.setAttribute('aria-haspopup', 'true');
      els.btn.setAttribute('aria-expanded', 'false');
      els.accName.textContent = name;
      els.accMail.textContent = String(user.email || '');
    } else {
      els.guest.hidden = false;
      els.initials.hidden = true;
      els.initials.textContent = '';
      els.btn.classList.remove('is-user');
      els.btn.setAttribute('aria-label', 'Войти или зарегистрироваться');
      els.btn.setAttribute('aria-haspopup', 'dialog');
      els.btn.removeAttribute('aria-expanded');
      closeMenu();
    }
    swapAnim();
  }

  function isMenuOpen() { return !els.menu.hidden; }
  function openMenu() {
    els.menu.hidden = false;
    els.btn.setAttribute('aria-expanded', 'true');
    els.logout.focus();
  }
  function closeMenu() {
    if (!isMenuOpen()) return;
    els.menu.hidden = true;
    els.btn.setAttribute('aria-expanded', 'false');
  }

  // Сначала узнаём, кто вошёл. Если сервера нет (сайт на GitHub Pages), аватар прячем
  function loadMe() {
    request('GET', '/api/auth/me').then(function (r) {
      if (r.status === 404) { els.btn.hidden = true; return; }
      if (r.status === 200 && r.data.ok) setUser(r.data.user || null);
    }).catch(function () { /* оставляем гостя */ });
  }

  /* ---------- Диалог и вкладки ---------- */
  function activePanel() { return els.tabLogin.getAttribute('aria-selected') === 'true' ? els.panelLogin : els.panelRegister; }

  function setTab(name) {
    var isLogin = name === 'login';
    els.tabLogin.setAttribute('aria-selected', String(isLogin));
    els.tabRegister.setAttribute('aria-selected', String(!isLogin));
    els.tabLogin.tabIndex = isLogin ? 0 : -1;
    els.tabRegister.tabIndex = isLogin ? -1 : 0;
    els.panelLogin.hidden = !isLogin;
    els.panelRegister.hidden = isLogin;
    clearKeys(LOGIN_KEYS.concat(REG_KEYS));
    formErr(els.loginError, '');
    formErr(els.registerError, '');
  }

  function openAuth() {
    if (els.dialog.open) return;
    closeMenu();
    setTab('login');
    els.dialog.showModal();
    var first = activePanel().querySelector('input');
    if (first) first.focus();
  }

  function closeAuth() {
    if (els.dialog.open) els.dialog.close();
  }

  // После закрытия фокус возвращаем на кнопку аватара
  function onDialogClose() {
    if (els.btn && document.body.contains(els.btn)) els.btn.focus({ preventScroll: true });
  }

  /* ---------- Проверки (как на сервере) ---------- */
  function checkName(v) {
    var s = v.trim();
    if (s.length < 2) return 'Введите имя: от 2 до 50 символов';
    if (s.length > 50) return 'Имя — не больше 50 символов';
    if (/[0-9<>]/.test(s)) return 'В имени не должно быть цифр и знаков < >';
    return '';
  }

  // В поле телефона всегда «+380 67 123 45 67»; возвращаем цифры без кода страны.
  // Код снимаем в цикле: при вставке в поле с уже стоящим +380 он может прийти дважды.
  function phoneNational(v) {
    var d = String(v || '').replace(/\D/g, '');
    while (d.indexOf('380') === 0) d = d.slice(3);
    return d;
  }

  function checkPhone(v) {
    var n = phoneNational(v);
    if (!n) return 'Введите телефон в формате +380 67 123 45 67';
    if (n.length !== 9) return 'Телефон: нужно 9 цифр после +380';
    return '';
  }

  function checkEmail(v) {
    var s = v.trim();
    if (!s) return 'Введите почту';
    if (s.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s)) return 'Почта выглядит неверно, например name@ukr.net';
    return '';
  }

  function checkPassword(v) {
    if (!v) return 'Придумайте пароль';
    if (v.length < 8) return 'Пароль: минимум 8 символов';
    if (v.length > 128) return 'Пароль: не больше 128 символов';
    if (!LETTER_RE.test(v) || !/[0-9]/.test(v)) return 'В пароле нужны и буквы, и цифры';
    return '';
  }

  function checkPassword2(v, pass) {
    if (!v) return 'Повторите пароль';
    if (v !== pass) return 'Пароли не совпадают';
    return '';
  }

  // 0 — пусто, 1 — слабый, 2 — средний, 3 — надёжный
  function strengthOf(v) {
    if (!v) return 0;
    if (checkPassword(v)) return 1;
    var features = 0;
    if (v.length >= 12) features++;
    if (LOWER_RE.test(v) && UPPER_RE.test(v)) features++;
    if (SYMBOL_RE.test(v)) features++;
    return features >= 2 ? 3 : 2;
  }

  var LEVEL_TEXT = ['', 'слабый', 'средний', 'надёжный'];

  function updateStrength() {
    var lvl = strengthOf(input('pass').value);
    els.pwMeter.setAttribute('data-level', String(lvl));
    els.pwHint.setAttribute('data-level', String(lvl));
    els.pwHint.textContent = lvl ? 'Надёжность пароля: ' + LEVEL_TEXT[lvl] : '';
  }

  // Маска телефона: всегда +380 и до 9 цифр
  function formatPhone(raw) {
    if (!String(raw || '').trim()) return '';
    var all = String(raw).replace(/\D/g, '');
    // Если пользователь стёр часть «380», это не номер: оставляем только «+380»
    var d = '380'.indexOf(all) === 0 ? '' : phoneNational(all);
    d = d.replace(/^0/, '').slice(0, 9);
    if (!d) return '+380';
    var parts = [d.slice(0, 2), d.slice(2, 5), d.slice(5, 7), d.slice(7, 9)].filter(Boolean);
    return '+380 ' + parts.join(' ');
  }

  function readRegister() {
    return {
      name: input('name').value,
      phone: input('phone').value,
      email: input('email').value,
      pass: input('pass').value,
      pass2: input('pass2').value,
      consent: input('consent').checked
    };
  }

  // Проверка всей формы регистрации; возвращает {ключ: текст} и ставит ошибки у полей
  function validateRegister(v) {
    var errs = {};
    errs.name = checkName(v.name);
    errs.phone = checkPhone(v.phone);
    errs.email = checkEmail(v.email);
    errs.pass = checkPassword(v.pass);
    errs.pass2 = checkPassword2(v.pass2, v.pass);
    errs.consent = v.consent ? '' : 'Нужно согласие на обработку персональных данных';
    REG_KEYS.forEach(function (k) { setErr(k, errs[k]); });
    return errs;
  }

  function hasErrors(errs) {
    return Object.keys(errs).some(function (k) { return !!errs[k]; });
  }

  // Ошибки с сервера (fields): по своим полям; что не удалось привязать — в общую строку
  function showServerFields(fields, keysOrder) {
    var rest = [];
    var shown = false;
    if (fields && typeof fields === 'object') {
      Object.keys(fields).forEach(function (sk) {
        var msg = String(fields[sk] || '');
        var key = SERVER_KEY[sk];
        if (key && F[key]) { setErr(key, msg); shown = true; }
        else if (msg) rest.push(msg);
      });
    }
    if (keysOrder) focusFirstInvalid(keysOrder);
    return { shown: shown, rest: rest };
  }

  /* ---------- Регистрация с капчей ---------- */
  function openCaptcha() {
    var cap = window.ZentoCaptcha;
    if (!cap || typeof cap.open !== 'function') {
      formErr(els.registerError, 'Проверка сейчас недоступна. Попробуйте позже.');
      return;
    }
    try {
      cap.open({
        onTicket: function (t) {
          ticket = String(t || '');
          ticketExp = Date.now() + TICKET_MS;
          var v = readRegister();
          var errs = validateRegister(v); // поля могли поменяться, пока шла капча
          if (hasErrors(errs)) { focusFirstInvalid(REG_KEYS); return; }
          sendRegister(v);
        },
        onCancel: function () {
          // Капчу закрыли без билета: просто возвращаем фокус на кнопку
          els.registerSubmit.focus();
        }
      });
    } catch (e) {
      formErr(els.registerError, 'Проверка сейчас недоступна. Попробуйте позже.');
    }
  }

  function sendRegister(v) {
    if (busy) return;
    formErr(els.registerError, '');
    setBusy(true);
    els.registerSubmit.textContent = 'Создаём аккаунт…';
    var body = {
      name: v.name.trim(),
      phone: '+380' + phoneNational(v.phone),
      email: v.email.trim().toLowerCase(),
      password: v.pass,
      captcha_ticket: ticket,
      consent: true
    };
    request('POST', '/api/auth/register', body).then(function (r) {
      var d = r.data || {};
      if ((r.status === 201 || r.status === 200) && d.ok && d.user) {
        ticket = '';
        onAuthed(d.user);
        return;
      }
      if (r.status === 403 || d.error === 'captcha_required') {
        // Билет уже не годится: снова пройти проверку
        ticket = '';
        formErr(els.registerError, 'Нужно пройти проверку ещё раз.');
        openCaptcha();
        return;
      }
      if (r.status === 409 || d.error === 'already_registered') {
        var res = showServerFields(d.fields, ['phone', 'email']);
        if (!res.shown) formErr(els.registerError, d.message || 'Такой телефон или почта уже зарегистрированы');
        else if (res.rest.length) formErr(els.registerError, res.rest.join(' '));
        return;
      }
      if (r.status === 400 || d.error === 'validation_error') {
        var res2 = showServerFields(d.fields, REG_KEYS);
        if (res2.rest.length || !res2.shown) formErr(els.registerError, res2.rest.join(' ') || d.message || 'Проверьте поля формы');
        return;
      }
      if (r.status === 429) {
        formErr(els.registerError, d.message || 'Слишком много попыток. Подождите немного');
        return;
      }
      formErr(els.registerError, d.message || 'Не получилось создать аккаунт. Попробуйте ещё раз.');
    }).catch(function () {
      formErr(els.registerError, NET_MSG);
    }).then(function () {
      setBusy(false);
      els.registerSubmit.textContent = 'Создать аккаунт';
    });
  }

  /* ---------- Вход ---------- */
  function sendLogin(login, pass) {
    if (busy) return;
    formErr(els.loginError, '');
    setBusy(true);
    els.loginSubmit.textContent = 'Входим…';
    request('POST', '/api/auth/login', { login: login, password: pass }).then(function (r) {
      var d = r.data || {};
      if (r.status === 200 && d.ok && d.user) {
        onAuthed(d.user);
        return;
      }
      if (r.status === 429 || d.error === 'too_many_attempts') {
        formErr(els.loginError, 'Слишком много попыток. Подождите немного');
      } else if (r.status === 401 || d.error === 'invalid_credentials') {
        formErr(els.loginError, 'Неверный телефон или пароль');
        input('lpass').focus();
      } else {
        formErr(els.loginError, d.message || 'Не получилось войти. Попробуйте ещё раз.');
      }
    }).catch(function () {
      formErr(els.loginError, NET_MSG);
    }).then(function () {
      setBusy(false);
      els.loginSubmit.textContent = 'Войти';
    });
  }

  function onAuthed(u) {
    setUser(u);
    els.loginForm.reset();
    els.registerForm.reset();
    updateStrength();
    closeAuth();
    notify('Добро пожаловать, ' + String(u.name || '') + '!');
  }

  function logout() {
    if (busy) return;
    request('POST', '/api/auth/logout', {}).then(function (r) {
      if (r.status === 200 && (r.data.ok !== false)) {
        setUser(null);
        notify('Вы вышли из аккаунта');
      } else {
        notify(r.data.message || 'Не получилось выйти. Попробуйте ещё раз.');
      }
    }).catch(function () {
      notify(NET_MSG);
    }).then(function () {
      els.btn.focus();
    });
  }

  /* ---------- Привязка событий ---------- */
  function bind() {
    els.btn.addEventListener('click', function () {
      if (!user) { openAuth(); return; }
      if (isMenuOpen()) closeMenu(); else openMenu();
    });

    document.addEventListener('click', function (e) {
      if (!isMenuOpen()) return;
      var t = e.target;
      if (els.menu.contains(t) || els.btn.contains(t)) return;
      closeMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isMenuOpen()) { closeMenu(); els.btn.focus(); }
    });

    els.logout.addEventListener('click', logout);

    els.dialog.addEventListener('click', function (e) {
      var t = e.target;
      if (t === els.dialog || (t.closest && t.closest('[data-auth-close]'))) closeAuth();
    });
    els.dialog.addEventListener('close', onDialogClose);

    els.tabLogin.addEventListener('click', function () { setTab('login'); });
    els.tabRegister.addEventListener('click', function () { setTab('register'); });
    // Стрелки переключают вкладки, Home и End — к первой и последней
    els.tabs.addEventListener('keydown', function (e) {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(e.key) === -1) return;
      e.preventDefault();
      var current = els.tabLogin.getAttribute('aria-selected') === 'true' ? 'login' : 'register';
      var next = current === 'login' ? 'register' : 'login';
      if (e.key === 'Home') next = 'login';
      if (e.key === 'End') next = 'register';
      setTab(next);
      (next === 'login' ? els.tabLogin : els.tabRegister).focus();
    });

    // Вход
    els.loginForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var login = input('login').value.trim();
      var pass = input('lpass').value;
      setErr('login', login ? '' : 'Введите телефон или почту');
      setErr('lpass', pass ? '' : 'Введите пароль');
      if (!login || !pass) { focusFirstInvalid(LOGIN_KEYS); return; }
      sendLogin(login, pass);
    });
    LOGIN_KEYS.forEach(function (k) {
      input(k).addEventListener('input', function () { setErr(k, ''); });
    });

    // Регистрация
    var regInputs = ['name', 'email', 'pass2'];
    regInputs.forEach(function (k) {
      input(k).addEventListener('input', function () { setErr(k, ''); });
    });
    els.phone.addEventListener('focus', function () {
      if (!els.phone.value) els.phone.value = '+380 ';
    });
    els.phone.addEventListener('blur', function () {
      if (phoneNational(els.phone.value) === '') els.phone.value = '';
    });
    els.phone.addEventListener('input', function () {
      els.phone.value = formatPhone(els.phone.value);
      setErr('phone', '');
    });
    input('pass').addEventListener('input', function () {
      setErr('pass', '');
      updateStrength();
    });
    input('consent').addEventListener('change', function () { setErr('consent', ''); });

    els.registerForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;
      var v = readRegister();
      var errs = validateRegister(v);
      if (hasErrors(errs)) { focusFirstInvalid(REG_KEYS); return; }
      if (ticket && Date.now() < ticketExp) { sendRegister(v); return; }
      ticket = '';
      openCaptcha();
    });
  }

  function init() {
    if (!els.btn || !els.dialog || !els.loginForm || !els.registerForm) return;
    bind();
    updateStrength();
    loadMe();
  }

  init();
})();
