"""Работа с базой SQLite. Весь SQL и все правила проверки данных живут здесь.

Только стандартная библиотека. Ошибки проверки поднимаются как ValidationError,
их превращает в ответ 400 слой HTTP (app.py).
"""

from __future__ import annotations

import json
import re
import unicodedata
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from sqlite3 import OperationalError
from time import sleep
from zoneinfo import ZoneInfo

import achievements
import auth
import captcha
import ranks

SERVER_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SERVER_DIR / "schema.sql"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

ORDER_STATUSES = ("new", "paid", "shipped", "done", "cancelled")
# Разрешённые переходы статуса. Отмена окончательная: товар уже вернули на склад,
# и заново резервировать его при «возврате из отмены» нельзя без конфликтов.
ORDER_TRANSITIONS = {
    "new": ("paid", "cancelled"),
    "paid": ("shipped", "cancelled"),
    "shipped": ("done",),
    "done": (),
    "cancelled": (),
}
# Неоплаченный заказ в статусе new отменяется автоматически через это время.
ORDER_TTL_MINUTES = 60

MAX_QTY = 99
MAX_ORDER_ITEMS = 50
MAX_INT_DIGITS = 18          # длиннее — 400, чтобы не упереться в лимиты SQLite
STATS_DAYS = 14
TOP_PRODUCTS = 5
MAX_SEARCH_LEN = 100

DB_TIMEOUT_SECONDS = 10
DB_RETRY_ATTEMPTS = 3

PRODUCT_FIELDS = ("name", "category", "price", "description", "color", "stock")
PUBLIC_PRODUCT_COLUMNS = "id, name, category, price, description, color, stock"
ADMIN_PRODUCT_COLUMNS = PUBLIC_PRODUCT_COLUMNS + ", created_at"

PHONE_CHARS_RE = re.compile(r"^\+?[0-9 ()\-.]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Ник: 3–20 символов, только английские буквы и цифры. Пробелов и знаков нет.
NICK_RE = re.compile(r"[A-Za-z0-9]{3,20}")
# Ник нельзя смешивать из латиницы и кириллицы: «аnna» с кириллической а выглядит как «anna».
NICK_RESERVED = frozenset({"admin", "zento", "support", "bot", "administrator", "root", "moderator"})
PUBLIC_ID_DIGITS = 10                 # публичный ID покупателя: столько цифр, с ведущими нулями
RESET_CODE_RE = re.compile(r"[0-9]{6}")
RESET_CODE_TTL_SECONDS = 10 * 60      # код действует 10 минут
RESET_RESEND_SECONDS = 60             # новый код можно запросить не раньше чем через минуту
RESET_MAX_ATTEMPTS = 5                # неверных вводов на один код
COLOR_FORBIDDEN_RE = re.compile(r"[<>\"'\\;{}]")
INT_TEXT_RE = re.compile(r"^\s*-?\d+\s*$")
LONG_NUMBER_RE = re.compile(r"^\s*[+-]?[0-9]+\s*$")


class ValidationError(Exception):
    """Ошибка проверки: fields = {поле: текст}."""

    def __init__(self, fields, message="Проверьте заполненные поля"):
        super().__init__(message)
        self.fields = fields
        self.message = message


class NotFound(Exception):
    """Запись не найдена."""


class Rejected(Exception):
    """Отказ по правилам входа и капчи. code — короткий код для ответа (см. app.py), fields — ошибки полей."""

    def __init__(self, code, fields=None, message=None):
        super().__init__(code)
        self.code = code
        self.fields = fields
        self.message = message


# ---------------------------------------------------------------- время и id

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def new_product_id() -> str:
    return "p" + secrets.token_hex(6)


def _new_public_id(conn) -> str:
    """Случайный публичный ID из 10 цифр, которого ещё нет у других покупателей. Вызывать в транзакции."""
    while True:
        candidate = f"{secrets.randbelow(10 ** PUBLIC_ID_DIGITS):0{PUBLIC_ID_DIGITS}d}"
        if conn.execute("SELECT 1 FROM users WHERE public_id = ?", (candidate,)).fetchone() is None:
            return candidate


def is_too_long_number(text) -> bool:
    """True, если строка — число длиннее MAX_INT_DIGITS цифр."""
    text = str(text)
    return bool(LONG_NUMBER_RE.match(text)) and len(text.strip().lstrip("+-").strip()) > MAX_INT_DIGITS


# ---------------------------------------------------------------- соединение

def _lower_unicode(value):
    # Встроенный LOWER в SQLite понимает только ASCII, поэтому для поиска
    # по кириллице регистр меняем в Python.
    return None if value is None else value.casefold()


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("lower_unicode", 1, _lower_unicode, deterministic=True)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Транзакция на запись. BEGIN IMMEDIATE не даёт двум заказам списать один остаток."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def with_retry(func, attempts=DB_RETRY_ATTEMPTS, pause=0.05):
    """Повторяет операцию, если SQLite ответил «database is locked». Остальные ошибки не трогаем."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts:
                raise
            sleep(pause * attempt)


# ---------------------------------------------------------------- инициализация

def _migrate(conn) -> None:
    """Добавляет колонки, которых не было в ранней версии схемы, и пересобирает users по договору 2."""
    # Колесо фортуны убрано: старую таблицу удаляем (если она осталась в базе).
    conn.execute("DROP TABLE IF EXISTS roulette_spins")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "address" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN address TEXT NOT NULL DEFAULT ''")
    _migrate_users(conn)
    # Профиль покупателя (описание и дата последней смены ника). Идемпотентно.
    _add_missing_columns(conn, "users", {
        "bio": "TEXT NOT NULL DEFAULT ''",
        "nick_changed_at": "TEXT",
        "xp": "INTEGER NOT NULL DEFAULT 0",
    })
    _reset_bad_nicks(conn)
    _migrate_public_ids(conn)
    seed_achievements(conn)
    _backfill_achievements(conn, utc_now())


def _reset_bad_nicks(conn) -> None:
    """Ники не из английских букв и цифр (русские, с символами, с _) заменяем случайными вида user123456.
    Идемпотентно: после замены все ники подходят и функция ничего не меняет. В лог идёт id и старый/новый ник."""
    rows = conn.execute("SELECT id, nick FROM users").fetchall()
    bad = [row for row in rows if not NICK_RE.fullmatch(row["nick"])]
    if not bad:
        return
    taken = {row["nick"].lower() for row in rows}
    reserved = {r["nick_lower"] for r in conn.execute("SELECT nick_lower FROM nick_reservations")}
    with transaction(conn):
        for row in bad:
            while True:
                fresh = "user" + "".join(secrets.choice("0123456789") for _ in range(8))
                if fresh not in taken and fresh not in reserved:
                    break
            taken.add(fresh)
            conn.execute("UPDATE users SET nick = ?, nick_lower = ?, nick_changed_at = NULL WHERE id = ?",
                         (fresh, fresh, row["id"]))
            print(f"[nick-reset] user id={row['id']}: {row['nick']!r} -> {fresh}", flush=True)


def _add_missing_columns(conn, table, definitions) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_public_ids(conn) -> None:
    """Колонка users.public_id и публичный ID всем покупателям, у кого его ещё нет. Идемпотентно.

    SQLite не умеет добавить колонку с UNIQUE, поэтому в старой базе уникальность даёт индекс.
    Пересобранная таблица (USERS_TABLE_SQL) уже содержит колонку, для неё только заполнение."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "public_id" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN public_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users (public_id)")
    with transaction(conn):
        missing = [row["id"] for row in conn.execute(
            "SELECT id FROM users WHERE public_id IS NULL ORDER BY id"
        ).fetchall()]
        for user_id in missing:
            conn.execute(
                "UPDATE users SET public_id = ? WHERE id = ?", (_new_public_id(conn), user_id)
            )


