/* ZENTO: окно «Мой профиль». Открывает auth.js событием zento:profile-open.
   Ник и описание: PATCH /api/profile. Фото: PUT или DELETE /api/profile/avatar и /banner.
   Фото сжимаем в браузере через canvas (заодно уходит EXIF). Превью тоже canvas: blob: и object URL не используем,
   потому что CSP разрешает картинки только с 'self' и data:.
   Всё, что пришло с сервера, выводим через textContent. */
(function () {
  'use strict';

  var NICK_RE = /^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_]+$/;
  var BIO_MAX = 300;
  var SOURCE_MAX = 15 * 1024 * 1024;
  var NOT_SUPPORTED = 'Этот формат не поддерживается. Сохраните фото как JPEG или сделайте скриншот';
  var NET_MSG = 'нет связи с сервером. Введённое не потеряно';
  // w, h — размер файла на выходе; cw, ch — кадр на экране (те же пропорции)
  var KIND = {
    avatar: { label: 'фото профиля', crop: 'фото профиля', w: 512, h: 512, cw: 600, ch: 600, max: 2 * 1024 * 1024, url: '/api/profile/avatar', noun: 'фото' },
    banner: { label: 'баннер', crop: 'баннер', w: 1500, h: 500, cw: 900, ch: 300, max: 4 * 1024 * 1024, url: '/api/profile/banner', noun: 'баннер' }
  };

  var dlg = document.getElementById('profile-dialog');
  if (!dlg || typeof dlg.showModal !== 'function') return;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function $(id) { return document.getElementById(id); }
  var el = {
    title: $('profile-title'),
    close: $('profile-close'),
    loading: $('pf-loading'),
    loadingText: $('pf-loading-text'),
    retry: $('pf-retry'),
    editor: $('pf-editor'),
    banner: $('pf-banner'),
    bannerView: $('pf-banner-view'),
    avatar: $('pf-avatar'),
    avatarView: $('pf-avatar-view'),
    avatarDel: $('pf-avatar-del'),
    bannerDel: $('pf-banner-del'),
    photoErr: $('pf-photo-err'),
    form: $('pf-form'),
    nick: $('pf-nick'),
    nickHint: $('pf-nick-hint'),
    nickErr: $('pf-nick-err'),
    bio: $('pf-bio'),
    bioCount: $('pf-bio-count'),
    bioErr: $('pf-bio-err'),
    crop: $('pf-crop'),
    cropTitle: $('pf-crop-title'),
    canvas: $('pf-canvas'),
    zoom: $('pf-zoom'),
    cropErr: $('pf-crop-err'),
    cropBack: $('pf-crop-back'),
    cropDone: $('pf-crop-done'),
    done: $('pf-done'),
    doneTitle: $('pf-done-title'),
    confirm: $('pf-confirm'),
    confirmTitle: $('pf-confirm-title'),
    stay: $('pf-stay'),
    leave: $('pf-leave'),
    foot: $('pf-foot'),
    dirty: $('pf-dirty'),
    status: $('pf-status'),
    cancel: $('pf-cancel'),
    save: $('pf-save'),
    file: $('pf-file'),
    toast: $('toast')
  };

  // st.base — что сейчас на сервере (после каждого успешного шага обновляется)
  // st.photos[kind] — выбор: keep (как есть), new (новое фото, есть canvas), delete (удалить)
  var st = {
    view: 'loading',
    seq: 0,
    loading: false,
    busy: false,
    opener: null,
    saved: false,
    base: null,
    photos: { avatar: none(), banner: none() },
    pick: '',
    crop: null,
    timer: 0
  };
  var toastTimer = 0;

  function none() { return { status: 'keep', blob: null, canvas: null }; }

  /* ---------- Помощники ---------- */
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function len(s) { return [...String(s)].length; }
  function dot(s) { return String(s).replace(/[.!?]?$/, '.'); }

  // Инициалы из ника: «Ivan_99» -> «IV»
  function initialsOf(nick) {
    var words = String(nick || '').trim().split(/[\s_]+/).filter(Boolean);
    var out = words[0] ? words[0].charAt(0) : '';
    if (words[1]) out += words[1].charAt(0);
    else if (words[0] && words[0].length > 1) out += words[0].charAt(1);
    return out.toUpperCase() || '?';
  }

  // Дата «DD.MM» по Киеву (для подсказки про 30 дней)
  function ddmm(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    try {
      var parts = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', timeZone: 'Europe/Kyiv' }).formatToParts(d);
      var day = '';
      var month = '';
      parts.forEach(function (p) {
        if (p.type === 'day') day = p.value;
        if (p.type === 'month') month = p.value;
      });
      if (day && month) return day + '.' + month;
    } catch (e) { /* старые браузеры: считаем по локальному времени */ }
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1);
  }

  function cooling() {
    return !!(st.base && st.base.next && Date.parse(st.base.next) > Date.now());
  }

  function toast(msg) {
    if (!el.toast) return;
    el.toast.textContent = msg;
    el.toast.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.toast.classList.remove('is-on'); }, 2800);
  }

  /* ---------- Сеть ---------- */
  // Ответ: {status, data}. Сбой сети — отказ с кодом network
  function api(method, url, body, type) {
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, 30000) : 0;
    var opts = { method: method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
    if (ctl) opts.signal = ctl.signal;
    if (body instanceof Blob) {
      opts.body = body;
      opts.headers['Content-Type'] = type;
    } else if (body !== undefined) {
      opts.body = JSON.stringify(body);
      opts.headers['Content-Type'] = 'application/json';
    }
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
      throw { code: 'network' };
    });
  }

  // Ошибка ответа сервера в одном виде: {code, message, fields}
  function failOf(r) {
    if (r.status === 401) return { code: 'auth' };
    var d = r.data || {};
    return { code: String(d.error || 'server'), message: String(d.message || ''), fields: d.fields || {} };
  }

  function errorText(err, fallback) {
    var code = err && err.code;
    if (code === 'network') return NET_MSG;
    if (code === 'nick_cooldown') {
      var f = err.fields && err.fields.nick;
      var when = f && !isNaN(Date.parse(f)) ? ddmm(f) : '';
      if (when) return 'Ник можно менять раз в 30 дней, следующий раз — ' + when;
      return f ? String(f) : 'Ник пока нельзя менять. Попробуйте позже';
    }
    if (code === 'nick_taken') return 'Этот ник уже занят. Придумайте другой';
    if (code === 'image_type_unsupported') return NOT_SUPPORTED;
    if (code === 'avatar_too_large') return 'Фото больше 2 МБ. Выберите другое';
    if (code === 'banner_too_large') return 'Баннер больше 4 МБ. Выберите другой';
    if (code === 'image_invalid') return 'Этот файл не получилось принять как картинку. Выберите другое фото';
    if (code === 'auth') return 'Сессия закончилась. Войдите снова';
    if (err && err.message) return err.message;
    return fallback;
  }

  /* ---------- Поля и вывод ---------- */
  function fieldErr(box, input, msg) {
    box.textContent = msg || '';
    box.hidden = !msg;
    if (input) {
      if (msg) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }
  }

  function setPhotoErr(msg) {
    el.photoErr.textContent = msg || '';
    el.photoErr.hidden = !msg;
  }

  function setCropErr(msg) {
    el.cropErr.textContent = msg || '';
    el.cropErr.hidden = !msg;
  }

  // Статус внизу: текст и, если нужно, «Повторить»
  function setStatus(msg, retry) {
    el.status.textContent = '';
    if (!msg) return;
    var box = document.createElement('div');
    box.className = 'form-error';
    var p = document.createElement('p');
    p.textContent = msg;
    box.appendChild(p);
    if (retry) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-ghost btn-small press';
      btn.textContent = 'Повторить';
      btn.addEventListener('click', save);
      box.appendChild(btn);
    }
    el.status.appendChild(box);
  }

  function nickValue() { return el.nick.value.trim(); }
  function bioText() { return el.bio.value.replace(/\r\n/g, '\n'); }
  function nickChanged() { return !!st.base && !el.nick.disabled && nickValue() !== st.base.nick; }
  function bioChanged() { return !!st.base && bioText() !== String(st.base.bio).replace(/\r\n/g, '\n'); }
  function photoChanged() { return st.photos.avatar.status !== 'keep' || st.photos.banner.status !== 'keep'; }
  function isDirty() { return nickChanged() || bioChanged() || photoChanged(); }

  function renderBio() {
    var n = len(bioText());
    el.bioCount.textContent = n + ' / ' + BIO_MAX;
    el.bioCount.classList.toggle('is-over', n > BIO_MAX);
  }

  function renderNick() {
    if (!st.base) return;
    var cool = cooling();
    el.nick.disabled = cool;
    el.nickHint.textContent = cool
      ? 'Ник можно менять раз в 30 дней, следующий раз — ' + ddmm(st.base.next)
      : 'От 3 до 20 символов: буквы, цифры и знак _. Ник можно менять раз в 30 дней.';
  }

  function placeholder(box, kind) {
    box.replaceChildren();
    if (kind === 'avatar') {
      var s = document.createElement('span');
      s.className = 'pf-initials';
      s.textContent = initialsOf(nickValue() || (st.base && st.base.nick) || '');
      box.appendChild(s);
    }
  }

  // Фото: новое (превью-canvas), сохранённое (картинка с сервера) или заглушка.
  // Ключ показа сравниваем, чтобы не перезагружать картинку при каждом нажатии клавиши
  function renderPhoto(kind) {
    if (!st.base) return;
    var box = kind === 'avatar' ? el.avatarView : el.bannerView;
    var btn = kind === 'avatar' ? el.avatar : el.banner;
    var del = kind === 'avatar' ? el.avatarDel : el.bannerDel;
    var ph = st.photos[kind];
    var noun = KIND[kind].noun;
    var key;

    if (ph.status === 'new' && ph.canvas) {
      key = ph.canvas;
    } else if (ph.status !== 'delete' && st.base['has_' + kind]) {
      var src = '/api/users/' + encodeURIComponent(st.base.nick) + '/' + kind +
        '?v=' + encodeURIComponent(String(st.base[kind + '_v'] || 0));
      key = 'img:' + src;
    } else {
      key = 'ph:' + (kind === 'avatar' ? initialsOf(nickValue() || st.base.nick) : '');
    }

    if (box._key !== key) {
      box._key = key;
      if (typeof key !== 'string') {
        key.className = 'pf-img';
        box.replaceChildren(key);
      } else if (key.indexOf('img:') === 0) {
        var img = document.createElement('img');
        img.className = 'pf-img';
        img.alt = '';
        img.decoding = 'async';
        img.addEventListener('error', function () {
          if (img.parentNode !== box) return;
          box._key = 'ph:';
          placeholder(box, kind);
          btn.classList.remove('has-photo');
        });
        img.src = key.slice(4);
        box.replaceChildren(img);
      } else {
        placeholder(box, kind);
      }
    }
    btn.classList.toggle('has-photo', typeof key !== 'string' || key.indexOf('img:') === 0);

    // Кнопка «Удалить…»: меняет смысл в зависимости от выбора
    if (ph.status === 'new') {
      del.hidden = false;
      del.textContent = 'Отменить выбор';
    } else if (ph.status === 'delete') {
      del.hidden = false;
      del.textContent = 'Оставить ' + noun;
    } else {
      del.hidden = !st.base['has_' + kind];
      del.textContent = 'Удалить ' + noun;
    }
  }

  function renderFoot() {
    var dirty = st.view === 'edit' && isDirty();
    el.dirty.hidden = !dirty;
    el.save.disabled = st.busy || !dirty;
    el.save.textContent = st.busy ? 'Сохраняю…' : 'Сохранить';
    el.cancel.disabled = st.busy;
    el.close.disabled = st.busy;
    el.nick.readOnly = st.busy;
    el.bio.readOnly = st.busy;
    el.avatar.disabled = st.busy;
    el.banner.disabled = st.busy;
    el.avatarDel.disabled = st.busy;
    el.bannerDel.disabled = st.busy;
    dlg.setAttribute('aria-busy', st.busy ? 'true' : 'false');
  }

  function renderAll() {
    renderNick();
    renderBio();
    renderPhoto('avatar');
    renderPhoto('banner');
    renderFoot();
  }

  // Какой экран показываем: loading | edit | crop | done
  function setView(name) {
    st.view = name;
    el.loading.hidden = name !== 'loading';
    el.editor.hidden = name !== 'edit';
    el.crop.hidden = name !== 'crop';
    el.done.hidden = name !== 'done';
    el.foot.hidden = name !== 'edit';
    el.confirm.hidden = true;
    renderFoot();
  }

  // Данные с сервера: base всегда; поля ввода — только при открытии (fill).
  // notify — сообщить шапке (auth.js), что изменились ник или фото
  function applyUser(u, fill, notify) {
    st.base = {
      nick: String(u.nick || ''),
      bio: String(u.bio || ''),
      has_avatar: !!u.has_avatar,
      avatar_v: u.avatar_v || 0,
      has_banner: !!u.has_banner,
      banner_v: u.banner_v || 0,
      next: u.nick_next_change_at || null
    };
    if (fill) {
      el.nick.value = st.base.nick;
      el.bio.value = st.base.bio;
    }
    renderAll();
    if (notify) document.dispatchEvent(new CustomEvent('zento:user-updated', { detail: { user: u } }));
  }

  /* ---------- Открытие, загрузка, закрытие ---------- */
  function reset() {
    clearTimeout(st.timer);
    if (st.crop) { try { st.crop.bitmap.close(); } catch (e) { /* уже закрыт */ } }
    st.crop = null;
    st.seq += 1;
    st.loading = false;
    st.busy = false;
    st.saved = false;
    st.base = null;
    st.pick = '';
    st.photos = { avatar: none(), banner: none() };
    el.nick.value = '';
    el.bio.value = '';
    fieldErr(el.nickErr, el.nick, '');
    fieldErr(el.bioErr, el.bio, '');
    setPhotoErr('');
    setCropErr('');
    setStatus('');
    el.file.value = '';
    el.avatarView.replaceChildren();
    el.bannerView.replaceChildren();
    el.avatarView._key = null;
    el.bannerView._key = null;
  }

  function open(opener) {
    if (dlg.open) return;
    reset();
    st.opener = opener || null;
    try { dlg.showModal(); } catch (e) { return; }
    if (!reduceMotion.matches && dlg.animate) {
      dlg.animate([
        { transform: 'translateY(40px)', opacity: 0 },
        { transform: 'translateY(0)', opacity: 1 }
      ], { duration: 320, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' });
    }
    // Фокус на заголовке: клавиатура на телефоне не открывается
    el.title.focus({ preventScroll: true });
    load();
  }

  function showLoadError(msg) {
    el.loadingText.textContent = msg || 'Загружаю профиль…';
    el.retry.hidden = !msg;
  }

  function load() {
    var seq = st.seq + 1;
    st.seq = seq;
    st.loading = true;
    showLoadError('');
    setView('loading');
    api('GET', '/api/profile/me').then(function (r) {
      if (seq !== st.seq) return;
      st.loading = false;
      if (r.status !== 200 || !r.data || r.data.ok !== true || !r.data.user) throw failOf(r);
      applyUser(r.data.user, true, false);
      setView('edit');
    }).catch(function (err) {
      if (seq !== st.seq) return;
      st.loading = false;
      if (err && err.code === 'auth') { sessionEnded(); return; }
      showLoadError(dot(errorText(err, 'Не получилось загрузить профиль. Попробуйте ещё раз')));
    });
  }

  // Вход кончился: закрываем окно и просим auth.js показать экран входа
  function sessionEnded() {
    if (dlg.open) dlg.close();
    document.dispatchEvent(new CustomEvent('zento:need-login'));
  }

  function showConfirm(on) {
    el.confirm.hidden = !on;
    if (on) el.confirmTitle.focus({ preventScroll: true });
    else el.cancel.focus({ preventScroll: true });
  }

  function requestClose() {
    if (st.busy) return;
    if (st.view === 'crop') { cancelCrop(); return; }
    if (st.view === 'edit' && isDirty()) { showConfirm(true); return; }
    if (dlg.open) dlg.close();
  }

  /* ---------- Выбор и проверка фото ---------- */
  function pickPhoto(kind) {
    if (st.busy || st.view !== 'edit') return;
    st.pick = kind;
    el.file.value = '';
    el.file.click();
  }

  // Первые байты файла: PNG, JPEG или WEBP. Расширение и тип из браузера не учитываем
  function sniff(file) {
    return file.slice(0, 16).arrayBuffer().then(function (buf) {
      var b = new Uint8Array(buf);
      function at(i, s) {
        for (var k = 0; k < s.length; k++) if (b[i + k] !== s.charCodeAt(k)) return false;
        return true;
      }
      if (b[0] === 0x89 && at(1, 'PNG\r\n\x1a\n')) return 'png';
      if (b[0] === 0xFF && b[1] === 0xD8 && b[2] === 0xFF) return 'jpeg';
      if (at(0, 'RIFF') && at(8, 'WEBP')) return 'webp';
      return null;
    });
  }

  function decode(file) {
    if (typeof createImageBitmap !== 'function') return Promise.reject(new Error('no'));
    return createImageBitmap(file, { imageOrientation: 'from-image' }).catch(function () {
      return createImageBitmap(file);
    });
  }

  function handleFile(kind, file) {
    setPhotoErr('');
    if (file.size > SOURCE_MAX) { setPhotoErr('Файл больше 15 МБ. Выберите фото поменьше'); return; }
    sniff(file).then(function (type) {
      if (!type) { setPhotoErr(NOT_SUPPORTED); return null; }
      return decode(file).then(function (bmp) {
        openCrop(kind, bmp);
      }, function () {
        setPhotoErr('Не получилось открыть это фото. Выберите другое');
      });
    }, function () {
      setPhotoErr('Не получилось прочитать файл. Выберите другой');
    });
  }

  /* ---------- Кадрирование ---------- */
  // Кадр: fw×fh; картинка в кадре — c.x, c.y (левый верхний угол), масштаб base*zoom
  function clampCrop(c) {
    var s = c.base * c.zoom;
    c.x = Math.min(0, Math.max(c.fw - c.bitmap.width * s, c.x));
    c.y = Math.min(0, Math.max(c.fh - c.bitmap.height * s, c.y));
  }

  // Рисуем кадр в canvas любого размера (пропорции те же). bg — белый фон (для JPEG)
  function paint(c, canvas, bg) {
    var ctx = canvas.getContext('2d');
    var k = canvas.width / c.fw;
    var s = c.base * c.zoom * k;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (bg) {
      ctx.fillStyle = '#FFFFFF';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(c.bitmap, c.x * k, c.y * k, c.bitmap.width * s, c.bitmap.height * s);
  }

  function paintCrop() {
    if (st.crop) paint(st.crop, el.canvas, false);
  }

  function openCrop(kind, bmp) {
    var cfg = KIND[kind];
    el.canvas.width = cfg.cw;
    el.canvas.height = cfg.ch;
    var c = { kind: kind, bitmap: bmp, fw: cfg.cw, fh: cfg.ch, zoom: 1, base: 1, x: 0, y: 0, drag: null };
    c.base = Math.max(c.fw / bmp.width, c.fh / bmp.height);
    c.x = (c.fw - bmp.width * c.base) / 2;
    c.y = (c.fh - bmp.height * c.base) / 2;
    st.crop = c;
    el.zoom.value = '1';
    el.cropTitle.textContent = 'Кадрирование: ' + cfg.crop;
    setCropErr('');
    setView('crop');
    paintCrop();
    el.cropTitle.focus({ preventScroll: true });
  }

  function setZoom(z) {
    var c = st.crop;
    if (!c) return;
    // Масштабируем вокруг центра кадра
    var s0 = c.base * c.zoom;
    var cx = (c.fw / 2 - c.x) / s0;
    var cy = (c.fh / 2 - c.y) / s0;
    c.zoom = z;
    var s1 = c.base * z;
    c.x = c.fw / 2 - cx * s1;
    c.y = c.fh / 2 - cy * s1;
    clampCrop(c);
    paintCrop();
  }

  function cancelCrop() {
    var c = st.crop;
    if (c) { try { c.bitmap.close(); } catch (e) { /* уже закрыт */ } }
    st.crop = null;
    setView('edit');
    (c && c.kind === 'banner' ? el.banner : el.avatar).focus({ preventScroll: true });
  }

  // Итог: 512×512 для аватара и 1500×500 для баннера, WEBP 0.85;
  // если браузер не умеет WEBP — JPEG 0.85 на белом фоне
  function toBlob(canvas, type, q) {
    return new Promise(function (resolve, reject) {
      canvas.toBlob(function (b) { if (b) resolve(b); else reject(new Error('blob')); }, type, q);
    });
  }

  function encode(c) {
    var cfg = KIND[c.kind];
    var out = document.createElement('canvas');
    out.width = cfg.w;
    out.height = cfg.h;
    paint(c, out, false);
    return toBlob(out, 'image/webp', 0.85).then(function (blob) {
      if (blob.type === 'image/webp') return { blob: blob, canvas: out };
      paint(c, out, true);
      return toBlob(out, 'image/jpeg', 0.85).then(function (b2) { return { blob: b2, canvas: out }; });
    }).then(function (res) {
      if (res.blob.size > cfg.max) throw new Error('big');
      return res;
    });
  }

  function setCropBusy(on) {
    el.cropDone.disabled = on;
    el.cropBack.disabled = on;
    el.cropDone.textContent = on ? 'Готовлю…' : 'Готово';
  }

  /* ---------- Сохранение ---------- */
  function validate() {
    var ok = true;
    var first = null;
    if (nickChanged()) {
      var v = nickValue();
      var n = len(v);
      if (n < 3 || n > 20 || !NICK_RE.test(v)) {
        fieldErr(el.nickErr, el.nick, 'От 3 до 20 символов: буквы, цифры и знак _');
        first = el.nick;
        ok = false;
      } else {
        fieldErr(el.nickErr, el.nick, '');
      }
    }
    if (len(bioText()) > BIO_MAX) {
      fieldErr(el.bioErr, el.bio, 'Описание длиннее 300 символов. Сократите текст');
      first = first || el.bio;
      ok = false;
    } else {
      fieldErr(el.bioErr, el.bio, '');
    }
    if (first) first.focus();
    return ok;
  }

  function saveText() {
    var body = {};
    if (nickChanged()) body.nick = nickValue();
    if (bioChanged()) body.bio = bioText();
    return api('PATCH', '/api/profile', body).then(function (r) {
      if (r.status !== 200 || !r.data || r.data.ok !== true || !r.data.user) throw failOf(r);
      applyUser(r.data.user, true, true);
    });
  }

  function savePhoto(kind) {
    var p = st.photos[kind];
    var url = KIND[kind].url;
    var req = p.status === 'new' ? api('PUT', url, p.blob, p.blob.type) : api('DELETE', url);
    return req.then(function (r) {
      if (r.status !== 200 || !r.data || r.data.ok !== true || !r.data.user) throw failOf(r);
      st.photos[kind] = none();
      applyUser(r.data.user, false, true);
    });
  }

  // Порядок: сначала ник и описание, потом фото. Шаг, который прошёл, больше не повторяем
  function save() {
    if (st.busy || st.view !== 'edit' || !isDirty()) return;
    if (!validate()) return;
    var steps = [];
    if (nickChanged() || bioChanged()) {
      var names = [];
      if (nickChanged()) names.push('ник');
      if (bioChanged()) names.push('описание');
      steps.push({ label: names.join(' и '), run: saveText });
    }
    ['avatar', 'banner'].forEach(function (k) {
      if (st.photos[k].status !== 'keep') {
        steps.push({ label: KIND[k].label, run: function () { return savePhoto(k); } });
      }
    });
    if (!steps.length) return;

    st.busy = true;
    setStatus('');
    renderFoot();
    var saved = [];
    var i = 0;
    (function next() {
      if (i >= steps.length) { finish(); return; }
      var step = steps[i];
      step.run().then(function () {
        saved.push(step.label);
        i += 1;
        next();
      }, function (err) {
        fail(steps, i, saved, err);
      });
    })();
  }

  function fail(steps, i, saved, err) {
    st.busy = false;
    renderFoot();
    if (err && err.code === 'auth') { sessionEnded(); return; }
    var step = steps[i];
    var text = '';
    if (saved.length) text += 'Сохранено: ' + saved.join(', ') + '. ';
    text += 'Не сохранилось: ' + step.label + '. ' + dot(errorText(err, 'Не получилось сохранить. Попробуйте ещё раз'));
    var rest = steps.slice(i + 1).map(function (s) { return s.label; });
    if (rest.length) text += ' Ещё не сохранено: ' + rest.join(', ') + '.';
    if (err && (err.code === 'nick_taken' || err.code === 'nick_cooldown')) {
      fieldErr(el.nickErr, el.nick, errorText(err, ''));
    }
    if (err && err.code === 'validation_error' && err.fields) {
      if (err.fields.nick) fieldErr(el.nickErr, el.nick, String(err.fields.nick));
      if (err.fields.bio) fieldErr(el.bioErr, el.bio, String(err.fields.bio));
    }
    setStatus(text, true);
  }

  function finish() {
    st.busy = false;
    st.saved = true;
    setStatus('');
    setView('done');
    el.doneTitle.focus({ preventScroll: true });
    st.timer = setTimeout(function () { if (dlg.open) dlg.close(); }, reduceMotion.matches ? 400 : 1000);
  }

  /* ---------- Связка событий ---------- */
  document.addEventListener('zento:profile-open', function (e) {
    open(e.detail && e.detail.opener);
  });

  // Escape: не закрываем во время загрузки и сохранения, при несохранённом тексте спрашиваем
  dlg.addEventListener('cancel', function (e) {
    if (st.busy || st.loading) { e.preventDefault(); return; }
    if (st.view === 'done') return;
    if (!el.confirm.hidden) { e.preventDefault(); showConfirm(false); return; }
    if (st.view === 'crop') { e.preventDefault(); cancelCrop(); return; }
    if (st.view === 'edit' && isDirty()) { e.preventDefault(); showConfirm(true); }
  });

  dlg.addEventListener('close', function () {
    var back = st.opener;
    var saved = st.saved;
    reset();
    st.opener = null;
    if (back && document.body.contains(back)) back.focus({ preventScroll: true });
    if (saved) toast('Профиль сохранён');
  });

  el.close.addEventListener('click', requestClose);
  el.cancel.addEventListener('click', requestClose);
  el.stay.addEventListener('click', function () { showConfirm(false); });
  el.leave.addEventListener('click', function () { if (dlg.open) dlg.close(); });
  el.retry.addEventListener('click', load);

  el.form.addEventListener('submit', function (e) { e.preventDefault(); save(); });
  el.save.addEventListener('click', save);

  el.nick.addEventListener('input', function () {
    fieldErr(el.nickErr, el.nick, '');
    renderPhoto('avatar');
    renderFoot();
  });
  el.bio.addEventListener('input', function () {
    fieldErr(el.bioErr, el.bio, '');
    renderBio();
    renderFoot();
  });

  el.avatar.addEventListener('click', function () { pickPhoto('avatar'); });
  el.banner.addEventListener('click', function () { pickPhoto('banner'); });
  el.file.addEventListener('change', function () {
    var f = el.file.files && el.file.files[0];
    var kind = st.pick;
    st.pick = '';
    if (f && kind) handleFile(kind, f);
  });

  function toggleDelete(kind) {
    if (st.busy || st.view !== 'edit') return;
    var p = st.photos[kind];
    if (p.status === 'keep') st.photos[kind] = { status: 'delete', blob: null, canvas: null };
    else st.photos[kind] = none();
    setPhotoErr('');
    renderAll();
  }
  el.avatarDel.addEventListener('click', function () { toggleDelete('avatar'); });
  el.bannerDel.addEventListener('click', function () { toggleDelete('banner'); });

  // Кадр: перетаскивание мышкой и пальцем (pointer events), стрелки для клавиатуры
  el.canvas.addEventListener('pointerdown', function (e) {
    var c = st.crop;
    if (!c || e.button > 0) return;
    c.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, ox: c.x, oy: c.y };
    try { el.canvas.setPointerCapture(e.pointerId); } catch (err) { /* не критично */ }
    e.preventDefault();
  });
  el.canvas.addEventListener('pointermove', function (e) {
    var c = st.crop;
    if (!c || !c.drag || c.drag.id !== e.pointerId) return;
    var rect = el.canvas.getBoundingClientRect();
    var k = c.fw / (rect.width || c.fw);
    c.x = c.drag.ox + (e.clientX - c.drag.x) * k;
    c.y = c.drag.oy + (e.clientY - c.drag.y) * k;
    clampCrop(c);
    paintCrop();
  });
  function endDrag(e) {
    var c = st.crop;
    if (c && c.drag && c.drag.id === e.pointerId) c.drag = null;
  }
  el.canvas.addEventListener('pointerup', endDrag);
  el.canvas.addEventListener('pointercancel', endDrag);
  el.canvas.addEventListener('keydown', function (e) {
    var c = st.crop;
    if (!c) return;
    var d = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[e.key];
    if (!d) return;
    e.preventDefault();
    c.x += d[0];
    c.y += d[1];
    clampCrop(c);
    paintCrop();
  });

  el.zoom.addEventListener('input', function () { setZoom(parseFloat(el.zoom.value) || 1); });
  el.cropBack.addEventListener('click', cancelCrop);
  el.cropDone.addEventListener('click', function () {
    var c = st.crop;
    if (!c || st.busy) return;
    setCropBusy(true);
    setCropErr('');
    encode(c).then(function (out) {
      if (st.crop !== c) return;
      st.photos[c.kind] = { status: 'new', blob: out.blob, canvas: out.canvas };
      try { c.bitmap.close(); } catch (e) { /* уже закрыт */ }
      st.crop = null;
      setCropBusy(false);
      setView('edit');
      renderAll();
      (c.kind === 'banner' ? el.banner : el.avatar).focus({ preventScroll: true });
    }, function (err) {
      setCropBusy(false);
      setCropErr(err && err.message === 'big'
        ? 'Фото после сжатия всё ещё больше ' + (c.kind === 'avatar' ? '2 МБ' : '4 МБ') + '. Выберите другое'
        : 'Не получилось подготовить фото. Попробуйте ещё раз');
    });
  });
})();
