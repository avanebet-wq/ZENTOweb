"""Тесты входа и регистрации покупателей: пароли, сессии, cookie, лимиты, отделение от админки.

Сервер поднимается на временной базе и временной папке (картинки капчи — фиктивные SVG внутри tempfile).
Реальные файлы сайта, data/store.db и ключи не трогаются.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_api
from test_api import TOKEN, BIG_LIMIT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import auth  # noqa: E402
import app  # noqa: E402

FORM = {
    "name": "Анна Петрова",
    "phone": "+380 67 123 45 67",
    "email": "Anna.Petrova@Example.com",
    "password": "Secret123",
    "consent": True,
}
PHONE_FORMS = ["+380671234567", "067 123 45 67", "380 67 123 45 67", "(067) 123-45-67"]
SESSION_RE_PREFIX = "zento_session="


def make_images(folder, count):
    """Фиктивные картинки капчи: короткие SVG без скриптов."""
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        (folder / f"{number:02d}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">'
            f'<rect width="800" height="800" fill="#{number:02x}{number:02x}ff"/></svg>',
            encoding="utf-8",
        )


def solve(tiles, shift=0):
    """Расстановка: каждый кусочек в свою ячейку. shift сдвигает всё, чтобы получить неверную."""
    return {tile["token"]: (tile["piece"] + shift) % 4 for tile in tiles}


def cookie_from(resp):
    """Значение zento_session из Set-Cookie или None."""
    for header in resp.headers.get_all("Set-Cookie") or []:
        if header.startswith(SESSION_RE_PREFIX):
            return header.split(";", 1)[0][len(SESSION_RE_PREFIX):]
    return None


class AuthServerBase(test_api.ServerTestCase):
    """Сервер со своей временной папкой, в которой есть 12 фиктивных картинок капчи."""

    captcha_count = 12
    server_options = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-auth-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.products_path = self.tmp / "products.json"
        self.products_path.write_text(json.dumps(test_api.PRODUCTS, ensure_ascii=False), encoding="utf-8")
        self.site = self.tmp / "site"
        (self.site / "server").mkdir(parents=True)
        (self.site / "data").mkdir()
        (self.site / "index.html").write_text("<!doctype html><title>ZENTO</title>", encoding="utf-8")
        make_images(self.site / "assets" / "captcha", self.captcha_count)
        self.start_server(admin_token=TOKEN, db_name="auth.db",
                          order_limit=BIG_LIMIT, event_limit=BIG_LIMIT, **self.server_options)

    # --- помощники
    def assert_error(self, resp, status, code):
        self.assertEqual(resp.status, status, resp.body)
        self.assertIs(resp.body["ok"], False)
        self.assertEqual(resp.body["error"], code)
        self.assertIsInstance(resp.body["message"], str)

    def get_ticket(self):
        challenge = self.request("GET", "/api/captcha/new").body
        resp = self.request("POST", "/api/captcha/verify",
                            body={"challenge_id": challenge["challenge_id"],
                                  "placements": solve(challenge["tiles"])})
        self.assertEqual(resp.status, 200, resp.body)
        return resp.body["ticket"]

    def register(self, ticket=None, **changes):
        body = {**FORM, **changes}
        body["captcha_ticket"] = ticket if ticket is not None else self.get_ticket()
        return self.request("POST", "/api/auth/register", body=body)

    def login(self, login, password=FORM["password"]):
        return self.request("POST", "/api/auth/login", body={"login": login, "password": password})

    def me(self, token=None):
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}"} if token is not None else {}
        return self.request("GET", "/api/auth/me", headers=headers).body

    def user_rows(self):
        conn = self.db()
        try:
            return conn.execute("SELECT id, name, phone, email, password_hash FROM users").fetchall()
        finally:
            conn.close()


# ---------------------------------------------------------------- пароль и токены (без сервера)

class PasswordRulesTest(unittest.TestCase):
    def test_good_passwords_pass(self):
        for password in ["Secret123", "abcdefg1", "Пароль2026", "x" * 100 + "1"]:
            with self.subTest(password=password):
                self.assertIsNone(auth.password_error(password, "anna@example.com"))

    def test_length_bounds(self):
        self.assertIn("короче", auth.password_error("abc1234", ""))
        self.assertIsNone(auth.password_error("a" * 127 + "1", ""))
        self.assertIn("длиннее", auth.password_error("a" * 128 + "1", ""))

    def test_needs_letter_and_digit(self):
        self.assertIsNotNone(auth.password_error("12345678901", ""))
        self.assertIsNotNone(auth.password_error("abcdefghij", ""))

    def test_common_passwords_are_refused_in_any_case(self):
        self.assertIn("простой", auth.password_error("qwerty123", ""))
        self.assertIn("простой", auth.password_error("Qwerty123", ""))
        self.assertIn("простой", auth.password_error("PASSWORD1", ""))

    def test_email_local_part_is_refused(self):
        self.assertIn("почты", auth.password_error("Anna.K2024", "anna.k@example.com"))
        # Короткую часть почты не проверяем, иначе любой пароль с буквой «a» был бы запрещён.
        self.assertIsNone(auth.password_error("Mango2024", "a@example.com"))

    def test_hash_format_and_verify(self):
        stored = auth.hash_password("Secret123")
        parts = stored.split("$")
        self.assertEqual(parts[:4], ["scrypt", "16384", "8", "1"])
        self.assertEqual(len(parts), 6)
        self.assertTrue(auth.verify_password("Secret123", stored))
        self.assertFalse(auth.verify_password("Secret124", stored))
        self.assertFalse(auth.verify_password("Secret123", "md5$whatever"))
        self.assertFalse(auth.verify_password("Secret123", "scrypt$oops"))

    def test_same_password_gets_different_salt(self):
        self.assertNotEqual(auth.hash_password("Secret123"), auth.hash_password("Secret123"))

    def test_dummy_hash_never_matches(self):
        self.assertFalse(auth.verify_password("Secret123", auth.dummy_hash()))

    def test_session_cookie_text(self):
        token = auth.new_token()
        self.assertEqual(
            auth.session_cookie(token, True),
            f"zento_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000; Secure",
        )
        self.assertNotIn("Secure", auth.session_cookie(token, False))
        self.assertEqual(
            auth.expired_session_cookie(True),
            "zento_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0; Secure",
        )

    def test_read_session_token(self):
        token = auth.new_token()
        self.assertEqual(auth.read_session_token(f"a=1; zento_session={token}"), token)
        self.assertIsNone(auth.read_session_token("zento_session=short"))
        self.assertIsNone(auth.read_session_token("other=" + token))
        self.assertIsNone(auth.read_session_token(None))

    def test_token_hash_is_sha256(self):
        token = auth.new_token()
        self.assertEqual(auth.token_hash(token), hashlib.sha256(token.encode()).hexdigest())


# ---------------------------------------------------------------- регистрация

class RegisterTest(AuthServerBase):
    def test_success_returns_201_user_and_cookie(self):
        resp = self.register()
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(resp.body["ok"], True)
        self.assertEqual(resp.body["user"], {
            "id": resp.body["user"]["id"], "name": "Анна Петрова",
            "phone": "+380671234567", "email": "anna.petrova@example.com",
        })
        self.assertIsNotNone(cookie_from(resp))

    def test_database_keeps_only_hashes_and_lowercase_email(self):
        resp = self.register()
        token = cookie_from(resp)
        (row,) = self.user_rows()
        self.assertEqual(row[3], "anna.petrova@example.com")
        self.assertTrue(row[4].startswith("scrypt$16384$8$1$"))
        self.assertNotIn("Secret123", row[4])
        conn = self.db()
        try:
            hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
            self.assertEqual(hashes, [auth.token_hash(token)])
            self.assertNotIn(token, hashes)
            used = conn.execute("SELECT COUNT(*) FROM captcha_tickets WHERE used_at IS NOT NULL").fetchone()[0]
            self.assertEqual(used, 1)
        finally:
            conn.close()

    def test_password_and_hash_never_in_response(self):
        resp = self.register()
        self.assertNotIn(b"Secret123", resp.raw)
        self.assertNotIn(b"scrypt", resp.raw)

    def test_register_does_not_touch_customers(self):
        self.assertEqual(self.db_count("customers"), 0)
        self.assertEqual(self.register().status, 201)
        self.assertEqual(self.db_count("customers"), 0)

    def test_all_field_errors_come_at_once_and_ticket_survives(self):
        ticket = self.get_ticket()
        bad = {"name": "A1", "phone": "+7 912 345 67 89", "email": "not-an-email",
               "password": "abc", "consent": False, "captcha_ticket": ticket}
        resp = self.request("POST", "/api/auth/register", body=bad)
        self.assert_error(resp, 400, "validation_error")
        self.assertEqual(set(resp.body["fields"]), {"name", "phone", "email", "password", "consent"})
        # Ошибки полей не тратят билет: с тем же билетом регистрация проходит.
        ok = self.register(ticket=ticket)
        self.assertEqual(ok.status, 201, ok.body)

    def test_name_rules(self):
        ticket = self.get_ticket()
        for name in ["А", "Anna2", "<b>Анна</b>", "x" * 51]:
            with self.subTest(name=name):
                resp = self.register(ticket=ticket, name=name)
                self.assert_error(resp, 400, "validation_error")
                self.assertIn("name", resp.body["fields"])

    def test_phone_must_be_ukrainian(self):
        ticket = self.get_ticket()
        resp = self.register(ticket=ticket, phone="+7 912 345 67 89")
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("phone", resp.body["fields"])

    def test_weak_passwords_are_field_errors(self):
        ticket = self.get_ticket()
        cases = {
            "short": "Ab1",
            "no_digit": "password",
            "no_letter": "12345678901",
            "common": "qwerty123",
            "email_part": "Anna.Petrova1",
        }
        for label, password in cases.items():
            with self.subTest(case=label):
                resp = self.register(ticket=ticket, password=password)
                self.assert_error(resp, 400, "validation_error")
                self.assertIn("password", resp.body["fields"])
        self.assertEqual(self.db_count("users"), 0)

    def test_consent_must_be_true(self):
        ticket = self.get_ticket()
        resp = self.register(ticket=ticket, consent="yes")
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("consent", resp.body["fields"])

    def test_missing_ticket_is_403(self):
        resp = self.request("POST", "/api/auth/register", body=dict(FORM))
        self.assert_error(resp, 403, "captcha_required")
        resp = self.register(ticket="")
        self.assert_error(resp, 403, "captcha_required")

    def test_wrong_ticket_is_403(self):
        resp = self.register(ticket="not-a-real-ticket-value-0000000000000000")
        self.assert_error(resp, 403, "captcha_required")

    def test_used_ticket_is_rejected(self):
        ticket = self.get_ticket()
        self.assertEqual(self.register(ticket=ticket).status, 201)
        resp = self.register(ticket=ticket, phone="+380501112233", email="second@example.com")
        self.assert_error(resp, 403, "captcha_required")

    def test_expired_ticket_is_rejected(self):
        ticket = self.get_ticket()
        self.db_set("UPDATE captcha_tickets SET expires_at = ?", (test_api.minutes_ago(1),))
        resp = self.register(ticket=ticket)
        self.assert_error(resp, 403, "captcha_required")
        self.assertEqual(self.db_count("users"), 0)

    def test_duplicate_phone_is_409_with_field(self):
        self.assertEqual(self.register().status, 201)
        resp = self.register(email="other@example.com", phone="067 123 45 67")
        self.assert_error(resp, 409, "already_registered")
        self.assertIn("phone", resp.body["fields"])
        self.assertNotIn("email", resp.body["fields"])

    def test_duplicate_email_any_case_is_409(self):
        self.assertEqual(self.register().status, 201)
        resp = self.register(email="ANNA.PETROVA@example.COM", phone="+380501112233")
        self.assert_error(resp, 409, "already_registered")
        self.assertIn("email", resp.body["fields"])
        self.assertNotIn("phone", resp.body["fields"])

    def test_duplicate_does_not_burn_ticket(self):
        self.assertEqual(self.register().status, 201)
        ticket = self.get_ticket()
        self.assert_error(self.register(ticket=ticket, phone="+380671234567"), 409, "already_registered")
        resp = self.register(ticket=ticket, phone="+380501112233", email="fresh@example.com")
        self.assertEqual(resp.status, 201, resp.body)

    def test_non_json_content_type_is_415(self):
        resp = self.request("POST", "/api/auth/register", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assert_error(resp, 415, "unsupported_media_type")

    def test_body_that_is_not_an_object_is_validation_error(self):
        resp = self.request("POST", "/api/auth/register", raw=b"[1,2]",
                            headers={"Content-Type": "application/json"})
        self.assert_error(resp, 400, "validation_error")
        resp = self.request("POST", "/api/auth/register", raw=b"{broken",
                            headers={"Content-Type": "application/json"})
        self.assert_error(resp, 400, "validation_error")

    def test_register_limit_returns_429_with_retry_after(self):
        self.start_server(admin_token=TOKEN, db_name="reg-limit.db", auth_limits={"register": (2, 3600)})
        self.assertEqual(self.register(email="a1@example.com", phone="+380501000001").status, 201)
        self.assertEqual(self.register(email="a2@example.com", phone="+380501000002").status, 201)
        resp = self.register(email="a3@example.com", phone="+380501000003")
        self.assert_error(resp, 429, "too_many_requests")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)


# ---------------------------------------------------------------- вход, сессии, выход

class LoginTest(AuthServerBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.register().status, 201)

    def test_login_by_email_in_any_case(self):
        resp = self.login("ANNA.Petrova@example.com")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["user"]["phone"], "+380671234567")
        self.assertIsNotNone(cookie_from(resp))

    def test_login_by_phone_in_several_formats(self):
        for phone in PHONE_FORMS:
            with self.subTest(phone=phone):
                resp = self.login(phone)
                self.assertEqual(resp.status, 200, resp.body)
                self.assertEqual(resp.body["user"]["email"], "anna.petrova@example.com")

    def test_wrong_password_is_401(self):
        resp = self.login("anna.petrova@example.com", "Wrong12345")
        self.assert_error(resp, 401, "invalid_credentials")
        self.assertEqual(resp.body["message"], "Неверный логин или пароль")

    def test_unknown_login_gives_same_answer(self):
        wrong_password = self.login("anna.petrova@example.com", "Wrong12345").body
        unknown_email = self.login("nobody@example.com").body
        unknown_phone = self.login("+380501999999").body
        malformed = self.login("+7 912 345 67 89").body
        self.assertEqual(wrong_password, unknown_email)
        self.assertEqual(wrong_password, unknown_phone)
        self.assertEqual(wrong_password, malformed)

    def test_unrecognized_login_format_is_401_not_400(self):
        resp = self.login("hello world")
        self.assert_error(resp, 401, "invalid_credentials")

    def test_missing_fields_are_validation_errors(self):
        resp = self.request("POST", "/api/auth/login", body={"password": "x"})
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("login", resp.body["fields"])
        resp = self.request("POST", "/api/auth/login", body={"login": "a@b.cc", "password": ""})
        self.assertIn("password", resp.body["fields"])

    def test_cookie_attributes(self):
        cookie_line = self.login("anna.petrova@example.com").headers.get_all("Set-Cookie")[0]
        attributes = [part.strip() for part in cookie_line.split(";")]
        self.assertIn("HttpOnly", attributes)
        self.assertIn("SameSite=Lax", attributes)
        self.assertIn("Path=/", attributes)
        self.assertIn("Max-Age=2592000", attributes)
        self.assertIn("Secure", attributes)

    def test_cookie_without_secure_when_cookie_secure_is_0(self):
        with mock.patch.dict(os.environ, {"COOKIE_SECURE": "0"}):
            self.start_server(admin_token=TOKEN, db_name="insecure.db")
        self.assertEqual(self.register().status, 201)
        cookie_line = self.login("anna.petrova@example.com").headers.get_all("Set-Cookie")[0]
        self.assertNotIn("Secure", cookie_line)
        self.assertIn("HttpOnly", cookie_line)

    def test_me_guest_then_user(self):
        self.assertEqual(self.me(), {"ok": True, "user": None})
        token = cookie_from(self.login("anna.petrova@example.com"))
        body = self.me(token)
        self.assertEqual(body["user"]["email"], "anna.petrova@example.com")
        self.assertEqual(self.me("x" * 43), {"ok": True, "user": None})

    def test_session_row_has_only_hash(self):
        token = cookie_from(self.login("anna.petrova@example.com"))
        conn = self.db()
        try:
            stored = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
        finally:
            conn.close()
        self.assertIn(auth.token_hash(token), stored)
        self.assertNotIn(token, stored)

    def test_expired_session_counts_as_guest(self):
        token = cookie_from(self.login("anna.petrova@example.com"))
        self.db_set("UPDATE sessions SET expires_at = ?", (test_api.minutes_ago(1),))
        self.assertEqual(self.me(token), {"ok": True, "user": None})

    def test_logout_removes_session_and_clears_cookie(self):
        token = cookie_from(self.login("anna.petrova@example.com"))
        resp = self.request("POST", "/api/auth/logout", body={},
                            headers={"Cookie": f"zento_session={token}"})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body, {"ok": True})
        cleared = resp.headers.get_all("Set-Cookie")[0]
        self.assertIn("Max-Age=0", cleared)
        self.assertTrue(cleared.startswith("zento_session=;"))
        conn = self.db()
        try:
            stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
        finally:
            conn.close()
        # Сессия из регистрации в setUp остаётся, удалена только та, которой вышли.
        self.assertNotIn(auth.token_hash(token), stored)
        self.assertEqual(self.me(token), {"ok": True, "user": None})

    def test_logout_without_login_is_ok(self):
        resp = self.request("POST", "/api/auth/logout", body={})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, {"ok": True})

    def test_logout_needs_json_content_type(self):
        resp = self.request("POST", "/api/auth/logout", raw=b"", headers={"Content-Type": "text/plain"})
        self.assert_error(resp, 415, "unsupported_media_type")

    def test_session_is_not_admin(self):
        token = cookie_from(self.login("anna.petrova@example.com"))
        cookie_only = self.request("GET", "/api/admin/stats", headers={"Cookie": f"zento_session={token}"})
        self.assertEqual(cookie_only.status, 401)
        as_bearer = self.request("GET", "/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(as_bearer.status, 401)
        self.assertEqual(self.admin("GET", "/api/admin/stats").status, 200)

    def test_admin_token_still_works_as_before(self):
        resp = self.admin("GET", "/api/admin/customers")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.admin("GET", "/api/admin/orders").status, 200)

    def test_login_key_limit_shared_by_phone_and_email(self):
        self.start_server(admin_token=TOKEN, db_name="login-key.db",
                          auth_limits={"login_key": (2, 900), "login_ip": (100, 900)})
        self.assertEqual(self.register().status, 201)
        self.assert_error(self.login("anna.petrova@example.com", "Wrong12345"), 401, "invalid_credentials")
        self.assert_error(self.login("067 123 45 67", "Wrong12345"), 401, "invalid_credentials")
        resp = self.login("anna.petrova@example.com")   # верный пароль, но лимит по логину исчерпан
        self.assert_error(resp, 429, "too_many_attempts")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)

    def test_login_ip_limit(self):
        self.start_server(admin_token=TOKEN, db_name="login-ip.db",
                          auth_limits={"login_key": (100, 900), "login_ip": (2, 900)})
        self.assertEqual(self.register().status, 201)
        self.assert_error(self.login("a@example.com", "Wrong12345"), 401, "invalid_credentials")
        self.assert_error(self.login("b@example.com", "Wrong12345"), 401, "invalid_credentials")
        resp = self.login("anna.petrova@example.com")
        self.assert_error(resp, 429, "too_many_attempts")

    def test_successful_logins_do_not_count_toward_limit(self):
        self.start_server(admin_token=TOKEN, db_name="login-ok.db",
                          auth_limits={"login_key": (2, 900), "login_ip": (2, 900)})
        self.assertEqual(self.register().status, 201)
        for _ in range(4):
            self.assertEqual(self.login("anna.petrova@example.com").status, 200)

    def test_passwords_and_tokens_not_logged(self):
        self.server.quiet = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            token = cookie_from(self.login("anna.petrova@example.com"))
            self.login("anna.petrova@example.com", "Wrong12345")
            self.request("POST", "/api/auth/logout", body={}, headers={"Cookie": f"zento_session={token}"})
        text = out.getvalue() + err.getvalue()
        self.assertNotIn("Secret123", text)
        self.assertNotIn("Wrong12345", text)
        self.assertNotIn(token, text)
        self.assertNotIn("scrypt$", text)


class BotTokenTest(AuthServerBase):
    server_options = {"bot_token": "bot-token-0123456789abcdefghijklmnopq"}
    BOT = "bot-token-0123456789abcdefghijklmnopq"

    def test_bot_token_on_admin_paths_is_403(self):
        for method, path in [("GET", "/api/admin/stats"), ("GET", "/api/admin/orders"),
                             ("POST", "/api/admin/products")]:
            resp = self.admin(method, path, body={} if method == "POST" else None, token=self.BOT)
            self.assertEqual(resp.status, 403, path)

    def test_bot_token_does_not_count_as_failed_admin_login(self):
        for _ in range(app.ADMIN_FAIL_LIMIT + 2):
            self.assertEqual(self.admin("GET", "/api/admin/stats", token=self.BOT).status, 403)
        self.assertEqual(self.admin("GET", "/api/admin/stats").status, 200)

    def test_bot_token_works_on_public_routes(self):
        resp = self.request("GET", "/api/products", headers={"Authorization": f"Bearer {self.BOT}"})
        self.assertEqual(resp.status, 200)

    def test_wrong_token_on_admin_is_still_401(self):
        self.assertEqual(self.admin("GET", "/api/admin/stats", token="nope").status, 401)


class NoBotTokenTest(AuthServerBase):
    def test_without_bot_token_it_is_plain_401(self):
        resp = self.admin("GET", "/api/admin/stats", token="bot-token-0123456789abcdefghijklmnopq")
        self.assertEqual(resp.status, 401)


class SessionLifecycleTest(AuthServerBase):
    def test_many_users_each_get_own_session(self):
        tokens = []
        for index in range(3):
            resp = self.register(email=f"user{index}@example.com", phone=f"+38050100000{index}")
            self.assertEqual(resp.status, 201, resp.body)
            tokens.append(cookie_from(resp))
        self.assertEqual(len(set(tokens)), 3)
        for index, token in enumerate(tokens):
            self.assertEqual(self.me(token)["user"]["email"], f"user{index}@example.com")

    def test_deleted_user_loses_sessions(self):
        token = cookie_from(self.register())
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM users")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.me(token), {"ok": True, "user": None})


if __name__ == "__main__":
    unittest.main()
