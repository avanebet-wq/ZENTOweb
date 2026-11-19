"""Колесо фортуны: статус, прокрутка, одна прокрутка в сутки (день по Киеву), призы без денег, лимит IP.

Сервер поднимается на временной базе (test_api.ServerTestCase). Пользователей и сессии создаём прямо в базе,
без капчи. Время подменяем через roulette.now (часы сервера берутся оттуда).

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import random
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import test_api

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app  # noqa: E402
import auth  # noqa: E402
import roulette  # noqa: E402
import store  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
# Ноябрь 2026: Киев на UTC+2. Полночь 20 ноября по Киеву — это 22:00 UTC 19 ноября.
NEXT_MIDNIGHT_UTC = "2026-11-19T22:00:00.000000Z"
FORBIDDEN_IN_LABELS = ["₴", "грн", "грив", "uah", "%", "скидк", "джекпот", "деньг", "промокод",
                       "руб", "₽", "$", "usd"]


def kyiv(year, month, day, hour=12, minute=0):
    """Момент по Киеву, переведённый в UTC (datetime с tzinfo=UTC)."""
    return datetime(year, month, day, hour, minute, tzinfo=KYIV).astimezone(timezone.utc)


def clock(moment):
    """Подмена времени для колеса: roulette.now() вернёт moment."""
    return mock.patch.object(roulette, "now", return_value=moment)


def post_spin_raw(port, token):
    """Прямой запрос из потока (без общих помощников теста)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/api/roulette/spin", body=b"{}",
                     headers={"Content-Type": "application/json",
                              "Cookie": f"{auth.SESSION_COOKIE}={token}"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


class RouletteBase(test_api.ServerTestCase):
    def setUp(self):
        super().setUp()
        self.user_count = 0

    def make_user(self):
        """Покупатель со входом: строка в users и сессия. Возвращает (id, токен cookie)."""
        self.user_count += 1
        nick = f"roulette_{self.user_count}"
        stamp = store.to_iso(store.utc_now())
        conn = store.connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO users (nick, nick_lower, email, password_hash, created_at, rules_accepted_at)"
                " VALUES (?, ?, NULL, 'not-used', ?, ?)",
                (nick, nick.lower(), stamp, stamp),
            )
            user_id = cur.lastrowid
            token = auth.new_token()
            store.create_session(conn, user_id, auth.token_hash(token), store.utc_now())
        finally:
            conn.close()
        return user_id, token

    def cookie(self, token):
        return {"Cookie": f"{auth.SESSION_COOKIE}={token}"}

    def status(self, token=None, headers=None):
        merged = {**(self.cookie(token) if token else {}), **(headers or {})}
        return self.request("GET", "/api/roulette/status", headers=merged)

    def spin(self, token=None, headers=None):
        merged = {**(self.cookie(token) if token else {}), **(headers or {})}
        return self.request("POST", "/api/roulette/spin", body={}, headers=merged)

    def spin_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT user_id, day, prize_id FROM roulette_spins ORDER BY id"
            ).fetchall()
        finally:
            conn.close()


