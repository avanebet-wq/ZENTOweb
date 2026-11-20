"""Тесты покупателей: ник и почта, вход, сессии, восстановление пароля, почта, миграция, лимиты.

Сервер поднимается на временной базе и временной папке (картинки капчи — фиктивные SVG внутри tempfile).
Реальные файлы сайта, data/store.db и ключи не трогаются. Письма никуда не уходят:
отправку подменяем (mock), а в тестах почты Brevo — urlopen (тоже mock).

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest import mock

import test_api
from test_api import TOKEN, BIG_LIMIT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import auth  # noqa: E402
import app  # noqa: E402
import mailer  # noqa: E402
import store  # noqa: E402

FORM = {
    "nick": "Anna_Petrova",
    "password": "Secret123",
    "password2": "Secret123",
    "agree": True,
    "email": "Anna.Petrova@Example.com",
}
NICK_FORMS = ["anna_petrova", "ANNA_PETROVA", "Anna_Petrova"]
SESSION_RE_PREFIX = "zento_session="
BREVO_KEY = "brevo-test-key-0123456789abcdefABCDEF"
MAIL_SENDER = "zento-sender@example.com"
MAIL_ENV = {"BREVO_API_KEY": BREVO_KEY, "MAIL_FROM": MAIL_SENDER}
NO_MAIL_ENV = {"BREVO_API_KEY": "", "MAIL_FROM": ""}


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
    """Сервер со своей временной папкой (12 фиктивных картинок капчи) и настроенной почтой."""

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
        env = mock.patch.dict(os.environ, MAIL_ENV)
        env.start()
        self.addCleanup(env.stop)
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
            return conn.execute(
                "SELECT id, nick, nick_lower, email, phone, password_hash FROM users ORDER BY id"
            ).fetchall()
        finally:
            conn.close()


# ---------------------------------------------------------------- пароль, ник и токены (без сервера)

class PasswordRulesTest(unittest.TestCase):
    def test_good_passwords_pass(self):
        for password in ["Secret123", "abcdefg1", "Пароль2026", "x" * 100 + "1"]:
            with self.subTest(password=password):
                self.assertIsNone(auth.password_error(password, "anna_k"))

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

    def test_password_must_not_contain_nick_in_any_case(self):
        self.assertIn("ник", auth.password_error("Anna_K2024", "anna_k"))
        self.assertIn("ник", auth.password_error("xxANNA_kxx1", "Anna_K"))
        self.assertIsNone(auth.password_error("Mango2024", "anna_k"))

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

    def test_reset_code_is_six_digits_with_leading_zeros_allowed(self):
        codes = {auth.new_reset_code() for _ in range(200)}
        for code in codes:
            self.assertRegex(code, r"^[0-9]{6}$")


class NickAndLoginFormTest(unittest.TestCase):
    def test_valid_nicks(self):
        for nick in ["Anna", "анна_1", "Іван77", "ЁЖИК_2026", "a_b", "x" * 20]:
            with self.subTest(nick=nick):
                self.assertEqual(store.clean_nick(nick), nick)

    def test_nick_is_stripped_but_inner_spaces_refused(self):
        self.assertEqual(store.clean_nick("  Anna  "), "Anna")
        for bad in ["ab", "x" * 21, "a b", "ник-1", "Anna!", "", "   ", None, 12345]:
            with self.subTest(nick=bad):
                with self.assertRaises(ValueError):
                    store.clean_nick(bad)

    def test_login_identity_by_nick_or_email_only(self):
        self.assertEqual(store.login_identity("Anna_K"), ("nick", "anna_k"))
        self.assertEqual(store.login_identity("Anna.K@Example.COM"), ("email", "anna.k@example.com"))
        self.assertIsNone(store.login_identity("+380671234567"))   # телефон больше не логин
        self.assertIsNone(store.login_identity("hello world"))
        self.assertIsNone(store.login_identity("a@b"))


# ---------------------------------------------------------------- регистрация

class RegisterTest(AuthServerBase):
    def test_success_returns_201_user_and_cookie(self):
        resp = self.register()
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(resp.body, {
            "ok": True,
            "user": {"id": resp.body["user"]["id"], "nick": "Anna_Petrova", "email": "anna.petrova@example.com"},
        })
        self.assertIsNotNone(cookie_from(resp))

    def test_email_is_optional(self):
        resp = self.register(email="")
        self.assertEqual(resp.status, 201, resp.body)
        self.assertIsNone(resp.body["user"]["email"])
        (row,) = self.user_rows()
        self.assertIsNone(row[3])

    def test_without_email_key_at_all(self):
        body = {k: v for k, v in FORM.items() if k != "email"}
        body["captcha_ticket"] = self.get_ticket()
        resp = self.request("POST", "/api/auth/register", body=body)
        self.assertEqual(resp.status, 201, resp.body)

    def test_database_keeps_nick_lower_hash_and_rules_date(self):
        resp = self.register()
        token = cookie_from(resp)
        (row,) = self.user_rows()
        self.assertEqual(row[1], "Anna_Petrova")
        self.assertEqual(row[2], "anna_petrova")
        self.assertEqual(row[3], "anna.petrova@example.com")
        self.assertIsNone(row[4])                       # телефон при регистрации не заполняем
        self.assertTrue(row[5].startswith("scrypt$16384$8$1$"))
        self.assertNotIn("Secret123", row[5])
        conn = self.db()
        try:
            rules = conn.execute("SELECT rules_accepted_at, created_at FROM users").fetchone()
            self.assertIsNotNone(rules[0])
            self.assertEqual(rules[0], rules[1])
            hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
            self.assertEqual(hashes, [auth.token_hash(token)])
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

    def test_phone_is_not_accepted_in_registration(self):
        resp = self.register(phone="+380671234567")
        self.assertEqual(resp.status, 201, resp.body)
        self.assertNotIn("phone", resp.body["user"])

    def test_all_field_errors_come_at_once_and_ticket_survives(self):
        ticket = self.get_ticket()
        resp = self.register(ticket=ticket, nick="A1", email="not-an-email", password="abc", password2="xyz",
                             agree=True)
        self.assert_error(resp, 400, "validation_error")
        self.assertEqual(set(resp.body["fields"]), {"nick", "email", "password", "password2"})
        # Ошибки полей не тратят билет: с тем же билетом регистрация проходит.
        ok = self.register(ticket=ticket)
        self.assertEqual(ok.status, 201, ok.body)

    def test_nick_rules(self):
        ticket = self.get_ticket()
        for nick in ["ab", "x" * 21, "Anna Petrova", "ник-1", "Anna!"]:
            with self.subTest(nick=nick):
                resp = self.register(ticket=ticket, nick=nick)
                self.assert_error(resp, 400, "validation_error")
                self.assertIn("nick", resp.body["fields"])
        self.assertEqual(self.db_count("users"), 0)

    def test_cyrillic_nick_is_accepted(self):
        resp = self.register(nick="Анна_Петрова")
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(resp.body["user"]["nick"], "Анна_Петрова")

    def test_nick_taken_in_any_case_is_409_and_ticket_survives(self):
        self.assertEqual(self.register().status, 201)
        ticket = self.get_ticket()
        resp = self.register(ticket=ticket, nick="ANNA_petrova", email="other@example.com")
        self.assert_error(resp, 409, "nick_taken")
        self.assertEqual(set(resp.body["fields"]), {"nick"})
        ok = self.register(ticket=ticket, nick="Anna_New", email="other@example.com")
        self.assertEqual(ok.status, 201, ok.body)

    def test_email_taken_in_any_case_is_409(self):
        self.assertEqual(self.register().status, 201)
        resp = self.register(nick="Other_Nick", email="ANNA.PETROVA@example.COM")
        self.assert_error(resp, 409, "email_taken")
        self.assertEqual(set(resp.body["fields"]), {"email"})

    def test_both_taken_reports_both_fields(self):
        self.assertEqual(self.register().status, 201)
        resp = self.register(nick="anna_petrova", email="anna.petrova@example.com")
        self.assert_error(resp, 409, "nick_taken")
        self.assertEqual(set(resp.body["fields"]), {"nick", "email"})

    def test_password2_must_match(self):
        resp = self.register(password2="Secret124")
        self.assert_error(resp, 400, "validation_error")
        self.assertEqual(set(resp.body["fields"]), {"password2"})

    def test_weak_passwords_are_field_errors(self):
        ticket = self.get_ticket()
        cases = {
            "short": "Ab1",
            "no_digit": "password",
            "no_letter": "12345678901",
            "common": "qwerty123",
            "contains_nick": "Anna_Petrova1",
        }
        for label, password in cases.items():
            with self.subTest(case=label):
                resp = self.register(ticket=ticket, password=password, password2=password)
                self.assert_error(resp, 400, "validation_error")
                self.assertIn("password", resp.body["fields"])
        self.assertEqual(self.db_count("users"), 0)

    def test_rules_must_be_accepted(self):
        ticket = self.get_ticket()
        resp = self.register(ticket=ticket, agree=False)
        self.assert_error(resp, 400, "rules_not_accepted")
        self.assertEqual(self.db_count("users"), 0)
        # Галочка не тратит билет.
        self.assertEqual(self.register(ticket=ticket).status, 201)

    def test_missing_agree_is_rules_not_accepted(self):
        body = {k: v for k, v in FORM.items() if k != "agree"}
        body["captcha_ticket"] = self.get_ticket()
        resp = self.request("POST", "/api/auth/register", body=body)
        self.assert_error(resp, 400, "rules_not_accepted")

    def test_field_errors_come_before_rules_error(self):
        resp = self.register(agree=False, nick="A")
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("nick", resp.body["fields"])

    def test_old_consent_field_is_not_enough(self):
        body = {k: v for k, v in FORM.items() if k != "agree"}
        body["consent"] = True
        body["captcha_ticket"] = self.get_ticket()
        resp = self.request("POST", "/api/auth/register", body=body)
        self.assert_error(resp, 400, "rules_not_accepted")

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
        resp = self.register(ticket=ticket, nick="Second_User", email="second@example.com")
        self.assert_error(resp, 403, "captcha_required")

    def test_expired_ticket_is_rejected(self):
        ticket = self.get_ticket()
        self.db_set("UPDATE captcha_tickets SET expires_at = ?", (test_api.minutes_ago(1),))
        resp = self.register(ticket=ticket)
        self.assert_error(resp, 403, "captcha_required")
        self.assertEqual(self.db_count("users"), 0)

    def test_captcha_is_checked_before_taken_names(self):
        self.assertEqual(self.register().status, 201)
        resp = self.register(ticket="bad-ticket-0000000000000000000000000000000", nick="anna_petrova")
        self.assert_error(resp, 403, "captcha_required")

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
        self.assertEqual(self.register(nick="user_one", email="a1@example.com").status, 201)
        self.assertEqual(self.register(nick="user_two", email="a2@example.com").status, 201)
        resp = self.register(nick="user_three", email="a3@example.com")
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
        self.assertEqual(resp.body["user"], {
            "id": resp.body["user"]["id"], "nick": "Anna_Petrova", "email": "anna.petrova@example.com",
        })
        self.assertIsNotNone(cookie_from(resp))

    def test_login_by_nick_in_any_case(self):
        for nick in NICK_FORMS:
            with self.subTest(nick=nick):
                resp = self.login(nick)
                self.assertEqual(resp.status, 200, resp.body)
                self.assertEqual(resp.body["user"]["nick"], "Anna_Petrova")

    def test_login_by_phone_is_no_longer_accepted(self):
        resp = self.login("+380 67 123 45 67")
        self.assert_error(resp, 401, "invalid_credentials")
        self.assertEqual(resp.body, self.login("nobody_here_123").body)

    def test_wrong_password_is_401(self):
        resp = self.login("anna.petrova@example.com", "Wrong12345")
        self.assert_error(resp, 401, "invalid_credentials")
        self.assertEqual(resp.body["message"], "Неверный логин или пароль")

    def test_wrong_password_and_unknown_login_give_same_answer(self):
        wrong_password = self.login("anna.petrova@example.com", "Wrong12345").body
        wrong_nick_password = self.login("Anna_Petrova", "Wrong12345").body
        unknown_email = self.login("nobody@example.com").body
        unknown_nick = self.login("ghost_user").body
        malformed = self.login("+7 912 345 67 89").body
        self.assertEqual(wrong_password, wrong_nick_password)
        self.assertEqual(wrong_password, unknown_email)
        self.assertEqual(wrong_password, unknown_nick)
        self.assertEqual(wrong_password, malformed)

    def test_unrecognized_login_format_is_401_not_400(self):
        self.assert_error(self.login("hello world"), 401, "invalid_credentials")

    def test_missing_fields_are_validation_errors(self):
        resp = self.request("POST", "/api/auth/login", body={"password": "x"})
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("login", resp.body["fields"])
        resp = self.request("POST", "/api/auth/login", body={"login": "Anna_Petrova", "password": ""})
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
        cookie_line = self.login("Anna_Petrova").headers.get_all("Set-Cookie")[0]
        self.assertNotIn("Secure", cookie_line)
        self.assertIn("HttpOnly", cookie_line)

    def test_me_guest_then_user(self):
        self.assertEqual(self.me(), {"ok": True, "user": None})
        token = cookie_from(self.login("anna.petrova@example.com"))
        body = self.me(token)
        self.assertEqual(set(body["user"]), {"id", "nick", "email"})
        self.assertEqual(body["user"]["nick"], "Anna_Petrova")
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
        self.assertEqual(self.admin("GET", "/api/admin/customers").status, 200)
        self.assertEqual(self.admin("GET", "/api/admin/orders").status, 200)

    def test_login_key_limit_shared_by_nick_and_email(self):
        self.start_server(admin_token=TOKEN, db_name="login-key.db",
                          auth_limits={"login_key": (2, 900), "login_ip": (100, 900)})
        self.assertEqual(self.register().status, 201)
        self.assert_error(self.login("anna.petrova@example.com", "Wrong12345"), 401, "invalid_credentials")
        self.assert_error(self.login("ANNA_petrova", "Wrong12345"), 401, "invalid_credentials")
        resp = self.login("anna.petrova@example.com")   # верный пароль, но лимит по аккаунту исчерпан
        self.assert_error(resp, 429, "too_many_attempts")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)

    def test_login_ip_limit(self):
        self.start_server(admin_token=TOKEN, db_name="login-ip.db",
                          auth_limits={"login_key": (100, 900), "login_ip": (2, 900)})
        self.assertEqual(self.register().status, 201)
        self.assert_error(self.login("a@example.com", "Wrong12345"), 401, "invalid_credentials")
        self.assert_error(self.login("someone_else", "Wrong12345"), 401, "invalid_credentials")
        self.assert_error(self.login("Anna_Petrova"), 429, "too_many_attempts")

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
            resp = self.register(nick=f"user_{index}", email=f"user{index}@example.com")
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


# ---------------------------------------------------------------- почта Brevo (без сети)

class FakeResponse:
    def __init__(self, status=201):
        self.status = status

    def read(self):
        return b'{"messageId": "fake"}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MailerTest(unittest.TestCase):
    def test_configured_only_with_key_and_sender(self):
        with mock.patch.dict(os.environ, NO_MAIL_ENV):
            self.assertFalse(mailer.is_configured())
        with mock.patch.dict(os.environ, {"BREVO_API_KEY": BREVO_KEY, "MAIL_FROM": ""}):
            self.assertFalse(mailer.is_configured())
        with mock.patch.dict(os.environ, {"BREVO_API_KEY": "", "MAIL_FROM": MAIL_SENDER}):
            self.assertFalse(mailer.is_configured())
        with mock.patch.dict(os.environ, MAIL_ENV):
            self.assertTrue(mailer.is_configured())

    def test_request_to_brevo_has_key_header_and_sender(self):
        env = {**MAIL_ENV}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("MAIL_FROM_NAME", None)
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(201)) as opener:
                mailer.send_reset_code("anna@example.com", "123456")
        request = opener.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Api-key"), BREVO_KEY)
        self.assertEqual(opener.call_args.kwargs["timeout"], mailer.TIMEOUT_SECONDS)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["sender"], {"name": "ZENTO", "email": MAIL_SENDER})
        self.assertEqual(body["to"], [{"email": "anna@example.com"}])
        self.assertEqual(body["subject"], "Код для восстановления пароля ZENTO")
        self.assertIn("Ваш код: 123456.", body["textContent"])
        self.assertIn("10 минут", body["textContent"])
        self.assertNotIn("http", body["textContent"])

    def test_sender_name_from_env(self):
        with mock.patch.dict(os.environ, {**MAIL_ENV, "MAIL_FROM_NAME": "Магазин ZENTO"}):
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as opener:
                mailer.send_reset_code("anna@example.com", "654321")
        body = json.loads(opener.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["sender"]["name"], "Магазин ZENTO")

    def test_no_key_means_mail_error_without_any_request(self):
        with mock.patch.dict(os.environ, NO_MAIL_ENV):
            with mock.patch("urllib.request.urlopen") as opener:
                with self.assertRaises(mailer.MailError):
                    mailer.send_reset_code("anna@example.com", "123456")
        opener.assert_not_called()

    def test_errors_never_contain_key_or_address(self):
        failures = [
            urllib.error.HTTPError("https://api.brevo.com/v3/smtp/email", 401, "Unauthorized", {}, None),
            urllib.error.URLError("connection refused"),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.dict(os.environ, MAIL_ENV):
                    with mock.patch("urllib.request.urlopen", side_effect=failure):
                        with self.assertRaises(mailer.MailError) as caught:
                            mailer.send_reset_code("anna@example.com", "123456")
                text = str(caught.exception)
                self.assertNotIn(BREVO_KEY, text)
                self.assertNotIn("anna@example.com", text)
                self.assertNotIn("123456", text)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)


# ---------------------------------------------------------------- восстановление пароля

class ResetTest(AuthServerBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.register().status, 201)
        self.send_patcher = mock.patch("mailer.send_reset_code")
        self.send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)

    # --- помощники
    def ask(self, login):
        return self.request("POST", "/api/auth/reset/request", body={"login": login})

    def confirm(self, login, code, password="Newpass456", password2=None):
        body = {"login": login, "code": code, "password": password,
                "password2": password if password2 is None else password2}
        return self.request("POST", "/api/auth/reset/confirm", body=body)

    def sent_code(self):
        return self.send.call_args[0][1]

    def reset_rows(self):
        conn = self.db()
        try:
            return conn.execute("SELECT user_id, code_hash, attempts, used_at FROM reset_codes").fetchall()
        finally:
            conn.close()

    def login_works(self, login, password):
        return self.login(login, password).status == 200

    # --- запрос кода
    def test_unknown_accounts_get_the_same_answer_and_no_mail(self):
        answers = [self.ask("nobody_here_1").body, self.ask("nobody@example.com").body,
                   self.ask("hello world").body, self.ask("+380671234567").body]
        for answer in answers:
            self.assertEqual(answer, {"ok": True, "message": "Если аккаунт найден, код отправлен на почту"})
        self.assertEqual(self.ask("nobody_here_1").status, 200)
        self.send.assert_not_called()

    def test_existing_account_gets_same_answer_and_mail_with_code(self):
        found = self.ask("Anna_Petrova")
        self.assertEqual(found.status, 200)
        self.assertEqual(found.body, self.ask("nobody_here_1").body)
        self.send.assert_called_once()
        to, code = self.send.call_args[0]
        self.assertEqual(to, "anna.petrova@example.com")
        self.assertRegex(code, r"^[0-9]{6}$")

    def test_lookup_by_email_in_any_case_sends_mail(self):
        self.assertEqual(self.ask("ANNA.Petrova@Example.com").status, 200)
        self.send.assert_called_once()

    def test_code_is_stored_only_as_hash(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        (row,) = self.reset_rows()
        self.assertNotEqual(row[1], code)
        self.assertTrue(row[1].startswith("scrypt$"))
        self.assertNotIn(code, row[1])
        resp = self.ask("anna_petrova")
        self.assertNotIn(code.encode(), resp.raw)

    def test_account_without_email_gets_same_answer_and_no_mail(self):
        self.assertEqual(self.register(nick="No_Mail_User", email=None).status, 201)
        self.assertEqual(self.ask("No_Mail_User").body, self.ask("nobody_here_1").body)
        self.send.assert_not_called()

    def test_second_request_within_a_minute_sends_nothing(self):
        self.ask("anna_petrova")
        first_code = self.sent_code()
        again = self.ask("anna_petrova")
        self.assertEqual(again.status, 200)
        self.assertEqual(again.body["message"], "Если аккаунт найден, код отправлен на почту")
        self.assertEqual(self.send.call_count, 1)
        # Первый код остаётся действующим.
        self.assertEqual(self.confirm("anna_petrova", first_code).status, 200)

    def test_request_after_a_minute_sends_new_code_and_old_stops_working(self):
        self.ask("anna_petrova")
        old_code = self.sent_code()
        self.db_set("UPDATE reset_codes SET created_at = ?", (test_api.minutes_ago(2),))
        self.ask("anna_petrova")
        self.assertEqual(self.send.call_count, 2)
        new_code = self.sent_code()
        if new_code != old_code:
            self.assert_error(self.confirm("anna_petrova", old_code), 400, "code_invalid")
        self.assertEqual(self.confirm("anna_petrova", new_code).status, 200)

    def test_missing_login_is_validation_error(self):
        resp = self.request("POST", "/api/auth/reset/request", body={})
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("login", resp.body["fields"])

    def test_request_needs_json(self):
        resp = self.request("POST", "/api/auth/reset/request", raw=b"login=x",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assert_error(resp, 415, "unsupported_media_type")

    def test_request_limit_returns_429(self):
        self.start_server(admin_token=TOKEN, db_name="reset-limit.db", auth_limits={"reset_request": (2, 900)})
        self.assertEqual(self.ask("anna_petrova").status, 200)
        self.assertEqual(self.ask("nobody_here_1").status, 200)
        resp = self.ask("anna_petrova")
        self.assert_error(resp, 429, "too_many_requests")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)

    def test_default_reset_limit(self):
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["reset_request"], (5, 900))

    def test_mail_not_configured_is_503_for_everyone(self):
        with mock.patch.dict(os.environ, NO_MAIL_ENV):
            existing = self.ask("anna_petrova")
            unknown = self.ask("nobody_here_1")
        self.assert_error(existing, 503, "mail_failed")
        self.assertEqual(existing.body, unknown.body)
        self.send.assert_not_called()
        self.assertEqual(self.reset_rows(), [])

    def test_brevo_failure_answers_like_unknown_account(self):
        # Сбой Brevo не выдаёт, что аккаунт есть: ответ тот же, что для несуществующего логина.
        self.send.side_effect = mailer.MailError("почта недоступна")
        failed = self.ask("anna_petrova")
        self.assertEqual(failed.status, 200)
        self.assertEqual(failed.body, self.ask("nobody_here_1").body)
        self.assertEqual(self.reset_rows(), [])          # неотправленный код гасится
        self.send.side_effect = None
        self.assertEqual(self.ask("anna_petrova").status, 200)
        self.assertEqual(self.send.call_count, 2)        # повторить можно сразу

    def test_brevo_failure_gives_same_answer_for_every_login(self):
        self.send.side_effect = mailer.MailError("почта недоступна")
        answers = {self.ask(login).raw for login in ["anna_petrova", "anna.petrova@example.com",
                                                     "nobody_here_1", "nobody@example.com", "hello world"]}
        self.assertEqual(len(answers), 1)

    def test_brevo_failure_does_not_leak_key_or_address_into_answer_or_logs(self):
        # Здесь настоящая отправка: мок снимаем и подменяем только сетевой вызов Brevo.
        self.send_patcher.stop()
        out, err = io.StringIO(), io.StringIO()
        failing = urllib.error.HTTPError("https://api.brevo.com/v3/smtp/email", 401, "Unauthorized", {}, None)
        with mock.patch.dict(os.environ, MAIL_ENV), mock.patch("urllib.request.urlopen", side_effect=failing):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                resp = self.ask("anna_petrova")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, self.ask("nobody_here_1").body)
        text = resp.raw.decode("utf-8") + out.getvalue() + err.getvalue()
        self.assertNotIn(BREVO_KEY, text)
        self.assertNotIn("anna.petrova@example.com", text)
        self.assertIn("MailError", err.getvalue())       # в лог попадает только тип ошибки

    # --- подтверждение
    def test_correct_code_sets_new_password_and_logs_in(self):
        self.ask("anna_petrova")
        resp = self.confirm("anna_petrova", self.sent_code())
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["user"]["nick"], "Anna_Petrova")
        self.assertIsNotNone(cookie_from(resp))
        self.assertTrue(self.login_works("Anna_Petrova", "Newpass456"))
        self.assertFalse(self.login_works("Anna_Petrova", "Secret123"))

    def test_login_after_reset_by_email_works_too(self):
        self.ask("anna_petrova")
        self.assertEqual(self.confirm("anna.petrova@example.com", self.sent_code()).status, 200)
        self.assertTrue(self.login_works("anna.petrova@example.com", "Newpass456"))

    def test_used_code_cannot_be_reused(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        self.assertEqual(self.confirm("anna_petrova", code).status, 200)
        self.assert_error(self.confirm("anna_petrova", code, password="Another789"), 400, "code_invalid")

    def test_wrong_code_is_code_invalid(self):
        self.ask("anna_petrova")
        wrong = "000000" if self.sent_code() != "000000" else "111111"
        resp = self.confirm("anna_petrova", wrong)
        self.assert_error(resp, 400, "code_invalid")
        self.assertEqual(self.reset_rows()[0][2], 1)

    def test_unknown_login_gives_same_answer_as_wrong_code(self):
        self.ask("anna_petrova")
        wrong = "000000" if self.sent_code() != "000000" else "111111"
        self.assertEqual(self.confirm("nobody_here_1", wrong).body, self.confirm("anna_petrova", wrong).body)

    def test_expired_code_is_code_expired(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        self.db_set("UPDATE reset_codes SET expires_at = ?", (test_api.minutes_ago(1),))
        self.assert_error(self.confirm("anna_petrova", code), 400, "code_expired")
        self.assertFalse(self.login_works("Anna_Petrova", "Newpass456"))

    def test_code_lives_ten_minutes(self):
        self.ask("anna_petrova")
        (row,) = self.reset_rows()
        conn = self.db()
        try:
            created, expires = conn.execute("SELECT created_at, expires_at FROM reset_codes").fetchone()
        finally:
            conn.close()
        self.assertEqual(store.parse_iso(expires) - store.parse_iso(created), timedelta(minutes=10))

    def test_five_wrong_tries_burn_the_code(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assert_error(self.confirm("anna_petrova", wrong), 400, "code_invalid")
        # Шестая попытка — даже с правильным кодом — отказ, и код сгорает.
        self.assert_error(self.confirm("anna_petrova", code), 429, "code_attempts_exceeded")
        self.assert_error(self.confirm("anna_petrova", code), 400, "code_invalid")
        self.assertFalse(self.login_works("Anna_Petrova", "Newpass456"))
        self.assertTrue(self.login_works("Anna_Petrova", "Secret123"))

    def test_new_code_after_burn_works(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(6):
            self.confirm("anna_petrova", wrong)
        self.db_set("UPDATE reset_codes SET created_at = ?", (test_api.minutes_ago(2),))
        self.ask("anna_petrova")
        self.assertEqual(self.confirm("anna_petrova", self.sent_code()).status, 200)

    def test_five_wrong_tries_counted_even_with_good_password_form(self):
        self.ask("anna_petrova")
        wrong = "000000" if self.sent_code() != "000000" else "111111"
        for _ in range(5):
            self.confirm("anna_petrova", wrong, password="Newpass456")
        resp = self.confirm("anna_petrova", self.sent_code(), password="Newpass456")
        self.assert_error(resp, 429, "code_attempts_exceeded")

    def test_bad_password_does_not_burn_the_code(self):
        self.ask("anna_petrova")
        code = self.sent_code()
        resp = self.confirm("anna_petrova", code, password="Anna_Petrova1")
        self.assert_error(resp, 400, "validation_error")
        self.assertIn("password", resp.body["fields"])
        resp = self.confirm("anna_petrova", code, password="Newpass456", password2="Other4567")
        self.assert_error(resp, 400, "validation_error")
        self.assertEqual(set(resp.body["fields"]), {"password2"})
        self.assertEqual(self.reset_rows()[0][2], 0)
        self.assertEqual(self.confirm("anna_petrova", code).status, 200)

    def test_bad_code_shape_is_validation_error_and_not_counted(self):
        self.ask("anna_petrova")
        for bad in ["12345", "abcdef", "1234567", "", None]:
            with self.subTest(code=bad):
                body = {"login": "anna_petrova", "code": bad, "password": "Newpass456", "password2": "Newpass456"}
                resp = self.request("POST", "/api/auth/reset/confirm", body=body)
                self.assert_error(resp, 400, "validation_error")
                self.assertIn("code", resp.body["fields"])
        self.assertEqual(self.reset_rows()[0][2], 0)

    def test_old_sessions_are_removed_after_reset(self):
        old_token = cookie_from(self.login("Anna_Petrova"))
        other_token = cookie_from(self.login("anna.petrova@example.com"))
        self.assertIsNotNone(old_token)
        self.ask("anna_petrova")
        self.assertEqual(self.confirm("anna_petrova", self.sent_code()).status, 200)
        self.assertEqual(self.me(old_token), {"ok": True, "user": None})
        self.assertEqual(self.me(other_token), {"ok": True, "user": None})
        conn = self.db()
        try:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)   # осталась только сессия, открытая после смены пароля

    def test_new_session_after_reset_is_valid(self):
        self.ask("anna_petrova")
        resp = self.confirm("anna_petrova", self.sent_code())
        token = cookie_from(resp)
        self.assertEqual(self.me(token)["user"]["nick"], "Anna_Petrova")

    def test_confirm_needs_json(self):
        resp = self.request("POST", "/api/auth/reset/confirm", raw=b"x", headers={"Content-Type": "text/plain"})
        self.assert_error(resp, 415, "unsupported_media_type")


# ---------------------------------------------------------------- миграция старой таблицы users

OLD_USERS_SQL = """CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
)"""
SESSIONS_SQL = """CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)"""


class UsersMigrationTest(unittest.TestCase):
    """Старая база (name, phone, email обязательны) -> новая схема. Тест на отдельной временной базе."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-migr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "old.db"
        self.hash_one = auth.hash_password("Secret123")
        self.hash_two = auth.hash_password("Other4567")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(OLD_USERS_SQL)
            conn.execute(SESSIONS_SQL)
            conn.execute("INSERT INTO users (id, name, phone, email, password_hash, created_at) VALUES "
                         "(1, 'Анна Петрова', '+380671234567', 'anna@example.com', ?, '2026-01-05T10:00:00.000001Z')",
                         (self.hash_one,))
            conn.execute("INSERT INTO users (id, name, phone, email, password_hash, created_at) VALUES "
                         "(2, 'Олег', '+380501112233', 'oleg@example.com', ?, '2026-02-07T12:30:00.000002Z')",
                         (self.hash_two,))
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES "
                         "('hash-of-oleg-session', 2, '2026-02-07T12:31:00.000000Z', '2099-01-01T00:00:00.000000Z')")
            conn.commit()
        finally:
            conn.close()

    def snapshot(self):
        conn = store.connect(self.db_path)
        try:
            users = [tuple(r) for r in conn.execute(
                "SELECT id, nick, nick_lower, email, phone, password_hash, created_at, rules_accepted_at"
                " FROM users ORDER BY id")]
            sessions = [tuple(r) for r in conn.execute(
                "SELECT token_hash, user_id, expires_at FROM sessions ORDER BY token_hash")]
            columns = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
            return users, sessions, columns
        finally:
            conn.close()

    def test_old_rows_are_kept_with_nick_from_id(self):
        store.init_db(self.db_path, products_path=None)
        users, sessions, columns = self.snapshot()
        self.assertEqual(users, [
            (1, "user1", "user1", "anna@example.com", "+380671234567", self.hash_one,
             "2026-01-05T10:00:00.000001Z", "2026-01-05T10:00:00.000001Z"),
            (2, "user2", "user2", "oleg@example.com", "+380501112233", self.hash_two,
             "2026-02-07T12:30:00.000002Z", "2026-02-07T12:30:00.000002Z"),
        ])
        self.assertEqual(sessions, [("hash-of-oleg-session", 2, "2099-01-01T00:00:00.000000Z")])
        self.assertEqual(set(columns), {"id", "nick", "nick_lower", "email", "password_hash", "phone",
                                        "created_at", "rules_accepted_at", "bio", "nick_changed_at",
                                        "public_id", "xp"})

    def test_second_and_third_runs_change_nothing(self):
        store.init_db(self.db_path, products_path=None)
        first = self.snapshot()
        store.init_db(self.db_path, products_path=None)
        store.init_db(self.db_path, products_path=None)
        self.assertEqual(self.snapshot(), first)

    def test_migrated_database_works_for_login_and_sessions(self):
        store.init_db(self.db_path, products_path=None)
        conn = store.connect(self.db_path)
        try:
            user = store.find_user_by_login(conn, "USER1")
            self.assertEqual(user["id"], 1)
            self.assertTrue(auth.verify_password("Secret123", user["password_hash"]))
            by_email = store.find_user_by_login(conn, "Anna@Example.com")
            self.assertEqual(by_email["nick"], "user1")
            self.assertIsNone(store.find_user_by_login(conn, "+380671234567"))
            session_user = store.user_for_session(conn, "hash-of-oleg-session", store.utc_now())
            self.assertEqual(session_user, {"id": 2, "nick": "user2", "email": "oleg@example.com"})
            self.assertEqual(conn.execute("PRAGMA foreign_key_check(sessions)").fetchall(), [])
        finally:
            conn.close()

    def test_fresh_database_has_new_schema_from_start(self):
        fresh = self.tmp / "fresh.db"
        store.init_db(fresh, products_path=None)
        store.init_db(fresh, products_path=None)
        conn = sqlite3.connect(fresh)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertIn("nick_lower", columns)
        self.assertIn("reset_codes", tables)
        self.assertNotIn("users_new", tables)

    def test_migration_leaves_no_temporary_table(self):
        store.init_db(self.db_path, products_path=None)
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertNotIn("users_new", tables)
        self.assertIn("reset_codes", tables)