def seed_achievements(conn) -> None:
    """Каталог достижений из achievements.CATALOG. Уже существующие строки не меняем."""
    with transaction(conn):
        for item in achievements.CATALOG:
            conn.execute(
                "INSERT OR IGNORE INTO achievements (code, title, description, how_to, image)"
                " VALUES (?, ?, ?, ?, ?)",
                (item["code"], item["title"], item["description"], item["how_to"], item["image"]),
            )


def _backfill_achievements(conn, now) -> None:
    """Задним числом выдаёт достижения тем, кто уже выполнил условие до их появления. Идемпотентно.

    first_login — всем с аккаунтом (дата регистрации), profile_photo — у кого есть аватар,
    profile_bio — у кого описание непустое. first_purchase сюда не входит: его выдаёт админ."""
    stamp = to_iso(now)
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code, granted_at)"
            " SELECT id, ?, created_at FROM users",
            (achievements.FIRST_LOGIN,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code, granted_at)"
            " SELECT user_id, ?, updated_at FROM user_images WHERE kind = 'avatar'",
            (achievements.PROFILE_PHOTO,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code, granted_at)"
            " SELECT id, ?, ? FROM users WHERE bio <> ''",
            (achievements.PROFILE_BIO, stamp),
        )


def _grant(conn, user_id, code, now) -> bool:
    """Выдаёт достижение. True — выдано сейчас, False — уже было. Вызывать внутри transaction()."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO user_achievements (user_id, code, granted_at) VALUES (?, ?, ?)",
        (user_id, code, to_iso(now)),
    )
    return cur.rowcount == 1


# Та же структура, что в schema.sql. Нужна миграции, чтобы создать users_new.
USERS_TABLE_SQL = """CREATE TABLE {name} (
    id                INTEGER PRIMARY KEY,
    nick              TEXT NOT NULL,
    nick_lower        TEXT NOT NULL UNIQUE,
    email             TEXT UNIQUE,
    password_hash     TEXT NOT NULL,
    phone             TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    rules_accepted_at TEXT NOT NULL,
    bio               TEXT NOT NULL DEFAULT '',
    nick_changed_at   TEXT,
    public_id         TEXT UNIQUE
)"""


def _migrate_users(conn) -> None:
    """Старая таблица users (name, phone обязателен, email обязателен) -> новая (nick, nick_lower, ...).

    Ник = user<id>, rules_accepted_at = created_at, почта и телефон сохраняются, пароли не меняются.
    Имя из старой таблицы не переносится: в новой схеме колонки нет (заказы хранят имя отдельно).
    Сессии сохраняются: внешние ключи выключены на время пересборки, иначе DROP TABLE users
    удалил бы сессии каскадом. Идемпотентно: на новой схеме ничего не делает."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if {"nick", "nick_lower", "rules_accepted_at"} <= columns:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(USERS_TABLE_SQL.format(name="users_new"))
            conn.execute(
                "INSERT INTO users_new (id, nick, nick_lower, email, password_hash, phone, created_at,"
                " rules_accepted_at)"
                " SELECT id, 'user' || id, 'user' || id, email, password_hash, phone, created_at, created_at"
                " FROM users"
            )
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_new RENAME TO users")
            # Проверяем связи сессий с пользователями. Если что-то не так, миграция откатится.
            if conn.execute("PRAGMA foreign_key_check(sessions)").fetchall():
                raise RuntimeError("миграция users: нарушены связи сессий")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(db_path, products_path=None) -> None:
    """Создаёт таблицы, при необходимости заполняет товары из JSON и отменяет просроченные заказы."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        if products_path is not None:
            seed_products(conn, products_path)
        with transaction(conn):
            expire_stale_orders(conn)
            purge_auth_expired(conn, utc_now())
    finally:
        conn.close()


def _read_seed_file(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Не могу прочитать {path}: {exc}") from None
    if isinstance(data, dict):
        data = data.get("products")
    if not isinstance(data, list):
        raise ValueError(f"{path}: ожидается массив товаров")
    return data


def seed_products(conn: sqlite3.Connection, products_path) -> int:
    """Добавляет товары из JSON, только если таблица товаров пуста. Возвращает число добавленных."""
    path = Path(products_path)
    if not path.is_file():
        return 0
    items = _read_seed_file(path)
    with transaction(conn):
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            return 0
        base = utc_now()
        for index, raw in enumerate(items):
            where = f"{path.name}, товар #{index + 1}"
            if not isinstance(raw, dict):
                raise ValueError(f"{where}: ожидается объект")
            # Из файла берём только поля товара; лишние поля пропускаем.
            known = {key: raw[key] for key in PRODUCT_FIELDS if key in raw}
            try:
                fields = clean_product(known)
            except ValidationError as exc:
                raise ValueError(f"{where}: {exc.fields}") from None
            raw_id = raw.get("id")
            if raw_id not in (None, ""):
                if is_too_long_number(raw_id):
                    raise ValueError(f"{where}: слишком длинный id")
                product_id = str(raw_id).strip()
            else:
                product_id = new_product_id()
            # Сдвиг на микросекунду сохраняет порядок товаров из файла.
            created = to_iso(base + timedelta(microseconds=index))
            try:
                conn.execute(
                    "INSERT INTO products (id, name, category, price, description, color, stock, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (product_id, fields["name"], fields["category"], fields["price"],
                     fields.get("description", ""), fields.get("color", ""),
                     fields.get("stock", 0), created),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"{where}: повторяется id {product_id!r}") from None
        return len(items)


# ---------------------------------------------------------------- проверка полей

def as_int(value):
    """Превращает значение от пользователя в int. Слишком большие числа и не-числа дают None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer() or abs(value) >= 10 ** MAX_INT_DIGITS:
            return None
        number = int(value)
    elif isinstance(value, str):
        if not INT_TEXT_RE.match(value) or is_too_long_number(value):
            return None
        number = int(value)
    else:
        return None
    if abs(number) >= 10 ** MAX_INT_DIGITS:
        return None
    return number