class StatusTest(RouletteBase):
    def test_status_without_login_is_401(self):
        resp = self.status()
        self.assertEqual(resp.status, 401)
        self.assertIs(resp.body["ok"], False)
        self.assertEqual(resp.body["error"], "login_required")

    def test_status_with_login_before_spin(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            resp = self.status(token)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["ok"], True)
        self.assertIs(resp.body["can_spin"], True)
        self.assertIsNone(resp.body["next_at"])
        self.assertEqual(resp.body["prizes"], roulette.public_prizes())
        for prize in resp.body["prizes"]:
            self.assertEqual(set(prize), {"id", "label"})

    def test_status_after_spin_gives_next_kyiv_midnight(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            self.assertEqual(self.spin(token).status, 200)
            resp = self.status(token)
        self.assertEqual(resp.status, 200)
        self.assertIs(resp.body["can_spin"], False)
        self.assertEqual(resp.body["next_at"], NEXT_MIDNIGHT_UTC)


class SpinTest(RouletteBase):
    def test_first_spin_returns_prize_from_list(self):
        user_id, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            resp = self.spin(token)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["ok"], True)
        ids = {prize["id"]: prize["label"] for prize in roulette.public_prizes()}
        self.assertIn(resp.body["prize"]["id"], ids)
        self.assertEqual(resp.body["prize"]["label"], ids[resp.body["prize"]["id"]])
        self.assertEqual(set(resp.body["prize"]), {"id", "label"})
        self.assertEqual(resp.body["next_at"], NEXT_MIDNIGHT_UTC)
        rows = self.spin_rows()
        self.assertEqual(rows, [(user_id, "2026-11-19", resp.body["prize"]["id"])])

    def test_second_spin_same_day_is_429_already_spun(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 9)):
            self.assertEqual(self.spin(token).status, 200)
        with clock(kyiv(2026, 11, 19, 20)):
            resp = self.spin(token)
        self.assertEqual(resp.status, 429, resp.body)
        self.assertIs(resp.body["ok"], False)
        self.assertEqual(resp.body["error"], "already_spun")
        self.assertIsInstance(resp.body["message"], str)
        self.assertEqual(resp.body["next_at"], NEXT_MIDNIGHT_UTC)
        self.assertNotIn("prize", resp.body)
        self.assertEqual(len(self.spin_rows()), 1)

    def test_next_kyiv_day_can_spin_again(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 23, 30)):
            self.assertEqual(self.spin(token).status, 200)
        with clock(kyiv(2026, 11, 20, 0, 10)):
            resp = self.spin(token)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.body["next_at"], "2026-11-20T22:00:00.000000Z")
        self.assertEqual([row[1] for row in self.spin_rows()], ["2026-11-19", "2026-11-20"])

    def test_day_boundary_is_kyiv_not_utc(self):
        # 21:59 UTC — в Киеве уже 23:59 того же дня: второй раз нельзя.
        _, token = self.make_user()
        with clock(datetime(2026, 11, 19, 21, 59, tzinfo=timezone.utc)):
            self.assertEqual(self.spin(token).status, 200)
        with clock(datetime(2026, 11, 19, 21, 59, 59, tzinfo=timezone.utc)):
            self.assertEqual(self.spin(token).body["error"], "already_spun")
        # 22:00 UTC — в Киеве уже 00:00 следующего дня: можно.
        with clock(datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)):
            self.assertEqual(self.spin(token).status, 200)

    def test_status_follows_the_day_too(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            self.spin(token)
        with clock(kyiv(2026, 11, 20, 8)):
            resp = self.status(token)
        self.assertIs(resp.body["can_spin"], True)
        self.assertIsNone(resp.body["next_at"])

    def test_each_user_has_own_day(self):
        _, first = self.make_user()
        _, second = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            self.assertEqual(self.spin(first).status, 200)
            self.assertEqual(self.spin(second).status, 200)
        self.assertEqual(len(self.spin_rows()), 2)

    def test_double_click_in_parallel_leaves_one_spin(self):
        _, token = self.make_user()
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def click():
            barrier.wait()
            outcome = post_spin_raw(self.port, token)
            with lock:
                results.append(outcome)

        with clock(kyiv(2026, 11, 19, 12)):
            threads = [threading.Thread(target=click) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200, 429], results)
        rejected = [body for status, body in results if status == 429][0]
        self.assertEqual(rejected["error"], "already_spun")
        self.assertEqual(len(self.spin_rows()), 1)

    def test_empty_json_body_is_fine(self):
        _, token = self.make_user()
        resp = self.request("POST", "/api/roulette/spin", raw=b"",
                            headers={**self.cookie(token), "Content-Type": "application/json"})
        self.assertEqual(resp.status, 200, resp.body)

    def test_spin_needs_json_content_type(self):
        _, token = self.make_user()
        resp = self.request("POST", "/api/roulette/spin", raw=b"{}",
                            headers={**self.cookie(token), "Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        self.assertEqual(resp.body["error"], "unsupported_media_type")
        self.assertEqual(self.spin_rows(), [])

    def test_spin_without_login_is_401(self):
        resp = self.spin()
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body["error"], "login_required")
        self.assertEqual(self.spin_rows(), [])

    def test_expired_session_is_401(self):
        user_id, token = self.make_user()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000000Z'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.spin(token).status, 401)

    def test_bot_and_admin_tokens_are_401(self):
        bot = {"Authorization": f"Bearer {test_api.SHOP_BOT}"}
        admin = {"Authorization": f"Bearer {test_api.TOKEN}"}
        for headers in (bot, admin):
            resp = self.spin(headers=headers)
            self.assertEqual(resp.status, 401, headers)
            self.assertEqual(resp.body["error"], "login_required")
            resp = self.status(headers=headers)
            self.assertEqual(resp.status, 401, headers)
            self.assertEqual(resp.body["error"], "login_required")
        self.assertEqual(self.spin_rows(), [])

    def test_spin_limit_per_ip_is_ten_per_hour(self):
        tokens = [self.make_user()[1] for _ in range(app.ROULETTE_RATE_LIMIT + 1)]
        with clock(kyiv(2026, 11, 19, 12)):
            for token in tokens[:app.ROULETTE_RATE_LIMIT]:
                self.assertEqual(self.spin(token).status, 200)
            resp = self.spin(tokens[-1])
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.body["error"], "too many requests")
        self.assertEqual(len(self.spin_rows()), app.ROULETTE_RATE_LIMIT)