class MigratedServerTest(unittest.TestCase):
    """После миграции сервер со старой базой отвечает как новый: вход по нику."""

    def test_old_user_logs_in_by_nick_after_migration(self):
        tmp = Path(tempfile.mkdtemp(prefix="zento-migr-server-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        products = tmp / "products.json"
        products.write_text(json.dumps(test_api.PRODUCTS, ensure_ascii=False), encoding="utf-8")
        db_path = tmp / "old.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(OLD_USERS_SQL)
            conn.execute(SESSIONS_SQL)
            conn.execute("INSERT INTO users (id, name, phone, email, password_hash, created_at) VALUES "
                         "(1, 'Анна', '+380671234567', 'anna@example.com', ?, '2026-01-05T10:00:00.000001Z')",
                         (auth.hash_password("Secret123"),))
            conn.commit()
        finally:
            conn.close()
        server = app.create_server("127.0.0.1", 0, db_path=db_path, products_path=products,
                                   admin_token=TOKEN, static_root=tmp, quiet=True, cookie_secure=True)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        self.addCleanup(client.close)
        client.request("POST", "/api/auth/login", body=json.dumps({"login": "user1", "password": "Secret123"}),
                       headers={"Content-Type": "application/json"})
        resp = client.getresponse()
        body = json.loads(resp.read())
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["user"], {"id": 1, "nick": "user1", "email": "anna@example.com"})


if __name__ == "__main__":
    unittest.main()