def _text(value, min_len, max_len):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("должно быть строкой")
    value = value.strip()
    if len(value) < min_len:
        raise ValueError("обязательное поле" if not value else f"короче {min_len} символов")
    if len(value) > max_len:
        raise ValueError(f"не длиннее {max_len} символов")
    return value


def _int_range(value, low, high):
    number = as_int(value)
    if number is None:
        raise ValueError("должно быть целым числом")
    if not low <= number <= high:
        raise ValueError(f"должно быть от {low} до {high}")
    return number


def _color(value):
    text = _text(value, 0, 60)
    if COLOR_FORBIDDEN_RE.search(text):
        raise ValueError("недопустимые символы")
    return text


def normalize_phone(value) -> str:
    """Возвращает украинский телефон в виде +380991234567.

    Принимает +380..., 380... и 0XX... (например 067 111 22 33)."""
    if not isinstance(value, str):
        raise ValueError("укажите телефон")
    text = value.strip()
    if not text or len(text) > 30 or not PHONE_CHARS_RE.match(text):
        raise ValueError("допустимы цифры, пробелы, + и скобки")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and digits[0] == "0":
        digits = "38" + digits
    if len(digits) != 12 or not digits.startswith("380"):
        raise ValueError("укажите украинский номер, например +380 67 123 45 67")
    return "+" + digits


def _email(value):
    text = _text(value, 1, 254)
    if not EMAIL_RE.match(text):
        raise ValueError("неверный формат email")
    return text


PRODUCT_CHECKS = {
    "name": lambda v: _text(v, 1, 120),
    "category": lambda v: _text(v, 1, 60),
    "price": lambda v: _int_range(v, 0, 10_000_000),
    "stock": lambda v: _int_range(v, 0, 1_000_000),
    "description": lambda v: _text(v, 0, 2000),
    "color": _color,
}
PRODUCT_REQUIRED_ON_CREATE = ("name", "category", "price")


def clean_product(data, partial=False) -> dict:
    """Проверяет поля товара. partial=True для PATCH: возвращает только переданные поля."""
    if not isinstance(data, dict):
        raise ValidationError({"body": "Ожидается объект с полями товара"})
    errors, out = {}, {}
    for key in data:
        if key not in PRODUCT_FIELDS:
            errors[key] = "неизвестное поле"
    if not partial:
        for key in PRODUCT_REQUIRED_ON_CREATE:
            if key not in data:
                errors[key] = "обязательное поле"
    for key, check in PRODUCT_CHECKS.items():
        if key in data and key not in errors:
            try:
                out[key] = check(data[key])
            except ValueError as exc:
                errors[key] = str(exc)
    if errors:
        raise ValidationError(errors)
    if not partial:
        out.setdefault("description", "")
        out.setdefault("color", "")
        out.setdefault("stock", 0)
    return out


def clean_customer(data) -> dict:
    if not isinstance(data, dict):
        raise ValidationError({"customer": "Укажите данные клиента"})
    checks = {
        "name": lambda v: _text(v, 2, 100),
        "phone": normalize_phone,
        "email": _email,
        "address": lambda v: _text(v, 5, 300),
    }
    errors, out = {}, {}
    for key, check in checks.items():
        try:
            out[key] = check(data.get(key))
        except ValueError as exc:
            errors[f"customer.{key}"] = str(exc)
    if errors:
        raise ValidationError(errors)
    return out


def clean_items(data) -> list:
    if not isinstance(data, list) or not data:
        raise ValidationError({"items": "Добавьте хотя бы один товар"})
    if len(data) > MAX_ORDER_ITEMS:
        raise ValidationError({"items": f"не больше {MAX_ORDER_ITEMS} позиций"})
    errors, out, seen = {}, [], set()
    for index, raw in enumerate(data):
        prefix = f"items[{index}]"
        if not isinstance(raw, dict):
            errors[prefix] = "ожидается объект с product_id и qty"
            continue
        raw_pid = raw.get("product_id")
        pid = None
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (str, int)) or not str(raw_pid).strip():
            errors[f"{prefix}.product_id"] = "укажите товар"
        elif is_too_long_number(raw_pid):
            errors[f"{prefix}.product_id"] = "слишком длинный номер товара"
        else:
            pid = str(raw_pid).strip()
            if pid in seen:
                errors[f"{prefix}.product_id"] = "этот товар уже есть в заказе"
            seen.add(pid)
        qty = as_int(raw.get("qty"))
        if qty is None or not 1 <= qty <= MAX_QTY:
            errors[f"{prefix}.qty"] = f"количество — целое от 1 до {MAX_QTY}"
        if pid is not None and qty is not None and f"{prefix}.product_id" not in errors:
            out.append({"product_id": pid, "qty": qty})
    if errors:
        raise ValidationError(errors)
    return out


# ---------------------------------------------------------------- остатки

def _move_stock(conn, product_id, delta) -> bool:
    """Меняет остаток на delta. Уменьшение не даёт уйти ниже нуля. Возвращает True при успехе."""
    cur = conn.execute(
        "UPDATE products SET stock = stock + ? WHERE id = ? AND stock + ? >= 0",
        (delta, product_id, delta),
    )
    return cur.rowcount == 1


def _restore_order_stock(conn, order_id) -> None:
    for item in conn.execute(
        "SELECT product_id, qty FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall():
        _move_stock(conn, item["product_id"], item["qty"])


# ---------------------------------------------------------------- товары

def get_product(conn, product_id, admin=False):
    columns = ADMIN_PRODUCT_COLUMNS if admin else PUBLIC_PRODUCT_COLUMNS
    row = conn.execute(f"SELECT {columns} FROM products WHERE id = ?", (product_id,)).fetchone()
    return dict(row) if row is not None else None


def list_products(conn, category=None, search=None, admin=False):
    columns = ADMIN_PRODUCT_COLUMNS if admin else PUBLIC_PRODUCT_COLUMNS
    sql = f"SELECT {columns} FROM products WHERE 1 = 1"
    params = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if search:
        sql += " AND instr(lower_unicode(name), ?) > 0"
        params.append(search.casefold())
    sql += " ORDER BY created_at, id"
    return [dict(row) for row in conn.execute(sql, params)]


def create_product(conn, payload) -> dict:
    data = clean_product(payload)
    product_id = new_product_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO products (id, name, category, price, description, color, stock, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, data["name"], data["category"], data["price"],
             data["description"], data["color"], data["stock"], to_iso(utc_now())),
        )
    return get_product(conn, product_id, admin=True)


def update_product(conn, product_id, payload) -> dict:
    data = clean_product(payload, partial=True)
    with transaction(conn):
        if get_product(conn, product_id) is None:
            raise NotFound()
        if data:
            # Имена колонок берутся только из PRODUCT_FIELDS, значения идут параметрами.
            assignments = ", ".join(f"{key} = ?" for key in data)
            conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ?",
                [*data.values(), product_id],
            )
    return get_product(conn, product_id, admin=True)


# ---------------------------------------------------------------- заказы

