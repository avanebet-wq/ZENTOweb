"""Тесты профиля покупателя: описание, ник с кулдауном и резервом, публичная страница, миграция.

Покупатели создаются прямо в базе временного сервера (пароль Secret123), вход — по cookie-сессии.
Картинки и их проверка — в test_images.py.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from test_api import TOKEN, SHOP_BOT
from test_images import ProfileServerBase, make_ticket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import auth  # noqa: E402
import store  # noqa: E402

PASSWORD = "Secret123"
OWN_KEYS = {"id", "nick", "email", "bio", "has_avatar", "has_banner", "avatar_v", "banner_v",
            "nick_next_change_at", "public_id", "created_at", "achievements", "xp", "rank"}
PUBLIC_KEYS = {"nick", "bio", "has_avatar", "has_banner", "avatar_v", "banner_v"}


class ProfileTestBase(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.anna = self.make_user("Anna", email="anna@example.com", bio="Старое описание")
        self.anna_cookie = self.session_for(self.anna)

    def patch(self, body, headers=None):
        return self.request("PATCH", "/api/profile", body=body,
                            headers=self.anna_cookie if headers is None else headers)

    def row(self, user_id):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT nick, nick_lower, bio, nick_changed_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()

    def set_db(self, sql, params=()):
        self.db_set(sql, params)

    def register(self, nick):
        """Регистрация через настоящий маршрут (с билетом капчи вместо картинок)."""
        conn = sqlite3.connect(self.db_path)
        try:
            ticket = make_ticket(conn)
            conn.commit()
        finally:
            conn.close()
        body = {"nick": nick, "password": PASSWORD, "password2": PASSWORD, "agree": True,
                "captcha_ticket": ticket}
        return self.request("POST", "/api/auth/register", body=body)


# ---------------------------------------------------------------- доступ

class ProfileAccessTest(ProfileTestBase):
    def test_me_needs_session_and_bot_or_admin_token_is_refused(self):
        for headers in ({}, {"Authorization": f"Bearer {SHOP_BOT}"}, {"Authorization": f"Bearer {TOKEN}"}):
            resp = self.request("GET", "/api/profile/me", headers=headers)
            self.assertEqual(resp.status, 401, resp.body)
            self.assertEqual(resp.body["error"], "login_required")
            self.assertNotIn("user", resp.body)

    def test_patch_needs_session_and_bot_token_changes_nothing(self):
        for headers in ({}, {"Authorization": f"Bearer {SHOP_BOT}"}, {"Authorization": f"Bearer {TOKEN}"}):
            resp = self.patch({"bio": "взлом", "nick": "hacker"}, headers=headers)
            self.assertEqual(resp.status, 401, resp.body)
        self.assertEqual(self.row(self.anna)[:3], ("Anna", "anna", "Старое описание"))

    def test_put_and_delete_need_session(self):
        resp = self.request("DELETE", "/api/profile/banner", headers={"Authorization": f"Bearer {SHOP_BOT}"})
        self.assertEqual(resp.status, 401)
        resp = self.request("DELETE", "/api/profile/avatar")
        self.assertEqual(resp.status, 401)

    def test_profile_answers_are_not_cached(self):
        resp = self.request("GET", "/api/profile/me", headers=self.anna_cookie)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        resp = self.request("GET", "/api/users/anna/profile", headers=self.anna_cookie)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")


# ---------------------------------------------------------------- своя страница и публичный профиль

class ProfileReadTest(ProfileTestBase):
    def test_me_returns_own_fields(self):
        resp = self.request("GET", "/api/profile/me", headers=self.anna_cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["ok"], True)
        self.assertEqual(set(resp.body["user"]), OWN_KEYS)
        user = resp.body["user"]
        self.assertEqual((user["id"], user["nick"], user["email"], user["bio"]),
                         (self.anna, "Anna", "anna@example.com", "Старое описание"))
        self.assertFalse(user["has_avatar"])
        self.assertIsNone(user["avatar_v"])
        self.assertIsNone(user["nick_next_change_at"])

    def test_public_profile_hides_email_and_id_from_logged_in(self):
        resp = self.request("GET", "/api/users/ANNA/profile", headers=self.anna_cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(set(resp.body["user"]), PUBLIC_KEYS)
        self.assertEqual(resp.body["user"]["nick"], "Anna")
        self.assertEqual(resp.body["user"]["bio"], "Старое описание")
        self.assertNotIn("anna@example.com", json.dumps(resp.body))

    def test_public_profile_needs_session_for_guest_bot_and_admin(self):
        for headers in ({}, {"Authorization": f"Bearer {SHOP_BOT}"}, {"Authorization": f"Bearer {TOKEN}"}):
            resp = self.request("GET", "/api/users/anna/profile", headers=headers)
            self.assertEqual(resp.status, 401, resp.body)
            self.assertEqual(resp.body["error"], "login_required")
            self.assertNotIn("user", resp.body)

    def test_public_profile_unknown_nick_is_404(self):
        resp = self.request("GET", "/api/users/nobodyhere/profile", headers=self.anna_cookie)
        self.assertEqual(resp.status, 404)

    def test_public_profile_flags_follow_uploads(self):
        from test_images import png_bytes
        self.upload("banner", png_bytes(40, 20), "image/png", self.anna_cookie)
        user = self.request("GET", "/api/users/anna/profile", headers=self.anna_cookie).body["user"]
        self.assertFalse(user["has_avatar"])
        self.assertTrue(user["has_banner"])
        self.assertEqual(len(user["banner_v"]), 16)


# ---------------------------------------------------------------- описание

class BioTest(ProfileTestBase):
    def test_saved_trimmed_nfc_with_newlines(self):
        resp = self.patch({"bio": "  Привет\r\n\r\nCafé мир  "})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["user"]["bio"], "Привет\n\nCafé мир")
        self.assertEqual(self.row(self.anna)[2], "Привет\n\nCafé мир")

    def test_length_300_ok_301_rejected(self):
        self.assertEqual(self.patch({"bio": "я" * 300}).status, 200)
        resp = self.patch({"bio": "я" * 301})
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.body["error"], "validation_error")
        self.assertEqual(list(resp.body["fields"]), ["bio"])

    def test_angle_brackets_control_and_invisible_rejected(self):
        for bad in ("<b>жирный</b>", "a>b", "строка\x07звонок", "табуляция\tздесь",
                    "без​шири", "разворот‮текста", "фраза⁦", "бом﻿ок", "ок\x85ок"):
            resp = self.patch({"bio": bad})
            self.assertEqual(resp.status, 400, bad)
            self.assertEqual(list(resp.body["fields"]), ["bio"], bad)
        self.assertEqual(self.row(self.anna)[2], "Старое описание")

    def test_newlines_up_to_five(self):
        self.assertEqual(self.patch({"bio": "\n".join(["строка"] * 7)}).status, 400)
        self.assertEqual(self.patch({"bio": "\n".join(["строка"] * 6)}).status, 200)

    def test_not_a_string_rejected_and_empty_clears(self):
        self.assertEqual(self.patch({"bio": 123}).status, 400)
        resp = self.patch({"bio": "   "})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["user"]["bio"], "")

    def test_unknown_field_rejected(self):
        resp = self.patch({"email": "other@example.com"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(list(resp.body["fields"]), ["email"])

    def test_body_must_be_json(self):
        resp = self.request("PATCH", "/api/profile", raw=b"bio=text",
                            headers={**self.anna_cookie, "Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415, resp.body)
        self.assertEqual(resp.body["error"], "unsupported_media_type")


# ---------------------------------------------------------------- ник

class NickChangeTest(ProfileTestBase):
    def test_case_only_change_has_no_cooldown_and_keeps_timer(self):
        self.assertEqual(self.patch({"nick": "ANNA"}).status, 200)
        self.assertEqual(self.patch({"nick": "anna"}).status, 200)
        resp = self.patch({"nick": "Anna"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body["user"]["nick"], "Anna")
        self.assertIsNone(resp.body["user"]["nick_next_change_at"])
        self.assertIsNone(self.row(self.anna)[3])

    def test_first_change_is_free_second_waits_30_days(self):
        first = self.patch({"nick": "AnnaOne"})
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual(first.body["user"]["nick"], "AnnaOne")
        next_at = first.body["user"]["nick_next_change_at"]
        self.assertRegex(next_at, r"^\d{4}-\d{2}-\d{2}T.*Z$")

        second = self.patch({"nick": "AnnaTwo"})
        self.assertEqual(second.status, 409, second.body)
        self.assertEqual(second.body["error"], "nick_cooldown")
        self.assertRegex(second.body["fields"]["nick"], r"\d{2}\.\d{2}\.\d{4}")
        self.assertEqual(self.row(self.anna)[0], "AnnaOne")

    def test_change_allowed_after_30_days(self):
        self.assertEqual(self.patch({"nick": "AnnaOne"}).status, 200)
        self.set_db("UPDATE users SET nick_changed_at = ? WHERE id = ?",
                    (store.to_iso(store.utc_now() - timedelta(days=31)), self.anna))
        resp = self.patch({"nick": "AnnaTwo"})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["user"]["nick"], "AnnaTwo")

    def test_nick_changed_at_is_set_on_real_change(self):
        self.patch({"nick": "AnnaOne"})
        changed = self.row(self.anna)[3]
        self.assertIsNotNone(changed)
        self.assertTrue(changed.endswith("Z"))

    def test_cooldown_blocks_bio_too(self):
        self.patch({"nick": "AnnaOne"})
        resp = self.patch({"bio": "новое описание", "nick": "AnnaTwo"})
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.row(self.anna)[2], "Старое описание")

    def test_login_follows_the_new_nick(self):
        self.patch({"nick": "AnnaOne"})
        ok = self.request("POST", "/api/auth/login", body={"login": "annaone", "password": PASSWORD})
        self.assertEqual(ok.status, 200, ok.body)
        old = self.request("POST", "/api/auth/login", body={"login": "anna", "password": PASSWORD})
        self.assertEqual(old.status, 401)

    def test_nick_format_errors_are_nick_invalid(self):
        for bad in ("ab", "слишком_длинный_ник_для_сайта", "with space", "ник-с-дефисом"):
            resp = self.patch({"nick": bad})
            self.assertEqual(resp.status, 400, bad)
            self.assertEqual(resp.body["error"], "nick_invalid", bad)
            self.assertEqual(list(resp.body["fields"]), ["nick"])

    def test_only_english_letters_and_digits_allowed(self):
        for bad in ("аnna", "Анна_1", "Anna_1", "Anna!", "Anna-2"):
            resp = self.patch({"nick": bad})
            self.assertEqual(resp.status, 400, resp.body)
            self.assertEqual(resp.body["error"], "nick_invalid")
        self.assertEqual(self.patch({"nick": "Anna77"}).status, 200)

    def test_reserved_words_rejected_in_any_case(self):
        for bad in ("admin", "Zento", "SUPPORT", "bot", "administrator", "root", "Moderator"):
            resp = self.patch({"nick": bad})
            self.assertEqual(resp.status, 400, bad)
            self.assertEqual(resp.body["error"], "nick_invalid", bad)

    def test_bio_and_nick_errors_together_report_nick_code(self):
        resp = self.patch({"bio": "<", "nick": "ab"})
        self.assertEqual(resp.body["error"], "nick_invalid")
        self.assertEqual(set(resp.body["fields"]), {"bio", "nick"})
        self.assertEqual(self.row(self.anna)[2], "Старое описание")

    def test_taken_nick_is_refused(self):
        self.make_user("Olga")
        resp = self.patch({"nick": "olga"})
        self.assertEqual(resp.status, 409, resp.body)
        self.assertEqual(resp.body["error"], "nick_taken")


class NickReservationTest(ProfileTestBase):
    def setUp(self):
        super().setUp()
        self.patch({"nick": "AnnaOne"})          # старый ник «Anna» теперь зарезервирован
        self.olga = self.make_user("Olga")
        self.olga_cookie = self.session_for(self.olga)

    def test_old_nick_is_held_for_30_days_against_other_users(self):
        resp = self.patch({"nick": "Anna"}, headers=self.olga_cookie)
        self.assertEqual(resp.status, 409, resp.body)
        self.assertEqual(resp.body["error"], "nick_taken")

    def test_registration_cannot_take_reserved_nick(self):
        resp = self.register("ANNA")
        self.assertEqual(resp.status, 409, resp.body)
        self.assertEqual(resp.body["error"], "nick_taken")

    def test_reservation_ends_after_30_days(self):
        self.set_db("UPDATE nick_reservations SET reserved_until = ?",
                    (store.to_iso(store.utc_now() - timedelta(minutes=1)),))
        resp = self.patch({"nick": "Anna"}, headers=self.olga_cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.register("AnnaFree").status, 201)

    def test_owner_can_take_back_own_old_nick_after_cooldown(self):
        self.set_db("UPDATE users SET nick_changed_at = ? WHERE id = ?",
                    (store.to_iso(store.utc_now() - timedelta(days=31)), self.anna))
        resp = self.patch({"nick": "anna"})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["user"]["nick"], "anna")

    def test_reserved_word_refused_at_registration(self):
        resp = self.register("admin")
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.body["error"], "validation_error")
        self.assertEqual(list(resp.body["fields"]), ["nick"])

    def test_mixed_script_refused_at_registration(self):
        resp = self.register("аnna_2")
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(list(resp.body["fields"]), ["nick"])


# ---------------------------------------------------------------- частота

class ProfileRateLimitTest(ProfileTestBase):
    def test_patch_limit_is_per_user(self):
        self.start_server(admin_token=TOKEN, db_name="rate.db", auth_limits={"profile_patch": (2, 3600)})
        anna = self.make_user("Anna")
        other = self.make_user("Olga_R")
        cookie = self.session_for(anna)
        self.assertEqual(self.patch({"bio": "1"}, headers=cookie).status, 200)
        self.assertEqual(self.patch({"bio": "2"}, headers=cookie).status, 200)
        resp = self.patch({"bio": "3"}, headers=cookie)
        self.assertEqual(resp.status, 429, resp.body)
        self.assertEqual(resp.body["error"], "too_many_requests")
        self.assertIn("Retry-After", resp.headers)
        self.assertEqual(self.patch({"bio": "3"}, headers=self.session_for(other)).status, 200)


# ---------------------------------------------------------------- миграция старой базы

OLD_USERS_SQL = """CREATE TABLE users (
    id                INTEGER PRIMARY KEY,
    nick              TEXT NOT NULL,
    nick_lower        TEXT NOT NULL UNIQUE,
    email             TEXT UNIQUE,
    password_hash     TEXT NOT NULL,
    phone             TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    rules_accepted_at TEXT NOT NULL
)"""


class ProfileMigrationTest(unittest.TestCase):
    """База, созданная до профиля: колонок bio и nick_changed_at нет, таблиц картинок нет."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-profile-migr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "before.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(OLD_USERS_SQL)
            conn.execute("INSERT INTO users (id, nick, nick_lower, email, password_hash, created_at,"
                         " rules_accepted_at) VALUES (1, 'Anna', 'anna', 'a@example.com', ?,"
                         " '2026-01-05T10:00:00.000001Z', '2026-01-05T10:00:00.000001Z')",
                         (auth.hash_password(PASSWORD),))
            conn.commit()
        finally:
            conn.close()

    def columns(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(users)")}, {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()

    def test_adds_profile_columns_and_tables_and_keeps_rows(self):
        store.init_db(self.db_path, products_path=None)
        columns, tables = self.columns()
        self.assertIn("bio", columns)
        self.assertIn("nick_changed_at", columns)
        self.assertTrue({"user_images", "nick_reservations"} <= tables)
        conn = store.connect(self.db_path)
        try:
            row = conn.execute("SELECT nick, bio, nick_changed_at FROM users WHERE id = 1").fetchone()
        finally:
            conn.close()
        self.assertEqual((row["nick"], row["bio"], row["nick_changed_at"]), ("Anna", "", None))

    def test_repeated_start_changes_nothing(self):
        store.init_db(self.db_path, products_path=None)
        first = self.columns()
        store.init_db(self.db_path, products_path=None)
        store.init_db(self.db_path, products_path=None)
        self.assertEqual(self.columns(), first)

    def test_fresh_database_has_profile_schema(self):
        fresh = self.tmp / "fresh.db"
        store.init_db(fresh, products_path=None)
        conn = sqlite3.connect(fresh)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            images_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_images)")}
        finally:
            conn.close()
        self.assertTrue({"bio", "nick_changed_at"} <= columns)
        self.assertEqual(images_cols, {"user_id", "kind", "content_type", "data", "width", "height",
                                       "sha256", "updated_at"})


if __name__ == "__main__":
    unittest.main()
