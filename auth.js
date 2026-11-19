/* ZENTO: вход, регистрация и восстановление пароля. Экран входа #gate (обязательный), сайт #site скрыт до входа.
   Аватар в шапке, капча — window.ZentoCaptcha (captcha.js). Сообщаем магазину (app.js) событием zento:session.
   Данные с сервера выводим только через textContent. Правила полей совпадают с договором 2.
   Клавиатура: фокус на поле ставим только после нажатия пользователя (клик или отправка формы),
   но не при загрузке страницы и не после ответа сервера. */
(function () {
  'use strict';

  var NET_MSG = 'Нет связи с сервером, попробуйте ещё раз';
  var MAIL_MSG = 'Не получилось отправить письмо. Попробуйте позже';
  var SENT_MSG = 'Если аккаунт найден, код отправлен на почту';
  var TICKET_MS = 290 * 1000; // билет живёт 300 с, держим небольшой запас
  var RESEND_MS = 60 * 1000; // «Отправить код снова» доступна через 60 секунд
  var NICK_RE = /^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_]+$/;
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
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
    gate: $('gate'),
    gateCard: $('gate-card'),
    gateDown: $('gate-down'),
    site: $('site'),
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
    forgot: $('forgot-btn'),
    pwMeter: $('pw-meter'),
    pwHint: $('pw-hint'),
    toast: $('toast'),
    resetDialog: $('reset-dialog'),
    resetClose: $('reset-close'),
    resetTitle: $('reset-title'),
    resetStep1: $('reset-step1'),
    resetStep2: $('reset-step2'),
    resetRequestForm: $('reset-request-form'),
    resetConfirmForm: $('reset-confirm-form'),
    resetRequestSubmit: $('reset-request-submit'),
    resetConfirmSubmit: $('reset-confirm-submit'),
    resetRequestError: $('reset-request-error'),
    resetConfirmError: $('reset-confirm-error'),
    resetNote: $('reset-note'),
    resetResend: $('reset-resend'),
    resetTimer: $('reset-timer'),
    resetBack: $('reset-back')
  };

  // Ключ поля -> [id поля ввода, id его ошибки]
  var F = {
    login: ['l-login', 'le-login'],
    lpass: ['l-pass', 'le-pass'],
    nick: ['r-nick', 're-nick'],
    pass: ['r-pass', 're-pass'],
    pass2: ['r-pass2', 're-pass2'],
    email: ['r-email', 're-email'],
    consent: ['r-consent', 're-consent'],
    rlogin: ['rq-login', 'rq-error'],
    code: ['rc-code', 'rc-code-err'],
    rpass: ['rc-pass', 're-rpass'],
    rpass2: ['rc-pass2', 're-rpass2']
  };
  var LOGIN_KEYS = ['login', 'lpass'];
  var REG_KEYS = ['nick', 'pass', 'pass2', 'email', 'consent'];
  var RESET_KEYS = ['code', 'rpass', 'rpass2'];
  // Поля из ответа сервера (fields) -> наши ключи; для каждой формы свой набор
  var REG_MAP = { nick: 'nick', password: 'pass', password2: 'pass2', email: 'email', agree: 'consent' };
  var RESET_REQ_MAP = { login: 'rlogin' };
  var RESET_MAP = { code: 'code', password: 'rpass', password2: 'rpass2' };

  var user = null;
  var busy = false;
  var ticket = '';
  var ticketExp = 0;
  var notifyTimer = 0;
  var gateTimer = 0;
  // Состояние окна восстановления: чей логин, и когда снова можно отправить код
  var resend = { login: '', until: 0, timer: 0 };

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

  // Фокус на первое поле с ошибкой. Вызываем только из обработчика отправки формы (нажатие пользователя)
  function focusFirstInvalid(keys) {
    for (var i = 0; i < keys.length; i++) {
      var inp = input(keys[i]);
      if (inp && inp.getAttribute('aria-invalid') === 'true') { inp.focus(); return; }
    }
  }

  // Разбирает fields из ответа сервера: ставит ошибки у полей, остальное отдаёт текстом
  function applyFields(d, map) {
    var rest = [];
    var shown = false;
    var fields = d && d.fields;
    if (fields && typeof fields === 'object') {
      Object.keys(fields).forEach(function (sk) {
        var msg = String(fields[sk] || '');
        var key = map[sk];
        if (!msg) return;
        if (key && F[key]) { setErr(key, msg); shown = true; }
        else rest.push(msg);
      });
    }
    return { shown: shown, rest: rest };
  }

  function setBusy(on) {
    busy = on;
    els.loginSubmit.disabled = on;
    els.registerSubmit.disabled = on;
    els.resetRequestSubmit.disabled = on;
    els.resetConfirmSubmit.disabled = on;
    renderResend();
  }

  /* ---------- Аватар ---------- */
  // Инициалы из ника: «Ivan_99» -> «IV», «Анна Петрова» -> «АП»
  function initialsOf(nick) {
    var words = String(nick || '').trim().split(/[\s_]+/).filter(Boolean);
    var out = words[0] ? words[0].charAt(0) : '';
    if (words[1]) out += words[1].charAt(0);
    else if (words[0] && words[0].length > 1) out += words[0].charAt(1);
    return out.toUpperCase() || '?';
  }

  function swapAnim() {
    restart(els.btn, 'is-swap');
  }

  function setUser(u) {
    user = u && typeof u === 'object' ? u : null;
    if (user) {
      var nick = String(user.nick || '');
      var mail = String(user.email || '');
      els.guest.toggleAttribute('hidden', true);
      els.initials.hidden = false;
      els.initials.textContent = initialsOf(nick);
      els.btn.classList.add('is-user');
      els.btn.setAttribute('aria-label', 'Аккаунт: ' + (nick || 'вход выполнен') + '. Открыть меню');
      els.btn.setAttribute('aria-haspopup', 'true');
      els.btn.setAttribute('aria-expanded', 'false');
      els.accName.textContent = nick || 'Аккаунт';
      els.accMail.textContent = mail;
      els.accMail.hidden = !mail;
    } else {
      els.guest.toggleAttribute('hidden', false);
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

  /* ---------- Экран входа и сайт ---------- */
  // Сообщаем магазину (app.js), вошли ли: 'in' — показать товары, 'out' — убрать их
  function emitSession(state) {
    try {
      document.dispatchEvent(new CustomEvent('zento:session', { detail: { state: state } }));
    } catch (e) { /* старые браузеры: магазин останется пустым */ }
  }

  // Экран входа обязательный: без крестика и без закрытия по клику в фон.
  // state: 'guest' — форма входа и регистрации; 'down' — только сообщение, что сайт недоступен
  // Фокус здесь не ставим: страница открывается без клавиатуры
  function showGate(state) {
    clearTimeout(gateTimer);
    els.gate.classList.remove('is-leaving');
    els.gate.hidden = false;
    els.site.hidden = true;
    els.gateCard.hidden = state !== 'guest';
    els.gateDown.hidden = state !== 'down';
    if (state === 'guest') setTab('login');
  }

  // Экран входа мягко растворяется, сайт проявляется под ним
  function hideGate() {
    clearTimeout(gateTimer);
    if (els.gate.hidden) return;
    if (reduceMotion.matches) { els.gate.hidden = true; return; }
    els.gate.classList.add('is-leaving');
    gateTimer = setTimeout(function () {
      els.gate.hidden = true;
      els.gate.classList.remove('is-leaving');
    }, 400);
  }

  function showSite() {
    els.site.hidden = false;
    restart(els.site, 'is-entering');
  }

  // Вошли: сайт показываем сразу, без перезагрузки. focusAvatar — только если вход сделал сам пользователь
  function enterSite(focusAvatar) {
    showSite();
    hideGate();
    emitSession('in');
    if (focusAvatar) els.btn.focus({ preventScroll: true });
  }

  // Вышли или сессия кончилась: снова экран входа, товары и корзина скрыты.
  // byUser — выход нажал пользователь: тогда фокус на вкладку «Вход»
  function leaveSite(byUser) {
    setUser(null);
    closeMenu();
    showGate('guest');
    if (byUser) els.tabLogin.focus({ preventScroll: true });
    emitSession('out');
  }

  // Сначала узнаём, кто вошёл. Нет ответа или это не наш сервер (GitHub Pages) — «сайт временно недоступен»
  function loadMe() {
    request('GET', '/api/auth/me').then(function (r) {
      var d = r.data || {};
      if (r.status !== 200 || d.ok !== true) { showGate('down'); return; }
      if (d.user && typeof d.user === 'object') {
        setUser(d.user);
        enterSite(false);
      } else {
        showGate('guest');
        emitSession('out');
      }
    }).catch(function () { showGate('down'); });
  }

  /* ---------- Вкладки «Вход» и «Регистрация» ---------- */
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

  /* ---------- Проверки (как на сервере) ---------- */
  function checkNick(v) {
    var s = v.trim();
    if (!s) return 'Придумайте ник';
    if (s.length < 3) return 'Ник: от 3 до 20 символов';
    if (s.length > 20) return 'Ник: не больше 20 символов';
    if (!NICK_RE.test(s)) return 'В нике только буквы, цифры и знак _';
    return '';
  }

  // Почта необязательна: пусто — проверки нет
  function checkEmail(v) {
    var s = v.trim();
    if (!s) return '';
    if (s.length > 254 || !EMAIL_RE.test(s)) return 'Почта выглядит неверно, например name@ukr.net';
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

  function readRegister() {
    return {
      nick: input('nick').value,
      pass: input('pass').value,
      pass2: input('pass2').value,
      email: input('email').value,
      consent: input('consent').checked
    };
  }

  // Проверка всей формы регистрации; возвращает {ключ: текст} и ставит ошибки у полей
  function validateRegister(v) {
    var errs = {};
    errs.nick = checkNick(v.nick);
    errs.pass = checkPassword(v.pass);
    errs.pass2 = checkPassword2(v.pass2, v.pass);
    errs.email = checkEmail(v.email);
    errs.consent = v.consent ? '' : 'Нужно принять правила сервиса';
    REG_KEYS.forEach(function (k) { setErr(k, errs[k]); });
    return errs;
  }

  function hasErrors(errs) {
    return Object.keys(errs).some(function (k) { return !!errs[k]; });
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
          // Ошибки уже видны у полей; фокус после капчи сами не ставим
          if (hasErrors(errs)) return;
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
      nick: v.nick.trim(),
      password: v.pass,
      password2: v.pass2,
      agree: true,
      captcha_ticket: ticket
    };
    var mail = v.email.trim().toLowerCase();
    if (mail) body.email = mail; // пустую почту не отправляем
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
      if (d.error === 'rules_not_accepted') {
        setErr('consent', 'Чтобы создать аккаунт, примите правила сервиса');
        return;
      }
      if (r.status === 409 || d.error === 'nick_taken' || d.error === 'email_taken') {
        var res = applyFields(d, REG_MAP);
        if (!res.shown && !res.rest.length) {
          if (d.error === 'email_taken') setErr('email', 'Эта почта уже зарегистрирована');
          else setErr('nick', 'Этот ник уже занят');
        }
        formErr(els.registerError, res.rest.join(' '));
        return;
      }
      if (r.status === 400 || d.error === 'validation_error') {
        var res2 = applyFields(d, REG_MAP);
        formErr(els.registerError, res2.rest.join(' ') || (res2.shown ? '' : (d.message || 'Проверьте поля формы')));
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
        formErr(els.loginError, 'Неверная почта, ник или пароль');
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
    clearKeys(LOGIN_KEYS.concat(REG_KEYS));
    formErr(els.loginError, '');
    formErr(els.registerError, '');
    updateStrength();
    enterSite(true);
    document.dispatchEvent(new CustomEvent('zento:login', { detail: { user: u } }));
  }

  function logout() {
    if (busy) return;
    request('POST', '/api/auth/logout', {}).then(function (r) {
      if (r.status === 200 && (r.data.ok !== false)) {
        leaveSite(true);
        notify('Вы вышли из аккаунта');
        return;
      }
      notify(r.data.message || 'Не получилось выйти. Попробуйте ещё раз.');
      els.btn.focus();
    }).catch(function () {
      notify(NET_MSG);
      els.btn.focus();
    });
  }

  /* ---------- Окно «Забыли пароль?» ---------- */
  // Таймер «Отправить код снова»: считаем по времени, а не по тикам, чтобы не сбивался в фоне
  function resendLeft() {
    return Math.max(0, Math.ceil((resend.until - Date.now()) / 1000));
  }

  function renderResend() {
    var left = resendLeft();
    els.resetResend.disabled = busy || left > 0;
    els.resetTimer.textContent = left > 0 ? 'через ' + left + ' с' : '';
  }

  function startResend() {
    clearInterval(resend.timer);
    resend.until = Date.now() + RESEND_MS;
    resend.timer = setInterval(function () {
      renderResend();
      if (resendLeft() <= 0) { clearInterval(resend.timer); resend.timer = 0; }
    }, 1000);
    renderResend();
  }

  function stopResend() {
    clearInterval(resend.timer);
    resend.timer = 0;
    resend.until = 0;
    resend.login = '';
  }

  function showResetStep(n) {
    els.resetStep1.hidden = n !== 1;
    els.resetStep2.hidden = n !== 2;
  }

  // Сбрасываем окно к первому шагу: без старых кодов, паролей и ошибок
  function resetDialogState() {
    stopResend();
    input('rlogin').value = '';
    input('code').value = '';
    input('rpass').value = '';
    input('rpass2').value = '';
    clearKeys(['rlogin'].concat(RESET_KEYS));
    formErr(els.resetRequestError, '');
    formErr(els.resetConfirmError, '');
    els.resetNote.textContent = '';
    els.resetRequestSubmit.textContent = 'Отправить код';
    els.resetConfirmSubmit.textContent = 'Сохранить и войти';
    showResetStep(1);
    renderResend();
  }

  function openReset() {
    var dlg = els.resetDialog;
    resetDialogState();
    input('rlogin').value = input('login').value.trim();
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    // Окно открыл сам пользователь (клик по ссылке), поэтому фокус на поле почты или ника — можно
    input('rlogin').focus({ preventScroll: true });
  }

  function closeReset() {
    var dlg = els.resetDialog;
    if (dlg.open) dlg.close(); // событие close сбросит окно
    else resetDialogState();
  }

  // Шаг 1 — отправка запроса. again — повторная отправка с шага 2 (из кнопки «Отправить код снова»)
  function sendResetRequest(login, again) {
    if (busy) return;
    var box = again ? els.resetConfirmError : els.resetRequestError;
    formErr(box, '');
    setBusy(true);
    if (!again) els.resetRequestSubmit.textContent = 'Отправляем…';
    request('POST', '/api/auth/reset/request', { login: login }).then(function (r) {
      var d = r.data || {};
      if (r.status === 200 && d.ok) {
        // Ответ всегда один и тот же: не показываем, есть ли аккаунт
        resend.login = login;
        els.resetNote.textContent = SENT_MSG;
        showResetStep(2);
        startResend();
        if (!again) els.resetTitle.focus({ preventScroll: true });
        return;
      }
      if (r.status === 429 || d.error === 'too_many_requests') {
        formErr(box, d.message || 'Слишком много запросов. Подождите немного');
        return;
      }
      if (r.status === 503 || d.error === 'mail_failed') {
        formErr(box, MAIL_MSG);
        return;
      }
      if (r.status === 400 || d.error === 'validation_error') {
        var res = applyFields(d, RESET_REQ_MAP);
        formErr(box, res.rest.join(' ') || (res.shown ? '' : (d.message || 'Введите почту или ник')));
        return;
      }
      formErr(box, d.message || 'Не получилось отправить код. Попробуйте ещё раз.');
    }).catch(function () {
      formErr(box, NET_MSG);
    }).then(function () {
      setBusy(false);
      if (!again) els.resetRequestSubmit.textContent = 'Отправить код';
    });
  }

  function readReset() {
    return {
      code: input('code').value,
      pass: input('rpass').value,
      pass2: input('rpass2').value
    };
  }

  function validateReset(v) {
    var errs = {};
    errs.code = /^[0-9]{6}$/.test(v.code.trim()) ? '' : 'Введите 6 цифр из письма';
    errs.rpass = checkPassword(v.pass);
    errs.rpass2 = checkPassword2(v.pass2, v.pass);
    RESET_KEYS.forEach(function (k) { setErr(k, errs[k]); });
    return errs;
  }

  // Шаг 2 — код и новый пароль. Успех — сразу сайт, как после входа
  function sendResetConfirm(v) {
    if (busy) return;
    formErr(els.resetConfirmError, '');
    setBusy(true);
    els.resetConfirmSubmit.textContent = 'Сохраняем…';
    request('POST', '/api/auth/reset/confirm', {
      login: resend.login,
      code: v.code.trim(),
      password: v.pass,
      password2: v.pass2
    }).then(function (r) {
      var d = r.data || {};
      var box = els.resetConfirmError;
      if (r.status === 200 && d.ok && d.user) {
        closeReset();
        onAuthed(d.user);
        return;
      }
      if (d.error === 'code_invalid') {
        setErr('code', 'Код неверный. Проверьте письмо и введите его ещё раз');
        return;
      }
      if (d.error === 'code_expired') {
        formErr(box, 'Код истёк. Нажмите «Отправить код снова»');
        return;
      }
      if (d.error === 'code_attempts_exceeded') {
        formErr(box, 'Слишком много неверных попыток. Нажмите «Отправить код снова»');
        return;
      }
      if (d.error === 'mail_failed') {
        formErr(box, MAIL_MSG);
        return;
      }
      if (r.status === 429 || d.error === 'too_many_attempts') {
        formErr(box, d.message || 'Слишком много попыток. Подождите немного');
        return;
      }
      if (r.status === 400 || d.error === 'validation_error') {
        var res = applyFields(d, RESET_MAP);
        formErr(box, res.rest.join(' ') || (res.shown ? '' : (d.message || 'Проверьте поля формы')));
        return;
      }
      formErr(box, d.message || 'Не получилось сохранить пароль. Попробуйте ещё раз.');
    }).catch(function () {
      formErr(els.resetConfirmError, NET_MSG);
    }).then(function () {
      setBusy(false);
      els.resetConfirmSubmit.textContent = 'Сохранить и войти';
    });
  }

  /* ---------- Привязка событий ---------- */
  function bind() {
    // Гость шапку не видит, поэтому меню только для вошедшего
    els.btn.addEventListener('click', function () {
      if (!user) return;
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

    // Магазин узнал, что вход кончился (401 на товарах): снова экран входа
    document.addEventListener('zento:need-login', function () {
      leaveSite(false);
      notify('Сессия закончилась. Войдите снова');
    });

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
      setErr('login', login ? '' : 'Введите почту или ник');
      setErr('lpass', pass ? '' : 'Введите пароль');
      if (!login || !pass) { focusFirstInvalid(LOGIN_KEYS); return; }
      sendLogin(login, pass);
    });
    LOGIN_KEYS.forEach(function (k) {
      input(k).addEventListener('input', function () { setErr(k, ''); });
    });
    els.forgot.addEventListener('click', openReset);

    // Регистрация
    ['nick', 'email', 'pass2'].forEach(function (k) {
      input(k).addEventListener('input', function () { setErr(k, ''); });
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

    // Окно восстановления
    els.resetDialog.addEventListener('close', resetDialogState);
    els.resetClose.addEventListener('click', closeReset);
    els.resetBack.addEventListener('click', function () {
      showResetStep(1);
      formErr(els.resetRequestError, '');
      input('rlogin').value = resend.login || input('rlogin').value;
      input('rlogin').focus({ preventScroll: true }); // нажатие пользователя
    });
    els.resetRequestForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var login = input('rlogin').value.trim();
      setErr('rlogin', login ? '' : 'Введите почту или ник');
      if (!login) { input('rlogin').focus(); return; }
      sendResetRequest(login, false);
    });
    input('rlogin').addEventListener('input', function () { setErr('rlogin', ''); });
    // Код — только цифры, не больше шести (вставка из письма тоже обрезается)
    input('code').addEventListener('input', function () {
      var c = input('code');
      var clean = c.value.replace(/\D/g, '').slice(0, 6);
      if (c.value !== clean) c.value = clean;
      setErr('code', '');
    });
    input('rpass').addEventListener('input', function () { setErr('rpass', ''); });
    input('rpass2').addEventListener('input', function () { setErr('rpass2', ''); });
    els.resetConfirmForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;
      var v = readReset();
      var errs = validateReset(v);
      if (hasErrors(errs)) { focusFirstInvalid(RESET_KEYS); return; }
      sendResetConfirm(v);
    });
    els.resetResend.addEventListener('click', function () {
      if (busy || resendLeft() > 0 || !resend.login) return;
      sendResetRequest(resend.login, true);
    });
  }

  function init() {
    if (!els.btn || !els.gate || !els.site || !els.loginForm || !els.registerForm || !els.resetDialog) return;
    bind();
    updateStrength();
    loadMe();
  }

  init();
})();