def expire_stale_orders(conn, now=None) -> int:
    """Отменяет заказы в статусе new старше ORDER_TTL_MINUTES и возвращает товар на склад.
    Вызывать внутри transaction()."""
    cutoff = to_iso((now or utc_now()) - timedelta(minutes=ORDER_TTL_MINUTES))
    stale = [
        row["id"] for row in conn.execute(
            "SELECT id FROM orders WHERE status = 'new' AND created_at < ?", (cutoff,)
        ).fetchall()
    ]
    for order_id in stale:
        _restore_order_stock(conn, order_id)
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    return len(stale)


def create_order(conn, payload) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Ожидается объект с customer и items"})
    errors = {}
    customer = items = None
    try:
        customer = clean_customer(payload.get("customer"))
    except ValidationError as exc:
        errors.update(exc.fields)
    try:
        items = clean_items(payload.get("items"))
    except ValidationError as exc:
        errors.update(exc.fields)
    if errors:
        raise ValidationError(errors)

    # Сначала освобождаем просроченные заказы, чтобы их товар был доступен.
    with transaction(conn):
        expire_stale_orders(conn)

    with transaction(conn):
        # Проверяем остатки и берём цены из базы, а не из запроса.
        lines = []
        for index, item in enumerate(items):
            row = conn.execute(
                "SELECT price, stock FROM products WHERE id = ?", (item["product_id"],)
            ).fetchone()
            if row is None:
                errors[f"items[{index}].product_id"] = "товар не найден"
            elif row["stock"] < item["qty"]:
                errors[f"items[{index}].qty"] = (
                    "товара нет в наличии" if row["stock"] == 0
                    else f"на складе осталось {row['stock']} шт."
                )
            else:
                lines.append((item["product_id"], item["qty"], row["price"]))
        if errors:
            raise ValidationError(errors)

        now = to_iso(utc_now())
        found = conn.execute(
            "SELECT id FROM customers WHERE phone = ?", (customer["phone"],)
        ).fetchone()
        if found is not None:
            # Клиент уже есть: обновляем его данные последними значениями.
            customer_id = found["id"]
            conn.execute(
                "UPDATE customers SET name = ?, email = ?, address = ? WHERE id = ?",
                (customer["name"], customer["email"], customer["address"], customer_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO customers (name, phone, email, address, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (customer["name"], customer["phone"], customer["email"], customer["address"], now),
            )
            customer_id = cur.lastrowid

        total = sum(qty * price for _, qty, price in lines)
        cur = conn.execute(
            "INSERT INTO orders (customer_id, status, total, address, created_at)"
            " VALUES (?, 'new', ?, ?, ?)",
            (customer_id, total, customer["address"], now),
        )
        order_id = cur.lastrowid
        for product_id, qty, price in lines:
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, qty, price) VALUES (?, ?, ?, ?)",
                (order_id, product_id, qty, price),
            )
            if not _move_stock(conn, product_id, -qty):
                raise ValidationError({"items": "не хватает товара на складе, попробуйте снова"})
    return {"order_id": order_id, "total": total, "status": "new"}


def update_order_status(conn, order_id, status) -> dict:
    if status not in ORDER_STATUSES:
        raise ValidationError(
            {"status": "допустимо: " + ", ".join(ORDER_STATUSES)},
            message="Неизвестный статус",
        )
    with transaction(conn):
        order = conn.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NotFound()
        old = order["status"]
        allowed = ORDER_TRANSITIONS[old]
        if status not in allowed:
            hint = ", ".join(allowed) if allowed else "нет, статус финальный"
            raise ValidationError(
                {"status": f"из «{old}» нельзя перевести в «{status}». Можно: {hint}"},
                message="Неверный переход статуса",
            )
        if status == "cancelled":
            # Отмена возвращает товар на склад.
            _restore_order_stock(conn, order_id)
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    return {"id": order_id, "status": status}


def list_orders(conn) -> list:
    orders = [
        dict(row) for row in conn.execute(
            "SELECT o.id, o.status, o.total, o.address, o.created_at,"
            "       c.id AS customer_id, c.name AS customer_name,"
            "       c.phone AS customer_phone, c.email AS customer_email"
            "  FROM orders o JOIN customers c ON c.id = o.customer_id"
            " ORDER BY o.created_at DESC, o.id DESC"
        )
    ]
    items_by_order = {}
    for row in conn.execute(
        "SELECT oi.order_id, oi.product_id, p.name AS product_name, oi.qty, oi.price"
        "  FROM order_items oi JOIN products p ON p.id = oi.product_id"
        " ORDER BY oi.id"
    ):
        items_by_order.setdefault(row["order_id"], []).append({
            "product_id": row["product_id"],
            "name": row["product_name"],
            "qty": row["qty"],
            "price": row["price"],
        })
    for order in orders:
        order["items"] = items_by_order.get(order["id"], [])
    return orders


def list_customers(conn) -> list:
    # Отменённые заказы в количество заказов клиента не входят.
    return [
        dict(row) for row in conn.execute(
            "SELECT c.id, c.name, c.phone, c.email, c.address, c.created_at,"
            "       COUNT(o.id) AS orders_count"
            "  FROM customers c"
            "  LEFT JOIN orders o ON o.customer_id = c.id AND o.status <> 'cancelled'"
            " GROUP BY c.id, c.name, c.phone, c.email, c.address, c.created_at"
            " ORDER BY c.created_at DESC, c.id DESC"
        )
    ]


# ---------------------------------------------------------------- события и статистика

def record_event(conn, payload) -> None:
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Ожидается объект с полями type и path"})
    errors = {}
    event_type = path = product_id = None
    try:
        event_type = _text(payload.get("type"), 1, 64)
    except ValueError as exc:
        errors["type"] = str(exc)
    try:
        path = _text(payload.get("path"), 1, 300)
    except ValueError as exc:
        errors["path"] = str(exc)
    raw_pid = payload.get("product_id")
    if raw_pid is not None:
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (str, int)):
            errors["product_id"] = "должно быть строкой"
        elif is_too_long_number(raw_pid):
            errors["product_id"] = "слишком длинный номер товара"
        else:
            product_id = str(raw_pid).strip() or None
    if errors:
        raise ValidationError(errors)
    # Событие про неизвестный товар сохраняем без товара, а не отклоняем.
    if product_id is not None and get_product(conn, product_id) is None:
        product_id = None
    conn.execute(
        "INSERT INTO events (type, path, product_id, created_at) VALUES (?, ?, ?, ?)",
        (event_type, path, product_id, to_iso(utc_now())),
    )


