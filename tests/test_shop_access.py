"""Тесты доступа к товарам и заказам: без входа закрыто, с входом или токеном бота открыто.

Правила:
- GET /api/products, GET /api/products/<id>, POST /api/orders — только с активной сессией покупателя,
  токеном бота (SHOP_BOT_TOKEN) или админским токеном. Иначе 401 login_required.
- Истёкшая или выйденная сессия — то же, что гость.
- /api/admin/* не меняется: токен бота там по-прежнему 403, сессия покупателя админки не открывает.
- /api/auth/*, /api/captcha/*, /api/events остаются открытыми.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import unittest

import test_api
from test_api import CUSTOMER, PRODUCTS, TOKEN, SHOP_BOT
from test_auth import AuthServerBase, cookie_from

import auth  # noqa: E402
import app  # noqa: E402

LOGIN_PRODUCTS = {"ok": False, "error": "login_required", "message": "Войдите, чтобы смотреть товары"}
ITEMS = [{"product_id": "p1", "qty": 1}]


class ShopAccessTest(AuthServerBase):
    server_options = {"bot_token": SHOP_BOT}

    # --- помощники
    def session_headers(self):
        """Регистрирует покупателя и возвращает заголовок Cookie с его сессией."""
        resp = self.register()
        self.assertEqual(resp.status, 201, resp.body)
        token = cookie_from(resp)
        self.assertIsNotNone(token)
        return {"Cookie": f"{auth.SESSION_COOKIE}={token}"}, token

    def bot(self):
        return {"Authorization": f"Bearer {SHOP_BOT}"}

    def order(self, headers=None, anonymous=False):
        payload = {"customer": dict(CUSTOMER), "items": ITEMS}
        return self.request("POST", "/api/orders", body=payload, headers=headers, anonymous=anonymous)

    # --- без входа
    def test_products_list_without_login_is_401(self):
        resp = self.request("GET", "/api/products", anonymous=True)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body, LOGIN_PRODUCTS)

    def test_product_by_id_without_login_is_401(self):
        resp = self.request("GET", "/api/products/p1", anonymous=True)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body, LOGIN_PRODUCTS)

    def test_missing_product_without_login_is_401_not_404(self):
        # Сначала проверка входа, потом поиск: гость не узнаёт, есть ли товар.
        resp = self.request("GET", "/api/products/nope", anonymous=True)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body["error"], "login_required")

    def test_order_without_login_is_401_and_changes_nothing(self):
        before_stock = self.db_stock("p1")
        resp = self.order(anonymous=True)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body, {"ok": False, "error": "login_required",
                                     "message": "Войдите, чтобы оформить заказ"})
        self.assertEqual(self.db_count("orders"), 0)
        self.assertEqual(self.db_stock("p1"), before_stock)
        self.assertEqual(self.db_stock("p1"), PRODUCTS[0]["stock"])

    def test_wrong_token_without_login_is_401(self):
        headers = {"Authorization": "Bearer wrong-token-0123456789abcdefghijklmn"}
        self.assertEqual(self.request("GET", "/api/products", headers=headers).status, 401)
        self.assertEqual(self.order(headers=headers).status, 401)
        self.assertEqual(self.db_count("orders"), 0)

    def test_user_session_on_admin_routes_is_still_401(self):
        headers, _ = self.session_headers()
        self.assertEqual(self.request("GET", "/api/admin/stats", headers=headers).status, 401)
        self.assertEqual(self.request("GET", "/api/admin/orders", headers=headers).status, 401)

    # --- с входом
    def test_session_can_read_products_and_order(self):
        headers, _ = self.session_headers()
        resp = self.request("GET", "/api/products", headers=headers)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.body), 3)
        self.assertEqual(self.request("GET", "/api/products/p1", headers=headers).status, 200)
        resp = self.order(headers=headers)
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(self.db_count("orders"), 1)
        self.assertEqual(self.db_stock("p1"), PRODUCTS[0]["stock"] - 1)

    def test_expired_session_is_login_required(self):
        headers, token = self.session_headers()
        self.db_set("UPDATE sessions SET expires_at = ?", (test_api.minutes_ago(1),))
        self.assertEqual(self.request("GET", "/api/products", headers=headers).body, LOGIN_PRODUCTS)
        self.assertEqual(self.request("GET", "/api/products", headers=headers).status, 401)
        self.assertEqual(self.order(headers=headers).status, 401)
        self.assertEqual(self.db_count("orders"), 0)

    def test_logged_out_session_is_login_required(self):
        headers, _ = self.session_headers()
        self.assertEqual(self.request("GET", "/api/products", headers=headers).status, 200)
        self.assertEqual(self.request("POST", "/api/auth/logout", body={}, headers=headers).status, 200)
        self.assertEqual(self.request("GET", "/api/products", headers=headers).status, 401)
        self.assertEqual(self.order(headers=headers).status, 401)
        self.assertEqual(self.db_count("orders"), 0)

    # --- токены
    def test_bot_token_can_read_products_and_order(self):
        resp = self.request("GET", "/api/products", headers=self.bot())
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.request("GET", "/api/products/p2", headers=self.bot()).status, 200)
        resp = self.order(headers=self.bot())
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(self.db_stock("p1"), PRODUCTS[0]["stock"] - 1)

    def test_admin_token_can_read_products_and_order(self):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(self.request("GET", "/api/products", headers=headers).status, 200)
        self.assertEqual(self.order(headers=headers).status, 201)

    def test_bot_token_on_admin_routes_is_still_403(self):
        for method, path in [("GET", "/api/admin/stats"), ("GET", "/api/admin/orders"),
                             ("GET", "/api/admin/customers"), ("GET", "/api/admin/products"),
                             ("GET", "/api/admin/backup"), ("POST", "/api/admin/products")]:
            with self.subTest(path=path):
                body = {} if method == "POST" else None
                self.assertEqual(self.admin(method, path, body=body, token=SHOP_BOT).status, 403)

    def test_open_routes_stay_open_for_guests(self):
        resp = self.request("GET", "/api/auth/me", anonymous=True)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, {"ok": True, "user": None})
        self.assertEqual(self.request("GET", "/api/captcha/new", anonymous=True).status, 200)
        resp = self.request("POST", "/api/events", body={"type": "view", "path": "/"}, anonymous=True)
        self.assertEqual(resp.status, 204)

    def test_guest_attempts_do_not_use_up_order_limit(self):
        # Лимит заказов с одного IP = 1. Гостевые попытки его не тратят: настоящий заказ всё равно проходит.
        self.start_server(admin_token=TOKEN, db_name="guest-limit.db", order_limit=1,
                          event_limit=test_api.BIG_LIMIT)
        for _ in range(3):
            self.assertEqual(self.order(anonymous=True).status, 401)
        self.assertEqual(self.order(headers=self.bot()).status, 201)
        self.assertEqual(self.order(headers=self.bot()).status, 429)


class NoAdminTokenShopAccessTest(test_api.ServerTestCase):
    """Без ADMIN_TOKEN проверка входа не должна падать с ошибкой сервера."""

    def setUp(self):
        super().setUp()
        self.start_server(admin_token=None, db_name="no-admin-shop.db",
                          order_limit=1000, event_limit=1000)

    def test_guest_gets_401_without_admin_token(self):
        self.assertEqual(self.request("GET", "/api/products", anonymous=True).status, 401)

    def test_bot_token_still_works_without_admin_token(self):
        self.assertEqual(self.request("GET", "/api/products").status, 200)


if __name__ == "__main__":
    unittest.main()
