"""Работа с базой SQLite. Весь SQL и все правила проверки данных живут здесь.

Только стандартная библиотека. Ошибки проверки поднимаются как ValidationError,
их превращает в ответ 400 слой HTTP (app.py).
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from sqlite3 import OperationalError
from time import sleep
from zoneinfo import ZoneInfo

import auth
import captcha

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
    """Добавляет колонки, которых не было в ранней версии схемы."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "address" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN address TEXT NOT NULL DEFAULT ''")


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
NAME_FORBIDDEN_RE = re.compile(r"[0-9<>]")
CAPTCHA_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
CAPTCHA_KEY_MAX = 64


def clean_registration(payload) -> dict:
    """Проверяет все поля регистрации сразу. Возвращает name, phone, email, password. Ошибки — в fields."""
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Нужен JSON-объект с полями регистрации"})
    errors, out = {}, {}

    try:
        name = _text(payload.get("name"), 2, 50)
        if NAME_FORBIDDEN_RE.search(name):
            raise ValueError("не должно быть цифр и символов < >")
        out["name"] = name
    except ValueError as exc:
        errors["name"] = str(exc)

    try:
        out["phone"] = normalize_phone(payload.get("phone"))
    except ValueError as exc:
        errors["phone"] = str(exc)

    try:
        out["email"] = _email(payload.get("email")).lower()
    except ValueError as exc:
        errors["email"] = str(exc)

    password = payload.get("password")
    password_problem = auth.password_error(password, out.get("email", ""))
    if password_problem:
        errors["password"] = password_problem
    else:
        out["password"] = password

    if payload.get("consent") is not True:
        errors["consent"] = "Нужно согласие на обработку персональных данных"

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
        errors["login"] = "укажите почту или телефон"
    if not isinstance(password, str) or not password:
        errors["password"] = "укажите пароль"
    if errors:
        raise ValidationError(errors)
    return login, password


def login_identity(login: str):
    """Что введено: ('email', почта) или ('phone', +380...). Не распознано — None (это не ошибка формата)."""
    text = login.strip()
    if "@" in text:
        email = text.lower()
        if len(email) <= LOGIN_FIELD_MAX and EMAIL_RE.match(email):
            return ("email", email)
        return None
    try:
        return ("phone", normalize_phone(text))
    except ValueError:
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


def _public_user(row) -> dict:
    return {"id": row["id"], "name": row["name"], "phone": row["phone"], "email": row["email"]}


def user_for_session(conn, token_hash, now):
    """Пользователь по хешу токена, если сессия не истекла. Иначе None (гость)."""
    row = conn.execute(
        "SELECT u.id, u.name, u.phone, u.email FROM sessions s"
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
            "SELECT id, name, phone, email, password_hash FROM users WHERE email = ?", (value,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name, phone, email, password_hash FROM users WHERE phone = ?", (value,)
        ).fetchone()
    if row is None:
        return None
    return {**_public_user(row), "password_hash": row["password_hash"]}


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
    Порядок проверок: билет (403), занятость телефона или почты (409)."""
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

        taken = {}
        for found in conn.execute(
            "SELECT phone, email FROM users WHERE phone = ? OR email = ?",
            (data["phone"], data["email"]),
        ):
            if found["phone"] == data["phone"]:
                taken["phone"] = "Этот телефон уже зарегистрирован"
            if found["email"] == data["email"]:
                taken["email"] = "Эта почта уже зарегистрирована"
        if taken:
            raise Rejected("already_registered", fields=taken)

        cur = conn.execute(
            "INSERT INTO users (name, phone, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (data["name"], data["phone"], data["email"], password_hash, stamp),
        )
        user_id = cur.lastrowid
        conn.execute("UPDATE captcha_tickets SET used_at = ? WHERE token_hash = ?", (stamp, ticket_hash))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_hash, user_id, stamp,
             to_iso(now + timedelta(seconds=auth.SESSION_TTL_SECONDS))),
        )
    return {"id": user_id, "name": data["name"], "phone": data["phone"], "email": data["email"]}


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