def get_stats(conn, now=None) -> dict:
    """Статистика. Дни считаются по Киеву. Отменённые заказы в выручку и в количество не входят.
    Просмотры — только события с type = 'view'."""
    local_now = (now or utc_now()).astimezone(KYIV_TZ)
    first_day = local_now.date() - timedelta(days=STATS_DAYS - 1)
    since = to_iso(datetime.combine(first_day, time.min, tzinfo=KYIV_TZ))

    totals = conn.execute(
        "SELECT COUNT(*) AS orders_count, COALESCE(SUM(total), 0) AS revenue"
        "  FROM orders WHERE status <> 'cancelled'"
    ).fetchone()
    customers_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    views_count = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'view'").fetchone()[0]
    top_products = [
        dict(row) for row in conn.execute(
            "SELECT oi.product_id AS id, p.name AS name, SUM(oi.qty) AS qty"
            "  FROM order_items oi"
            "  JOIN orders o ON o.id = oi.order_id"
            "  JOIN products p ON p.id = oi.product_id"
            " WHERE o.status <> 'cancelled'"
            " GROUP BY oi.product_id, p.name"
            " ORDER BY qty DESC, p.name"
            " LIMIT ?",
            (TOP_PRODUCTS,),
        )
    ]
    # Группируем по московскому дню в Python: в SQL нет удобного часового пояса.
    by_day = {}
    for row in conn.execute(
        "SELECT created_at, total FROM orders WHERE status <> 'cancelled' AND created_at >= ?",
        (since,),
    ):
        day = parse_iso(row["created_at"]).astimezone(KYIV_TZ).date().isoformat()
        count, revenue = by_day.get(day, (0, 0))
        by_day[day] = (count + 1, revenue + row["total"])
    orders_by_day = []
    for offset in range(STATS_DAYS):
        day = (first_day + timedelta(days=offset)).isoformat()
        count, revenue = by_day.get(day, (0, 0))
        orders_by_day.append({"day": day, "count": count, "revenue": revenue})
    return {
        "orders_count": totals["orders_count"],
        "revenue": totals["revenue"],
        "customers_count": customers_count,
        "views_count": views_count,
        "top_products": top_products,
        "orders_by_day": orders_by_day,
    }


# ---------------------------------------------------------------- проверка данных для входа и капчи

LOGIN_FIELD_MAX = 254
CAPTCHA_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
CAPTCHA_KEY_MAX = 64


def clean_nick(value) -> str:
    """Ник как ввёл пользователь (без пробелов по краям). Ошибка — ValueError с текстом для пользователя."""
    if not isinstance(value, str):
        raise ValueError("укажите ник")
    text = value.strip()
    if not NICK_RE.fullmatch(text):
        raise ValueError("от 3 до 20 символов: только английские буквы и цифры")
    if text.lower() in NICK_RESERVED:
        raise ValueError("этот ник нельзя использовать")
    return text


def clean_registration(payload) -> dict:
    """Проверяет поля регистрации сразу: nick, password, password2, email (необязательно).
    Возвращает nick, email (в нижнем регистре или None), password. Ошибки — в fields.
    Галочку правил проверяет app.py отдельно (ответ rules_not_accepted)."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с полями регистрации"})
    errors, out = {}, {}

    try:
        out["nick"] = clean_nick(payload.get("nick"))
    except ValueError as exc:
        errors["nick"] = str(exc)

    email = payload.get("email")
    out["email"] = None
    if email not in (None, ""):
        try:
            out["email"] = _email(email).lower()
        except ValueError as exc:
            errors["email"] = str(exc)

    password = payload.get("password")
    password_problem = auth.password_error(password, out.get("nick", ""))
    if password_problem:
        errors["password"] = password_problem
    if payload.get("password2") != password:
        errors["password2"] = "Пароли не совпадают"
    out["password"] = password

    if errors:
        raise ValidationError(errors)
    return out


def clean_login(payload):
    """Проверяет форму входа: логин и пароль должны быть непустыми строками. Возвращает (логин, пароль)."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с логином и паролем"})
    errors = {}
    login = payload.get("login")
    password = payload.get("password")
    if not isinstance(login, str) or not login.strip() or len(login) > LOGIN_FIELD_MAX:
        errors["login"] = "укажите почту или ник"
    if not isinstance(password, str) or not password:
        errors["password"] = "укажите пароль"
    if errors:
        raise ValidationError(errors)
    return login, password


def clean_reset_request(payload) -> str:
    """Форма «забыли пароль»: нужен логин (почта или ник). Возвращает логин."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с логином"})
    login = payload.get("login")
    if not isinstance(login, str) or not login.strip() or len(login) > LOGIN_FIELD_MAX:
        raise ValidationError({"login": "укажите почту или ник"})
    return login


def clean_reset_confirm(payload):
    """Форма нового пароля: логин и код из письма (6 цифр). Пароль проверяется позже, после кода."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с логином и кодом"})
    errors = {}
    login = payload.get("login")
    code = payload.get("code")
    if not isinstance(login, str) or not login.strip() or len(login) > LOGIN_FIELD_MAX:
        errors["login"] = "укажите почту или ник"
    if not isinstance(code, str) or not RESET_CODE_RE.fullmatch(code.strip()):
        errors["code"] = "введите 6 цифр из письма"
    if errors:
        raise ValidationError(errors)
    return login, code.strip()


def login_identity(login: str):
    """Что введено: ('email', почта) или ('nick', ник в нижнем регистре). Не распознано — None (это не ошибка формата)."""
    text = login.strip()
    if "@" in text:
        email = text.lower()
        if len(email) <= LOGIN_FIELD_MAX and EMAIL_RE.match(email):
            return ("email", email)
        return None
    if NICK_RE.fullmatch(text):
        return ("nick", text.lower())
    return None