class PrizeTest(unittest.TestCase):
    def test_prize_ids_are_unique_and_list_is_not_empty(self):
        ids = [prize["id"] for prize in roulette.PRIZES]
        self.assertGreaterEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))

    def test_labels_have_no_money_or_discounts(self):
        for prize in roulette.PRIZES:
            label = prize["label"].lower()
            for word in FORBIDDEN_IN_LABELS:
                self.assertNotIn(word, label, prize)

    def test_public_prizes_have_no_weights(self):
        for prize in roulette.public_prizes():
            self.assertEqual(set(prize), {"id", "label"})

    def test_pick_prize_returns_known_prize(self):
        known = {prize["id"]: prize["label"] for prize in roulette.PRIZES}
        for _ in range(50):
            prize = roulette.pick_prize()
            self.assertEqual(set(prize), {"id", "label"})
            self.assertEqual(known[prize["id"]], prize["label"])

    def test_pick_prize_follows_weights(self):
        rng = random.Random(20261119)  # фиксированное зерно: проверка весов без случайных сбоев
        total = sum(prize["weight"] for prize in roulette.PRIZES)
        counts = {}
        samples = 20000
        for _ in range(samples):
            prize_id = roulette.pick_prize(rng)["id"]
            counts[prize_id] = counts.get(prize_id, 0) + 1
        for prize in roulette.PRIZES:
            expected = prize["weight"] / total
            self.assertAlmostEqual(counts.get(prize["id"], 0) / samples, expected, delta=0.02, msg=prize["id"])

    def test_default_generator_is_system_random(self):
        import secrets
        self.assertIsInstance(roulette._SYSTEM_RANDOM, secrets.SystemRandom)

    def test_again_is_the_biggest_segment(self):
        weights = {prize["id"]: prize["weight"] for prize in roulette.PRIZES}
        self.assertEqual(max(weights, key=weights.get), "again")


class KyivDayTest(unittest.TestCase):
    def test_day_and_next_start_use_kyiv_time(self):
        self.assertEqual(store.roulette_day(datetime(2026, 11, 19, 21, 59, tzinfo=timezone.utc)), "2026-11-19")
        self.assertEqual(store.roulette_day(datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)), "2026-11-20")
        self.assertEqual(store.roulette_next_at(kyiv(2026, 11, 19, 12)), NEXT_MIDNIGHT_UTC)
        self.assertEqual(store.roulette_next_at(kyiv(2026, 12, 31, 23)), "2026-12-31T22:00:00.000000Z")


class TableTest(RouletteBase):
    def test_init_db_twice_keeps_table_and_data(self):
        _, token = self.make_user()
        with clock(kyiv(2026, 11, 19, 12)):
            self.assertEqual(self.spin(token).status, 200)
        store.init_db(self.db_path, self.products_path)
        store.init_db(self.db_path, self.products_path)
        self.assertEqual(len(self.spin_rows()), 1)

    def test_unique_user_and_day_at_database_level(self):
        user_id, _ = self.make_user()
        conn = store.connect(self.db_path)
        try:
            insert = ("INSERT INTO roulette_spins (user_id, day, prize_id, created_at)"
                      " VALUES (?, ?, 'again', '2026-11-19T10:00:00.000000Z')")
            conn.execute(insert, (user_id, "2026-11-19"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, (user_id, "2026-11-19"))
            conn.execute(insert, (user_id, "2026-11-20"))
        finally:
            conn.close()
        self.assertEqual(len(self.spin_rows()), 2)

    def test_store_spin_twice_same_day_is_refused(self):
        user_id, _ = self.make_user()
        conn = store.connect(self.db_path)
        try:
            now = kyiv(2026, 11, 19, 12)
            first = store.roulette_spin(conn, user_id, "tea", now)
            second = store.roulette_spin(conn, user_id, "badge", now)
        finally:
            conn.close()
        self.assertTrue(first["spun"])
        self.assertFalse(second["spun"])
        self.assertEqual(second["next_at"], NEXT_MIDNIGHT_UTC)
        self.assertEqual(self.spin_rows(), [(user_id, "2026-11-19", "tea")])

    def test_deleting_user_removes_spins(self):
        user_id, _ = self.make_user()
        conn = store.connect(self.db_path)
        try:
            store.roulette_spin(conn, user_id, "tip", kyiv(2026, 11, 19, 12))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        finally:
            conn.close()
        self.assertEqual(self.spin_rows(), [])


if __name__ == "__main__":
    unittest.main()
