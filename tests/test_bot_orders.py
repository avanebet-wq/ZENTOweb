"""Маршруты для бота: новые заказы для уведомления владельца в Telegram."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import CUSTOMER, TOKEN, ServerTestCase  # noqa: E402

BOT = "test-bot-token-0123456789abcdefghijk"


class BotOrdersTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.start_server(admin_token=TOKEN, bot_token=BOT, db_name="botorders.db",
                          order_limit=1000, event_limit=1000)

    def call(self, method, path, token=BOT, body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.request(method, path, body=body, headers=headers)

    def new_order(self, product="p1"):
        payload = {"customer": dict(CUSTOMER), "items": [{"product_id": product, "qty": 2}]}
        return self.call("POST", "/api/orders", body=payload)

    def test_needs_token(self):
        self.assertEqual(self.call("GET", "/api/bot/orders", token=None).status, 401)
        self.assertEqual(self.call("GET", "/api/bot/orders", token="wrong-token").status, 401)
        self.assertEqual(self.call("POST", "/api/bot/orders/1/notified", token=None).status, 401)

    def test_new_order_is_listed_once_until_marked(self):
        self.assertEqual(self.call("GET", "/api/bot/orders").body, [])
        order_id = self.new_order().body["order_id"]
        listed = self.call("GET", "/api/bot/orders").body
        self.assertEqual([o["id"] for o in listed], [order_id])
        order = listed[0]
        self.assertEqual(order["customer_name"], CUSTOMER["name"])
        self.assertEqual(order["address"], CUSTOMER["address"])
        self.assertEqual(order["items"][0]["qty"], 2)
        self.assertIn("unit", order["items"][0])
        self.assertEqual(self.call("POST", f"/api/bot/orders/{order_id}/notified").status, 200)
        self.assertEqual(self.call("GET", "/api/bot/orders").body, [])

    def test_unknown_order_is_404(self):
        self.assertEqual(self.call("POST", "/api/bot/orders/999/notified").status, 404)

    def test_cancelled_order_is_not_announced(self):
        order_id = self.new_order().body["order_id"]
        self.call("PATCH", f"/api/admin/orders/{order_id}", token=TOKEN, body={"status": "cancelled"})
        self.assertEqual(self.call("GET", "/api/bot/orders").body, [])

    def test_admin_token_works_too(self):
        self.new_order()
        self.assertEqual(self.call("GET", "/api/bot/orders", token=TOKEN).status, 200)


if __name__ == "__main__":
    unittest.main()
