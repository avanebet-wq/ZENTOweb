"""Тесты API ZENTO.

Каждый тест поднимает сервер на временной базе и временной папке со статикой.
Файл data/store.db и реальные файлы сайта не трогаются. Даты в тестах фиксированные,
поэтому они не зависят от текущего времени и от полуночи.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

import app  # noqa: E402
import store  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
TOKEN = "test-admin-token-0123456789abcdefghij"   # 40 символов, не короче 32
SHOP_BOT = "test-shop-bot-token-0123456789abcdef"  # токен бота по умолчанию в тестах, не короче 32
BIG_LIMIT = 1000  # в обычных тестах лимиты не мешают; сами лимиты проверяем отдельно

PRODUCTS = [
    {"id": "p1", "name": "Тестовый Плеер", "category": "Музыка", "price": 0,
     "description": "Бесплатный плеер", "color": "#ff8800", "stock": 5},
    {"id": "p2", "name": "Редактор текста", "category": "Утилиты", "price": 299,
     "description": "Текстовый редактор", "color": "#00aa88", "stock": 2},
    {"id": "p3", "name": "Курс языка", "category": "Образование", "price": 990,
     "description": "Курс для начинающих", "color": "#3366ff", "stock": 0},
]

Resp = namedtuple("Resp", "status body headers raw")

CUSTOMER = {
    "name": "Анна Петрова",
    "phone": "+380 (99) 111-22-33",
    "email": "anna@example.com",
    "address": "Киев, ул. Тестовая, 1",
}


def minutes_ago(minutes):
    return store.to_iso(store.utc_now() - timedelta(minutes=minutes))


class ServerTestCase(unittest.TestCase):
    """Базовый класс: временные файлы и запущенный сервер.

    Товары и заказы закрыты для гостей (401 login_required). Поэтому по умолчанию сервер поднимается
    с токеном бота, а request() сам добавляет его к запросам товаров и заказов. Чтобы проверить гостя
    или вход, передайте anonymous=True или свой заголовок Authorization/Cookie."""

    shop_bot_token = SHOP_BOT

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.products_path = self.tmp / "products.json"
        self.products_path.write_text(json.dumps(PRODUCTS, ensure_ascii=False), encoding="utf-8")

        site = self.tmp / "site"
        (site / "server").mkdir(parents=True)
        (site / "data").mkdir()
        (site / "index.html").write_text("<!doctype html><title>ZENTO</title>", encoding="utf-8")
        (site / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
        (site / "app.js").write_text("console.log(1);", encoding="utf-8")
        (site / "notes.txt").write_text("не для сети", encoding="utf-8")
        (site / "server" / "app.py").write_text("print('secret')", encoding="utf-8")
        (site / "data" / "store.db").write_bytes(b"SQLite format 3\x00")
        (site / ".env").write_text("SECRET=1", encoding="utf-8")
        (site / "CLAUDE.md").write_text("notes", encoding="utf-8")
        self.site = site

        self.start_server(admin_token=TOKEN, db_name="test.db",
                          order_limit=BIG_LIMIT, event_limit=BIG_LIMIT)

    def start_server(self, admin_token, db_name, **limits):
        limits.setdefault("bot_token", self.shop_bot_token)
        db_path = self.tmp / db_name
        server = app.create_server(
            "127.0.0.1", 0,
            db_path=db_path,
            products_path=self.products_path,
            admin_token=admin_token,
            static_root=self.site,
            quiet=True,
            **limits,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.server = server
        self.port = server.server_address[1]
        self.db_path = db_path

        def stop():
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        return server

    def request(self, method, path, body=None, raw=None, headers=None, anonymous=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        send_headers = dict(headers or {})
        bot = getattr(self.server, "bot_token", None)
        needs_login = path.split("?", 1)[0].startswith(("/api/products", "/api/orders"))
        has_auth = any(name.lower() in ("authorization", "cookie") for name in send_headers)
        if bot and needs_login and not anonymous and not has_auth:
            send_headers["Authorization"] = f"Bearer {bot}"
        data = raw
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        conn.request(method, path, body=data, headers=send_headers)
        resp = conn.getresponse()
        payload = resp.read()
        content_type = resp.getheader("Content-Type", "")
        parsed = json.loads(payload) if payload and "json" in content_type else None
        return Resp(resp.status, parsed, resp.headers, payload)

    def admin(self, method, path, body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.request(method, path, body=body, headers=headers)

    def create_order(self, items, customer=None):
        payload = {"customer": dict(customer or CUSTOMER), "items": items}
        return self.request("POST", "/api/orders", body=payload)

    def set_status(self, order_id, status):
        return self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": status})

    def stock_of(self, product_id):
        return self.request("GET", f"/api/products/{product_id}").body["stock"]

    def db(self):
        return sqlite3.connect(self.db_path)

    def db_count(self, table):
        conn = self.db()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def db_stock(self, product_id):
        conn = self.db()
        try:
            return conn.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()[0]
        finally:
            conn.close()

    def db_set(self, sql, params=()):
        conn = self.db()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def set_order_time(self, order_id, iso):
        self.db_set("UPDATE orders SET created_at = ? WHERE id = ?", (iso, order_id))

    def order_status(self, order_id):
        for order in self.admin("GET", "/api/admin/orders").body:
            if order["id"] == order_id:
                return order["status"]
        return None


# ---------------------------------------------------------------- товары

class ProductsTest(ServerTestCase):
    def test_list_returns_public_fields(self):
        resp = self.request("GET", "/api/products")
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body), 3)
        self.assertEqual(
            set(resp.body[0]),
            {"id", "name", "category", "price", "description", "color", "stock"},
        )
        self.assertEqual([p["id"] for p in resp.body], ["p1", "p2", "p3"])

    def test_filter_by_category(self):
        resp = self.request("GET", "/api/products?category=" + quote("Утилиты"))
        self.assertEqual([p["id"] for p in resp.body], ["p2"])

    def test_search_is_case_insensitive_for_cyrillic(self):
        resp = self.request("GET", "/api/products?q=" + quote("ТЕСТОВЫЙ"))
        self.assertEqual([p["id"] for p in resp.body], ["p1"])
        resp = self.request("GET", "/api/products?q=" + quote("редактор"))
        self.assertEqual([p["id"] for p in resp.body], ["p2"])

    def test_empty_search_returns_all(self):
        resp = self.request("GET", "/api/products?q=%20%20")
        self.assertEqual(len(resp.body), 3)

    def test_get_one_product(self):
        resp = self.request("GET", "/api/products/p2")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["name"], "Редактор текста")
        self.assertEqual(resp.body["price"], 299)

    def test_get_missing_product_is_404_with_error(self):
        resp = self.request("GET", "/api/products/nope")
        self.assertEqual(resp.status, 404)
        self.assertIn("error", resp.body)

    def test_seed_does_not_duplicate_existing_products(self):
        store.init_db(self.db_path, self.products_path)
        store.init_db(self.db_path, self.products_path)
        self.assertEqual(self.db_count("products"), 3)

    def test_missing_products_file_is_fine(self):
        db = self.tmp / "no-json.db"
        store.init_db(db, self.tmp / "absent.json")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        finally:
            conn.close()

    def test_seed_rejects_bad_price_with_clear_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps([{"id": "x", "name": "A", "category": "B", "price": "много"}]),
                       encoding="utf-8")
        with self.assertRaises(ValueError):
            store.init_db(self.tmp / "bad.db", bad)

    def test_seed_ignores_extra_fields(self):
        extra = self.tmp / "extra.json"
        extra.write_text(json.dumps([{"id": 7, "name": "Игра", "category": "Развлечения",
                                      "price": 10, "icon": "🎮", "rating": 4.5}]), encoding="utf-8")
        db = self.tmp / "extra.db"
        store.init_db(db, extra)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT id, name FROM products").fetchall(), [("7", "Игра")])
        finally:
            conn.close()


# ---------------------------------------------------------------- заказы

class OrdersTest(ServerTestCase):
    def test_create_order_returns_201_and_total_from_db_prices(self):
        resp = self.create_order([{"product_id": "p2", "qty": 2, "price": 1}])
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.body["total"], 598)
        self.assertEqual(resp.body["status"], "new")
        self.assertIsInstance(resp.body["order_id"], int)

    def test_create_order_decrements_stock(self):
        self.create_order([{"product_id": "p2", "qty": 2}])
        self.assertEqual(self.stock_of("p2"), 0)

    def test_customer_is_found_by_normalized_phone(self):
        self.create_order([{"product_id": "p1", "qty": 1}])
        other_format = dict(CUSTOMER, phone="099 111 22 33")
        self.create_order([{"product_id": "p1", "qty": 1}], customer=other_format)
        self.assertEqual(self.db_count("customers"), 1)
        resp = self.admin("GET", "/api/admin/customers")
        self.assertEqual(resp.body[0]["orders_count"], 2)

    def test_repeat_order_without_email_keeps_saved_email(self):
        self.create_order([{"product_id": "p1", "qty": 1}])
        no_email = dict(CUSTOMER, email="")
        resp = self.create_order([{"product_id": "p1", "qty": 1}], customer=no_email)
        self.assertEqual(resp.status, 201)
        customers = self.admin("GET", "/api/admin/customers").body
        self.assertEqual(customers[0]["email"], CUSTOMER["email"])

    def test_whitespace_email_is_treated_as_empty(self):
        resp = self.create_order([{"product_id": "p1", "qty": 1}], customer=dict(CUSTOMER, email="   "))
        self.assertEqual(resp.status, 201)

    def test_repeat_order_updates_customer_but_keeps_order_address(self):
        first = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        new_data = dict(CUSTOMER, name="Анна Смирнова", email="new@example.com",
                        address="Киев, ул. Новая, 2")
        second = self.create_order([{"product_id": "p1", "qty": 1}], customer=new_data).body["order_id"]

        orders = {o["id"]: o for o in self.admin("GET", "/api/admin/orders").body}
        self.assertEqual(orders[first]["address"], CUSTOMER["address"])
        self.assertEqual(orders[second]["address"], "Киев, ул. Новая, 2")

        customers = self.admin("GET", "/api/admin/customers").body
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["name"], "Анна Смирнова")
        self.assertEqual(customers[0]["email"], "new@example.com")
        self.assertEqual(customers[0]["address"], "Киев, ул. Новая, 2")

    def test_validation_errors_list_every_bad_field(self):
        payload = {
            "customer": {"name": " ", "phone": "12-34", "email": "не почта", "address": ""},
            "items": [{"product_id": "p1", "qty": 0}, {"product_id": "p2"}],
        }
        resp = self.request("POST", "/api/orders", body=payload)
        self.assertEqual(resp.status, 400)
        self.assertIn("error", resp.body)
        fields = resp.body["fields"]
        for key in ("customer.name", "customer.phone", "customer.email", "customer.address",
                    "items[0].qty", "items[1].qty"):
            self.assertIn(key, fields)
        self.assertEqual(self.db_count("orders"), 0)

    def test_unknown_product_is_400(self):
        resp = self.create_order([{"product_id": "ghost", "qty": 1}])
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["fields"], {"items[0].product_id": "товар не найден"})

    def test_qty_bounds(self):
        for qty in (100, -1, 1.5, "abc", True):
            resp = self.create_order([{"product_id": "p1", "qty": qty}])
            self.assertEqual(resp.status, 400, qty)
            self.assertIn("items[0].qty", resp.body["fields"], qty)

    def test_missing_items_and_customer(self):
        resp = self.request("POST", "/api/orders", body={"items": []})
        self.assertEqual(resp.status, 400)
        self.assertIn("items", resp.body["fields"])
        self.assertIn("customer", resp.body["fields"])

    def test_duplicate_product_in_one_order_is_rejected(self):
        resp = self.create_order([{"product_id": "p1", "qty": 1}, {"product_id": "p1", "qty": 1}])
        self.assertEqual(resp.status, 400)
        self.assertIn("items[1].product_id", resp.body["fields"])

    def test_insufficient_stock_rejects_and_keeps_stock(self):
        resp = self.create_order([{"product_id": "p2", "qty": 1}, {"product_id": "p3", "qty": 1}])
        self.assertEqual(resp.status, 400)
        self.assertIn("items[1].qty", resp.body["fields"])
        self.assertEqual(self.stock_of("p2"), 2)
        self.assertEqual(self.db_count("orders"), 0)

    def test_invalid_json_is_400_with_fields(self):
        resp = self.request("POST", "/api/orders", raw=b"{not json",
                            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body, {"error": "invalid json", "fields": {}})

    def test_body_over_64kb_is_413(self):
        big = b'{"customer": "' + b"a" * (70 * 1024) + b'"}'
        resp = self.request("POST", "/api/orders", raw=big,
                            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.body, {"error": "payload too large"})

    def test_slow_body_is_dropped_by_timeout(self):
        # Таймаут класса Handler (10 с) в тесте уменьшаем до 0.5 с.
        old_timeout = app.Handler.timeout
        app.Handler.timeout = 0.5
        self.addCleanup(setattr, app.Handler, "timeout", old_timeout)

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /api/orders HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
                     b'{"custo')          # обещали 100 байт, отправили 8 и замолчали
        started = time.monotonic()
        data = sock.recv(4096)            # сервер должен закрыть соединение сам
        self.assertEqual(data, b"")
        self.assertLess(time.monotonic() - started, 4)
        # Сервер после этого продолжает работать.
        self.assertEqual(self.request("GET", "/api/products").status, 200)

    def test_huge_content_length_is_400_not_500(self):
        resp = self.request("POST", "/api/orders", headers={"Content-Length": "9" * 5000})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["fields"], {})


class BigNumbersTest(ServerTestCase):
    def test_long_qty_is_400(self):
        resp = self.create_order([{"product_id": "p1", "qty": "1" * 19}])
        self.assertEqual(resp.status, 400)
        self.assertIn("items[0].qty", resp.body["fields"])

    def test_qty_beyond_int64_is_400(self):
        resp = self.create_order([{"product_id": "p1", "qty": 10 ** 19}])
        self.assertEqual(resp.status, 400)
        self.assertIn("items[0].qty", resp.body["fields"])

    def test_long_product_id_is_400(self):
        resp = self.create_order([{"product_id": "9" * 19, "qty": 1}])
        self.assertEqual(resp.status, 400)
        self.assertIn("items[0].product_id", resp.body["fields"])

    def test_18_digit_product_id_is_just_not_found(self):
        resp = self.create_order([{"product_id": "9" * 18, "qty": 1}])
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["fields"], {"items[0].product_id": "товар не найден"})

    def test_admin_order_path_id_too_long_is_400(self):
        resp = self.set_status("9" * 19, "paid")
        self.assertEqual(resp.status, 400)
        self.assertIn("id", resp.body["fields"])

    def test_admin_order_path_id_18_digits_is_404(self):
        self.assertEqual(self.set_status("9" * 18, "paid").status, 404)

    def test_admin_product_path_id_too_long_is_400(self):
        resp = self.admin("PATCH", "/api/admin/products/" + "9" * 19, body={"price": 1})
        self.assertEqual(resp.status, 400)

    def test_huge_price_and_stock_are_400(self):
        resp = self.admin("PATCH", "/api/admin/products/p1", body={"price": 10 ** 30, "stock": "9" * 25})
        self.assertEqual(resp.status, 400)
        self.assertIn("price", resp.body["fields"])
        self.assertIn("stock", resp.body["fields"])

    def test_long_event_product_id_is_400(self):
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/", "product_id": "9" * 19})
        self.assertEqual(resp.status, 400)
        self.assertIn("product_id", resp.body["fields"])

    def test_long_search_query_is_400(self):
        resp = self.request("GET", "/api/products?q=" + "a" * 200)
        self.assertEqual(resp.status, 400)
        self.assertIn("q", resp.body["fields"])


class StatusTransitionsTest(ServerTestCase):
    def test_full_happy_path(self):
        order_id = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        for status in ("paid", "shipped", "done"):
            resp = self.set_status(order_id, status)
            self.assertEqual(resp.status, 200, status)
            self.assertEqual(resp.body["status"], status)

    def test_invalid_transitions_are_400_with_text(self):
        order_id = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        resp = self.set_status(order_id, "shipped")          # new -> shipped нельзя
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body["error"], "Неверный переход статуса")
        self.assertIn("status", resp.body["fields"])
        self.assertIn("paid", resp.body["fields"]["status"])
        self.assertEqual(self.set_status(order_id, "new").status, 400)   # new -> new нельзя

        self.set_status(order_id, "paid")
        self.set_status(order_id, "shipped")
        self.set_status(order_id, "done")
        resp = self.set_status(order_id, "paid")             # done — финальный
        self.assertEqual(resp.status, 400)
        self.assertIn("финальный", resp.body["fields"]["status"])

    def test_shipped_cannot_be_cancelled(self):
        order_id = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        self.set_status(order_id, "paid")
        self.set_status(order_id, "shipped")
        self.assertEqual(self.set_status(order_id, "cancelled").status, 400)

    def test_cancel_from_paid_returns_stock(self):
        order_id = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.assertEqual(self.stock_of("p2"), 0)
        self.set_status(order_id, "paid")
        self.assertEqual(self.set_status(order_id, "cancelled").status, 200)
        self.assertEqual(self.stock_of("p2"), 2)

    def test_cancelled_is_final(self):
        order_id = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.set_status(order_id, "cancelled")
        resp = self.set_status(order_id, "new")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.stock_of("p2"), 2)

    def test_unknown_status_and_missing_order(self):
        order_id = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        resp = self.set_status(order_id, "lost")
        self.assertEqual(resp.status, 400)
        self.assertIn("status", resp.body["fields"])
        self.assertEqual(self.set_status(999, "paid").status, 404)
        resp = self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"other": 1})
        self.assertEqual(resp.status, 400)
        self.assertIn("status", resp.body["fields"])


class ExpiryTest(ServerTestCase):
    def test_stale_new_order_is_cancelled_and_stock_returned(self):
        old = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.assertEqual(self.stock_of("p2"), 0)
        self.set_order_time(old, minutes_ago(61))
        self.create_order([{"product_id": "p1", "qty": 1}])    # любой заказ запускает проверку
        self.assertEqual(self.order_status(old), "cancelled")
        self.assertEqual(self.stock_of("p2"), 2)

    def test_order_younger_than_an_hour_stays_new(self):
        fresh = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.set_order_time(fresh, minutes_ago(59))
        self.create_order([{"product_id": "p1", "qty": 1}])
        self.assertEqual(self.order_status(fresh), "new")
        self.assertEqual(self.stock_of("p2"), 0)

    def test_paid_order_is_not_expired(self):
        paid = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.set_status(paid, "paid")
        self.set_order_time(paid, minutes_ago(180))
        self.create_order([{"product_id": "p1", "qty": 1}])
        self.assertEqual(self.order_status(paid), "paid")
        self.assertEqual(self.stock_of("p2"), 0)

    def test_expired_orders_are_handled_at_startup(self):
        old = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.set_order_time(old, minutes_ago(61))
        store.init_db(self.db_path, self.products_path)     # перезапуск сервера
        self.assertEqual(self.db_stock("p2"), 2)
        self.assertEqual(self.order_status(old), "cancelled")


class CustomersStatsTest(ServerTestCase):
    def test_customer_orders_count_ignores_cancelled(self):
        kept = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        gone = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        self.set_status(gone, "cancelled")
        self.assertIsNotNone(kept)
        customers = self.admin("GET", "/api/admin/customers").body
        self.assertEqual(customers[0]["orders_count"], 1)

    def test_stats_fixed_kyiv_days(self):
        # Фиксированное «сейчас»: 11 ноября 2026, 10:00 по Киеву.
        now = datetime(2026, 11, 11, 10, 0, tzinfo=KYIV)
        ids = [self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"] for _ in range(4)]
        self.set_order_time(ids[0], "2026-11-10T21:30:00.000000Z")   # 10 ноября 23:30 по Киеву
        self.set_order_time(ids[1], "2026-11-10T22:30:00.000000Z")   # 11 ноября 00:30 по Киеву
        self.set_order_time(ids[2], "2026-10-28T21:59:59.000000Z")   # 28 октября 23:59 по Киеву, вне окна
        self.set_order_time(ids[3], "2026-10-28T22:00:00.000000Z")   # 29 октября 00:00 по Киеву, первый день окна

        conn = store.connect(self.db_path)
        try:
            stats = store.get_stats(conn, now=now)
        finally:
            conn.close()
        days = {d["day"]: d for d in stats["orders_by_day"]}
        self.assertEqual(len(stats["orders_by_day"]), 14)
        self.assertEqual(stats["orders_by_day"][0]["day"], "2026-10-29")
        self.assertEqual(stats["orders_by_day"][-1]["day"], "2026-11-11")
        self.assertEqual(days["2026-10-29"]["count"], 1)
        self.assertEqual(days["2026-11-10"]["count"], 1)
        self.assertEqual(days["2026-11-11"]["count"], 1)
        self.assertEqual(sum(d["count"] for d in stats["orders_by_day"]), 3)

    def test_stats_http_shape_and_totals(self):
        first = self.create_order([{"product_id": "p1", "qty": 2}, {"product_id": "p2", "qty": 1}]).body
        second = self.create_order([{"product_id": "p2", "qty": 1}]).body
        self.set_status(second["order_id"], "cancelled")
        self.request("POST", "/api/events", body={"type": "view", "path": "/", "product_id": "p1"})
        self.request("POST", "/api/events", body={"type": "click", "path": "/", "product_id": "p1"})

        resp = self.admin("GET", "/api/admin/stats")
        self.assertEqual(resp.status, 200)
        stats = resp.body
        self.assertEqual(stats["orders_count"], 1)
        self.assertEqual(stats["revenue"], first["total"])
        self.assertEqual(stats["customers_count"], 1)
        self.assertEqual(stats["top_products"][0], {"id": "p1", "name": "Тестовый Плеер", "qty": 2})
        self.assertEqual(stats["top_products"][1], {"id": "p2", "name": "Редактор текста", "qty": 1})
        self.assertEqual(len(stats["orders_by_day"]), 14)
        self.assertEqual(sum(d["count"] for d in stats["orders_by_day"]), 1)
        self.assertEqual(sum(d["revenue"] for d in stats["orders_by_day"]), first["total"])

    def test_admin_customers_list(self):
        self.create_order([{"product_id": "p1", "qty": 1}])
        resp = self.admin("GET", "/api/admin/customers")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body[0]["name"], CUSTOMER["name"])
        self.assertEqual(resp.body[0]["orders_count"], 1)


class EventsTest(ServerTestCase):
    def test_event_is_recorded_with_204(self):
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/", "product_id": "p1"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.raw, b"")
        self.assertEqual(self.db_count("events"), 1)

    def test_unknown_product_is_stored_without_product(self):
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/p", "product_id": "ghost"})
        self.assertEqual(resp.status, 204)
        conn = self.db()
        try:
            pid = conn.execute("SELECT product_id FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(pid)

    def test_event_validation(self):
        resp = self.request("POST", "/api/events", body={"type": "view"})
        self.assertEqual(resp.status, 400)
        self.assertIn("path", resp.body["fields"])

    def test_only_view_type_is_counted_as_views(self):
        for event_type in ("view", "click", "add_to_cart", "view"):
            self.request("POST", "/api/events", body={"type": event_type, "path": "/"})
        self.assertEqual(self.db_count("events"), 4)          # все принимаем и сохраняем
        self.assertEqual(self.admin("GET", "/api/admin/stats").body["views_count"], 2)


# ---------------------------------------------------------------- лимиты и админка

class RateLimitTest(ServerTestCase):
    def test_default_constants(self):
        self.assertEqual(app.ORDER_RATE_LIMIT, 10)
        self.assertEqual(app.EVENT_RATE_LIMIT, 120)
        self.assertEqual(app.HANDLER_TIMEOUT_SECONDS, 10)
        self.assertEqual(app.Handler.timeout, 10)
        self.assertEqual(app.ADMIN_FAIL_LIMIT, 10)
        self.assertEqual(app.ADMIN_FAIL_WINDOW, 15 * 60)
        self.assertEqual(store.ORDER_TTL_MINUTES, 60)
        self.assertGreaterEqual(store.DB_TIMEOUT_SECONDS, 10)

    def test_order_limit_returns_429(self):
        self.start_server(admin_token=TOKEN, db_name="rl-orders.db", order_limit=3, event_limit=BIG_LIMIT)
        for _ in range(3):
            self.assertEqual(self.create_order([{"product_id": "p1", "qty": 1}]).status, 201)
        resp = self.create_order([{"product_id": "p1", "qty": 1}])
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.body, {"error": "too many requests"})
        self.assertEqual(self.db_count("orders"), 3)

    def test_event_limit_returns_429(self):
        self.start_server(admin_token=TOKEN, db_name="rl-events.db", order_limit=BIG_LIMIT, event_limit=2)
        for _ in range(2):
            self.assertEqual(self.request("POST", "/api/events", body={"type": "view", "path": "/"}).status, 204)
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/"})
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.body["error"], "too many requests")

    def test_public_reads_are_not_limited(self):
        self.start_server(admin_token=TOKEN, db_name="rl-reads.db", order_limit=1, event_limit=1)
        for _ in range(20):
            self.assertEqual(self.request("GET", "/api/products").status, 200)

    def test_admin_blocked_after_ten_failed_logins(self):
        for _ in range(app.ADMIN_FAIL_LIMIT):
            self.assertEqual(self.admin("GET", "/api/admin/stats", token="wrong").status, 401)
        self.assertEqual(self.admin("GET", "/api/admin/stats", token="wrong").status, 429)
        # Блокировка действует на весь IP: даже верный токен теперь не проходит.
        self.assertEqual(self.admin("GET", "/api/admin/stats").status, 429)
        # Публичные маршруты при этом работают.
        self.assertEqual(self.request("GET", "/api/products").status, 200)

    def test_rate_limiter_window_with_fake_clock(self):
        clock = [0.0]
        limiter = app.RateLimiter(clock=lambda: clock[0])
        self.assertTrue(limiter.try_hit("orders", "1.2.3.4", 2, 60))
        self.assertTrue(limiter.try_hit("orders", "1.2.3.4", 2, 60))
        self.assertFalse(limiter.try_hit("orders", "1.2.3.4", 2, 60))
        self.assertTrue(limiter.try_hit("orders", "5.6.7.8", 2, 60))   # другой IP не задет
        clock[0] = 61.0
        self.assertTrue(limiter.try_hit("orders", "1.2.3.4", 2, 60))   # окно прошло

    def test_failed_admin_logins_expire_with_window(self):
        clock = [0.0]
        limiter = app.RateLimiter(clock=lambda: clock[0])
        for _ in range(app.ADMIN_FAIL_LIMIT):
            limiter.record("admin", "1.2.3.4", app.ADMIN_FAIL_WINDOW)
        self.assertTrue(limiter.is_over("admin", "1.2.3.4", app.ADMIN_FAIL_LIMIT, app.ADMIN_FAIL_WINDOW))
        clock[0] = app.ADMIN_FAIL_WINDOW + 1
        self.assertFalse(limiter.is_over("admin", "1.2.3.4", app.ADMIN_FAIL_LIMIT, app.ADMIN_FAIL_WINDOW))


class AdminTest(ServerTestCase):
    def test_admin_503_when_token_not_set(self):
        self.start_server(admin_token=None, db_name="no-admin.db",
                          order_limit=BIG_LIMIT, event_limit=BIG_LIMIT)
        for method, path in [
            ("GET", "/api/admin/stats"),
            ("GET", "/api/admin/orders"),
            ("GET", "/api/admin/customers"),
            ("GET", "/api/admin/products"),
            ("POST", "/api/admin/products"),
            ("PATCH", "/api/admin/products/p1"),
            ("PATCH", "/api/admin/orders/1"),
        ]:
            resp = self.admin(method, path, body={}, token=TOKEN)
            self.assertEqual(resp.status, 503, path)
            self.assertEqual(resp.body, {"error": "admin disabled"}, path)

    def test_short_admin_token_refuses_to_start(self):
        with self.assertRaises(ValueError) as ctx:
            app.create_server("127.0.0.1", 0, db_path=self.tmp / "short.db",
                              products_path=self.products_path, admin_token="short")
        self.assertIn("32", str(ctx.exception))

    def test_main_exits_with_message_on_short_token(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ADMIN_TOKEN": "x" * 31}), contextlib.redirect_stderr(err):
            code = app.main(["--port", "0", "--db", str(self.tmp / "main.db"),
                             "--products", str(self.products_path)])
        self.assertEqual(code, 1)
        self.assertIn("32", err.getvalue())

    def test_wrong_or_missing_token_is_401(self):
        self.assertEqual(self.admin("GET", "/api/admin/stats", token=None).status, 401)
        self.assertEqual(self.admin("GET", "/api/admin/stats", token="wrong").status, 401)
        resp = self.request("GET", "/api/admin/stats", headers={"Authorization": "Token " + TOKEN})
        self.assertEqual(resp.status, 401)

    def test_admin_orders_list_has_customer_address_and_items(self):
        self.create_order([{"product_id": "p1", "qty": 1}, {"product_id": "p2", "qty": 1}])
        resp = self.admin("GET", "/api/admin/orders")
        self.assertEqual(resp.status, 200)
        order = resp.body[0]
        self.assertEqual(order["customer_name"], CUSTOMER["name"])
        self.assertEqual(order["address"], CUSTOMER["address"])
        self.assertEqual(order["total"], 299)
        self.assertEqual({i["product_id"] for i in order["items"]}, {"p1", "p2"})
        self.assertEqual(order["status"], "new")

    def test_admin_products_create_and_patch(self):
        resp = self.admin("POST", "/api/admin/products", body={
            "name": "Новый сервис", "category": "Бизнес", "price": 150, "stock": 7,
        })
        self.assertEqual(resp.status, 201)
        pid = resp.body["id"]
        self.assertEqual(resp.body["description"], "")
        self.assertIn("created_at", resp.body)

        resp = self.admin("PATCH", f"/api/admin/products/{pid}", body={
            "name": "Сервис+", "price": 200, "stock": 3, "description": "Описание", "color": "#123456",
            "category": "Утилиты",
        })
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["name"], "Сервис+")
        self.assertEqual(resp.body["price"], 200)
        self.assertEqual(resp.body["stock"], 3)
        self.assertEqual(resp.body["category"], "Утилиты")
        self.assertEqual(self.stock_of(pid), 3)

    def test_admin_products_list_includes_all(self):
        resp = self.admin("GET", "/api/admin/products")
        self.assertEqual(len(resp.body), 3)
        self.assertIn("created_at", resp.body[0])

    def test_patch_product_errors(self):
        resp = self.admin("PATCH", "/api/admin/products/p1", body={"price": -5, "hack": 1})
        self.assertEqual(resp.status, 400)
        self.assertIn("price", resp.body["fields"])
        self.assertIn("hack", resp.body["fields"])
        resp = self.admin("PATCH", "/api/admin/products/missing", body={"price": 1})
        self.assertEqual(resp.status, 404)

    def test_create_product_requires_fields(self):
        resp = self.admin("POST", "/api/admin/products", body={"name": "Без цены"})
        self.assertEqual(resp.status, 400)
        self.assertIn("category", resp.body["fields"])
        self.assertIn("price", resp.body["fields"])


class HttpTest(ServerTestCase):
    def test_static_files_are_served(self):
        resp = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"ZENTO", resp.raw)
        self.assertIn("text/html", resp.headers["Content-Type"])
        resp = self.request("GET", "/styles.css")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/css", resp.headers["Content-Type"])
        self.assertEqual(self.request("GET", "/app.js").status, 200)

    def test_only_whitelisted_extensions_are_served(self):
        self.assertEqual(self.request("GET", "/notes.txt").status, 404)
        self.assertEqual(self.request("GET", "/CLAUDE.md").status, 404)

    def test_hidden_and_server_files_are_not_served(self):
        for path in ("/server/app.py", "/data/store.db", "/.env", "/server/", "/data/",
                     "/.git/config", "/Server/app.py", "/DATA/store.db"):
            self.assertEqual(self.request("GET", path).status, 404, path)

    def test_path_traversal_is_blocked(self):
        for path in ("/../server/app.py", "/%2e%2e/server/app.py", "/..%2fserver%2fapp.py",
                     "/styles.css/../../server/app.py", "/%00index.html", "/.."):
            self.assertEqual(self.request("GET", path).status, 404, path)

    def test_security_headers_on_api_and_static(self):
        csp = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
               "script-src 'self' ; img-src 'self' data:")
        for path in ("/api/products", "/styles.css", "/api/unknown"):
            resp = self.request("GET", path)
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff", path)
            self.assertIn("Referrer-Policy", resp.headers, path)
            self.assertEqual(resp.headers["X-Frame-Options"], "DENY", path)
            self.assertEqual(resp.headers["Content-Security-Policy"], csp, path)

    def test_json_content_type(self):
        resp = self.request("GET", "/api/products")
        self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")

    def test_unknown_api_path_is_json_404(self):
        resp = self.request("GET", "/api/unknown")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body, {"error": "not found"})

    def test_wrong_method_is_405(self):
        self.assertEqual(self.request("DELETE", "/api/products").status, 405)
        self.assertEqual(self.request("POST", "/api/products").status, 405)

    def test_non_get_on_static_is_405(self):
        self.assertEqual(self.request("POST", "/index.html", body={}).status, 405)


class DbTest(unittest.TestCase):
    def test_with_retry_repeats_on_locked_database(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        self.assertEqual(store.with_retry(flaky, pause=0), "ok")
        self.assertEqual(len(calls), 3)

    def test_with_retry_gives_up_and_other_errors_pass_through(self):
        calls = []

        def always_locked():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            store.with_retry(always_locked, attempts=3, pause=0)
        self.assertEqual(len(calls), 3)

        calls.clear()

        def broken():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: x")

        with self.assertRaises(sqlite3.OperationalError):
            store.with_retry(broken, pause=0)
        self.assertEqual(len(calls), 1)

    def test_old_database_gets_address_column(self):
        schema = store.SCHEMA_PATH.read_text(encoding="utf-8")
        old_line = "    address     TEXT NOT NULL DEFAULT '',\n    created_at  TEXT NOT NULL\n);\n\nCREATE INDEX IF NOT EXISTS idx_orders_created"
        self.assertIn(old_line, schema)
        old_schema = schema.replace(old_line, "    created_at  TEXT NOT NULL\n);\n\nCREATE INDEX IF NOT EXISTS idx_orders_created")
        tmp = Path(tempfile.mkdtemp(prefix="zento-old-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(old_schema)
        conn.close()
        store.init_db(db, None)
        conn = sqlite3.connect(db)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(orders)")]
        finally:
            conn.close()
        self.assertIn("address", columns)


class AdminTokenNotLoggedTest(ServerTestCase):
    def test_token_is_not_written_to_stdout_or_stderr(self):
        self.server.quiet = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.admin("GET", "/api/admin/stats")
            self.admin("GET", "/api/admin/stats", token="wrong-secret-value-0123456789abcdef")
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertNotIn("wrong-secret-value", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
