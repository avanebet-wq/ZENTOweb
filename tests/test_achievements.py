"""Тесты достижений покупателей и публичного ID.

Что проверяем:
- users.public_id: 10 цифр строкой, уникальность, повтор при коллизии, миграция старых баз;
- таблицы achievements и user_achievements: каталог, выдача без дублей, выдача задним числом при старте;
- выдача: «Первый вход» при регистрации, «Фото профиля» при загрузке аватара, «О себе» при описании;
- админская выдача POST /api/admin/achievements/grant (по почте и по нику, доступ только по ADMIN_TOKEN);
- поля в GET /api/profile/me и отдача картинок из assets/achievements/.

Сервер поднимается на временной базе; data/store.db не трогаем.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_api
from test_api import TOKEN, SHOP_BOT
from test_auth import OLD_USERS_SQL, SESSIONS_SQL
from test_images import ProfileServerBase, make_ticket, png_bytes

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets" / "achievements"
sys.path.insert(0, str(REPO / "server"))
import achievements  # noqa: E402
import auth  # noqa: E402
import store  # noqa: E402

PASSWORD = "Secret123"
PUBLIC_ID_RE = re.compile(r"^[0-9]{10}$")
CATALOG_CODES = ["first_purchase", "first_login", "profile_photo", "profile_bio"]
IMAGE_MAX_BYTES = 60 * 1024
ENTRY_KEYS = {"code", "title", "description", "how_to", "image_url", "earned", "granted_at"}

# Таблица users в том виде, как она была до public_id (но уже с ником и описанием).
USERS_BEFORE_PUBLIC_ID = """CREATE TABLE users (
    id                INTEGER PRIMARY KEY,
    nick              TEXT NOT NULL,
    nick_lower        TEXT NOT NULL UNIQUE,
    email             TEXT UNIQUE,
    password_hash     TEXT NOT NULL,
    phone             TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    rules_accepted_at TEXT NOT NULL,
    bio               TEXT NOT NULL DEFAULT '',
    nick_changed_at   TEXT
)"""


def sql(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def admin_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------- формат public_id и каталог

class PublicIdFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-public-id-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "store.db"
        store.init_db(self.db_path, products_path=None)

    def insert_user(self, nick, public_id):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (nick, nick_lower, password_hash, created_at, rules_accepted_at, public_id)"
                " VALUES (?, ?, 'hash', '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z', ?)",
                (nick, nick.lower(), public_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_public_id_is_ten_digits_with_leading_zeros_kept(self):
        conn = store.connect(self.db_path)
        try:
            with mock.patch.object(store.secrets, "randbelow", return_value=7):
                value = store._new_public_id(conn)
        finally:
            conn.close()
        self.assertEqual(value, "0000000007")
        self.assertRegex(value, PUBLIC_ID_RE)

    def test_generated_ids_are_ten_digits_and_mostly_distinct(self):
        conn = store.connect(self.db_path)
        try:
            values = [store._new_public_id(conn) for _ in range(200)]
        finally:
            conn.close()
        for value in values:
            self.assertRegex(value, PUBLIC_ID_RE)
        self.assertEqual(len(set(values)), len(values))

    def test_collision_is_retried_with_a_new_value(self):
        self.insert_user("taken", "0000000001")
        conn = store.connect(self.db_path)
        try:
            with mock.patch.object(store.secrets, "randbelow", side_effect=[1, 2]) as draw:
                value = store._new_public_id(conn)
        finally:
            conn.close()
        self.assertEqual(value, "0000000002")
        self.assertEqual(draw.call_count, 2)

    def test_database_refuses_duplicate_public_id(self):
        self.insert_user("first", "1234567890")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_user("second", "1234567890")

    def test_catalog_is_seeded_in_order_and_not_duplicated(self):
        store.init_db(self.db_path, products_path=None)
        store.init_db(self.db_path, products_path=None)
        rows = sql(self.db_path, "SELECT code, title, how_to, image FROM achievements ORDER BY rowid")
        self.assertEqual([row[0] for row in rows], CATALOG_CODES)
        self.assertEqual(rows[0], ("first_purchase", "Первая покупка",
                                   "Сделайте первую покупку в магазине", "first_purchase.svg"))
        self.assertEqual(rows[1][2], "Зарегистрируйтесь на сайте")
        self.assertEqual(rows[2][2], "Загрузите фото в профиль")
        self.assertEqual(rows[3][2], "Напишите описание профиля")

    def test_catalog_images_exist_and_are_small_and_safe(self):
        for item in achievements.CATALOG:
            with self.subTest(code=item["code"]):
                path = ASSETS / item["image"]
                data = path.read_bytes()
                self.assertLessEqual(len(data), IMAGE_MAX_BYTES)
                if item["image"].endswith(".svg"):
                    self.assertNotIn(b"<script", data.lower())
                else:
                    self.assertEqual(data[:4], b"RIFF")
                    self.assertEqual(data[8:12], b"WEBP")


# ---------------------------------------------------------------- миграция старых баз

class PublicIdMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-achv-migr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "old.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(OLD_USERS_SQL)
            conn.execute(SESSIONS_SQL)
            conn.execute("INSERT INTO users (id, name, phone, email, password_hash, created_at) VALUES "
                         "(1, 'Анна', '+380671234567', 'anna@example.com', 'h1', '2026-01-05T10:00:00.000001Z')")
            conn.execute("INSERT INTO users (id, name, phone, email, password_hash, created_at) VALUES "
                         "(2, 'Олег', '+380501112233', 'oleg@example.com', 'h2', '2026-02-07T12:30:00.000002Z')")
            conn.commit()
        finally:
            conn.close()

    def snapshot(self):
        return (
            sql(self.db_path, "SELECT id, nick, public_id FROM users ORDER BY id"),
            sql(self.db_path, "SELECT user_id, code, granted_at FROM user_achievements ORDER BY user_id, code"),
            sql(self.db_path, "SELECT code, title FROM achievements ORDER BY rowid"),
        )

    def test_old_users_get_distinct_public_ids(self):
        store.init_db(self.db_path, products_path=None)
        rows = sql(self.db_path, "SELECT id, public_id FROM users ORDER BY id")
        self.assertEqual([row[0] for row in rows], [1, 2])
        for _, public_id in rows:
            self.assertRegex(public_id, PUBLIC_ID_RE)
        self.assertNotEqual(rows[0][1], rows[1][1])

    def test_old_users_get_first_login_dated_by_registration(self):
        store.init_db(self.db_path, products_path=None)
        rows = sql(self.db_path, "SELECT user_id, code, granted_at FROM user_achievements"
                                 " WHERE code = 'first_login' ORDER BY user_id")
        self.assertEqual(rows, [(1, "first_login", "2026-01-05T10:00:00.000001Z"),
                                (2, "first_login", "2026-02-07T12:30:00.000002Z")])

    def test_first_purchase_is_not_given_automatically(self):
        store.init_db(self.db_path, products_path=None)
        self.assertEqual(sql(self.db_path, "SELECT COUNT(*) FROM user_achievements"
                                           " WHERE code = 'first_purchase'"), [(0,)])

    def test_repeated_startups_change_nothing(self):
        store.init_db(self.db_path, products_path=None)
        first = self.snapshot()
        store.init_db(self.db_path, products_path=None)
        store.init_db(self.db_path, products_path=None)
        self.assertEqual(self.snapshot(), first)

    def test_photo_and_bio_are_given_on_next_startup_when_condition_already_holds(self):
        store.init_db(self.db_path, products_path=None)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE users SET bio = 'Пишу код' WHERE id = 2")
            conn.execute("INSERT INTO user_images (user_id, kind, content_type, data, width, height, sha256,"
                         " updated_at) VALUES (1, 'avatar', 'image/png', x'00', 1, 1, 'abc',"
                         " '2026-03-01T09:00:00.000000Z')")
            conn.commit()
        finally:
            conn.close()
        store.init_db(self.db_path, products_path=None)
        self.assertEqual(sql(self.db_path, "SELECT user_id, granted_at FROM user_achievements"
                                           " WHERE code = 'profile_photo'"),
                         [(1, "2026-03-01T09:00:00.000000Z")])
        self.assertEqual(sql(self.db_path, "SELECT user_id FROM user_achievements"
                                           " WHERE code = 'profile_bio'"), [(2,)])

    def test_users_table_without_public_id_column_gets_one(self):
        """Текущая база (без колонки public_id) — колонка и уникальный индекс добавляются на месте."""
        other = self.tmp / "no-public-id.db"
        conn = sqlite3.connect(other)
        try:
            conn.execute(USERS_BEFORE_PUBLIC_ID)
            conn.execute("INSERT INTO users (nick, nick_lower, password_hash, created_at, rules_accepted_at)"
                         " VALUES ('Anna', 'anna', 'h', '2026-03-01T10:00:00.000000Z',"
                         " '2026-03-01T10:00:00.000000Z')")
            conn.commit()
        finally:
            conn.close()
        store.init_db(other, products_path=None)
        rows = sql(other, "SELECT public_id FROM users")
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0][0], PUBLIC_ID_RE)
        indexes = {row[0] for row in sql(other, "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_users_public_id", indexes)
        self.assertEqual(sql(other, "SELECT code FROM user_achievements"), [("first_login",)])


# ---------------------------------------------------------------- HTTP: выдача и поля профиля

class AchievementServerBase(ProfileServerBase):
    def setUp(self):
        super().setUp()
        # Тестовый сайт пустой: копируем картинки достижений из репозитория.
        shutil.copytree(ASSETS, self.site / "assets" / "achievements")

    def ticket(self):
        conn = sqlite3.connect(self.db_path)
        try:
            token = make_ticket(conn)
            conn.commit()
            return token
        finally:
            conn.close()

    def register(self, nick, email=None):
        body = {"nick": nick, "password": PASSWORD, "password2": PASSWORD, "agree": True,
                "captcha_ticket": self.ticket()}
        if email is not None:
            body["email"] = email
        return self.request("POST", "/api/auth/register", body=body)

    def register_cookie(self, nick, email=None):
        resp = self.register(nick, email)
        self.assertEqual(resp.status, 201, resp.body)
        return {"Cookie": resp.headers["Set-Cookie"].split(";", 1)[0]}

    def user_id(self, nick):
        return sql(self.db_path, "SELECT id FROM users WHERE nick_lower = ?", (nick.lower(),))[0][0]

    def me(self, cookie):
        resp = self.request("GET", "/api/profile/me", headers=cookie)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.body["user"]

    def entries(self, cookie):
        return {entry["code"]: entry for entry in self.me(cookie)["achievements"]}

    def grant_rows(self, user_id, code):
        return sql(self.db_path, "SELECT granted_at FROM user_achievements WHERE user_id = ? AND code = ?",
                   (user_id, code))

    def admin_grant(self, body, headers=None):
        return self.request("POST", "/api/admin/achievements/grant", body=body,
                            headers=admin_headers() if headers is None else headers)


class RegistrationAchievementTest(AchievementServerBase):
    def test_registration_gives_public_id_and_first_login(self):
        cookie = self.register_cookie("AnnaK", email="anna@example.com")
        user = self.me(cookie)
        self.assertRegex(user["public_id"], PUBLIC_ID_RE)
        self.assertEqual(user["created_at"], sql(self.db_path, "SELECT created_at FROM users")[0][0])
        first_login = self.entries(cookie)["first_login"]
        self.assertIs(first_login["earned"], True)
        self.assertEqual(first_login["granted_at"], user["created_at"])
        self.assertEqual(len(self.grant_rows(self.user_id("AnnaK"), "first_login")), 1)

    def test_public_ids_of_several_users_are_unique(self):
        ids = []
        for nick in ("userone", "usertwo", "userthree"):
            ids.append(self.me(self.register_cookie(nick))["public_id"])
        for public_id in ids:
            self.assertRegex(public_id, PUBLIC_ID_RE)
        self.assertEqual(len(set(ids)), 3)

    def test_public_profile_never_shows_public_id(self):
        cookie = self.register_cookie("AnnaK")
        resp = self.request("GET", "/api/users/annak/profile", headers=cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertNotIn("public_id", resp.body["user"])
        self.assertNotIn("achievements", resp.body["user"])
        self.assertNotIn(self.me(cookie)["public_id"], json.dumps(resp.body))

    def test_profile_me_lists_all_achievements_in_catalog_order(self):
        cookie = self.register_cookie("AnnaK", email="anna@example.com")
        achievements_list = self.me(cookie)["achievements"]
        self.assertEqual([entry["code"] for entry in achievements_list], CATALOG_CODES)
        for entry in achievements_list:
            self.assertEqual(set(entry), ENTRY_KEYS)
            self.assertTrue(entry["image_url"].startswith("/assets/achievements/"))
        by_code = {entry["code"]: entry for entry in achievements_list}
        self.assertEqual(by_code["first_purchase"]["title"], "Первая покупка")
        self.assertEqual(by_code["first_purchase"]["how_to"], "Сделайте первую покупку в магазине")
        self.assertEqual(by_code["first_purchase"]["image_url"], "/assets/achievements/first_purchase.svg")
        self.assertEqual(by_code["profile_bio"]["image_url"], "/assets/achievements/profile_bio.svg")
        self.assertIs(by_code["first_login"]["earned"], True)
        for code in ("first_purchase", "profile_photo", "profile_bio"):
            self.assertIs(by_code[code]["earned"], False)
            self.assertIsNone(by_code[code]["granted_at"])


class ProfileAchievementTest(AchievementServerBase):
    def setUp(self):
        super().setUp()
        self.cookie = self.register_cookie("AnnaK")
        self.anna = self.user_id("AnnaK")

    def test_first_avatar_upload_gives_profile_photo_once(self):
        resp = self.upload("avatar", png_bytes(300, 300), "image/png", self.cookie)
        self.assertEqual(resp.status, 200, resp.body)
        first = self.entries(self.cookie)["profile_photo"]
        self.assertIs(first["earned"], True)
        self.assertIsNotNone(first["granted_at"])
        self.upload("avatar", png_bytes(200, 200), "image/png", self.cookie)
        second = self.entries(self.cookie)["profile_photo"]
        self.assertEqual(second["granted_at"], first["granted_at"])
        self.assertEqual(len(self.grant_rows(self.anna, "profile_photo")), 1)

    def test_banner_does_not_give_profile_photo(self):
        self.upload("banner", png_bytes(400, 100), "image/png", self.cookie)
        self.assertIs(self.entries(self.cookie)["profile_photo"]["earned"], False)

    def test_saving_bio_gives_profile_bio_once(self):
        resp = self.request("PATCH", "/api/profile", body={"bio": "Люблю чай"}, headers=self.cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(self.entries(self.cookie)["profile_bio"]["earned"], True)
        self.request("PATCH", "/api/profile", body={"bio": "Другой текст"}, headers=self.cookie)
        self.assertEqual(len(self.grant_rows(self.anna, "profile_bio")), 1)

    def test_empty_or_spaces_bio_does_not_give_profile_bio(self):
        self.request("PATCH", "/api/profile", body={"bio": "   "}, headers=self.cookie)
        self.assertIs(self.entries(self.cookie)["profile_bio"]["earned"], False)
        self.assertEqual(self.grant_rows(self.anna, "profile_bio"), [])

    def test_first_purchase_is_not_given_by_profile_actions(self):
        self.request("PATCH", "/api/profile", body={"bio": "Текст"}, headers=self.cookie)
        self.upload("avatar", png_bytes(100, 100), "image/png", self.cookie)
        self.assertIs(self.entries(self.cookie)["first_purchase"]["earned"], False)


class AdminGrantTest(AchievementServerBase):
    def setUp(self):
        super().setUp()
        self.cookie = self.register_cookie("AnnaK", email="anna@example.com")
        self.register_cookie("OlegB")
        self.anna = self.user_id("AnnaK")

    def test_grant_by_email_is_reported_then_repeat_is_not_duplicated(self):
        body = {"login": "Anna@Example.com", "code": "first_purchase"}
        resp = self.admin_grant(body)
        self.assertEqual(resp.status, 200, resp.body)
        public_id = sql(self.db_path, "SELECT public_id FROM users WHERE id = ?", (self.anna,))[0][0]
        self.assertEqual(resp.body, {"ok": True, "granted": True,
                                     "user": {"nick": "AnnaK", "public_id": public_id}})
        self.assertIs(self.entries(self.cookie)["first_purchase"]["earned"], True)

        again = self.admin_grant(body)
        self.assertEqual(again.status, 200, again.body)
        self.assertIs(again.body["granted"], False)
        self.assertEqual(len(self.grant_rows(self.anna, "first_purchase")), 1)

    def test_grant_by_nick_in_any_case(self):
        resp = self.admin_grant({"login": "OLEGb", "code": "first_purchase"})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["granted"], True)
        self.assertEqual(resp.body["user"]["nick"], "OlegB")
        self.assertEqual(len(self.grant_rows(self.user_id("OlegB"), "first_purchase")), 1)

    def test_unknown_login_is_404_not_found(self):
        for login in ("nobodyhere", "nobody@example.com"):
            with self.subTest(login=login):
                resp = self.admin_grant({"login": login, "code": "first_purchase"})
                self.assertEqual(resp.status, 404, resp.body)
                self.assertEqual(resp.body, {"ok": False, "error": "not_found",
                                             "message": "Покупатель не найден"})

    def test_unknown_code_is_400_validation_error_and_nothing_changes(self):
        resp = self.admin_grant({"login": "anna@example.com", "code": "lucky_draw"})
        self.assertEqual(resp.status, 400, resp.body)
        self.assertIs(resp.body["ok"], False)
        self.assertEqual(resp.body["error"], "validation_error")
        self.assertIn("code", resp.body["fields"])
        self.assertEqual(sql(self.db_path, "SELECT COUNT(*) FROM user_achievements"
                                           " WHERE code NOT IN ('first_login')"), [(0,)])

    def test_bad_body_is_400_validation_error(self):
        resp = self.admin_grant({"code": "first_purchase"})
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.body["error"], "validation_error")
        self.assertIn("login", resp.body["fields"])
        resp = self.admin_grant(["first_purchase"])
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.body["error"], "validation_error")

    def test_without_admin_token_is_401(self):
        body = {"login": "anna@example.com", "code": "first_purchase"}
        self.assertEqual(self.admin_grant(body, headers={}).status, 401)
        self.assertEqual(self.admin_grant(body, headers={"Authorization": "Bearer wrong-token"}).status, 401)
        self.assertEqual(self.admin_grant(body, headers=self.cookie).status, 401)
        self.assertEqual(self.grant_rows(self.anna, "first_purchase"), [])

    def test_shop_bot_token_is_403(self):
        resp = self.admin_grant({"login": "anna@example.com", "code": "first_purchase"},
                                headers={"Authorization": f"Bearer {SHOP_BOT}"})
        self.assertEqual(resp.status, 403, resp.body)
        self.assertEqual(self.grant_rows(self.anna, "first_purchase"), [])


class AchievementImageServeTest(AchievementServerBase):
    def get_image(self, name):
        return self.request("GET", f"/assets/achievements/{name}")

    def test_other_medals_are_served_as_svg(self):
        for name in ("first_purchase.svg", "first_login.svg", "profile_photo.svg", "profile_bio.svg"):
            with self.subTest(name=name):
                resp = self.get_image(name)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")
                self.assertNotIn(b"<script", resp.raw.lower())

    def test_every_catalog_image_is_served(self):
        for item in achievements.CATALOG:
            with self.subTest(code=item["code"]):
                self.assertEqual(self.get_image(item["image"]).status, 200)


if __name__ == "__main__":
    unittest.main()
