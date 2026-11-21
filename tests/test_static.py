"""Тесты отдачи статики: звук входа (.mp3) должен идти как audio/mpeg, а не 404.

Сервер поднимается на временной базе и временной папке со статикой.
Файлы сайта и файл data/store.db не трогаются.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

import app  # noqa: E402

SOUND_REL = "assets/sounds/login-success.mp3"
PRODUCTS = [{"id": "p1", "name": "Тест", "category": "Музыка", "price": 0,
             "description": "Тест", "color": "#ff8800", "stock": 1}]


class StaticTypesTest(unittest.TestCase):
    def test_mp3_has_audio_mime_type(self):
        self.assertEqual(app.STATIC_TYPES.get(".mp3"), "audio/mpeg")

    def test_resolve_static_returns_mp3_with_audio_type(self):
        root = Path(tempfile.mkdtemp(prefix="zento-static-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "assets" / "sounds").mkdir(parents=True)
        (root / SOUND_REL).write_bytes(b"ID3fake")
        found = app.resolve_static(root, "/" + SOUND_REL)
        self.assertIsNotNone(found)
        path, content_type = found
        self.assertEqual(path, (root / SOUND_REL).resolve())
        self.assertEqual(content_type, "audio/mpeg")


class SoundHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zento-static-http-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        products = self.tmp / "products.json"
        products.write_text(json.dumps(PRODUCTS, ensure_ascii=False), encoding="utf-8")

        site = self.tmp / "site"
        (site / "assets" / "sounds").mkdir(parents=True)
        (site / "data").mkdir()
        (site / "index.html").write_text("<!doctype html><title>ZENTO</title>", encoding="utf-8")
        self.sound_bytes = (REPO / SOUND_REL).read_bytes() if (REPO / SOUND_REL).is_file() else b"ID3test"
        (site / SOUND_REL).write_bytes(self.sound_bytes)

        server = app.create_server(
            "127.0.0.1", 0,
            db_path=self.tmp / "test.db",
            products_path=products,
            admin_token="test-admin-token-0123456789abcdefghij",
            static_root=site,
            quiet=True,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.port = server.server_address[1]

        def stop():
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type", ""), resp.read()

    def test_login_sound_is_served_as_audio_mpeg(self):
        status, content_type, body = self.get("/" + SOUND_REL)
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(body, self.sound_bytes)



class ProfileDropsFilesTest(unittest.TestCase):
    """Профиль с каплями: движок отдаётся как JavaScript, разметка шапки на месте в обеих страницах."""

    def test_drops_js_is_served_as_javascript(self):
        found = app.resolve_static(REPO, "/drops.js")
        self.assertIsNotNone(found)
        self.assertIn("javascript", found[1])

    def test_both_pages_have_hero_markup_and_script_order(self):
        for page in ("index.html", "catalog.html"):
            html = (REPO / page).read_text(encoding="utf-8")
            for marker in ('id="pf-hero"', 'id="pf-cv-back"', 'id="pf-cv-front"', 'id="pf-rank"',
                           'id="pf-friends"', 'id="pf-follows"'):
                self.assertIn(marker, html, f"{page}: нет {marker}")
            self.assertLess(html.index('src="drops.js"'), html.index('src="profile.js"'), page)

    def test_drops_js_has_no_external_loads(self):
        code = (REPO / "drops.js").read_text(encoding="utf-8")
        for bad in ("http://", "https://", "eval(", "innerHTML"):
            self.assertNotIn(bad, code, bad)


if __name__ == "__main__":
    unittest.main()