def clean_captcha_verify(payload):
    """Форма проверки капчи. Возвращает (challenge_id, placements), где placements — {токен: ячейка или None}."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с challenge_id и placements"})
    errors = {}
    challenge_id = payload.get("challenge_id")
    if not isinstance(challenge_id, str) or not CAPTCHA_ID_RE.fullmatch(challenge_id):
        errors["challenge_id"] = "неверный идентификатор задания"
    placements = payload.get("placements")
    clean = {}
    if not isinstance(placements, dict) or len(placements) > captcha.PIECES:
        errors["placements"] = f"ожидается объект с не больше {captcha.PIECES} кусочками"
    else:
        for token, cell in placements.items():
            if not isinstance(token, str) or not 1 <= len(token) <= CAPTCHA_KEY_MAX:
                errors["placements"] = "неверный кусочек"
                break
            if cell is None:
                clean[token] = None
            elif isinstance(cell, bool) or not isinstance(cell, int) or not 0 <= cell < captcha.PIECES:
                errors["placements"] = f"ячейка — целое от 0 до {captcha.PIECES - 1}"
                break
            else:
                clean[token] = cell
    if errors:
        raise ValidationError(errors)
    return challenge_id, clean


# ---------------------------------------------------------------- пользователи, сессии, капча (SQL)

def purge_auth_expired(conn, now) -> None:
    """Удаляет истёкшие сессии, задания и билеты. Вызывать внутри transaction() или при старте."""
    stamp = to_iso(now)
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (stamp,))
    conn.execute("DELETE FROM captcha_challenges WHERE expires_at <= ?", (stamp,))
    conn.execute("DELETE FROM captcha_tickets WHERE expires_at <= ?", (stamp,))
    conn.execute("DELETE FROM nick_reservations WHERE reserved_until <= ?", (stamp,))


def _public_user(row) -> dict:
    """То, что видит покупатель: id, ник и почта (почта может быть None)."""
    return {"id": row["id"], "nick": row["nick"], "email": row["email"]}


def user_for_session(conn, token_hash, now):
    """Пользователь по хешу токена, если сессия не истекла. Иначе None (гость)."""
    row = conn.execute(
        "SELECT u.id, u.nick, u.email FROM sessions s"
        "  JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash, to_iso(now)),
    ).fetchone()
    return _public_user(row) if row is not None else None


def find_user_for_login(conn, identity):
    """Строка пользователя с хешем пароля или None. identity — из login_identity()."""
    kind, value = identity
    if kind == "email":
        row = conn.execute(
            "SELECT id, nick, email, password_hash FROM users WHERE email = ?", (value,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, nick, email, password_hash FROM users WHERE nick_lower = ?", (value,)
        ).fetchone()
    if row is None:
        return None
    return {**_public_user(row), "password_hash": row["password_hash"]}


def find_user_by_login(conn, login):
    """То же, что find_user_for_login, но по тексту из формы. Не распознано — None."""
    identity = login_identity(login)
    return find_user_for_login(conn, identity) if identity else None


def create_session(conn, user_id, token_hash, now) -> None:
    created = to_iso(now)
    expires = to_iso(now + timedelta(seconds=auth.SESSION_TTL_SECONDS))
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (created,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, created, expires),
        )


def delete_session(conn, token_hash) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def register_user(conn, data, ticket, password_hash, session_hash, now) -> dict:
    """Создаёт пользователя, гасит билет капчи и открывает сессию — одной транзакцией.
    Порядок проверок: билет (403), занятость ника (409 nick_taken) или почты (409 email_taken)."""
    stamp = to_iso(now)
    with transaction(conn):
        ticket_hash = auth.token_hash(ticket) if isinstance(ticket, str) and ticket else None
        row = None
        if ticket_hash is not None:
            row = conn.execute(
                "SELECT used_at, expires_at FROM captcha_tickets WHERE token_hash = ?", (ticket_hash,)
            ).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] <= stamp:
            raise Rejected("captcha_required")

        nick_lower = data["nick"].lower()
        taken = {}
        for found in conn.execute(
            "SELECT nick_lower, email FROM users WHERE nick_lower = ? OR email = ?",
            (nick_lower, data["email"]),
        ):
            if found["nick_lower"] == nick_lower:
                taken["nick"] = "Этот ник уже занят"
            if data["email"] is not None and found["email"] == data["email"]:
                taken["email"] = "Эта почта уже зарегистрирована"
        if conn.execute(
            "SELECT 1 FROM nick_reservations WHERE nick_lower = ? AND reserved_until > ?",
            (nick_lower, stamp),
        ).fetchone() is not None:
            taken["nick"] = "Этот ник уже занят"
        if taken:
            code = "nick_taken" if "nick" in taken else "email_taken"
            raise Rejected(code, fields=taken)

        cur = conn.execute(
            "INSERT INTO users (nick, nick_lower, email, password_hash, created_at, rules_accepted_at, public_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["nick"], nick_lower, data["email"], password_hash, stamp, stamp,
             _new_public_id(conn)),
        )
        user_id = cur.lastrowid
        conn.execute("UPDATE captcha_tickets SET used_at = ? WHERE token_hash = ?", (stamp, ticket_hash))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_hash, user_id, stamp,
             to_iso(now + timedelta(seconds=auth.SESSION_TTL_SECONDS))),
        )
        _grant(conn, user_id, achievements.FIRST_LOGIN, now)
    return {"id": user_id, "nick": data["nick"], "email": data["email"]}


# ---------------------------------------------------------------- восстановление пароля

def reset_begin(conn, user_id, code_hash, now):
    """Записывает новый код, если прошлый выслан не меньше минуты назад. Возвращает время выдачи или None.
    None — код уже выслан недавно, письмо отправлять не нужно."""
    stamp = to_iso(now)
    with transaction(conn):
        row = conn.execute("SELECT created_at FROM reset_codes WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None and row["created_at"] > to_iso(now - timedelta(seconds=RESET_RESEND_SECONDS)):
            return None
        conn.execute(
            "INSERT OR REPLACE INTO reset_codes (user_id, code_hash, created_at, expires_at, attempts, used_at)"
            " VALUES (?, ?, ?, ?, 0, NULL)",
            (user_id, code_hash, stamp, to_iso(now + timedelta(seconds=RESET_CODE_TTL_SECONDS))),
        )
    return stamp


def reset_cancel(conn, user_id, stamp) -> None:
    """Письмо не ушло: убираем код, чтобы пользователь мог повторить запрос сразу."""
    with transaction(conn):
        conn.execute("DELETE FROM reset_codes WHERE user_id = ? AND created_at = ?", (user_id, stamp))


def reset_confirm(conn, login, code, password, password2, session_hash, now) -> dict:
    """Проверяет код и меняет пароль. Меняет пароль, гасит все сессии пользователя и открывает новую.

    Порядок: код (code_invalid / code_expired / code_attempts_exceeded), потом пароль (ValidationError).
    Неверный код считается попыткой. На шестой попытке код сгорает.
    Ответы-отказы выходят из транзакции, а не изнутри неё: иначе откат отменил бы счётчик попыток."""
    stamp = to_iso(now)
    outcome = "code_invalid"
    user = None
    with transaction(conn):
        user = find_user_by_login(conn, login)
        reset = None
        if user is not None:
            reset = conn.execute(
                "SELECT code_hash, expires_at, attempts, used_at FROM reset_codes WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if reset is None or reset["used_at"] is not None:
            auth.verify_password(code, auth.dummy_hash())     # то же время, что и при проверке настоящего кода
        elif reset["expires_at"] <= stamp:
            outcome = "code_expired"
        elif reset["attempts"] >= RESET_MAX_ATTEMPTS:
            conn.execute("UPDATE reset_codes SET used_at = ? WHERE user_id = ?", (stamp, user["id"]))
            outcome = "code_attempts_exceeded"
        elif not auth.verify_password(code, reset["code_hash"]):
            conn.execute("UPDATE reset_codes SET attempts = attempts + 1 WHERE user_id = ?", (user["id"],))
        else:
            errors = {}
            problem = auth.password_error(password, user["nick"])
            if problem:
                errors["password"] = problem
            if password != password2:
                errors["password2"] = "Пароли не совпадают"
            if errors:
                raise ValidationError(errors)       # откат: код остаётся действительным
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (auth.hash_password(password), user["id"]))
            conn.execute("UPDATE reset_codes SET used_at = ? WHERE user_id = ?", (stamp, user["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (session_hash, user["id"], stamp, to_iso(now + timedelta(seconds=auth.SESSION_TTL_SECONDS))),
            )
            outcome = "ok"
    if outcome != "ok":
        raise Rejected(outcome)
    return _public_user(user)


def captcha_create(conn, challenge_id, expected, now) -> None:
    with transaction(conn):
        purge_auth_expired(conn, now)
        conn.execute(
            "INSERT INTO captcha_challenges (id, expected, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (challenge_id, json.dumps(expected, ensure_ascii=False), to_iso(now),
             to_iso(now + timedelta(seconds=captcha.CHALLENGE_TTL))),
        )


def captcha_verify(conn, challenge_id, placements, ticket_hash, now) -> str:
    """Сверяет расстановку. Возвращает 'incomplete', 'wrong' или 'ok'.
    Задание гаснет на 'wrong' и на 'ok'; на 'incomplete' не гаснет. Билет создаётся только на 'ok'.
    Истёкшее, использованное или несуществующее задание — Rejected('captcha_expired')."""
    stamp = to_iso(now)
    with transaction(conn):
        row = conn.execute(
            "SELECT expected, expires_at, used_at FROM captcha_challenges WHERE id = ?", (challenge_id,)
        ).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] <= stamp:
            raise Rejected("captcha_expired")
        expected = json.loads(row["expected"])
        if set(placements) - set(expected):
            raise ValidationError({"placements": "неизвестный кусочек"})
        outcome = captcha.evaluate(expected, placements)
        if outcome == "incomplete":
            return outcome
        conn.execute(
            "UPDATE captcha_challenges SET used_at = ? WHERE id = ? AND used_at IS NULL", (stamp, challenge_id)
        )
        if outcome == "ok":
            conn.execute(
                "INSERT INTO captcha_tickets (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (ticket_hash, stamp, to_iso(now + timedelta(seconds=captcha.TICKET_TTL))),
            )
    return outcome


# ---------------------------------------------------------------- профиль покупателя

BIO_MAX_LEN = 300
BIO_MAX_NEWLINES = 5
BIO_FORBIDDEN_RE = re.compile(r"[<>]")
# Управляющие символы, кроме перевода строки (переводы уже приведены к \n).
BIO_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
# Невидимые символы, которыми можно спрятать текст или подделать ник.
BIO_INVISIBLE_RE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
NICK_COOLDOWN_DAYS = 30        # менять ник можно раз в 30 дней
NICK_RESERVE_DAYS = 30         # старый ник держится 30 дней


def clean_bio(value) -> str:
    """Описание профиля: NFC, без пробелов по краям, до 300 символов, без < > и без управляющих
    и невидимых символов, не больше 5 переносов строки. Пустая строка — описание очищено."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("описание — текст")
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > BIO_MAX_LEN:
        raise ValueError(f"не больше {BIO_MAX_LEN} символов")
    if BIO_FORBIDDEN_RE.search(text):
        raise ValueError("символы < и > нельзя")
    if BIO_CONTROL_RE.search(text) or BIO_INVISIBLE_RE.search(text):
        raise ValueError("есть недопустимые символы")
    if text.count("\n") > BIO_MAX_NEWLINES:
        raise ValueError(f"не больше {BIO_MAX_NEWLINES} переносов строки")
    return text


