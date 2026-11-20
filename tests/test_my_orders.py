"""Заказы покупателя: привязка к аккаунту и список «Мои заказы» (GET /api/my/orders)."""

import unittest

from test_api import CUSTOMER
from test_images import ProfileServerBase


class MyOrdersTest(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.anna = self.make_user("Anna", email="anna@example.com")
        self.anna_cookie = self.session_for(self.anna)
        self.boris = self.make_user("Boris", email="boris@example.com")
        self.boris_cookie = self.session_for(self.boris)

    def order_as(self, cookie, product="p1"):
        payload = {"customer": dict(CUSTOMER), "items": [{"product_id": product, "qty": 1}]}
        return self.request("POST", "/api/orders", body=payload, headers=cookie)

    def test_anonymous_gets_401(self):
        self.assertEqual(self.request("GET", "/api/my/orders", anonymous=True).status, 401)

    def test_buyer_sees_only_own_orders(self):
        self.assertEqual(self.order_as(self.anna_cookie).status, 201)
        self.assertEqual(self.order_as(self.boris_cookie, "p2").status, 201)
        mine = self.request("GET", "/api/my/orders", headers=self.anna_cookie)
        self.assertEqual(mine.status, 200)
        self.assertEqual(len(mine.body), 1)
        order = mine.body[0]
        self.assertEqual(order["status"], "new")
        self.assertEqual(order["tracking"], "")
        self.assertEqual(order["items"][0]["product_id"], "p1")
        self.assertIn("unit", order["items"][0])
        self.assertEqual(self.request("GET", "/api/my/orders", headers=self.boris_cookie).body[0]["items"][0]["product_id"], "p2")

    def test_tracking_set_by_admin_is_visible_to_buyer(self):
        order_id = self.order_as(self.anna_cookie).body["order_id"]
        self.admin("PATCH", f"/api/admin/orders/{order_id}", body={"tracking": "20450123456789"})
        mine = self.request("GET", "/api/my/orders", headers=self.anna_cookie).body
        self.assertEqual(mine[0]["tracking"], "20450123456789")

    def test_buyer_without_orders_gets_empty_list(self):
        self.assertEqual(self.request("GET", "/api/my/orders", headers=self.anna_cookie).body, [])


if __name__ == "__main__":
    unittest.main()
