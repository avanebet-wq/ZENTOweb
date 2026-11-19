"""Тесты капчи-пазла: выбор картинок, задания, сверка расстановки, сгорание заданий, лимиты.

Картинки — фиктивные SVG во временной папке. Настоящая папка assets/captcha не нужна.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import test_auth
from test_auth import make_images, solve
from test_api import TOKEN, minutes_ago

import app  # noqa: E402  (путь к server/ добавлен в test_auth)
import auth  # noqa: E402
import captcha  # noqa: E402


class LoadImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-captcha-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_folder_gives_empty_list(self):
        self.assertEqual(captcha.load_images(self.tmp / "nope"), [])
        self.assertFalse(captcha.is_available([]))

    def test_nine_images_are_not_enough(self):
        make_images(self.tmp, 9)
        images = captcha.load_images(self.tmp)
        self.assertEqual(len(images), 9)
        self.assertFalse(captcha.is_available(images))

    def test_ten_images_are_enough_and_only_svg_names_count(self):
        make_images(self.tmp, 10)
        (self.tmp / "notes.txt").write_text("x", encoding="utf-8")
        (self.tmp / "bad name.svg").write_text("x", encoding="utf-8")
        images = captcha.load_images(self.tmp)
        self.assertEqual(images[0], "/assets/captcha/01.svg")
        self.assertEqual(len(images), 10)
        self.assertTrue(captcha.is_available(images))


class ChallengeTest(unittest.TestCase):
    IMAGES = [f"/assets/captcha/{n:02d}.svg" for n in range(1, 11)]

    def test_shape_of_new_challenge(self):
        challenge = captcha.new_challenge(self.IMAGES)
        tiles = challenge["tiles"]
        self.assertEqual(len(tiles), 4)
        self.assertEqual(sorted(tile["piece"] for tile in tiles), [0, 1, 2, 3])
        self.assertEqual(len({tile["token"] for tile in tiles}), 4)
        self.assertIn(challenge["image"], self.IMAGES)
        self.assertEqual(challenge["expected"], {tile["token"]: tile["piece"] for tile in tiles})

    def test_evaluate_outcomes(self):
        challenge = captcha.new_challenge(self.IMAGES)
        expected = challenge["expected"]
        tokens = list(expected)
        self.assertEqual(captcha.evaluate(expected, {}), "incomplete")
        self.assertEqual(captcha.evaluate(expected, {tokens[0]: expected[tokens[0]]}), "incomplete")
        partial_none = {token: expected[token] for token in tokens}
        partial_none[tokens[3]] = None
        self.assertEqual(captcha.evaluate(expected, partial_none), "incomplete")
        self.assertEqual(captcha.evaluate(expected, solve(challenge["tiles"])), "ok")
        self.assertEqual(captcha.evaluate(expected, solve(challenge["tiles"], shift=1)), "wrong")
        # Два кусочка в одной ячейке — ошибка, даже если все четыре ячейки заняты.
        crowded = {token: 0 for token in tokens}
        self.assertEqual(captcha.evaluate(expected, crowded), "wrong")


class CaptchaServerBase(test_auth.AuthServerBase):
    captcha_count = 12

    def new(self):
        return self.request("GET", "/api/captcha/new")

    def verify(self, challenge_id, placements, **extra):
        body = {"challenge_id": challenge_id, "placements": placements, **extra}
        return self.request("POST", "/api/captcha/verify", body=body)

    def fresh(self):
        return self.new().body


class CaptchaNewTest(CaptchaServerBase):
    def test_new_challenge_response_shape(self):
        resp = self.new()
        self.assertEqual(resp.status, 200, resp.body)
        body = resp.body
        self.assertIs(body["ok"], True)
        self.assertEqual(body["expires_in"], 120)
        self.assertEqual(body["grid"], 2)
        self.assertTrue(body["image"].startswith("/assets/captcha/"))
        self.assertEqual(sorted(t["piece"] for t in body["tiles"]), [0, 1, 2, 3])
        self.assertEqual(self.request("GET", body["image"]).status, 200)

    def test_image_is_served_as_svg(self):
        resp = self.new()
        image = self.request("GET", resp.body["image"])
        self.assertEqual(image.status, 200)
        self.assertEqual(image.headers["Content-Type"], "image/svg+xml")

    def test_answers_are_no_store(self):
        self.assertEqual(self.new().headers["Cache-Control"], "no-store")

    def test_challenge_stored_as_json_expected(self):
        body = self.fresh()
        conn = self.db()
        try:
            (expected_text,) = conn.execute(
                "SELECT expected FROM captcha_challenges WHERE id = ?", (body["challenge_id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(json.loads(expected_text),
                         {t["token"]: t["piece"] for t in body["tiles"]})

    def test_expired_challenges_are_purged_on_new(self):
        old = self.fresh()
        self.db_set("UPDATE captcha_challenges SET expires_at = ? WHERE id = ?",
                    (minutes_ago(1), old["challenge_id"]))
        self.fresh()
        self.assertEqual(self.db_count("captcha_challenges"), 1)

    def test_new_limit_returns_429(self):
        self.start_server(admin_token=TOKEN, db_name="cap-new.db", auth_limits={"captcha_new": (2, 600)})
        self.assertEqual(self.new().status, 200)
        self.assertEqual(self.new().status, 200)
        resp = self.new()
        self.assert_error(resp, 429, "too_many_requests")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)

    def test_default_limits(self):
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["captcha_new"], (30, 600))
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["captcha_verify"], (20, 600))
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["register"], (10, 3600))
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["login_key"], (5, 900))
        self.assertEqual(app.DEFAULT_AUTH_LIMITS["login_ip"], (20, 900))


class CaptchaUnavailableTest(CaptchaServerBase):
    def test_missing_folder_gives_503_and_server_still_runs(self):
        self.start_server(admin_token=TOKEN, db_name="no-folder.db",
                          captcha_dir=self.tmp / "missing")
        self.assert_error(self.new(), 503, "captcha_unavailable")
        self.assertEqual(self.request("GET", "/api/products").status, 200)

    def test_fewer_than_ten_images_gives_503(self):
        folder = self.tmp / "few"
        make_images(folder, 9)
        self.start_server(admin_token=TOKEN, db_name="few.db", captcha_dir=folder)
        self.assert_error(self.new(), 503, "captcha_unavailable")
        self.assertEqual(self.request("GET", "/api/products").status, 200)


class CaptchaVerifyTest(CaptchaServerBase):
    def test_correct_placement_gives_ticket_and_expires_in_300(self):
        body = self.fresh()
        resp = self.verify(body["challenge_id"], solve(body["tiles"]))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["ok"], True)
        self.assertEqual(resp.body["expires_in"], 300)
        self.assertTrue(resp.body["ticket"])

    def test_ticket_stored_only_as_hash(self):
        body = self.fresh()
        ticket = self.verify(body["challenge_id"], solve(body["tiles"])).body["ticket"]
        conn = self.db()
        try:
            stored = [row[0] for row in conn.execute("SELECT token_hash FROM captcha_tickets")]
        finally:
            conn.close()
        self.assertEqual(stored, [auth.token_hash(ticket)])
        self.assertNotIn(ticket, stored)

    def test_incomplete_does_not_burn_challenge(self):
        body = self.fresh()
        placements = solve(body["tiles"])
        partial = dict(list(placements.items())[:3])
        resp = self.verify(body["challenge_id"], partial)
        self.assert_error(resp, 400, "captcha_incomplete")
        self.assertNotIn("ticket", resp.body)
        empty = self.verify(body["challenge_id"], {})
        self.assert_error(empty, 400, "captcha_incomplete")
        done = self.verify(body["challenge_id"], placements)
        self.assertEqual(done.status, 200, done.body)

    def test_wrong_placement_burns_challenge(self):
        body = self.fresh()
        wrong = self.verify(body["challenge_id"], solve(body["tiles"], shift=1))
        self.assert_error(wrong, 400, "captcha_wrong")
        again = self.verify(body["challenge_id"], solve(body["tiles"]))
        self.assert_error(again, 410, "captcha_expired")
        self.assertEqual(self.db_count("captcha_tickets"), 0)

    def test_challenge_cannot_be_reused_after_success(self):
        body = self.fresh()
        placements = solve(body["tiles"])
        self.assertEqual(self.verify(body["challenge_id"], placements).status, 200)
        self.assert_error(self.verify(body["challenge_id"], placements), 410, "captcha_expired")

    def test_expired_challenge_is_410(self):
        body = self.fresh()
        self.db_set("UPDATE captcha_challenges SET expires_at = ?", (minutes_ago(1),))
        resp = self.verify(body["challenge_id"], solve(body["tiles"]))
        self.assert_error(resp, 410, "captcha_expired")
        self.assertEqual(resp.body["message"], "Проверка устарела. Начните заново")

    def test_unknown_challenge_gives_same_410(self):
        body = self.fresh()
        unknown = self.verify("u" * 24, solve(body["tiles"]))
        expired_body = self.fresh()
        self.db_set("UPDATE captcha_challenges SET expires_at = ? WHERE id = ?",
                    (minutes_ago(1), expired_body["challenge_id"]))
        expired = self.verify(expired_body["challenge_id"], solve(expired_body["tiles"]))
        self.assertEqual(unknown.body, expired.body)
        self.assertEqual(unknown.status, 410)

    def test_bad_shapes_are_validation_errors_and_do_not_burn(self):
        body = self.fresh()
        placements = solve(body["tiles"])
        bad_requests = [
            {"challenge_id": "short", "placements": placements},
            {"challenge_id": 12345, "placements": placements},
            {"challenge_id": body["challenge_id"], "placements": [0, 1, 2, 3]},
            {"challenge_id": body["challenge_id"], "placements": {**placements, "extra-token": 0}},
            {"challenge_id": body["challenge_id"], "placements": {t: 7 for t in placements}},
            {"challenge_id": body["challenge_id"], "placements": {t: True for t in placements}},
            {"challenge_id": body["challenge_id"], "placements": {t: 1.0 for t in placements}},
            {"challenge_id": body["challenge_id"], "placements": {**placements, "x" * 70: 0}},
            {"challenge_id": body["challenge_id"],
             "placements": {**{f"t{i}": 0 for i in range(4)}, "one-more": 1}},
        ]
        for request_body in bad_requests:
            with self.subTest(body=str(request_body)[:80]):
                resp = self.request("POST", "/api/captcha/verify", body=request_body)
                self.assertEqual(resp.status, 400, resp.body)
                self.assertEqual(resp.body["error"], "validation_error")
                self.assertIn("fields", resp.body)
        # После всех ошибок задание живо.
        self.assertEqual(self.verify(body["challenge_id"], placements).status, 200)

    def test_verify_needs_json_content_type(self):
        body = self.fresh()
        resp = self.request("POST", "/api/captcha/verify", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assert_error(resp, 415, "unsupported_media_type")
        self.assertEqual(self.verify(body["challenge_id"], solve(body["tiles"])).status, 200)

    def test_verify_limit_returns_429_too_many_attempts(self):
        self.start_server(admin_token=TOKEN, db_name="cap-verify.db", auth_limits={"captcha_verify": (2, 600)})
        body = self.fresh()
        self.assert_error(self.verify(body["challenge_id"], {}), 400, "captcha_incomplete")
        self.assert_error(self.verify(body["challenge_id"], {}), 400, "captcha_incomplete")
        resp = self.verify(body["challenge_id"], solve(body["tiles"]))
        self.assert_error(resp, 429, "too_many_attempts")
        self.assertGreaterEqual(int(resp.headers["Retry-After"]), 1)
        # Лимит по IP: задание не сгорело, но проверка заблокирована.
        self.assertEqual(self.db_count("captcha_tickets"), 0)


def minutes_ago(minutes):
    from test_api import minutes_ago
    return minutes_ago(minutes)


if __name__ == "__main__":
    unittest.main()