def clean_profile(payload) -> dict:
    """Поля профиля для PATCH: bio и/или nick. Лишние поля — ошибка. Ошибки — в fields."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Ожидается объект с полями bio или nick"})
    errors, out = {}, {}
    for key in payload:
        if key not in ("bio", "nick"):
            errors[key] = "неизвестное поле"
    if "bio" in payload:
        try:
            out["bio"] = clean_bio(payload["bio"])
        except ValueError as exc:
            errors["bio"] = str(exc)
    if "nick" in payload:
        try:
            out["nick"] = clean_nick(payload["nick"])
        except ValueError as exc:
            errors["nick"] = str(exc)
    if errors:
        raise ValidationError(errors)
    return out


def _kyiv_date(moment: datetime) -> str:
    return moment.astimezone(KYIV_TZ).strftime("%d.%m.%Y")


def _next_nick_change(changed_at, now):
    """Когда можно сменить ник в следующий раз (ISO) или None, если уже можно."""
    if not changed_at:
        return None
    available = parse_iso(changed_at) + timedelta(days=NICK_COOLDOWN_DAYS)
    return to_iso(available) if available > now else None


def _picture_fields(row) -> dict:
    avatar, banner = row["avatar_sha"], row["banner_sha"]
    return {
        "has_avatar": avatar is not None,
        "has_banner": banner is not None,
        # Версия для ссылки на картинку (?v=...): меняется вместе с картинкой.
        "avatar_v": avatar[:16] if avatar else None,
        "banner_v": banner[:16] if banner else None,
    }


PROFILE_SELECT = (
    "SELECT u.id, u.nick, u.email, u.bio, u.nick_changed_at, u.public_id, u.created_at, u.xp,"
    " (SELECT sha256 FROM user_images WHERE user_id = u.id AND kind = 'avatar') AS avatar_sha,"
    " (SELECT sha256 FROM user_images WHERE user_id = u.id AND kind = 'banner') AS banner_sha"
    " FROM users u WHERE "
)


KYIV = ZoneInfo("Europe/Kyiv")


def award_xp(conn, user_id, kind, now) -> int:
    """Начисляет XP за действие kind (ranks.XP_RULES) с суточным потолком по Киеву.
    Возвращает, сколько начислено (0, если потолок уже выбран или вида нет). Сам открывает транзакцию."""
    rule = ranks.XP_RULES.get(kind)
    if rule is None:
        return 0
    day = now.astimezone(KYIV).date().isoformat()
    used = conn.execute(
        "SELECT amount FROM xp_daily WHERE user_id = ? AND day = ? AND kind = ?", (user_id, day, kind)
    ).fetchone()
    left = rule["cap"] - (used["amount"] if used else 0)
    gain = min(rule["amount"], left)
    if gain <= 0:
        return 0
    with transaction(conn):
        conn.execute(
            "INSERT INTO xp_daily (user_id, day, kind, amount) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id, day, kind) DO UPDATE SET amount = amount + excluded.amount",
            (user_id, day, kind, gain),
        )
        conn.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (gain, user_id))
    return gain


def _achievement_list(conn, user_id) -> list:
    """Все достижения по порядку каталога. earned — выдано ли покупателю, granted_at — когда (или None)."""
    rows = conn.execute(
        "SELECT a.code, a.title, a.description, a.how_to, a.image, ua.granted_at"
        " FROM achievements a"
        "  LEFT JOIN user_achievements ua ON ua.code = a.code AND ua.user_id = ?"
        " ORDER BY a.rowid",
        (user_id,),
    ).fetchall()
    return [
        {
            "code": row["code"],
            "title": row["title"],
            "description": row["description"],
            "how_to": row["how_to"],
            "image_url": achievements.IMAGE_URL_PREFIX + row["image"],
            "earned": row["granted_at"] is not None,
            "granted_at": row["granted_at"],
        }
        for row in rows
    ]


def public_profile(conn, nick_lower):
    """То, что видно по нику: ник, описание и есть ли картинки. Почты и id нет."""
    row = conn.execute(PROFILE_SELECT + "u.nick_lower = ?", (nick_lower,)).fetchone()
    if row is None:
        return None
    return {"nick": row["nick"], "bio": row["bio"], **_picture_fields(row)}


def own_profile(conn, user_id, now):
    """Своя страница: всё про профиль, включая почту и дату следующей смены ника."""
    row = conn.execute(PROFILE_SELECT + "u.id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "nick": row["nick"],
        "email": row["email"],
        "bio": row["bio"],
        **_picture_fields(row),
        "nick_next_change_at": _next_nick_change(row["nick_changed_at"], now),
        "public_id": row["public_id"],
        "created_at": row["created_at"],
        "achievements": _achievement_list(conn, user_id),
        "xp": row["xp"],
        "rank": ranks.rank_for(row["xp"]),
    }


def _plan_nick_change(conn, user_id, me, nick, now) -> dict:
    """Проверяет смену ника и резервирует старый. Возвращает колонки users для UPDATE.

    Смена только регистра (anna -> Anna) кулдауна не имеет и срок смены не трогает.
    Вызывать внутри transaction()."""
    if nick.lower() == me["nick_lower"]:
        return {"nick": nick} if nick != me["nick"] else {}
    if _next_nick_change(me["nick_changed_at"], now) is not None:
        available = parse_iso(me["nick_changed_at"]) + timedelta(days=NICK_COOLDOWN_DAYS)
        raise Rejected("nick_cooldown", fields={
            "nick": f"Ник можно поменять не раньше {_kyiv_date(available)}",
        })
    nick_lower = nick.lower()
    stamp = to_iso(now)
    taken = conn.execute("SELECT 1 FROM users WHERE nick_lower = ?", (nick_lower,)).fetchone()
    reserved = conn.execute(
        "SELECT 1 FROM nick_reservations WHERE nick_lower = ? AND user_id <> ? AND reserved_until > ?",
        (nick_lower, user_id, stamp),
    ).fetchone()
    if taken is not None or reserved is not None:
        raise Rejected("nick_taken", fields={"nick": "Этот ник уже занят"})
    # Свою бронь на этот ник (или протухшую чужую) убираем, старый ник резервируем.
    conn.execute("DELETE FROM nick_reservations WHERE nick_lower = ?", (nick_lower,))
    conn.execute(
        "INSERT OR REPLACE INTO nick_reservations (nick_lower, user_id, reserved_until) VALUES (?, ?, ?)",
        (me["nick_lower"], user_id, to_iso(now + timedelta(days=NICK_RESERVE_DAYS))),
    )
    return {"nick": nick, "nick_lower": nick_lower, "nick_changed_at": stamp}


def update_profile(conn, user_id, payload, now) -> dict:
    """PATCH /api/profile: меняет описание и/или ник одной транзакцией. Ошибка любого поля ничего не меняет.

    Ошибки формы: nick_invalid, если есть ошибка ника, иначе validation_error (оба — 400)."""
    try:
        data = clean_profile(payload)
    except ValidationError as exc:
        code = "nick_invalid" if "nick" in exc.fields else "validation_error"
        raise Rejected(code, fields=exc.fields) from None
    with transaction(conn):
        me = conn.execute(
            "SELECT nick, nick_lower, nick_changed_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if me is None:
            raise NotFound()
        changes = {}
        if "bio" in data:
            changes["bio"] = data["bio"]
        if "nick" in data:
            changes.update(_plan_nick_change(conn, user_id, me, data["nick"], now))
        if changes:
            # Имена колонок заданы только в этом коде, значения идут параметрами.
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", [*changes.values(), user_id])
        # Описание непустое после сохранения — достижение «О себе» (повтор не дублирует).
        saved_bio = conn.execute("SELECT bio FROM users WHERE id = ?", (user_id,)).fetchone()["bio"]
        if saved_bio:
            _grant(conn, user_id, achievements.PROFILE_BIO, now)
    return own_profile(conn, user_id, now)


def get_image(conn, nick_lower, kind):
    """Картинка покупателя по нику (в нижнем регистре) и виду. None, если её нет."""
    row = conn.execute(
        "SELECT i.content_type, i.data, i.sha256 FROM user_images i"
        "  JOIN users u ON u.id = i.user_id"
        " WHERE u.nick_lower = ? AND i.kind = ?",
        (nick_lower, kind),
    ).fetchone()
    return dict(row) if row is not None else None


def save_image(conn, user_id, kind, data, meta, now) -> dict:
    """Записывает картинку вместо прежней того же вида. meta — из images.inspect(). Возвращает профиль."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO user_images (user_id, kind, content_type, data, width, height, sha256, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, kind) DO UPDATE SET"
            "   content_type = excluded.content_type, data = excluded.data,"
            "   width = excluded.width, height = excluded.height,"
            "   sha256 = excluded.sha256, updated_at = excluded.updated_at",
            (user_id, kind, meta["content_type"], data, meta["width"], meta["height"],
             meta["sha256"], to_iso(now)),
        )
        if kind == "avatar":
            _grant(conn, user_id, achievements.PROFILE_PHOTO, now)
    return own_profile(conn, user_id, now)


