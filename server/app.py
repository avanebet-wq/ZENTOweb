"""HTTP-сервер ZENTO: статические файлы из корня репозитория и JSON API под /api.

Запуск из корня репозитория:
    ADMIN_TOKEN=... python3 server/app.py --port 8080

Только стандартная библиотека Python 3.13. База: data/store.db (создаётся сама).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import auth
import captcha
import images
import mailer
import store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "store.db"
DEFAULT_PRODUCTS = ROOT / "data" / "products.json"

# ---------------------------------------------------------------- настройки

MAX_BODY_BYTES = 64 * 1024
# Тело больше лимита (но не больше 1 МБ) читаем и выбрасываем, чтобы клиент получил ответ 413, а не обрыв связи.
DISCARD_LIMIT_BYTES = 1024 * 1024
# Загрузка картинки профиля: тело читаем целиком до 4 МБ (больше лимита баннера — 413 без чтения).
# Порог «читать и выбросить» поднят только для этих маршрутов, чтобы клиент получал 413, а не обрыв.
IMAGE_UPLOAD_PATHS = frozenset({"/api/profile/avatar", "/api/profile/banner"})
PRODUCT_IMAGE_PATH = re.compile(r"^/api/admin/products/(?P<id>[^/]+)/image$")
IMAGE_DISCARD_LIMIT_BYTES = 8 * 1024 * 1024
IMAGE_CACHE_CONTROL = "private, max-age=3600"

HANDLER_TIMEOUT_SECONDS = 10       # висящие соединения закрываются через 10 секунд

ORDER_RATE_LIMIT = 10              # заказов с одного IP
ORDER_RATE_WINDOW = 60             # за столько секунд
ORDER_GLOBAL_LIMIT = 60            # заказов со всего магазина вместе (страховка, не зависит от IP)
ORDER_GLOBAL_WINDOW = 60           # за столько секунд
EVENT_RATE_LIMIT = 120             # событий с одного IP
EVENT_RATE_WINDOW = 60

ADMIN_FAIL_LIMIT = 10              # неверных входов в админку с одного IP
ADMIN_FAIL_WINDOW = 15 * 60        # за столько секунд; потом блокировка 429
MIN_ADMIN_TOKEN_LENGTH = 32
# Токен бота (SHOP_BOT_TOKEN): не имеет прав админа. Может только то же, что и обычный посетитель сайта.
MIN_BOT_TOKEN_LENGTH = 32

# Лимиты входа, регистрации и капчи: (сколько попаданий, за сколько секунд). Можно переопределить в тестах.
DEFAULT_AUTH_LIMITS = {
    "register": (10, 60 * 60),         # попыток регистрации с IP за час
    "login_key": (5, 15 * 60),         # неудачных входов на один логин (почту или телефон) за 15 минут
    "login_ip": (20, 15 * 60),         # неудачных входов с одного IP за 15 минут
    "captcha_new": (30, 10 * 60),      # новых заданий капчи с IP за 10 минут
    "captcha_verify": (20, 10 * 60),   # проверок капчи с IP за 10 минут
    "reset_request": (5, 15 * 60),     # запросов кода восстановления с IP за 15 минут
    "profile_patch": (30, 60 * 60),    # сохранений описания и ника на одного покупателя за час
    "image_upload": (10, 60 * 60),     # загрузок картинок (аватар или баннер) на одного покупателя за час
}

RESET_MESSAGE = "Если аккаунт найден, код отправлен на почту"

# Ответы для /api/auth/* и /api/captcha/*: код, статус и текст по умолчанию.
AUTH_ERRORS = {
    "validation_error": (400, "Проверьте заполненные поля"),
    "unsupported_media_type": (415, "Отправьте данные в формате JSON"),
    "payload_too_large": (413, "Слишком большой запрос"),
    "captcha_required": (403, "Подтвердите, что вы не робот, и попробуйте снова"),
    "captcha_incomplete": (400, "Соберите все четыре части картинки"),
    "captcha_wrong": (400, "Части стоят не на своих местах. Попробуйте ещё раз"),
    "captcha_expired": (410, "Проверка устарела. Начните заново"),
    "captcha_unavailable": (503, "Проверка временно недоступна"),
    "rules_not_accepted": (400, "Примите правила сервиса, чтобы создать аккаунт"),
    "nick_taken": (409, "Этот ник уже занят"),
    "email_taken": (409, "Эта почта уже зарегистрирована"),
    "invalid_credentials": (401, "Неверный логин или пароль"),
    "too_many_attempts": (429, "Слишком много попыток. Попробуйте позже"),
    "too_many_requests": (429, "Слишком много запросов. Попробуйте позже"),
    "already_spun": (429, "Сегодня колесо уже крутилось. Приходите завтра"),
    "login_required": (401, "Войдите, чтобы продолжить"),
    "code_invalid": (400, "Неверный код. Проверьте письмо и попробуйте снова"),
    "code_expired": (400, "Срок действия кода истёк. Запросите новый"),
    "code_attempts_exceeded": (429, "Слишком много неверных попыток. Запросите новый код"),
    "mail_failed": (503, "Не удалось отправить письмо. Попробуйте позже"),
    "nick_invalid": (400, "Такой ник нельзя использовать"),
    "nick_cooldown": (409, "Ник пока нельзя менять"),
    "avatar_too_large": (413, "Аватар больше 2 МБ. Выберите картинку поменьше"),
    "banner_too_large": (413, "Баннер больше 4 МБ. Выберите картинку поменьше"),
    "image_type_unsupported": (415, "Подходят только картинки PNG, JPEG и WEBP"),
    "image_invalid": (400, "Картинка повреждена или слишком большая"),
    "not_found": (404, "Покупатель не найден"),
}

# Эти папки никогда не отдаются как статика (сравнение без учёта регистра).
HIDDEN_DIRS = {"server", "tests", "data"}
# Только эти расширения отдаются как статика.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' ; img-src 'self' data:"
)


def pick_client_ip(real_ip_header, peer_ip, trust_proxy):
    """IP клиента для лимитов. За прокси Railway (TRUST_PROXY=1) берём X-Real-IP: его ставит
    граничный прокси Railway и перезаписывает, клиент его подделать не может. Иначе — адрес соединения."""
    if not trust_proxy or not real_ip_header:
        return peer_ip
    candidate = real_ip_header.strip()
    if re.fullmatch(r"[0-9a-fA-F:.]{2,45}", candidate):
        return candidate
    return peer_ip


TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip() == "1"


def _client_ip(self):
    return pick_client_ip(self.headers.get("X-Real-IP"), self.client_address[0], TRUST_PROXY)


class FileResponse:
    """Ответ-файл (например копия базы). Отдаётся как скачивание, не как JSON."""

    def __init__(self, data, filename, content_type="application/octet-stream"):
        self.data = data
        self.filename = filename
        self.content_type = content_type


class ImageResponse:
    """Картинка профиля: байты, Content-Type из базы и ETag (sha256)."""

    def __init__(self, data, content_type, sha256):
        self.data = data
        self.content_type = content_type
        self.sha256 = sha256


class ApiError(Exception):
    """Ответ с ошибкой: статус и тело JSON."""

    def __init__(self, status, body, headers=()):
        super().__init__(status)
        self.status = status
        self.body = body
        self.headers = tuple(headers)


# ---------------------------------------------------------------- лимиты

class RateLimiter:
    """Скользящее окно по ключу (обычно IP).

    Без db_path хранит попадания в памяти (для тестов). С db_path хранит их в базе SQLite:
    счётчики не сбрасываются при перезапуске и одинаковы для всех запросов."""

    def __init__(self, clock=time.monotonic, db_path=None):
        self._clock = clock
        self._db_path = str(db_path) if db_path else None
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = {}
        if self._db_path:
            conn = self._connect()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS rate_hits "
                             "(bucket TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS rate_hits_idx ON rate_hits (bucket, key, ts)")
            finally:
                conn.close()

    def _connect(self):
        return sqlite3.connect(self._db_path, timeout=10, isolation_level=None)

    # --- память
    def _window(self, bucket, key, window):
        """Возвращает очередь попаданий внутри окна и текущее время. Вызывать под блокировкой."""
        now = self._clock()
        hits = self._hits.get((bucket, key))
        if hits is None:
            return deque(), now
        while hits and now - hits[0] >= window:
            hits.popleft()
        if not hits:
            del self._hits[(bucket, key)]
            return deque(), now
        return hits, now

    def _mem_try_hit(self, bucket, key, limit, window):
        with self._lock:
            hits, now = self._window(bucket, key, window)
            if len(hits) >= limit:
                return False
            hits.append(now)
            self._hits[(bucket, key)] = hits
            return True

    def _mem_is_over(self, bucket, key, limit, window):
        with self._lock:
            hits, _ = self._window(bucket, key, window)
            return len(hits) >= limit

    def _mem_record(self, bucket, key, window):
        with self._lock:
            hits, now = self._window(bucket, key, window)
            hits.append(now)
            self._hits[(bucket, key)] = hits

    # --- публичные методы
    def try_hit(self, bucket, key, limit, window) -> bool:
        """Записывает попадание, если лимит не исчерпан. False — лимит исчерпан, попадание не записано."""
        if not self._db_path:
            return self._mem_try_hit(bucket, key, limit, window)
        now = self._clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_hits WHERE bucket = ? AND key = ? AND ts <= ?",
                         (bucket, key, now - window))
            count = conn.execute("SELECT COUNT(*) FROM rate_hits WHERE bucket = ? AND key = ?",
                                 (bucket, key)).fetchone()[0]
            allowed = count < limit
            if allowed:
                conn.execute("INSERT INTO rate_hits (bucket, key, ts) VALUES (?, ?, ?)", (bucket, key, now))
            conn.execute("COMMIT")
            return allowed
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def is_over(self, bucket, key, limit, window) -> bool:
        if not self._db_path:
            return self._mem_is_over(bucket, key, limit, window)
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM rate_hits WHERE bucket = ? AND key = ? AND ts > ?",
                                 (bucket, key, self._clock() - window)).fetchone()[0]
            return count >= limit
        finally:
            conn.close()

    def record(self, bucket, key, window) -> None:
        if not self._db_path:
            return self._mem_record(bucket, key, window)
        conn = self._connect()
        try:
            conn.execute("INSERT INTO rate_hits (bucket, key, ts) VALUES (?, ?, ?)",
                         (bucket, key, self._clock()))
        finally:
            conn.close()

    def retry_after(self, bucket, key, window) -> int:
        """Через сколько целых секунд самое старое попадание в окне выйдет из него (минимум 1)."""
        now = self._clock()
        if not self._db_path:
            with self._lock:
                hits, now = self._window(bucket, key, window)
                oldest = hits[0] if hits else now
        else:
            conn = self._connect()
            try:
                oldest = conn.execute("SELECT MIN(ts) FROM rate_hits WHERE bucket = ? AND key = ? AND ts > ?",
                                      (bucket, key, now - window)).fetchone()[0]
            finally:
                conn.close()
            if oldest is None:
                oldest = now
        return max(1, math.ceil(oldest + window - now))


# ---------------------------------------------------------------- статика

def _is_hidden(part: str) -> bool:
    return part.startswith(".") or part.lower() in HIDDEN_DIRS


def resolve_static(root: Path, url_path: str):
    """Возвращает (путь к файлу, Content-Type) или None. Не выпускает файлы за пределы root."""
    raw = unquote(url_path)
    if "\x00" in raw or "\\" in raw:
        return None
    if raw == "" or raw.endswith("/"):
        raw += "index.html"
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in (".", "..") or _is_hidden(part) for part in parts):
        return None
    candidate = (root / Path(*parts)).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    # Проверяем и после разрешения ссылок: симлинк не должен открывать скрытые папки.
    if any(_is_hidden(part) for part in relative.parts):
        return None
    content_type = STATIC_TYPES.get(candidate.suffix.lower())
    if content_type is None or not candidate.is_file():
        return None
    return candidate, content_type


# ---------------------------------------------------------------- обработчики API

def _run(handler, func, *args):
    """Открывает базу на один запрос, повторяет при блокировке и переводит ошибки store в HTTP."""
    def attempt():
        conn = store.connect(handler.server.db_path)
        try:
            return func(conn, *args)
        finally:
            conn.close()

    try:
        return store.with_retry(attempt)
    except store.ValidationError as exc:
        raise ApiError(400, {"error": exc.message, "fields": exc.fields}) from None
    except store.NotFound:
        raise ApiError(404, {"error": "not found"}) from None


def _admin_number(text):
    """Числовой id из пути админки. Слишком длинный номер — 400, не числовой — 404."""
    if store.is_too_long_number(text):
        raise ApiError(400, {"error": "invalid id", "fields": {"id": "слишком длинный номер"}})
    number = store.as_int(text)
    if number is None:
        raise ApiError(404, {"error": "not found"})
    return number


def _admin_text_id(text):
    if store.is_too_long_number(text):
        raise ApiError(400, {"error": "invalid id", "fields": {"id": "слишком длинный номер"}})
    return text


def _require_shopper(handler, message):
    """Товары и заказы открыты только для входа: активная сессия покупателя, токен бота или админский токен.
    Иначе 401 login_required. Сессия проверяется той же функцией, что и в /api/auth/me."""
    if handler._role() is not None:
        return
    token = auth.read_session_token(handler.headers.get("Cookie"))
    if token is not None:
        user = _run_auth(handler, store.user_for_session, auth.token_hash(token), store.utc_now())
        if user is not None:
            return
    raise _auth_error("login_required", message=message)


def _products_list(handler, params, query):
    _require_shopper(handler, "Войдите, чтобы смотреть товары")
    category = (query.get("category") or "").strip() or None
    search = (query.get("q") or "").strip() or None
    if search and len(search) > store.MAX_SEARCH_LEN:
        raise ApiError(400, {"error": "query too long", "fields": {"q": "слишком длинный запрос"}})
    return 200, _run(handler, lambda c: store.list_products(c, category=category, search=search))


def _product_get(handler, params, query):
    _require_shopper(handler, "Войдите, чтобы смотреть товары")
    product = _run(handler, store.get_product, params["id"])
    if product is None:
        raise ApiError(404, {"error": "not found"})
    return 200, product


def _order_create(handler, params, query):
    _require_shopper(handler, "Войдите, чтобы оформить заказ")
    handler._check_rate("orders", handler.server.order_limit, ORDER_RATE_WINDOW)
    if not handler.server.limiter.try_hit("orders_all", "all", ORDER_GLOBAL_LIMIT, ORDER_GLOBAL_WINDOW):
        raise ApiError(429, {"error": "too many requests"})
    payload = handler._read_json()
    # Если заказ оформляет вошедший покупатель, привязываем заказ к его аккаунту
    user_id = None
    token = auth.read_session_token(handler.headers.get("Cookie"))
    if token is not None:
        user = _run_auth(handler, store.user_for_session, auth.token_hash(token), store.utc_now())
        if user is not None:
            user_id = user["id"]
    return 201, _run(handler, store.create_order, payload, user_id)


def _require_bot_or_admin(handler):
    if handler._role() not in ("bot", "admin"):
        raise ApiError(401, {"error": "unauthorized"})


def _bot_orders(handler, params, query):
    """GET /api/bot/orders: новые заказы, о которых бот ещё не сообщил владельцу (токен бота или админа)."""
    _require_bot_or_admin(handler)
    return 200, _run(handler, store.list_unnotified_orders)


def _bot_order_notified(handler, params, query):
    """POST /api/bot/orders/{id}/notified: бот отметил, что владельцу написал."""
    _require_bot_or_admin(handler)
    return 200, _run(handler, store.mark_order_notified, _admin_number(params["id"]))


def _my_orders(handler, params, query):
    """GET /api/my/orders: заказы вошедшего покупателя."""
    user = _session_user(handler, "Войдите, чтобы посмотреть заказы")
    return 200, _run(handler, store.list_user_orders, user["id"])


def _event_create(handler, params, query):
    handler._check_rate("events", handler.server.event_limit, EVENT_RATE_WINDOW)
    payload = handler._read_json()
    _run(handler, store.record_event, payload)
    return 204, None


def _admin_stats(handler, params, query):
    return 200, _run(handler, store.get_stats)


def _admin_orders(handler, params, query):
    return 200, _run(handler, store.list_orders)


def _admin_order_patch(handler, params, query):
    order_id = _admin_number(params["id"])
    payload = handler._read_json()
    if not isinstance(payload, dict):
        raise ApiError(400, {"error": "Проверьте заполненные поля", "fields": {"body": "ожидается объект"}})
    if "status" not in payload and "tracking" not in payload:
        raise ApiError(400, {"error": "Проверьте заполненные поля", "fields": {"status": "обязательное поле"}})
    result = {"id": order_id}
    if "tracking" in payload:
        result.update(_run(handler, store.update_order_tracking, order_id, payload["tracking"]))
    if "status" in payload:
        result.update(_run(handler, store.update_order_status, order_id, payload["status"]))
    return 200, result


def _admin_customers(handler, params, query):
    return 200, _run(handler, store.list_customers)


def _admin_products(handler, params, query):
    return 200, _run(handler, lambda c: store.list_products(c, admin=True))


def _admin_product_create(handler, params, query):
    payload = handler._read_json()
    return 201, _run(handler, store.create_product, payload)


def _admin_product_image_put(handler, params, query):
    """PUT /api/admin/products/{id}/image: тело — сама картинка (PNG, JPEG или WEBP)."""
    product_id = _admin_text_id(params["id"])
    header = images.header_type(handler.headers.get("Content-Type"))
    if header is None:
        raise ApiError(415, {"error": "Проверьте картинку", "fields": {"image": "нужен PNG, JPEG или WEBP"}})
    state = handler._body_state
    if state == "no-length":
        raise ApiError(411, {"error": "length required"})
    if state == "bad-length":
        raise ApiError(400, {"error": "bad content-length"})
    if state == "too-large" or len(handler._body) > images.MAX_BYTES["product"]:
        raise ApiError(413, {"error": "Проверьте картинку", "fields": {"image": "больше 4 МБ"}})
    try:
        meta = images.inspect(handler._body, header, "product")
    except images.ImageError as exc:
        raise ApiError(400, {"error": "Проверьте картинку", "fields": exc.fields or {"image": exc.message}}) from None
    _run(handler, store.save_product_image, product_id, handler._body, meta, store.utc_now())
    return 200, {"ok": True}


def _admin_product_delete(handler, params, query):
    product_id = _admin_text_id(params["id"])
    _run(handler, store.delete_product, product_id)
    return 200, {"ok": True}


def _admin_product_image_delete(handler, params, query):
    product_id = _admin_text_id(params["id"])
    _run(handler, store.delete_product_image, product_id)
    return 200, {"ok": True}


def _product_image_get(handler, params, query):
    """Обложка товара публичная: её показывают и магазин, и админка."""
    found = _run(handler, store.get_product_image, params["id"])
    if found is None:
        raise ApiError(404, {"error": "not found"})
    return 200, ImageResponse(found["data"], found["content_type"], found["sha256"])


def _admin_product_patch(handler, params, query):
    product_id = _admin_text_id(params["id"])
    payload = handler._read_json()
    return 200, _run(handler, store.update_product, product_id, payload)


def _admin_achievement_grant(handler, params, query):
    """POST /api/admin/achievements/grant: {"login": почта или ник, "code": код достижения}."""
    payload = _auth_json(handler)
    result = _run_auth(handler, store.grant_achievement_by_login, payload, store.utc_now())
    return 200, {"ok": True, **result}


def _admin_rank_set(handler, params, query):
    """POST /api/admin/rank: {"login": почта или ник, "level": 1..15} — поднять ранг (опыт только растёт)."""
    payload = _auth_json(handler)
    result = _run_auth(handler, store.set_rank_by_login, payload, store.utc_now())
    return 200, {"ok": True, **result}


def make_backup(db_path) -> bytes:
    """Делает копию базы через встроенный механизм SQLite (безопасно, даже когда идут заказы).
    Проверяет копию командой PRAGMA integrity_check. Если проверка не 'ok' — ошибка, копию не отдаём."""
    with tempfile.TemporaryDirectory(prefix="zento-backup-") as tmp:
        target = Path(tmp) / "backup.db"
        source = sqlite3.connect(str(db_path), timeout=10)
        try:
            copy = sqlite3.connect(str(target))
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        check = sqlite3.connect(str(target))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise RuntimeError("backup integrity check failed")
        return target.read_bytes()


def _admin_backup(handler, params, query):
    data = make_backup(handler.server.db_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return 200, FileResponse(data, f"zento-backup-{stamp}.db")


# ---------------------------------------------------------------- профиль покупателя

def _session_user(handler, message=None):
    """Пользователь по cookie-сессии. Токен бота и админский токен сюда не пускают: без сессии — 401."""
    token = auth.read_session_token(handler.headers.get("Cookie"))
    user = None
    if token is not None:
        user = _run_auth(handler, store.user_for_session, auth.token_hash(token), store.utc_now())
    if user is None:
        raise _auth_error("login_required", message=message)
    return user


def _hit_user(handler, bucket, user_id):
    """Лимит на одного покупателя (не на IP) по DEFAULT_AUTH_LIMITS. Исчерпан — 429."""
    limit, window = handler.server.auth_limits[bucket]
    key = f"user:{user_id}"
    limiter = handler.server.limiter
    if not limiter.try_hit(bucket, key, limit, window):
        retry = limiter.retry_after(bucket, key, window)
        raise _auth_error("too_many_requests", headers=[("Retry-After", str(retry))])


def _profile_me(handler, params, query):
    user = _session_user(handler)
    return 200, {"ok": True, "user": _run_auth(handler, store.own_profile, user["id"], store.utc_now())}


def _profile_patch(handler, params, query):
    user = _session_user(handler)
    _hit_user(handler, "profile_patch", user["id"])
    payload = _auth_json(handler)
    profile = _run_auth(handler, store.update_profile, user["id"], payload, store.utc_now())
    return 200, {"ok": True, "user": profile}


def _profile_put_image(handler, kind):
    user = _session_user(handler)
    _hit_user(handler, "image_upload", user["id"])
    header = images.header_type(handler.headers.get("Content-Type"))
    if header is None:
        raise _auth_error("image_type_unsupported")
    state = handler._body_state
    if state == "no-length":
        raise ApiError(411, {"error": "length required"})
    if state == "bad-length":
        raise ApiError(400, {"error": "bad content-length"})
    if state == "too-large" or len(handler._body) > images.MAX_BYTES[kind]:
        raise _auth_error(f"{kind}_too_large")
    try:
        meta = images.inspect(handler._body, header, kind)
    except images.ImageError as exc:
        raise _auth_error(exc.code, exc.fields, exc.message) from None
    # Тело уже прочитано: база блокируется только на время записи.
    profile = _run_auth(handler, store.save_image, user["id"], kind, handler._body, meta, store.utc_now())
    return 200, {"ok": True, "user": profile}


def _profile_delete_image(handler, kind):
    user = _session_user(handler)
    profile = _run_auth(handler, store.delete_image, user["id"], kind, store.utc_now())
    return 200, {"ok": True, "user": profile}


def _profile_put_avatar(handler, params, query):
    return _profile_put_image(handler, "avatar")


def _profile_put_banner(handler, params, query):
    return _profile_put_image(handler, "banner")


def _profile_delete_avatar(handler, params, query):
    return _profile_delete_image(handler, "avatar")


def _profile_delete_banner(handler, params, query):
    return _profile_delete_image(handler, "banner")


def _user_profile(handler, params, query):
    _session_user(handler, "Войдите, чтобы посмотреть профиль")
    profile = _run(handler, store.public_profile, params["nick"].lower())
    if profile is None:
        raise ApiError(404, {"error": "not found"})
    return 200, {"ok": True, "user": profile}


def _user_image(handler, params, kind):
    _session_user(handler, "Войдите, чтобы посмотреть картинку")
    found = _run(handler, store.get_image, params["nick"].lower(), kind)
    if found is None:
        raise ApiError(404, {"error": "not found"})
    return 200, ImageResponse(found["data"], found["content_type"], found["sha256"])


def _user_avatar(handler, params, query):
    return _user_image(handler, params, "avatar")


def _user_banner(handler, params, query):
    return _user_image(handler, params, "banner")


# ---------------------------------------------------------------- вход, регистрация, капча

def _auth_error(code, fields=None, message=None, headers=()):
    """Ошибка в формате контракта: ok=false, error, message и fields (только при ошибках полей)."""
    status, default_message = AUTH_ERRORS[code]
    body = {"ok": False, "error": code, "message": message or default_message}
    if fields is not None:
        body["fields"] = fields
    return ApiError(status, body, headers)


def _clean(check, payload):
    """Запускает проверку формы; ошибки полей превращаются в ответ 400."""
    try:
        return check(payload)
    except store.ValidationError as exc:
        raise _auth_error("validation_error", exc.fields, exc.message) from None


def _require_json(handler):
    media = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise _auth_error("unsupported_media_type")


def _auth_json(handler):
    """Тело POST для входа и капчи: нужен Content-Type JSON и объект внутри."""
    _require_json(handler)
    state = getattr(handler, "_body_state", "no-length")
    if state == "too-large":
        raise _auth_error("payload_too_large")
    if state == "ok":
        try:
            data = json.loads(handler._body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    raise _auth_error("validation_error", {"body": "Нужен JSON-объект"})


def _run_auth(handler, func, *args):
    """Как _run, но ошибки store превращаются в ответы контракта (с кодом и текстом)."""
    def attempt():
        conn = store.connect(handler.server.db_path)
        try:
            return func(conn, *args)
        finally:
            conn.close()

    try:
        return store.with_retry(attempt)
    except store.ValidationError as exc:
        raise _auth_error("validation_error", exc.fields, exc.message) from None
    except store.Rejected as exc:
        raise _auth_error(exc.code, exc.fields, exc.message) from None


def _hit_auth(handler, name, code):
    """Считает попытку по лимиту из DEFAULT_AUTH_LIMITS. Лимит исчерпан — 429 с Retry-After."""
    limit, window = handler.server.auth_limits[name]
    ip = _client_ip(handler)
    limiter = handler.server.limiter
    if not limiter.try_hit(name, ip, limit, window):
        retry = limiter.retry_after(name, ip, window)
        raise _auth_error(code, headers=[("Retry-After", str(retry))])


def _public_user(user):
    return {"id": user["id"], "nick": user["nick"], "email": user["email"]}


def _login_key(login, identity, user):
    """Ключ лимита входа. Для найденного покупателя — его id, чтобы почта и телефон считались вместе.
    Для неизвестного логина — почта или телефон в нормальном виде. Хеш, чтобы логин не лежал в базе открыто."""
    if user is not None:
        subject = f"user:{user['id']}"
    elif identity is not None:
        subject = f"{identity[0]}:{identity[1]}"
    else:
        subject = "raw:" + login.strip().lower()
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def _auth_me(handler, params, query):
    token = auth.read_session_token(handler.headers.get("Cookie"))
    user = None
    if token is not None:
        user = _run_auth(handler, store.user_for_session, auth.token_hash(token), store.utc_now())
        if user is not None:  # вернулся по старой сессии: тоже ежедневный вход
            _run_auth(handler, store.award_xp, user["id"], "login", store.utc_now())
    return 200, {"ok": True, "user": user}


def _auth_register(handler, params, query):
    _hit_auth(handler, "register", "too_many_requests")
    payload = _auth_json(handler)
    data = _clean(store.clean_registration, payload)
    if payload.get("agree") is not True:
        raise _auth_error("rules_not_accepted")
    password_hash = auth.hash_password(data.pop("password"))
    token = auth.new_token()
    user = _run_auth(handler, store.register_user, data, payload.get("captcha_ticket"),
                     password_hash, auth.token_hash(token), store.utc_now())
    _run_auth(handler, store.award_xp, user["id"], "login", store.utc_now())
    cookie = auth.session_cookie(token, handler.server.cookie_secure)
    return 201, {"ok": True, "user": _public_user(user)}, [("Set-Cookie", cookie)]


def _auth_login(handler, params, query):
    payload = _auth_json(handler)
    login, password = _clean(store.clean_login, payload)
    identity = store.login_identity(login)
    user = _run_auth(handler, store.find_user_for_login, identity) if identity else None
    key = _login_key(login, identity, user)
    ip = _client_ip(handler)
    limiter = handler.server.limiter
    limits = handler.server.auth_limits

    waits = []
    for bucket, subject in (("login_key", key), ("login_ip", ip)):
        limit, window = limits[bucket]
        if limiter.is_over(bucket, subject, limit, window):
            waits.append(limiter.retry_after(bucket, subject, window))
    if waits:
        raise _auth_error("too_many_attempts", headers=[("Retry-After", str(max(waits)))])

    # Для неизвестного логина тоже считаем хеш: время ответа не должно выдавать, есть ли аккаунт.
    checked = auth.verify_password(password, user["password_hash"] if user else auth.dummy_hash())
    if user is None or not checked:
        limiter.record("login_key", key, limits["login_key"][1])
        limiter.record("login_ip", ip, limits["login_ip"][1])
        raise _auth_error("invalid_credentials")

    token = auth.new_token()
    _run_auth(handler, store.create_session, user["id"], auth.token_hash(token), store.utc_now())
    _run_auth(handler, store.award_xp, user["id"], "login", store.utc_now())
    cookie = auth.session_cookie(token, handler.server.cookie_secure)
    return 200, {"ok": True, "user": _public_user(user)}, [("Set-Cookie", cookie)]


def _auth_logout(handler, params, query):
    _require_json(handler)
    token = auth.read_session_token(handler.headers.get("Cookie"))
    if token is not None:
        _run_auth(handler, store.delete_session, auth.token_hash(token))
    return 200, {"ok": True}, [("Set-Cookie", auth.expired_session_cookie(handler.server.cookie_secure))]


def _log_mail_failure(exc):
    # В лог — только тип ошибки: ни адреса, ни ключа, ни кода.
    print(f"Письмо для восстановления пароля не отправлено: {type(exc).__name__}", file=sys.stderr)


def _auth_reset_request(handler, params, query):
    _hit_auth(handler, "reset_request", "too_many_requests")
    login = _clean(store.clean_reset_request, _auth_json(handler))
    # Проверку настроек делаем до поиска аккаунта: 503 не зависит от того, есть ли такой покупатель.
    if not mailer.is_configured():
        raise _auth_error("mail_failed")
    user = _run_auth(handler, store.find_user_by_login, login)
    if user is not None and user["email"]:
        code = auth.new_reset_code()
        stamp = _run_auth(handler, store.reset_begin, user["id"], auth.hash_password(code), store.utc_now())
        # stamp is None — код уже выслан меньше минуты назад: письмо повторно не шлём, ответ тот же.
        if stamp is not None:
            try:
                mailer.send_reset_code(user["email"], code)
            except mailer.MailError as exc:
                # Сбой Brevo не должен отличаться от «аккаунта нет»: ответ тот же, код гасим.
                _log_mail_failure(exc)
                _run_auth(handler, store.reset_cancel, user["id"], stamp)
    return 200, {"ok": True, "message": RESET_MESSAGE}


def _auth_reset_confirm(handler, params, query):
    payload = _auth_json(handler)
    login, code = _clean(store.clean_reset_confirm, payload)
    token = auth.new_token()
    user = _run_auth(handler, store.reset_confirm, login, code, payload.get("password"),
                     payload.get("password2"), auth.token_hash(token), store.utc_now())
    cookie = auth.session_cookie(token, handler.server.cookie_secure)
    return 200, {"ok": True, "user": _public_user(user)}, [("Set-Cookie", cookie)]


def _captcha_new(handler, params, query):
    _hit_auth(handler, "captcha_new", "too_many_requests")
    images = handler.server.captcha_images
    if not captcha.is_available(images):
        raise _auth_error("captcha_unavailable")
    challenge = captcha.new_challenge(images)
    _run_auth(handler, store.captcha_create, challenge["challenge_id"], challenge["expected"], store.utc_now())
    return 200, {
        "ok": True,
        "challenge_id": challenge["challenge_id"],
        "expires_in": captcha.CHALLENGE_TTL,
        "grid": captcha.GRID,
        "image": challenge["image"],
        "tiles": challenge["tiles"],
    }


def _captcha_verify(handler, params, query):
    _hit_auth(handler, "captcha_verify", "too_many_attempts")
    payload = _auth_json(handler)
    challenge_id, placements = _clean(store.clean_captcha_verify, payload)
    ticket = auth.new_token()
    outcome = _run_auth(handler, store.captcha_verify, challenge_id, placements,
                        auth.token_hash(ticket), store.utc_now())
    if outcome == "incomplete":
        raise _auth_error("captcha_incomplete")
    if outcome == "wrong":
        raise _auth_error("captcha_wrong")
    return 200, {"ok": True, "ticket": ticket, "expires_in": captcha.TICKET_TTL}


ROUTES = [
    ("GET", re.compile(r"^/api/auth/me$"), _auth_me),
    ("POST", re.compile(r"^/api/auth/register$"), _auth_register),
    ("POST", re.compile(r"^/api/auth/login$"), _auth_login),
    ("POST", re.compile(r"^/api/auth/logout$"), _auth_logout),
    ("POST", re.compile(r"^/api/auth/reset/request$"), _auth_reset_request),
    ("POST", re.compile(r"^/api/auth/reset/confirm$"), _auth_reset_confirm),
    ("GET", re.compile(r"^/api/captcha/new$"), _captcha_new),
    ("POST", re.compile(r"^/api/captcha/verify$"), _captcha_verify),
    ("GET", re.compile(r"^/api/products$"), _products_list),
    ("GET", re.compile(r"^/api/products/(?P<id>[^/]+)$"), _product_get),
    ("GET", re.compile(r"^/api/products/(?P<id>[^/]+)/image$"), _product_image_get),
    ("POST", re.compile(r"^/api/orders$"), _order_create),
    ("GET", re.compile(r"^/api/my/orders$"), _my_orders),
    ("GET", re.compile(r"^/api/bot/orders$"), _bot_orders),
    ("POST", re.compile(r"^/api/bot/orders/(?P<id>[^/]+)/notified$"), _bot_order_notified),
    ("POST", re.compile(r"^/api/events$"), _event_create),
    ("GET", re.compile(r"^/api/admin/stats$"), _admin_stats),
    ("GET", re.compile(r"^/api/admin/orders$"), _admin_orders),
    ("PATCH", re.compile(r"^/api/admin/orders/(?P<id>[^/]+)$"), _admin_order_patch),
    ("GET", re.compile(r"^/api/admin/customers$"), _admin_customers),
    ("GET", re.compile(r"^/api/admin/products$"), _admin_products),
    ("PUT", PRODUCT_IMAGE_PATH, _admin_product_image_put),
    ("DELETE", PRODUCT_IMAGE_PATH, _admin_product_image_delete),
    ("DELETE", re.compile(r"^/api/admin/products/(?P<id>[^/]+)$"), _admin_product_delete),
    ("POST", re.compile(r"^/api/admin/products$"), _admin_product_create),
    ("PATCH", re.compile(r"^/api/admin/products/(?P<id>[^/]+)$"), _admin_product_patch),
    ("GET", re.compile(r"^/api/admin/backup$"), _admin_backup),
    ("POST", re.compile(r"^/api/admin/achievements/grant$"), _admin_achievement_grant),
    ("POST", re.compile(r"^/api/admin/rank$"), _admin_rank_set),
    ("GET", re.compile(r"^/api/profile/me$"), _profile_me),
    ("PATCH", re.compile(r"^/api/profile$"), _profile_patch),
    ("PUT", re.compile(r"^/api/profile/avatar$"), _profile_put_avatar),
    ("PUT", re.compile(r"^/api/profile/banner$"), _profile_put_banner),
    ("DELETE", re.compile(r"^/api/profile/avatar$"), _profile_delete_avatar),
    ("DELETE", re.compile(r"^/api/profile/banner$"), _profile_delete_banner),
    ("GET", re.compile(r"^/api/users/(?P<nick>[^/]+)/profile$"), _user_profile),
    ("GET", re.compile(r"^/api/users/(?P<nick>[^/]+)/avatar$"), _user_avatar),
    ("GET", re.compile(r"^/api/users/(?P<nick>[^/]+)/banner$"), _user_banner),
]


# ---------------------------------------------------------------- HTTP

def _body_limits(method, path):
    """Сколько байт тела читать и до какого размера выбрасывать. Для картинок профиля порог выше."""
    if method == "PUT" and (path in IMAGE_UPLOAD_PATHS or PRODUCT_IMAGE_PATH.match(path)):
        return images.BANNER_MAX_BYTES, IMAGE_DISCARD_LIMIT_BYTES
    return MAX_BODY_BYTES, DISCARD_LIMIT_BYTES


class Handler(BaseHTTPRequestHandler):
    server_version = "ZENTO"
    sys_version = ""
    # Соединение, которое молчит дольше этого времени, закрывается.
    timeout = HANDLER_TIMEOUT_SECONDS

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        try:
            if method == "PUT" and path in IMAGE_UPLOAD_PATHS:
                # Сначала вход: без сессии тело картинки не читаем, а соединение закрываем после ответа.
                try:
                    _session_user(self)
                except ApiError:
                    self.close_connection = True
                    raise
            # Тело читаем сразу, даже если маршрут откажет (например 503): иначе сокет закроется с непрочитанными данными.
            read_limit, discard_limit = _body_limits(method, path)
            self._prefetch_body(read_limit, discard_limit)
            if path == "/api" or path.startswith("/api/"):
                status, body, *extra = self._route_api(method, path, query)
                headers = extra[0] if extra else ()
                if isinstance(body, FileResponse):
                    self._send_file(status, body, headers)
                elif isinstance(body, ImageResponse):
                    self._send_image(status, body, headers)
                else:
                    self._send_json(status, body, headers)
            elif method == "GET":
                self._send_static(path)
            else:
                raise ApiError(405, {"error": "method not allowed"})
        except ApiError as exc:
            body = exc.body
            # У старых ответов об ошибке всегда есть поле fields; ответы контракта (с ok) не трогаем.
            if exc.status == 400 and isinstance(body, dict) and "ok" not in body:
                body = {**body, "fields": body.get("fields", {})}
            self._send_json(exc.status, body, exc.headers)
        except (TimeoutError, ConnectionError):
            # Клиент молчит или оборвал соединение: отвечать некому, просто закрываем.
            self.close_connection = True
        except Exception:
            # В лог пишем только место ошибки, без тела запроса и заголовков.
            print(f"Внутренняя ошибка: {method} {path}", file=sys.stderr)
            traceback.print_exc()
            self._send_json(500, {"error": "internal error"})

    def _route_api(self, method, path, query):
        if path == "/api/admin" or path.startswith("/api/admin/"):
            if not self.server.admin_token:
                raise ApiError(503, {"error": "admin disabled"})
            ip = _client_ip(self)
            if self.server.limiter.is_over("admin", ip, ADMIN_FAIL_LIMIT, ADMIN_FAIL_WINDOW):
                raise ApiError(429, {"error": "too many requests"})
            role = self._role()
            if role is None:
                self.server.limiter.record("admin", ip, ADMIN_FAIL_WINDOW)
                raise ApiError(401, {"error": "unauthorized"})
            if role != "admin":
                # Токен бота правильный, но админке он не нужен: отказ, а не ошибка входа и не попытка взлома.
                raise ApiError(403, {"error": "forbidden"})
        path_matched = False
        for route_method, pattern, func in ROUTES:
            match = pattern.match(path)
            if match is None:
                continue
            path_matched = True
            if route_method != method:
                continue
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            return func(self, params, query)
        if path_matched:
            raise ApiError(405, {"error": "method not allowed"})
        raise ApiError(404, {"error": "not found"})

    def _check_rate(self, bucket, limit, window):
        if not self.server.limiter.try_hit(bucket, _client_ip(self), limit, window):
            raise ApiError(429, {"error": "too many requests"})

    def _role(self):
        """Кто пришёл по токену: 'admin', 'bot' или None (токен неверный или его нет)."""
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        given = token.strip().encode("utf-8")
        if self.server.admin_token and hmac.compare_digest(given, self.server.admin_token.encode("utf-8")):
            return "admin"
        if self.server.bot_token and hmac.compare_digest(given, self.server.bot_token.encode("utf-8")):
            return "bot"
        return None

    def _prefetch_body(self, read_limit=MAX_BODY_BYTES, discard_limit=DISCARD_LIMIT_BYTES):
        """Читает тело запроса один раз. Тело больше read_limit не сохраняем; до discard_limit читаем и выбрасываем."""
        self._body = b""
        self._body_state = "ok"
        if self.headers.get("Transfer-Encoding"):
            self._body_state = "no-length"
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body_state = "no-length"
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._body_state = "bad-length"
            return
        if length < 0:
            self._body_state = "bad-length"
            return
        if length > read_limit:
            self._body_state = "too-large"
            if length <= discard_limit:
                self.rfile.read(length)
            else:
                self.close_connection = True
            return
        self._body = self.rfile.read(length) if length else b""

    def _read_json(self):
        state = getattr(self, "_body_state", "no-length")
        if state == "no-length":
            raise ApiError(411, {"error": "length required"})
        if state == "bad-length":
            raise ApiError(400, {"error": "bad content-length"})
        if state == "too-large":
            raise ApiError(413, {"error": "payload too large"})
        try:
            return json.loads(self._body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, {"error": "invalid json"}) from None

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)

    def _send_json(self, status, body, headers=()):
        self.send_response(status)
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        if status == 204 or body is None:
            self.end_headers()
            return
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, status, file, headers=()):
        self.send_response(status)
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", file.content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{file.filename}"')
        self.send_header("Content-Length", str(len(file.data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(file.data)

    def _send_image(self, status, image, headers=()):
        etag = f'"{image.sha256}"'
        wanted = [item.strip() for item in self.headers.get("If-None-Match", "").split(",") if item.strip()]
        if any(item in ("*", etag, f"W/{etag}") for item in wanted):
            status = 304
        self.send_response(status)
        self._security_headers()
        self.send_header("Cache-Control", IMAGE_CACHE_CONTROL)
        self.send_header("ETag", etag)
        for name, value in headers:
            self.send_header(name, value)
        if status == 304:
            self.end_headers()
            return
        self.send_header("Content-Type", image.content_type)
        self.send_header("Content-Length", str(len(image.data)))
        self.end_headers()
        self.wfile.write(image.data)

    def _send_static(self, path):
        found = resolve_static(self.server.static_root, path)
        if found is None:
            raise ApiError(404, {"error": "not found"})
        file_path, content_type = found
        data = file_path.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # Ошибки, которые отдаёт сам http.server (например 501), тоже в JSON.
        self._send_json(code, {"error": message or "error"})

    def log_request(self, code="-", size="-"):
        # Пишем метод, путь без строки запроса и статус. Токен, тело и IP не пишем.
        if getattr(self.server, "quiet", False):
            return
        print(f"{self.command or '-'} {urlsplit(self.path).path} {code}", file=sys.stderr)

    def log_message(self, format, *args):
        # Стандартные сообщения содержат заголовки запроса, поэтому они отключены.
        pass


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path, admin_token, static_root=ROOT, quiet=False,
                 order_limit=ORDER_RATE_LIMIT, event_limit=EVENT_RATE_LIMIT,
                 bot_token=None, cookie_secure=True, auth_limits=None, captcha_dir=None):
        self.db_path = Path(db_path)
        self.admin_token = admin_token or None
        self.bot_token = bot_token or None
        self.static_root = Path(static_root).resolve()
        self.quiet = quiet
        self.order_limit = order_limit
        self.event_limit = event_limit
        self.cookie_secure = bool(cookie_secure)
        self.auth_limits = {**DEFAULT_AUTH_LIMITS, **(auth_limits or {})}
        # Картинки капчи читаются один раз при запуске. Если их мало, сервер всё равно работает.
        if captcha_dir is None:
            captcha_dir = self.static_root / "assets" / "captcha"
        self.captcha_images = captcha.load_images(captcha_dir)
        self.limiter = RateLimiter(clock=time.time, db_path=self.db_path)
        super().__init__(address, Handler)


def create_server(host, port, *, db_path=DEFAULT_DB, products_path=DEFAULT_PRODUCTS,
                  admin_token=None, static_root=ROOT, quiet=False,
                  order_limit=ORDER_RATE_LIMIT, event_limit=EVENT_RATE_LIMIT,
                  bot_token=None, cookie_secure=None, auth_limits=None, captcha_dir=None) -> AppServer:
    """Готовит базу (создаёт, заполняет, отменяет просроченные заказы) и возвращает сервер, который ещё не запущен.

    cookie_secure=None — взять из COOKIE_SECURE: флаг Secure выключен только при COOKIE_SECURE=0."""
    if admin_token and len(admin_token) < MIN_ADMIN_TOKEN_LENGTH:
        raise ValueError(
            f"ADMIN_TOKEN слишком короткий: нужно не меньше {MIN_ADMIN_TOKEN_LENGTH} символов"
        )
    if bot_token and len(bot_token) < MIN_BOT_TOKEN_LENGTH:
        raise ValueError(
            f"SHOP_BOT_TOKEN слишком короткий: нужно не меньше {MIN_BOT_TOKEN_LENGTH} символов"
        )
    if bot_token and bot_token == admin_token:
        raise ValueError("SHOP_BOT_TOKEN не должен совпадать с ADMIN_TOKEN")
    if cookie_secure is None:
        cookie_secure = os.environ.get("COOKIE_SECURE", "1").strip() != "0"
    store.init_db(db_path, products_path)
    return AppServer(
        (host, port),
        db_path=db_path,
        admin_token=admin_token,
        static_root=static_root,
        quiet=quiet,
        order_limit=order_limit,
        event_limit=event_limit,
        bot_token=bot_token,
        cookie_secure=cookie_secure,
        auth_limits=auth_limits,
        captcha_dir=captcha_dir,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сервер магазина ZENTO")
    parser.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="порт (по умолчанию 8080)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="файл базы SQLite")
    parser.add_argument("--products", default=str(DEFAULT_PRODUCTS), help="JSON с товарами для первого заполнения")
    args = parser.parse_args(argv)

    admin_token = os.environ.get("ADMIN_TOKEN", "").strip() or None
    if admin_token is None:
        print("ADMIN_TOKEN не задан: админка отключена (ответ 503).", file=sys.stderr)
    bot_token = os.environ.get("SHOP_BOT_TOKEN", "").strip() or None

    try:
        server = create_server(
            args.host, args.port,
            db_path=args.db, products_path=args.products, admin_token=admin_token,
            bot_token=bot_token,
        )
    except ValueError as exc:
        print(f"Сервер не запущен: {exc}", file=sys.stderr)
        return 1

    if not mailer.is_configured():
        print("Почта не настроена (BREVO_API_KEY, MAIL_FROM): восстановление пароля вернёт 503.", file=sys.stderr)
    if not captcha.is_available(server.captcha_images):
        print(f"Капча недоступна: нужно не меньше {captcha.MIN_IMAGES} картинок в assets/captcha "
              f"(найдено {len(server.captcha_images)}).", file=sys.stderr)
    print(f"ZENTO запущен: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
