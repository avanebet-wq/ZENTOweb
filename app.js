/* ZENTO: магазин (index.html) и админка (admin.html). Один файл для обеих страниц. */
(function () {
  'use strict';

  var page = document.body.getAttribute('data-page');
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function motionOK() { return !reduceMotion.matches; }

  /* ---------- Помощники ---------- */
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function el(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  var rub = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 0 });
  var intFmt = new Intl.NumberFormat('ru-RU');
  function money(v) { return rub.format(num(v)); }
  function count(v) { return intFmt.format(num(v)); }
  function safeColor(c) { return /^#[0-9a-f]{3,8}$/i.test(String(c || '')) ? c : '#1E40AF'; }
  function attrSel(name, val) { return '[' + name + '="' + String(val).replace(/["\\]/g, '\\$&') + '"]'; }
  function hasTag(tag) { return function (p) { return Array.isArray(p.tags) && p.tags.indexOf(tag) !== -1; }; }
  function isOnSale(p) { return hasTag('sale')(p) || num(p.old_price) > num(p.price); }
  function fmtDate(v) {
    if (!v) return '—';
    var d = new Date(v);
    return isNaN(d.getTime()) ? String(v) : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  // Хранилище может быть недоступно (приватный режим, запрет): тогда сайт работает без него
  function getStore(kind, key) { try { var s = window[kind]; return s ? s.getItem(key) : null; } catch (e) { return null; } }
  function setStore(kind, key, val) {
    try { var s = window[kind]; if (!s) return; if (val) s.setItem(key, val); else s.removeItem(key); } catch (e) { /* не запоминаем */ }
  }

  /* ---------- Иллюстрации товаров (SVG в стиле ZENTO, без фото) ---------- */
  var ART = {
    'Аудио': '<path d="M14 40v-6a18 18 0 0 1 36 0v6"/><rect x="10" y="38" width="9" height="15" rx="4"/><rect x="45" y="38" width="9" height="15" rx="4"/>',
    'Гаджеты': '<rect x="21" y="8" width="22" height="48" rx="5"/><path d="M26 15h12M29 49h6"/>',
    'Умный дом': '<path d="M24 35a8 8 0 1 1 16 0c0 3.5-2.5 5.5-3.5 9h-9c-1-3.5-3.5-5.5-3.5-9z"/><path d="M27 51h10M29 56h6M32 8v4"/>',
    'Фитнес': '<path d="M9 25v14M15 20v24M49 20v24M55 25v14M15 32h34"/>',
    'Офис': '<rect x="7" y="18" width="50" height="28" rx="5"/><path d="M15 27h3M22 27h3M29 27h3M36 27h3M43 27h3M15 36h22M43 36h6"/>',
    'Путешествия': '<rect x="18" y="14" width="28" height="40" rx="10"/><path d="M26 14v-4h12v4M24 30h16M18 24h-4M46 24h4"/>'
  };
  var ART_DEFAULT = '<rect x="14" y="14" width="36" height="36" rx="9"/>';
  function artSvg(p) {
    var key = String(p.category || '');
    var body = Object.prototype.hasOwnProperty.call(ART, key) ? ART[key] : ART_DEFAULT;
    return '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + body + '</svg>';
  }
  function artHTML(p, cls) {
    return '<span class="art ' + cls + '" style="--tile:' + safeColor(p.color) + '" aria-hidden="true">' + artSvg(p) + '</span>';
  }
  function setArt(node, p) {
    node.style.setProperty('--tile', safeColor(p.color));
    node.innerHTML = artSvg(p);
  }

  // На iPhone :active срабатывает только если у документа есть touch-обработчик
  document.addEventListener('touchstart', function () {}, { passive: true });

  /* ---------- Появление блоков при прокрутке (один раз) ---------- */
  var revealIO = ('IntersectionObserver' in window) ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        revealIO.unobserve(entry.target);
      }
    });
  }, { rootMargin: '0px 0px -6% 0px', threshold: 0.08 }) : null;

  function reveal(scope) {
    var nodes = $$('.reveal:not(.is-visible)', scope);
    if (scope.matches && scope.matches('.reveal:not(.is-visible)')) nodes.unshift(scope);
    nodes.forEach(function (node) {
      if (revealIO && motionOK()) revealIO.observe(node);
      else node.classList.add('is-visible');
    });
  }

  /* ---------- Короткое уведомление ---------- */
  var toastTimer = 0;
  function toast(msg) {
    var box = el('toast');
    if (!box) return;
    box.textContent = msg;
    box.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.classList.remove('is-on'); }, 2400);
  }

  /* ---------- Диалоги: лист товара и корзина ---------- */
  var lastOpener = null;

  function openDialog(d, opener) {
    if (d.open) return;
    lastOpener = opener || null;
    d.showModal();
    if (motionOK() && d.animate) {
      var drawer = d.classList.contains('drawer');
      d.animate(drawer
        ? [{ transform: 'translateX(100%)', opacity: 0 }, { transform: 'translateX(0)', opacity: 1 }]
        : [{ transform: 'translateY(48px)', opacity: 0 }, { transform: 'translateY(0)', opacity: 1 }],
        { duration: 360, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' });
    }
  }

  function closeDialog(d) {
    if (!d.open || d._closing) return;
    d._closing = true;
    function finish(anim) {
      if (anim) anim.cancel();
      d._closing = false;
      if (d.open) d.close();
      var back = lastOpener;
      lastOpener = null;
      if (back && document.body.contains(back)) back.focus({ preventScroll: true });
    }
    if (motionOK() && d.animate) {
      var drawer = d.classList.contains('drawer');
      var anim = d.animate(drawer
        ? [{ transform: 'translateX(0)', opacity: 1 }, { transform: 'translateX(100%)', opacity: 0 }]
        : [{ transform: 'translateY(0)', opacity: 1 }, { transform: 'translateY(40px)', opacity: 0 }],
        { duration: 220, easing: 'cubic-bezier(0.7, 0, 0.84, 0)', fill: 'forwards' });
      anim.onfinish = function () { finish(anim); };
    } else {
      finish(null);
    }
  }

  function bindDialog(d) {
    d.addEventListener('cancel', function (e) { e.preventDefault(); closeDialog(d); });
    d.addEventListener('click', function (e) {
      var t = e.target;
      if (t === d || (t.closest && t.closest('[data-close]'))) closeDialog(d);
    });
  }

  /* ---------- Переход между страницами без поддержки View Transitions ---------- */
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a.js-nav');
    if (!a || 'startViewTransition' in document || !motionOK()) return;
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    document.body.classList.add('is-leaving');
    setTimeout(function () { window.location.href = a.href; }, 200);
  });

  /* ================= МАГАЗИН ================= */
  var CART_KEY = 'zento-cart';
  var products = [];
  var byId = {};
  var apiOnline = false;
  var loadFailed = false;
  var activeCat = 'all';
  var searchText = '';
  var currentId = null;
  var cart = readCart();

  function readCart() {
    try {
      var list = JSON.parse(getStore('localStorage', CART_KEY) || '[]');
      if (!Array.isArray(list)) return [];
      return list
        .filter(function (i) { return i && i.id != null && Math.floor(num(i.qty)) > 0; })
        .map(function (i) { return { id: String(i.id), qty: Math.floor(num(i.qty)) }; });
    } catch (e) { return []; }
  }
  function writeCart() { setStore('localStorage', CART_KEY, JSON.stringify(cart)); }
  function findItem(id) { return cart.filter(function (i) { return i.id === String(id); })[0]; }
  function qtyInCart(id) { var it = findItem(id); return it ? it.qty : 0; }
  function cartLines() {
    return cart.map(function (item) { return { item: item, p: byId[item.id] }; })
      .filter(function (x) { return x.p; });
  }
  function cartTotal() { return cartLines().reduce(function (s, x) { return s + num(x.p.price) * x.item.qty; }, 0); }
  function cartCount() { return cartLines().reduce(function (s, x) { return s + x.item.qty; }, 0); }

  function priceHTML(p) {
    var now = '<span class="price-now">' + money(p.price) + '</span>';
    if (num(p.old_price) > num(p.price)) return '<s class="price-old">' + money(p.old_price) + '</s>' + now;
    return now;
  }
  function stockText(n) {
    n = num(n);
    if (n <= 0) return 'Нет в наличии';
    if (n <= 5) return 'Осталось мало: ' + n + ' шт.';
    return 'В наличии: ' + n + ' шт.';
  }
  function setStockLine(node, p) {
    node.textContent = stockText(p.stock);
    node.classList.toggle('is-out', num(p.stock) <= 0);
  }

  function renderBadge() {
    var n = cartCount();
    var badge = el('cart-count');
    badge.textContent = n;
    badge.hidden = n === 0;
    el('cart-open').setAttribute('aria-label', 'Корзина, товаров: ' + n);
  }

  function addToCart(id) {
    var p = byId[String(id)];
    if (!p) return false;
    if (num(p.stock) <= 0) { toast('Сейчас этого товара нет в наличии'); return false; }
    if (qtyInCart(id) >= num(p.stock)) { toast('В корзине уже все доступные ' + p.stock + ' шт.'); return false; }
    var item = findItem(id);
    if (item) item.qty += 1; else cart.push({ id: String(id), qty: 1 });
    writeCart();
    renderBadge();
    return true;
  }

  function handleAdd(id) {
    if (!addToCart(id)) return;
    toast('«' + byId[String(id)].name + '» добавлен в корзину');
  }

  function changeQty(id, delta) {
    var item = findItem(id);
    var p = byId[String(id)];
    if (!item || !p) return;
    var next = item.qty + delta;
    if (next > num(p.stock)) { next = num(p.stock); toast('Больше нет на складе'); }
    if (next <= 0) cart = cart.filter(function (i) { return i !== item; });
    else item.qty = next;
    writeCart();
    renderBadge();
    renderCart(delta > 0 ? attrSel('data-inc', id) : attrSel('data-dec', id));
  }

  function removeItem(id) {
    cart = cart.filter(function (i) { return i.id !== String(id); });
    writeCart();
    renderBadge();
    renderCart();
    el('cart-title').focus();
  }

  /* ----- Лист товара ----- */
  function openProduct(id, opener) {
    var p = byId[String(id)];
    if (!p) return;
    currentId = String(p.id);
    setArt(el('p-art'), p);
    el('p-cat').textContent = p.category;
    el('p-title').textContent = p.name;
    setStockLine(el('p-stock'), p);
    el('p-desc').textContent = p.description || '';
    el('p-price').innerHTML = priceHTML(p);
    refreshAddButton();
    openDialog(el('product-dialog'), opener);
    track('view', p.id);
  }

  function refreshAddButton() {
    var p = byId[currentId];
    if (!p) return;
    var btn = el('p-add');
    var have = qtyInCart(p.id);
    if (num(p.stock) <= 0) {
      btn.disabled = true;
      btn.textContent = 'В корзину';
    } else if (have >= num(p.stock)) {
      btn.disabled = true;
      btn.textContent = 'Все доступные уже в корзине';
    } else {
      btn.disabled = false;
      btn.textContent = have ? 'Ещё одну в корзину (сейчас ' + have + ')' : 'В корзину';
    }
  }

  /* ----- Корзина ----- */
  function openCart(opener) {
    showView('cart');
    renderCart();
    openDialog(el('cart-dialog'), opener);
  }

  var VIEW_TITLES = { cart: 'Корзина', checkout: 'Оформление заказа', done: 'Спасибо!' };
  function showView(name) {
    ['cart', 'checkout', 'done'].forEach(function (v) { el('view-' + v).hidden = v !== name; });
    el('cart-foot').hidden = name !== 'cart';
    el('cart-title').textContent = VIEW_TITLES[name];
  }

  function renderCart(focusSel) {
    var lines = cartLines();
    var list = el('cart-list');
    el('cart-empty').hidden = lines.length > 0;
    list.hidden = lines.length === 0;
    list.innerHTML = lines.map(function (x) {
      var id = esc(x.item.id);
      var p = x.p;
      var full = x.item.qty >= num(p.stock);
      return '<li class="cart-item glass">' +
        artHTML(p, 'art-sm') +
        '<div class="cart-info"><p class="cart-name">' + esc(p.name) + '</p>' +
        '<p class="cart-unit">' + money(p.price) + ' за шт.</p></div>' +
        '<div class="cart-row">' +
          '<div class="stepper">' +
            '<button class="qty-btn press" type="button" data-dec="' + id + '" aria-label="Уменьшить количество: ' + esc(p.name) + '">−</button>' +
            '<span class="qty">' + x.item.qty + '</span>' +
            '<button class="qty-btn press" type="button" data-inc="' + id + '" aria-label="Увеличить количество: ' + esc(p.name) + '"' + (full ? ' disabled' : '') + '>+</button>' +
          '</div>' +
          '<p class="cart-sum">' + money(p.price * x.item.qty) + '</p>' +
          '<button class="link-btn press" type="button" data-remove="' + id + '" aria-label="Удалить: ' + esc(p.name) + '">Удалить</button>' +
        '</div></li>';
    }).join('');
    el('cart-total').textContent = money(cartTotal());
    el('checkout-btn').disabled = lines.length === 0;
    el('checkout-btn').hidden = !apiOnline;
    el('offline-note').hidden = apiOnline;
    if (focusSel) {
      var target = list.querySelector(focusSel);
      if (target && !target.disabled) target.focus();
    }
  }

  /* ----- Оформление заказа ----- */
  var FIELDS = ['name', 'phone', 'email', 'address'];
  var RULES = {
    name: function (v) { return v.length >= 2 ? '' : 'Укажите имя: минимум 2 символа'; },
    phone: function (v) {
      var digits = v.replace(/\D/g, '');
      return (/^\+?[\d\s()\-]+$/.test(v) && digits.length >= 10 && digits.length <= 15)
        ? '' : 'Укажите телефон, например +7\u00A0900\u00A0123\u201145\u201167';
    },
    email: function (v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v) ? '' : 'Укажите почту, например name@mail.ru'; },
    address: function (v) { return v.length >= 5 ? '' : 'Укажите адрес доставки: город, улицу и дом'; }
  };

  function setFieldError(name, msg) {
    var input = el('f-' + name);
    var err = el('e-' + name);
    if (!input || !err) return false;
    err.textContent = msg || '';
    err.hidden = !msg;
    if (msg) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
    return true;
  }

  function setFormError(msg) {
    var box = el('form-error');
    box.textContent = msg || '';
    box.hidden = !msg;
  }

  function clearFormErrors() {
    setFormError('');
    FIELDS.forEach(function (n) { setFieldError(n, ''); });
  }

  function setSubmitting(on) {
    var btn = el('submit-btn');
    btn.disabled = on;
    btn.textContent = on ? 'Отправляем…' : 'Оформить заказ';
    el('order-form').setAttribute('aria-busy', on ? 'true' : 'false');
  }

  function onOrderSubmit(e) {
    e.preventDefault();
    clearFormErrors();
    var customer = {};
    FIELDS.forEach(function (n) { customer[n] = el('f-' + n).value.trim(); });

    var firstBad = null;
    FIELDS.forEach(function (n) {
      var msg = RULES[n](customer[n]);
      setFieldError(n, msg);
      if (msg && !firstBad) firstBad = el('f-' + n);
    });
    if (firstBad) { firstBad.focus(); return; }

    var lines = cartLines();
    if (!lines.length) { setFormError('Корзина пуста. Добавьте товары и попробуйте снова.'); return; }
    var total = cartTotal();
    var payload = {
      customer: customer,
      items: lines.map(function (x) { return { product_id: x.p.id, qty: x.item.qty }; })
    };

    setSubmitting(true);
    fetch('/api/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { status: res.status, body: body || {} };
      });
    }).then(function (r) {
      if (r.status === 201) orderDone(r.body, total);
      else if (r.status === 400) showServerErrors(r.body);
      else setFormError('Не получилось оформить заказ. Попробуйте ещё раз.');
    }).catch(function () {
      setFormError('Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.');
    }).then(function () {
      setSubmitting(false);
    });
  }

  function showServerErrors(body) {
    var fields = body.fields;
    var mapped = false;
    function mark(key) {
      var name = String(key).replace(/^customer\./, '');
      if (setFieldError(name, 'Проверьте это поле')) mapped = true;
    }
    if (Array.isArray(fields)) fields.forEach(mark);
    else if (fields && typeof fields === 'object') Object.keys(fields).forEach(mark);
    setFormError(mapped ? 'Проверьте поля, подсвеченные красным.' : 'Не получилось оформить заказ. Проверьте данные и попробуйте снова.');
    var bad = el('order-form').querySelector('[aria-invalid="true"]');
    if (bad) bad.focus();
  }

  function orderDone(body, total) {
    var sum = typeof body.total === 'number' ? body.total : total;
    cart = [];
    writeCart();
    renderBadge();
    el('done-id').textContent = body.order_id != null ? body.order_id : '';
    el('done-total').textContent = money(sum);
    el('order-form').reset();
    clearFormErrors();
    showView('done');
    el('done-title').focus();
  }

  /* ----- Каталог, ряды, категории, поиск ----- */
  function productCard(p, i) {
    var li = document.createElement('li');
    li.className = 'reveal';
    li.style.setProperty('--d', Math.min(i, 6) * 70 + 'ms');
    var out = num(p.stock) <= 0;
    li.innerHTML =
      '<article class="product glass">' +
        '<button class="product-main press" type="button" data-open="' + esc(p.id) + '">' +
          artHTML(p, 'art-card') +
          '<span class="product-cat">' + esc(p.category) + '</span>' +
          '<span class="product-name">' + esc(p.name) + '</span>' +
        '</button>' +
        '<div class="product-foot">' +
          '<p class="price">' + priceHTML(p) + '</p>' +
          (out ? '<p class="stock-out">Нет в наличии</p>' : '') +
          '<button class="btn btn-primary btn-small press" type="button" data-add="' + esc(p.id) + '"' + (out ? ' disabled' : '') + '>В корзину</button>' +
        '</div>' +
      '</article>';
    return li;
  }

  function skeletonCard() {
    var li = document.createElement('li');
    li.className = 'skeleton-card';
    li.setAttribute('aria-hidden', 'true');
    li.innerHTML = '<span class="sk sk-art"></span><span class="sk sk-line"></span><span class="sk sk-line short"></span><span class="sk sk-btn"></span>';
    return li;
  }

  function renderSkeleton() {
    var grid = el('grid');
    grid.innerHTML = '';
    for (var i = 0; i < 8; i++) grid.appendChild(skeletonCard());
    grid.setAttribute('aria-busy', 'true');
  }

  function fillRail(sectionId, listId, items) {
    var section = el(sectionId);
    var list = el(listId);
    section.hidden = items.length === 0;
    list.innerHTML = '';
    items.forEach(function (p, i) { list.appendChild(productCard(p, i)); });
    reveal(list);
  }

  function renderHero() {
    var section = el('today');
    var featured = products.filter(hasTag('top'))[0] || products[0];
    section.hidden = !featured;
    if (!featured) return;
    setArt(el('hero-art'), featured);
    el('hero-name').textContent = featured.name;
    el('hero-desc').textContent = featured.description || '';
    el('hero-price').innerHTML = priceHTML(featured);
    setStockLine(el('hero-stock'), featured);
    var add = el('hero-add');
    add.setAttribute('data-add', String(featured.id));
    add.disabled = num(featured.stock) <= 0;
    el('hero-open').setAttribute('data-open', String(featured.id));
    reveal(el('hero'));
  }

  function renderChips() {
    var cats = [];
    products.forEach(function (p) { if (cats.indexOf(p.category) === -1) cats.push(p.category); });
    var all = [{ key: 'all', label: 'Все' }].concat(cats.map(function (c) { return { key: c, label: c }; }));
    el('chips').innerHTML = all.map(function (c) {
      return '<button class="chip glass press" type="button" data-cat="' + esc(c.key) + '" aria-pressed="' +
        (c.key === activeCat ? 'true' : 'false') + '">' + esc(c.label) + '</button>';
    }).join('');
    el('categories').hidden = cats.length === 0;
  }

  function markChips() {
    $$('#chips [data-cat]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-cat') === activeCat ? 'true' : 'false');
    });
  }

  function renderRows() {
    var top = products.filter(hasTag('top'));
    if (!top.length) top = products.slice(0, 8);
    var fresh = products.filter(hasTag('new'));
    if (!fresh.length) fresh = products.slice(-6);
    var sale = products.filter(isOnSale);
    fillRail('top-sales', 'row-top', top.slice(0, 12));
    fillRail('new-items', 'row-new', fresh.slice(0, 12));
    fillRail('sale-items', 'row-sale', sale.slice(0, 12));
  }

  function renderGrid() {
    var grid = el('grid');
    var status = el('catalog-status');
    el('catalog-title').textContent = searchText.trim()
      ? 'Поиск: ' + searchText.trim()
      : (activeCat === 'all' ? 'Все товары' : activeCat);
    grid.setAttribute('aria-busy', 'false');
    if (loadFailed) {
      status.textContent = '';
      grid.innerHTML = '<li class="empty">Не удалось загрузить товары. Обновите страницу, пожалуйста.</li>';
      return;
    }
    var q = searchText.trim().toLowerCase();
    var list = products.filter(function (p) {
      if (activeCat !== 'all' && p.category !== activeCat) return false;
      if (!q) return true;
      return [p.name, p.category, p.description].join(' ').toLowerCase().indexOf(q) !== -1;
    });
    status.textContent = 'Товаров: ' + list.length;
    grid.innerHTML = '';
    if (!list.length) {
      grid.innerHTML = '<li class="empty">Ничего не найдено. Попробуйте другой запрос или категорию.</li>';
      return;
    }
    list.forEach(function (p, i) { grid.appendChild(productCard(p, i)); });
    reveal(grid);
  }

  function scrollToCatalog() {
    el('catalog').scrollIntoView({ behavior: motionOK() ? 'smooth' : 'auto', block: 'start' });
  }

  function pickCategory(key) {
    activeCat = key;
    searchText = '';
    el('search-input').value = '';
    markChips();
    renderGrid();
    scrollToCatalog();
  }

  function onSearch(e) {
    searchText = e.target.value;
    if (searchText.trim() && activeCat !== 'all') {
      activeCat = 'all';
      markChips();
    }
    renderGrid();
  }

  function renderAll() {
    renderHero();
    renderChips();
    renderRows();
    renderGrid();
    renderBadge();
    renderCart();
    el('demo-note').hidden = apiOnline;
  }

  /* ----- Данные и события ----- */
  function getJSON(url) {
    return fetch(url, { headers: { Accept: 'application/json' } }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      if (!Array.isArray(data)) throw new Error('bad data');
      return data;
    });
  }

  // Сначала сервер; если его нет (например, GitHub Pages), берём товары из файла
  function loadProducts() {
    return getJSON('/api/products')
      .then(function (list) { apiOnline = true; return list; })
      .catch(function () { return getJSON('data/products.json'); })
      .catch(function () { return null; });
  }

  // Просмотры: ошибки игнорируем, а без сервера не отправляем вовсе
  function track(type, productId) {
    if (!apiOnline) return;
    var body = { type: type, path: '/' };
    if (productId != null) body.product_id = productId;
    try {
      fetch('/api/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        keepalive: true
      }).catch(function () {});
    } catch (e) { /* игнорируем */ }
  }

  function onShopClick(e) {
    var t = e.target;
    if (!(t instanceof Element)) return;
    var hit;
    if ((hit = t.closest('[data-add]'))) return handleAdd(hit.getAttribute('data-add'));
    if ((hit = t.closest('[data-open]'))) return openProduct(hit.getAttribute('data-open'), hit);
    if ((hit = t.closest('[data-cat]'))) return pickCategory(hit.getAttribute('data-cat'));
    if ((hit = t.closest('[data-inc]'))) return changeQty(hit.getAttribute('data-inc'), 1);
    if ((hit = t.closest('[data-dec]'))) return changeQty(hit.getAttribute('data-dec'), -1);
    if ((hit = t.closest('[data-remove]'))) return removeItem(hit.getAttribute('data-remove'));
  }

  function initShop() {
    bindDialog(el('product-dialog'));
    bindDialog(el('cart-dialog'));
    document.addEventListener('click', onShopClick);
    el('cart-open').addEventListener('click', function (e) { openCart(e.currentTarget); });
    el('p-add').addEventListener('click', function () { if (addToCart(currentId)) refreshAddButton(); });
    el('checkout-btn').addEventListener('click', function () {
      showView('checkout');
      el('f-name').focus();
    });
    el('back-cart').addEventListener('click', function () {
      showView('cart');
      renderCart();
      el('cart-title').focus();
    });
    el('search-input').addEventListener('input', onSearch);
    el('search-form').addEventListener('submit', function (e) { e.preventDefault(); scrollToCatalog(); });
    el('order-form').addEventListener('submit', onOrderSubmit);
    renderBadge();
    renderSkeleton();

    loadProducts().then(function (list) {
      if (list) products = list; else loadFailed = true;
      byId = {};
      products.forEach(function (p) { byId[String(p.id)] = p; });
      // Убираем из корзины то, чего больше нет, и не даём количеству превышать остаток
      cart = cart.filter(function (i) {
        var p = byId[i.id];
        if (!p) return false;
        if (i.qty > num(p.stock)) i.qty = num(p.stock);
        return i.qty > 0;
      });
      writeCart();
      renderAll();
      track('view');
    });
  }

  /* ================= АДМИНКА ================= */
  var TOKEN_KEY = 'zento-admin-token';
  var STATUSES = [
    ['new', 'Новый'],
    ['paid', 'Оплачен'],
    ['shipped', 'Отправлен'],
    ['done', 'Выполнен'],
    ['cancelled', 'Отменён']
  ];

  function statusLabel(v) {
    var f = STATUSES.filter(function (s) { return s[0] === v; })[0];
    return f ? f[1] : String(v);
  }

  function initAdmin() {
    var token = getStore('sessionStorage', TOKEN_KEY) || '';
    var loginView = el('login-view');
    var adminView = el('admin-view');
    var logoutBtn = el('logout');
    var loginForm = el('login-form');
    var tokenInput = el('token');
    var tokenError = el('token-error');
    var loaded = {};

    // Все запросы идут с Authorization: Bearer; токен нигде, кроме sessionStorage, не хранится
    function api(path, opts) {
      opts = opts || {};
      var headers = { Accept: 'application/json', Authorization: 'Bearer ' + token };
      var init = { method: opts.method || 'GET', headers: headers };
      if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
      return fetch(path, init).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (!res.ok) {
            var err = new Error('HTTP ' + res.status);
            err.status = res.status;
            throw err;
          }
          return data;
        });
      }, function () {
        var err = new Error('network');
        err.status = 0;
        throw err;
      });
    }

    function showLogin(message, dropToken) {
      if (dropToken) {
        token = '';
        setStore('sessionStorage', TOKEN_KEY, '');
      }
      adminView.hidden = true;
      logoutBtn.hidden = true;
      loginView.hidden = false;
      tokenInput.value = '';
      tokenError.textContent = message || '';
      tokenError.hidden = !message;
    }

    function showAdmin() {
      loginView.hidden = true;
      adminView.hidden = false;
      logoutBtn.hidden = false;
    }

    function onApiError(err, text) {
      if (err.status === 401 || err.status === 403) {
        showLogin('Сессия закончилась или токен больше не подходит. Войдите снова.', true);
        return;
      }
      toast(text || 'Нет связи с сервером. Попробуйте ещё раз.');
    }

    /* Вход */
    loginForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var value = tokenInput.value.trim();
      tokenError.hidden = true;
      if (!value) {
        tokenError.textContent = 'Введите токен доступа';
        tokenError.hidden = false;
        tokenInput.focus();
        return;
      }
      var btn = el('login-btn');
      btn.disabled = true;
      token = value;
      api('/api/admin/stats').then(function (stats) {
        setStore('sessionStorage', TOKEN_KEY, value);
        showAdmin();
        renderStats(stats);
        loaded.stats = true;
      }).catch(function (err) {
        token = '';
        tokenError.textContent = err.status === 401 || err.status === 403
          ? 'Токен не подошёл. Проверьте и попробуйте снова.'
          : 'Не удалось связаться с сервером. Админка работает вместе с сервером.';
        tokenError.hidden = false;
      }).then(function () {
        btn.disabled = false;
      });
    });

    logoutBtn.addEventListener('click', function () {
      showLogin('Вы вышли из админки.', true);
    });

    /* Вкладки */
    var tabs = $$('[role="tab"]');
    function selectTab(name, focusTab) {
      function apply() {
        tabs.forEach(function (t) {
          var on = t.getAttribute('data-tab') === name;
          t.setAttribute('aria-selected', on ? 'true' : 'false');
          t.tabIndex = on ? 0 : -1;
          if (on && focusTab) t.focus();
        });
        $$('[role="tabpanel"]').forEach(function (p) {
          p.hidden = p.getAttribute('data-panel') !== name;
        });
      }
      if (document.startViewTransition && motionOK()) {
        var vt = document.startViewTransition(apply);
        // Если переход прервали (например, повернули телефон), промисы отклоняются: это не ошибка
        ['updateCallbackDone', 'ready', 'finished'].forEach(function (k) {
          if (vt && vt[k] && vt[k].catch) vt[k].catch(function () {});
        });
      } else {
        apply();
      }
      if (!loaded[name]) loadPanel(name);
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { selectTab(t.getAttribute('data-tab'), false); });
      t.addEventListener('keydown', function (e) {
        var dir = e.key === 'ArrowRight' ? 1 : (e.key === 'ArrowLeft' ? -1 : 0);
        if (!dir) return;
        e.preventDefault();
        selectTab(tabs[(i + dir + tabs.length) % tabs.length].getAttribute('data-tab'), true);
      });
    });

    document.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-refresh]');
      if (b) loadPanel(b.getAttribute('data-refresh'));
    });

    function loadPanel(name) {
      if (name === 'stats') return loadStats();
      if (name === 'orders') return loadOrders();
      if (name === 'customers') return loadCustomers();
      if (name === 'products') return loadProducts();
    }

    /* Статистика */
    function loadStats() {
      return api('/api/admin/stats').then(function (s) {
        renderStats(s);
        loaded.stats = true;
      }).catch(function (err) { onApiError(err, 'Не удалось загрузить статистику'); });
    }

    function renderStats(s) {
      s = s || {};
      el('stat-orders').textContent = count(s.orders_count);
      el('stat-revenue').textContent = money(s.revenue);
      el('stat-customers').textContent = count(s.customers_count);
      el('stat-views').textContent = count(s.views_count);
      var days = Array.isArray(s.orders_by_day) ? s.orders_by_day : [];
      el('days-body').innerHTML = days.length
        ? days.map(function (d) {
          return '<tr><td>' + esc(d.day) + '</td>' +
            '<td data-label="Заказов">' + count(d.count) + '</td>' +
            '<td data-label="Выручка">' + money(d.revenue) + '</td></tr>';
        }).join('')
        : '<tr><td colspan="3" class="muted">Заказов пока нет</td></tr>';
      var top = Array.isArray(s.top_products) ? s.top_products : [];
      el('top-body').innerHTML = top.length
        ? top.map(function (p) {
          return '<tr><td>' + esc(p.name || ('№ ' + p.id)) + '</td>' +
            '<td data-label="Продано, шт.">' + count(p.qty) + '</td></tr>';
        }).join('')
        : '<tr><td colspan="2" class="muted">Пока нет данных</td></tr>';
    }

    /* Заказы */
    function statusSelect(id, current) {
      var opts = STATUSES.slice();
      if (current && !opts.some(function (s) { return s[0] === current; })) opts.push([current, String(current)]);
      return '<select class="status-select" data-order="' + esc(id) + '" data-prev="' + esc(current) +
        '" aria-label="Статус заказа ' + esc(id) + '">' +
        opts.map(function (s) {
          return '<option value="' + esc(s[0]) + '"' + (s[0] === current ? ' selected' : '') + '>' + esc(s[1]) + '</option>';
        }).join('') +
        '</select>';
    }

    function loadOrders() {
      return api('/api/admin/orders').then(function (list) {
        list = Array.isArray(list) ? list : [];
        el('orders-body').innerHTML = list.length ? list.map(function (o) {
          var c = o.customer || { name: o.customer_name, phone: o.customer_phone };
          var items = Array.isArray(o.items)
            ? o.items.map(function (i) { return esc(i.name || ('№ ' + i.product_id)) + ' × ' + count(i.qty); }).join('<br>')
            : '';
          return '<tr><td>№ ' + esc(o.id) + '</td>' +
            '<td data-label="Дата">' + esc(fmtDate(o.created_at)) + '</td>' +
            '<td data-label="Покупатель">' + esc(c.name || '—') + '<br><span class="muted">' + esc(c.phone || '') + '</span></td>' +
            '<td data-label="Товары">' + items + '</td>' +
            '<td data-label="Сумма">' + money(o.total) + '</td>' +
            '<td data-label="Статус">' + statusSelect(o.id, o.status) + '</td></tr>';
        }).join('') : '<tr><td colspan="6" class="muted">Заказов пока нет</td></tr>';
        loaded.orders = true;
      }).catch(function (err) { onApiError(err, 'Не удалось загрузить заказы'); });
    }

    el('orders-body').addEventListener('change', function (e) {
      var sel = e.target;
      if (!sel.matches || !sel.matches('select[data-order]')) return;
      var id = sel.getAttribute('data-order');
      var prev = sel.getAttribute('data-prev');
      sel.disabled = true;
      api('/api/admin/orders/' + encodeURIComponent(id), { method: 'PATCH', body: { status: sel.value } })
        .then(function () {
          sel.setAttribute('data-prev', sel.value);
          toast('Заказ ' + id + ': ' + statusLabel(sel.value));
        })
        .catch(function (err) {
          sel.value = prev;
          onApiError(err, 'Не удалось сменить статус');
        })
        .then(function () { sel.disabled = false; });
    });

    /* Покупатели */
    function loadCustomers() {
      return api('/api/admin/customers').then(function (list) {
        list = Array.isArray(list) ? list : [];
        el('customers-body').innerHTML = list.length ? list.map(function (c) {
          return '<tr><td>' + esc(c.name || '—') + '</td>' +
            '<td data-label="Телефон">' + esc(c.phone || '—') + '</td>' +
            '<td data-label="Почта">' + esc(c.email || '—') + '</td>' +
            '<td data-label="Адрес">' + esc(c.address || '—') + '</td>' +
            '<td data-label="Заказов">' + (c.orders_count != null ? count(c.orders_count) : '—') + '</td></tr>';
        }).join('') : '<tr><td colspan="5" class="muted">Покупателей пока нет</td></tr>';
        loaded.customers = true;
      }).catch(function (err) { onApiError(err, 'Не удалось загрузить покупателей'); });
    }

    /* Товары */
    function isInt(s) { return /^\d+$/.test(s); }

    function productRow(p) {
      var id = esc(p.id);
      return '<tr>' +
        '<td>' + esc(p.name) + '</td>' +
        '<td data-label="Категория">' + esc(p.category) + '</td>' +
        '<td data-label="Цена, ₽"><label class="visually-hidden" for="price-' + id + '">Цена: ' + esc(p.name) + '</label>' +
          '<input class="num-input" id="price-' + id + '" type="number" inputmode="numeric" min="0" step="1" value="' + num(p.price) + '"></td>' +
        '<td data-label="Остаток, шт."><label class="visually-hidden" for="stock-' + id + '">Остаток: ' + esc(p.name) + '</label>' +
          '<input class="num-input" id="stock-' + id + '" type="number" inputmode="numeric" min="0" step="1" value="' + num(p.stock) + '"></td>' +
        '<td class="actions"><button class="btn btn-ghost btn-small press" type="button" data-save="' + id + '">Сохранить</button></td>' +
        '</tr>';
    }

    function loadProducts() {
      return api('/api/admin/products').then(function (list) {
        list = Array.isArray(list) ? list : [];
        el('products-body').innerHTML = list.length
          ? list.map(productRow).join('')
          : '<tr><td colspan="5" class="muted">Товаров пока нет</td></tr>';
        loaded.products = true;
      }).catch(function (err) { onApiError(err, 'Не удалось загрузить товары'); });
    }

    el('products-body').addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-save]');
      if (!btn) return;
      var id = btn.getAttribute('data-save');
      var price = el('price-' + id).value.trim();
      var stock = el('stock-' + id).value.trim();
      if (!isInt(price) || !isInt(stock)) {
        toast('Цена и остаток — целые числа, не меньше нуля');
        return;
      }
      var name = btn.closest('tr').children[0].textContent;
      btn.disabled = true;
      api('/api/admin/products/' + encodeURIComponent(id), {
        method: 'PATCH',
        body: { price: Number(price), stock: Number(stock) }
      }).then(function () {
        toast('Сохранено: ' + name);
      }).catch(function (err) {
        onApiError(err, 'Не удалось сохранить товар');
      }).then(function () {
        btn.disabled = false;
      });
    });

    el('add-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var errBox = el('add-error');
      var data = {
        name: el('add-name').value.trim(),
        category: el('add-category').value.trim(),
        price: el('add-price').value.trim(),
        stock: el('add-stock').value.trim(),
        color: el('add-color').value,
        description: el('add-desc').value.trim()
      };
      var problem = '';
      if (data.name.length < 2) problem = 'Укажите название товара';
      else if (!data.category) problem = 'Укажите категорию';
      else if (!isInt(data.price)) problem = 'Цена — целое число, в рублях';
      else if (!isInt(data.stock)) problem = 'Остаток — целое число, не меньше нуля';
      errBox.textContent = problem;
      errBox.hidden = !problem;
      if (problem) return;
      api('/api/admin/products', {
        method: 'POST',
        body: { name: data.name, category: data.category, price: Number(data.price), stock: Number(data.stock), color: data.color, description: data.description }
      }).then(function () {
        el('add-form').reset();
        el('add-color').value = '#1E40AF';
        toast('Товар добавлен');
        loadProducts();
      }).catch(function (err) {
        onApiError(err, 'Не удалось добавить товар');
      });
    });

    /* Старт: если токен уже был в этой вкладке, проверяем его */
    if (token) {
      api('/api/admin/stats').then(function (s) {
        showAdmin();
        renderStats(s);
        loaded.stats = true;
      }).catch(function (err) {
        if (err.status === 401 || err.status === 403) showLogin('Сессия закончилась. Войдите снова.', true);
        else showLogin('Не удалось связаться с сервером. Попробуйте позже.', false);
      });
    } else {
      showLogin('', false);
    }
  }

  /* ---------- Запуск ---------- */
  if (page === 'shop') {
    initShop();
  } else if (page === 'admin') {
    initAdmin();
  }
})();