def delete_image(conn, user_id, kind, now) -> dict:
    """Убирает картинку. Если её не было, ничего не меняется. Возвращает профиль."""
    with transaction(conn):
        conn.execute("DELETE FROM user_images WHERE user_id = ? AND kind = ?", (user_id, kind))
    return own_profile(conn, user_id, now)


# ---------------------------------------------------------------- достижения: выдача админом

def clean_achievement_grant(payload):
    """Тело POST /api/admin/achievements/grant: login (почта или ник) и code. Возвращает (login, code)."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Ожидается объект с полями login и code"})
    errors = {}
    login = payload.get("login")
    if not isinstance(login, str) or login_identity(login) is None:
        errors["login"] = "укажите почту или ник покупателя"
    code = payload.get("code")
    if not isinstance(code, str) or code not in achievements.CODES:
        errors["code"] = "неизвестное достижение"
    if errors:
        raise ValidationError(errors)
    return login.strip(), code


def grant_achievement_by_login(conn, payload, now) -> dict:
    """Выдаёт достижение покупателю, найденному по почте или нику. Повтор ничего не меняет.

    Неизвестный логин — Rejected('not_found') (404), неверные поля — ValidationError (400)."""
    login, code = clean_achievement_grant(payload)
    kind, value = login_identity(login)
    column = "email" if kind == "email" else "nick_lower"  # имя колонки из двух констант, значение — параметр
    with transaction(conn):
        user = conn.execute(
            f"SELECT id, nick, public_id FROM users WHERE {column} = ?", (value,)
        ).fetchone()
        if user is None:
            raise Rejected("not_found")
        granted = _grant(conn, user["id"], code, now)
    return {"granted": granted, "user": {"nick": user["nick"], "public_id": user["public_id"]}}
