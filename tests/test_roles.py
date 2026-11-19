"""Тесты ролей токенов и резервной копии базы.

Роли:
- ADMIN_TOKEN — полный доступ (админка, товары, заказы, копия базы).
- SHOP_BOT_TOKEN — токен бота. На админские маршруты не пускает (403), на обычные — ведёт себя как посетитель.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from test_api import CUSTOMER, TOKEN, ServerTestCase  # noqa: E402
import app  # noqa: E402

BOT = "test-bot-token-0123456789abcdefghijk"   # 36 символов, не короче 32


class BotRoleTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.start_server(admin_token=TOKEN, bot_token=BOT, db_name="roles.db",
                          order_limit=1000, event_limit=1000)

    def call(self, method, path, token=None, body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.request(method, path, body=body, headers=headers)

    def test_bot_can_read_products(self):
        resp = self.call("GET", "/api/products", token=BOT)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body), 3)

    def test_bot_can_create_order(self):
        resp = self.call("POST", "/api/orders", token=BOT,
                         body={"customer": dict(CUSTOMER), "items": [{"product_id": "p1", "qty": 1}]})
        self.assertEqual(resp.status, 201)

    def test_bot_cannot_edit_product(self):
        resp = self.call("PATCH", "/api/admin/products/p2", token=BOT, body={"price": 1})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.request("GET", "/api/products/p2").body["price"], 299)

    def test_bot_cannot_create_product(self):
        resp = self.call("POST", "/api/admin/products", token=BOT,
                         body={"name": "Взлом", "category": "Х", "price": 1, "stock": 1})
        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.request("GET", "/api/products").body), 3)

    def test_bot_cannot_read_admin_data(self):
        for path in ("/api/admin/stats", "/api/admin/orders", "/api/admin/customers",
                     "/api/admin/products", "/api/admin/backup"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path, token=BOT).status, 403)

    def test_admin_still_has_full_access(self):
        self.assertEqual(self.call("GET", "/api/admin/stats", token=TOKEN).status, 200)
        resp = self.call("PATCH", "/api/admin/products/p2", token=TOKEN, body={"price": 300})
        self.assertEqual(resp.status, 200)

    def test_missing_or_wrong_token_is_401(self):
        self.assertEqual(self.call("GET", "/api/admin/stats").status, 401)
        self.assertEqual(self.call("GET", "/api/admin/stats", token="wrong-token-0123456789abcdefghijklmn").status, 401)


class NoBotTokenTest(ServerTestCase):
    def test_bot_token_is_useless_when_not_configured(self):
        # В базовом сервере токена бота нет: любой чужой токен — просто неверный вход.
        resp = self.request("GET", "/api/admin/stats", headers={"Authorization": f"Bearer {BOT}"})
        self.assertEqual(resp.status, 401)


class BotTokenConfigTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp_dir = tempfile.TemporaryDirectory(prefix="zento-roles-")
        self.addCleanup(tmp_dir.cleanup)
        self.tmp = Path(tmp_dir.name)

    def make(self, **tokens):
        server = app.create_server(
            "127.0.0.1", 0, db_path=self.tmp / "cfg.db",
            products_path=self.tmp / "none.json", quiet=True, **tokens,
        )
        server.server_close()

    def test_short_bot_token_refuses_to_start(self):
        with self.assertRaises(ValueError):
            self.make(admin_token=TOKEN, bot_token="short")

    def test_bot_token_equal_to_admin_refuses_to_start(self):
        with self.assertRaises(ValueError):
            self.make(admin_token=TOKEN, bot_token=TOKEN)


class BackupTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.start_server(admin_token=TOKEN, bot_token=BOT, db_name="backup.db",
                          order_limit=1000, event_limit=1000)
        self.create_order([{"product_id": "p1", "qty": 1}])

    def fetch_backup(self, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.request("GET", "/api/admin/backup", headers=headers)

    def test_admin_gets_database_copy(self):
        resp = self.fetch_backup()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/octet-stream")
        self.assertTrue(resp.headers.get("Content-Disposition", "").startswith('attachment; filename="zento-backup-'))
        self.assertTrue(resp.raw.startswith(b"SQLite format 3\x00"))

        copy_path = self.tmp / "downloaded.db"
        copy_path.write_bytes(resp.raw)
        conn = sqlite3.connect(str(copy_path))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        finally:
            conn.close()

    def test_copy_is_a_snapshot_not_a_live_link(self):
        copy_path = self.tmp / "snapshot.db"
        copy_path.write_bytes(self.fetch_backup().raw)
        self.create_order([{"product_id": "p2", "qty": 1}])   # заказ уже после копии
        conn = sqlite3.connect(str(copy_path))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        finally:
            conn.close()

    def test_bot_cannot_download_backup(self):
        self.assertEqual(self.fetch_backup(token=BOT).status, 403)

    def test_no_token_cannot_download_backup(self):
        self.assertEqual(self.fetch_backup(token=None).status, 401)


if __name__ == "__main__":
    unittest.main()
