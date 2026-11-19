"""Тесты API ZENTO.

Каждый тест поднимает сервер на временной базе и временной папке со статикой.
Файл data/store.db и реальные файлы сайта не трогаются.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

import app  # noqa: E402
import store  # noqa: E402

TOKEN = "test-admin-token-123"

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
    "phone": "+7 (999) 111-22-33",
    "email": "anna@example.com",
    "address": "Москва, ул. Тестовая, 1",
}


class ServerTestCase(unittest.TestCase):
    """Базовый класс: создаёт временные файлы и запускает сервер."""

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
        (site / "server" / "app.py").write_text("print('secret')", encoding="utf-8")
        (site / "data" / "store.db").write_bytes(b"SQLite format 3\x00")
        (site / ".env").write_text("SECRET=1", encoding="utf-8")
        (site / "CLAUDE.md").write_text("notes", encoding="utf-8")
        self.site = site

        self.start_server(admin_token=TOKEN, db_name="test.db")

    def start_server(self, admin_token, db_name):
        db_path = self.tmp / db_name
        server = app.create_server(
            "127.0.0.1", 0,
            db_path=db_path,
            products_path=self.products_path,
            admin_token=admin_token,
            static_root=self.site,
            quiet=True,
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

    def request(self, method, path, body=None, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        send_headers = dict(headers or {})
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

    def stock_of(self, product_id):
        resp = self.request("GET", f"/api/products/{product_id}")
        return resp.body["stock"]

    def db_count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


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
        # Повторный запуск с тем же JSON не должен добавлять товары второй раз.
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


class OrdersTest(ServerTestCase):
    def test_create_order_returns_201_and_total_from_db_prices(self):
        # Цену, которую прислал клиент, игнорируем: берём цену из базы.
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
        other_format = dict(CUSTOMER, phone="8 999 111 22 33")
        self.create_order([{"product_id": "p1", "qty": 1}], customer=other_format)
        self.assertEqual(self.db_count("customers"), 1)
        resp = self.admin("GET", "/api/admin/customers")
        self.assertEqual(resp.body[0]["orders_count"], 2)

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
        # Транзакция откатилась: остаток первого товара не изменился, заказа нет.
        self.assertEqual(self.stock_of("p2"), 2)
        self.assertEqual(self.db_count("orders"), 0)

    def test_invalid_json_is_400(self):
        resp = self.request("POST", "/api/orders", raw=b"{not json",
                            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.body, {"error": "invalid json"})

    def test_body_over_64kb_is_413(self):
        big = b'{"customer": "' + b"a" * (70 * 1024) + b'"}'
        resp = self.request("POST", "/api/orders", raw=big,
                            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.body, {"error": "payload too large"})


class EventsTest(ServerTestCase):
    def test_event_is_recorded_with_204(self):
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/", "product_id": "p1"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.raw, b"")
        self.assertEqual(self.db_count("events"), 1)

    def test_unknown_product_is_stored_without_product(self):
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/p", "product_id": "ghost"})
        self.assertEqual(resp.status, 204)
        conn = sqlite3.connect(self.db_path)
        try:
            pid = conn.execute("SELECT product_id FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(pid)

    def test_event_validation(self):
        resp = self.request("POST", "/api/events", body={"type": "view"})
        self.assertEqual(resp.status, 400)
        self.assertIn("path", resp.body["fields"])


class AdminTest(ServerTestCase):
    def test_admin_503_when_token_not_set(self):
        self.start_server(admin_token=None, db_name="no-admin.db")
        self.port = self.server.server_address[1]
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

    def test_wrong_or_missing_token_is_401(self):
        self.assertEqual(self.admin("GET", "/api/admin/stats", token=None).status, 401)
        self.assertEqual(self.admin("GET", "/api/admin/stats", token="wrong").status, 401)
        resp = self.request("GET", "/api/admin/stats", headers={"Authorization": "Token " + TOKEN})
        self.assertEqual(resp.status, 401)

    def test_admin_orders_list_has_customer_and_items(self):
        self.create_order([{"product_id": "p1", "qty": 1}, {"product_id": "p2", "qty": 1}])
        resp = self.admin("GET", "/api/admin/orders")
        self.assertEqual(resp.status, 200)
        order = resp.body[0]
        self.assertEqual(order["customer_name"], CUSTOMER["name"])
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

    def test_order_status_changes(self):
        order_id = self.create_order([{"product_id": "p1", "qty": 1}]).body["order_id"]
        resp = self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "paid"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["status"], "paid")
        resp = self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "lost"})
        self.assertEqual(resp.status, 400)
        self.assertIn("status", resp.body["fields"])
        resp = self.admin("PATCH", "/api/admin/orders/999", body={"status": "done"})
        self.assertEqual(resp.status, 404)

    def test_cancel_returns_stock_and_uncancel_takes_it_back(self):
        order_id = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.assertEqual(self.stock_of("p2"), 0)
        self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "cancelled"})
        self.assertEqual(self.stock_of("p2"), 2)
        self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "new"})
        self.assertEqual(self.stock_of("p2"), 0)

    def test_uncancel_fails_when_stock_gone(self):
        order_id = self.create_order([{"product_id": "p2", "qty": 2}]).body["order_id"]
        self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "cancelled"})
        self.create_order([{"product_id": "p2", "qty": 2}])
        resp = self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"status": "new"})
        self.assertEqual(resp.status, 400)
        self.assertIn("status", resp.body["fields"])
        self.assertEqual(self.stock_of("p2"), 0)

    def test_stats(self):
        first = self.create_order([{"product_id": "p1", "qty": 2}, {"product_id": "p2", "qty": 1}]).body
        second = self.create_order([{"product_id": "p2", "qty": 1}]).body
        self.admin("PATCH", f"/api/admin/orders/{second['order_id']}", body={"status": "cancelled"})
        self.request("POST", "/api/events", body={"type": "view", "path": "/", "product_id": "p1"})
        self.request("POST", "/api/events", body={"type": "view", "path": "/x"})

        resp = self.admin("GET", "/api/admin/stats")
        self.assertEqual(resp.status, 200)
        stats = resp.body
        self.assertEqual(stats["orders_count"], 1)
        self.assertEqual(stats["revenue"], first["total"])
        self.assertEqual(stats["customers_count"], 1)
        self.assertEqual(stats["views_count"], 2)
        # Отменённый заказ в топ не попадает: p2 продан только 1 раз.
        self.assertEqual(stats["top_products"][0], {"id": "p1", "name": "Тестовый Плеер", "qty": 2})
        self.assertEqual(stats["top_products"][1], {"id": "p2", "name": "Редактор текста", "qty": 1})

        days = stats["orders_by_day"]
        self.assertEqual(len(days), 14)
        today = datetime.now(timezone.utc).date()
        self.assertEqual(days[-1]["day"], today.isoformat())
        self.assertEqual(days[0]["day"], (today - timedelta(days=13)).isoformat())
        self.assertEqual(days[-1]["count"], 1)
        self.assertEqual(days[-1]["revenue"], first["total"])
        self.assertEqual(sum(d["count"] for d in days), 1)

    def test_admin_customers_list(self):
        self.create_order([{"product_id": "p1", "qty": 1}])
        resp = self.admin("GET", "/api/admin/customers")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body[0]["name"], CUSTOMER["name"])
        self.assertEqual(resp.body[0]["orders_count"], 1)


class HttpTest(ServerTestCase):
    def test_static_files_are_served(self):
        resp = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"ZENTO", resp.raw)
        self.assertIn("text/html", resp.headers["Content-Type"])
        resp = self.request("GET", "/styles.css")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/css", resp.headers["Content-Type"])

    def test_hidden_and_server_files_are_not_served(self):
        for path in ("/server/app.py", "/data/store.db", "/.env", "/CLAUDE.md", "/server/", "/data/"):
            self.assertEqual(self.request("GET", path).status, 404, path)

    def test_path_traversal_is_blocked(self):
        for path in ("/../server/app.py", "/%2e%2e/server/app.py", "/..%2fserver%2fapp.py",
                     "/styles.css/../../server/app.py", "/%00index.html", "/.."):
            self.assertEqual(self.request("GET", path).status, 404, path)

    def test_security_headers_on_api_and_static(self):
        for path in ("/api/products", "/styles.css"):
            resp = self.request("GET", path)
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff", path)
            self.assertIn("Referrer-Policy", resp.headers, path)

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


class AdminTokenNotLoggedTest(ServerTestCase):
    def test_token_is_not_written_to_stdout_or_stderr(self):
        import io
        import contextlib

        self.server.quiet = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.admin("GET", "/api/admin/stats")
            self.admin("GET", "/api/admin/stats", token="wrong-secret-value")
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertNotIn("wrong-secret-value", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
