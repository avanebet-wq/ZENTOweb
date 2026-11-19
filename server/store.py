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

SERVER_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SERVER_DIR / "schema.sql"

ORDER_STATUSES = ("new", "paid", "shipped", "done", "cancelled")
MAX_QTY = 99
MAX_ORDER_ITEMS = 50
STATS_DAYS = 14
TOP_PRODUCTS = 5
MAX_SEARCH_LEN = 100

PRODUCT_FIELDS = ("name", "category", "price", "description", "color", "stock")
PUBLIC_PRODUCT_COLUMNS = "id, name, category, price, description, color, stock"
ADMIN_PRODUCT_COLUMNS = PUBLIC_PRODUCT_COLUMNS + ", created_at"

PHONE_CHARS_RE = re.compile(r"^\+?[0-9 ()\-.]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COLOR_FORBIDDEN_RE = re.compile(r"[<>\"'\\;{}]")
INT_TEXT_RE = re.compile(r"^\s*-?\d+\s*$")


class ValidationError(Exception):
    """Ошибка проверки: fields = {поле: текст}."""

    def __init__(self, fields, message="Проверьте заполненные поля"):
        super().__init__(message)
        self.fields = fields
        self.message = message


class NotFound(Exception):
    """Запись не найдена."""


# ---------------------------------------------------------------- время и id

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_product_id() -> str:
    return "p" + secrets.token_hex(6)


# ---------------------------------------------------------------- соединение

def _lower_unicode(value):
    # Встроенный LOWER в SQLite понимает только ASCII, поэтому для поиска
    # по кириллице регистр меняем в Python.
    return None if value is None else value.casefold()


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
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
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---------------------------------------------------------------- инициализация

def init_db(db_path, products_path=None) -> None:
    """Создаёт таблицы и, если товаров ещё нет, заполняет их из products.json."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if products_path is not None:
            seed_products(conn, products_path)
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
            # Из файла берём только поля товара; id обрабатываем отдельно, лишние поля пропускаем.
            known = {key: raw[key] for key in PRODUCT_FIELDS if key in raw}
            try:
                fields = clean_product(known)
            except ValidationError as exc:
                raise ValueError(f"{where}: {exc.fields}") from None
            raw_id = raw.get("id")
            product_id = str(raw_id).strip() if raw_id not in (None, "") else new_product_id()
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

def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and INT_TEXT_RE.match(value):
        return int(value)
    return None


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
    number = _as_int(value)
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
    """Возвращает телефон в виде +79991234567. 8 в начале заменяем на +7."""
    if not isinstance(value, str):
        raise ValueError("укажите телефон")
    text = value.strip()
    if not text or len(text) > 30 or not PHONE_CHARS_RE.match(text):
        raise ValueError("допустимы цифры, пробелы, + и скобки")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError("нужно от 10 до 15 цифр")
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
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (str, int)) or not str(raw_pid).strip():
            errors[f"{prefix}.product_id"] = "укажите товар"
            pid = None
        else:
            pid = str(raw_pid).strip()
            if pid in seen:
                errors[f"{prefix}.product_id"] = "этот товар уже есть в заказе"
            seen.add(pid)
        qty = _as_int(raw.get("qty"))
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
            customer_id = found["id"]
        else:
            cur = conn.execute(
                "INSERT INTO customers (name, phone, email, address, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (customer["name"], customer["phone"], customer["email"], customer["address"], now),
            )
            customer_id = cur.lastrowid

        total = sum(qty * price for _, qty, price in lines)
        cur = conn.execute(
            "INSERT INTO orders (customer_id, status, total, created_at) VALUES (?, 'new', ?, ?)",
            (customer_id, total, now),
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
        raise ValidationError({"status": "допустимо: " + ", ".join(ORDER_STATUSES)})
    with transaction(conn):
        order = conn.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NotFound()
        old = order["status"]
        if old != status:
            items = conn.execute(
                "SELECT product_id, qty FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            if status == "cancelled":
                # Отмена возвращает товар на склад.
                for item in items:
                    _move_stock(conn, item["product_id"], item["qty"])
            elif old == "cancelled":
                # Возврат из отмены снова резервирует товар.
                for item in items:
                    if not _move_stock(conn, item["product_id"], -item["qty"]):
                        raise ValidationError({"status": "товара уже не хватает на складе"})
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    return {"id": order_id, "status": status}


def list_orders(conn) -> list:
    orders = [
        dict(row) for row in conn.execute(
            "SELECT o.id, o.status, o.total, o.created_at,"
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
    return [
        dict(row) for row in conn.execute(
            "SELECT c.id, c.name, c.phone, c.email, c.address, c.created_at,"
            "       COUNT(o.id) AS orders_count"
            "  FROM customers c LEFT JOIN orders o ON o.customer_id = c.id"
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
    """Статистика. Отменённые заказы в выручку и в количество не входят."""
    now = now or utc_now()
    first_day = now.astimezone(timezone.utc).date() - timedelta(days=STATS_DAYS - 1)
    since = to_iso(datetime.combine(first_day, time.min, tzinfo=timezone.utc))

    totals = conn.execute(
        "SELECT COUNT(*) AS orders_count, COALESCE(SUM(total), 0) AS revenue"
        "  FROM orders WHERE status <> 'cancelled'"
    ).fetchone()
    customers_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    views_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
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
    by_day = {
        row["day"]: row for row in conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count,"
            "       COALESCE(SUM(total), 0) AS revenue"
            "  FROM orders"
            " WHERE status <> 'cancelled' AND created_at >= ?"
            " GROUP BY substr(created_at, 1, 10)",
            (since,),
        )
    }
    orders_by_day = []
    for offset in range(STATS_DAYS):
        day = (first_day + timedelta(days=offset)).isoformat()
        row = by_day.get(day)
        orders_by_day.append({
            "day": day,
            "count": row["count"] if row else 0,
            "revenue": row["revenue"] if row else 0,
        })
    return {
        "orders_count": totals["orders_count"],
        "revenue": totals["revenue"],
        "customers_count": customers_count,
        "views_count": views_count,
        "top_products": top_products,
        "orders_by_day": orders_by_day,
    }
