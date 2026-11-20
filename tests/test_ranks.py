"""Тесты рангов и опыта: таблица уровней, ежедневный вход один раз в сутки, потолки, ответ профиля."""

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_images import ProfileServerBase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import ranks  # noqa: E402
import store  # noqa: E402


class RankTableTest(unittest.TestCase):
    def test_fifteen_levels_with_growing_steps(self):
        self.assertEqual(len(ranks.NAMES), ranks.MAX_LEVEL)
        steps = [ranks.threshold(n + 1) - ranks.threshold(n) for n in range(1, ranks.MAX_LEVEL)]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(len(set(steps)), len(steps))

    def test_level_borders(self):
        self.assertEqual(ranks.rank_for(0)["level"], 1)
        self.assertEqual(ranks.rank_for(29)["level"], 1)
        self.assertEqual(ranks.rank_for(30)["level"], 2)
        self.assertEqual(ranks.rank_for(ranks.threshold(15))["level"], 15)
        top = ranks.rank_for(10 ** 9)
        self.assertEqual((top["level"], top["name"], top["next_at"]), (15, "Архитектор", None))

    def test_negative_xp_is_level_one(self):
        self.assertEqual(ranks.rank_for(-5)["xp"], 0)


class AwardXpTest(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.uid = self.make_user("Anna")
        self.cookie = self.session_for(self.uid)

    def xp(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT xp FROM users WHERE id = ?", (self.uid,)).fetchone()[0]
        finally:
            conn.close()

    def with_conn(self, func):
        conn = store.connect(self.db_path)
        try:
            return func(conn)
        finally:
            conn.close()

    def test_daily_login_once_per_day(self):
        now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "login", now)), 10)
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "login", now)), 0)
        self.assertEqual(self.xp(), 10)
        tomorrow = now + timedelta(days=1)
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "login", tomorrow)), 10)
        self.assertEqual(self.xp(), 20)

    def test_day_changes_at_kyiv_midnight(self):
        before = datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc)  # 23:30 в Киеве
        after = datetime(2026, 11, 20, 22, 30, tzinfo=timezone.utc)   # 00:30 в Киеве, уже 21-е
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "login", before)), 10)
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "login", after)), 10)

    def test_message_cap_per_day(self):
        now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        gains = [self.with_conn(lambda c: store.award_xp(c, self.uid, "message", now)) for _ in range(12)]
        self.assertEqual(sum(gains), ranks.XP_RULES["message"]["cap"])
        self.assertEqual(gains[-1], 0)

    def test_unknown_kind_gives_nothing(self):
        self.assertEqual(self.with_conn(lambda c: store.award_xp(c, self.uid, "cheat", store.utc_now())), 0)

    def test_me_awards_login_and_profile_shows_rank(self):
        self.assertEqual(self.request("GET", "/api/auth/me", headers=self.cookie).status, 200)
        self.request("GET", "/api/auth/me", headers=self.cookie)  # второй раз в тот же день: без прибавки
        resp = self.request("GET", "/api/profile/me", headers=self.cookie)
        user = resp.body["user"]
        self.assertEqual(user["xp"], 10)
        self.assertEqual(user["rank"]["level"], 1)
        self.assertEqual(user["rank"]["name"], "Наблюдатель")
        self.assertEqual(user["rank"]["next_at"], 30)


class AdminRankTest(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.uid = self.make_user("Anna", email="anna@example.com")

    def post(self, body, headers=None):
        from test_api import TOKEN
        h = {"Authorization": f"Bearer {TOKEN}"} if headers is None else headers
        return self.request("POST", "/api/admin/rank", body=body, headers=h)

    def test_admin_raises_rank_by_email_and_xp_never_drops(self):
        resp = self.post({"login": "ANNA@example.com", "level": 15})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["rank"]["level"], 15)
        again = self.post({"login": "anna", "level": 3})
        self.assertEqual(again.body["rank"]["level"], 15)

    def test_needs_admin_token_and_valid_body(self):
        self.assertEqual(self.post({"login": "anna", "level": 15}, headers={}).status, 401)
        self.assertEqual(self.post({"login": "anna", "level": 16}).status, 400)
        self.assertEqual(self.post({"login": "nobodyhere", "level": 2}).status, 404)


if __name__ == "__main__":
    unittest.main()
