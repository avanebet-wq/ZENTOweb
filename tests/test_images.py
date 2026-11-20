"""Тесты картинок профиля: проверка по сигнатуре, размеры, лимиты, загрузка, отдача и бэкап.

Картинки собираем здесь же: это настоящие заголовки PNG, JPEG и WEBP с нужными размерами.
Тело после заголовка может быть «мусором» (pad) — для лимитов размера этого достаточно.
Сервер поднимается на временной базе; data/store.db не трогаем.

Запуск из корня репозитория:
    python3 -m unittest discover -s tests -v
"""

import hashlib
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import test_api
from test_api import TOKEN, SHOP_BOT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import auth  # noqa: E402
import images  # noqa: E402
import store  # noqa: E402

MIB = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- сборка картинок для тестов

def png_bytes(width, height, pad=b""):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 3))
            + chunk(b"IEND", b"") + pad)


def jpeg_bytes(width, height, pad=b"", extra=b""):
    """extra — произвольные сегменты перед кадром (например APP и COM), чтобы проверить разбор маркеров."""
    jfif = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = b"\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    sof = b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame
    return b"\xff\xd8" + jfif + extra + sof + b"\xff\xd9" + pad


def webp_vp8x(width, height, pad=b""):
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body + pad


def webp_vp8(width, height, pad=b""):
    frame = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    body = b"WEBP" + b"VP8 " + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", len(body)) + body + pad


def webp_vp8l(width, height, pad=b""):
    payload = b"\x2f" + ((width - 1) | ((height - 1) << 14)).to_bytes(4, "little")
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body + pad


def sized(data, total):
    """Дополняет картинку хвостом до нужного размера (хвост после заголовков разбор не читает)."""
    assert len(data) <= total, (len(data), total)
    return data + b"\x00" * (total - len(data))


GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><script>alert(1)</script></svg>'


# ---------------------------------------------------------------- модуль images без сети

class ImageHeaderTest(unittest.TestCase):
    def test_signatures_recognize_three_types(self):
        self.assertEqual(images.sniff(png_bytes(4, 4)), "image/png")
        self.assertEqual(images.sniff(jpeg_bytes(4, 4)), "image/jpeg")
        self.assertEqual(images.sniff(webp_vp8x(4, 4)), "image/webp")

    def test_gif_svg_and_garbage_are_not_images(self):
        for data in (GIF, SVG, b"BM" + b"\x00" * 40, b"hello", b"", b"RIFF\x00\x00\x00\x00WAVE"):
            self.assertIsNone(images.sniff(data), data[:8])

    def test_header_type_accepts_only_three_types(self):
        self.assertEqual(images.header_type("image/png"), "image/png")
        self.assertEqual(images.header_type("Image/JPEG; charset=binary"), "image/jpeg")
        self.assertEqual(images.header_type("image/webp "), "image/webp")
        for value in ("image/gif", "image/svg+xml", "image/jpg", "text/plain", "", None):
            self.assertIsNone(images.header_type(value), value)


class ImageSizeTest(unittest.TestCase):
    def test_png_size_from_ihdr(self):
        self.assertEqual(images.png_size(png_bytes(640, 480)), (640, 480))

    def test_jpeg_size_after_extra_segments(self):
        comment = b"\xff\xfe" + struct.pack(">H", 14) + b"hello world!"
        self.assertEqual(images.jpeg_size(jpeg_bytes(300, 200)), (300, 200))
        self.assertEqual(images.jpeg_size(jpeg_bytes(300, 200, extra=comment + comment)), (300, 200))

    def test_webp_size_for_all_three_flavours(self):
        self.assertEqual(images.webp_size(webp_vp8(320, 240)), (320, 240))
        self.assertEqual(images.webp_size(webp_vp8l(77, 33)), (77, 33))
        self.assertEqual(images.webp_size(webp_vp8x(1000, 500)), (1000, 500))

    def test_truncated_headers_have_no_size(self):
        self.assertIsNone(images.png_size(png_bytes(10, 10)[:20]))
        self.assertIsNone(images.jpeg_size(b"\xff\xd8\xff\xe0\x00\x10JFIF"))
        self.assertIsNone(images.webp_size(webp_vp8(10, 10)[:20]))
        self.assertIsNone(images.jpeg_size(jpeg_bytes(10, 10)[:15]))     # обрыв внутри APP0, до кадра

    def test_jpeg_scan_before_frame_is_rejected(self):
        scan_first = b"\xff\xd8\xff\xda\x00\x02" + jpeg_bytes(10, 10)[2:]
        self.assertIsNone(images.jpeg_size(scan_first))


class InspectTest(unittest.TestCase):
    def test_returns_type_size_and_sha256(self):
        data = png_bytes(64, 32)
        meta = images.inspect(data, "image/png", "avatar")
        self.assertEqual(meta, {"content_type": "image/png", "width": 64, "height": 32,
                                "sha256": hashlib.sha256(data).hexdigest()})

    def test_side_limits_differ_for_avatar_and_banner(self):
        images.inspect(png_bytes(2048, 2048), "image/png", "avatar")
        images.inspect(png_bytes(4096, 4096), "image/png", "banner")
        with self.assertRaises(images.ImageError) as ctx:
            images.inspect(png_bytes(2049, 10), "image/png", "avatar")
        self.assertEqual((ctx.exception.code, list(ctx.exception.fields)), ("image_invalid", ["image"]))
        with self.assertRaises(images.ImageError) as ctx:
            images.inspect(webp_vp8x(10, 4097), "image/webp", "banner")
        self.assertEqual(ctx.exception.code, "image_invalid")

    def test_unknown_bytes_give_415_code(self):
        for data in (GIF, SVG):
            with self.assertRaises(images.ImageError) as ctx:
                images.inspect(data, "image/png", "avatar")
            self.assertEqual(ctx.exception.code, "image_type_unsupported")

    def test_header_must_match_bytes(self):
        with self.assertRaises(images.ImageError) as ctx:
            images.inspect(jpeg_bytes(10, 10), "image/png", "avatar")
        self.assertEqual(ctx.exception.code, "image_invalid")

    def test_empty_and_zero_size_are_invalid(self):
        for data, header in ((b"", "image/png"), (png_bytes(0, 10), "image/png")):
            with self.assertRaises(images.ImageError) as ctx:
                images.inspect(data, header, "avatar")
            self.assertEqual(ctx.exception.code, "image_invalid")


# ---------------------------------------------------------------- сервер: общая основа для профиля

def make_ticket(conn):
    """Билет капчи без картинок: нужен, чтобы регистрировать покупателей в тестах."""
    token = auth.new_token()
    now = store.utc_now()
    conn.execute(
        "INSERT INTO captcha_tickets (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (auth.token_hash(token), store.to_iso(now), store.to_iso(now + store.timedelta(hours=1))),
    )
    return token


class ProfileServerBase(test_api.ServerTestCase):
    """Сервер с покупателями и входом по cookie. Покупателей создаём прямо в базе (пароль Secret123)."""

    def make_user(self, nick, email=None, bio="", nick_changed_at=None):
        conn = sqlite3.connect(self.db_path)
        try:
            now = store.to_iso(store.utc_now())
            cur = conn.execute(
                "INSERT INTO users (nick, nick_lower, email, password_hash, created_at, rules_accepted_at,"
                " bio, nick_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (nick, nick.lower(), email, auth.hash_password("Secret123"), now, now, bio, nick_changed_at),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def session_for(self, user_id):
        """Заголовок Cookie для покупателя: сессия открыта прямо в базе."""
        token = auth.new_token()
        now = store.utc_now()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (auth.token_hash(token), user_id, store.to_iso(now),
                 store.to_iso(now + store.timedelta(days=1))),
            )
            conn.commit()
        finally:
            conn.close()
        return {"Cookie": f"{auth.SESSION_COOKIE}={token}"}

    def image_row(self, nick, kind):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT i.content_type, i.data, i.width, i.height, i.sha256 FROM user_images i"
                " JOIN users u ON u.id = i.user_id WHERE u.nick_lower = ? AND i.kind = ?",
                (nick.lower(), kind),
            ).fetchone()
        finally:
            conn.close()

    def image_count(self):
        return self.db_count("user_images")

    def upload(self, kind, data, content_type, headers=None):
        send = {"Content-Type": content_type, **(headers or {})}
        return self.request("PUT", f"/api/profile/{kind}", raw=data, headers=send)

    def assert_error(self, resp, status, code):
        self.assertEqual(resp.status, status, resp.body)
        self.assertIs(resp.body["ok"], False)
        self.assertEqual(resp.body["error"], code)


# ---------------------------------------------------------------- сервер: загрузка

class ImageUploadTest(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("AnnaPetrova", email="anna@example.com")
        self.cookie = self.session_for(self.user_id)

    def test_avatar_png_is_saved_in_database(self):
        data = png_bytes(300, 200)
        resp = self.upload("avatar", data, "image/png", self.cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(resp.body["ok"], True)
        self.assertTrue(resp.body["user"]["has_avatar"])
        self.assertFalse(resp.body["user"]["has_banner"])
        self.assertEqual(len(resp.body["user"]["avatar_v"]), 16)
        content_type, stored, width, height, sha = self.image_row("annapetrova", "avatar")
        self.assertEqual((content_type, bytes(stored), width, height), ("image/png", data, 300, 200))
        self.assertEqual(sha, hashlib.sha256(data).hexdigest())

    def test_banner_jpeg_and_webp_are_accepted(self):
        jpeg = self.upload("banner", jpeg_bytes(1200, 400), "image/jpeg", self.cookie)
        self.assertEqual(jpeg.status, 200, jpeg.body)
        self.assertEqual(self.image_row("annapetrova", "banner")[0], "image/jpeg")
        webp = self.upload("banner", webp_vp8l(800, 300), "image/webp", self.cookie)
        self.assertEqual(webp.status, 200, webp.body)
        self.assertEqual(self.image_row("annapetrova", "banner")[0], "image/webp")
        self.assertEqual(self.image_count(), 1)

    def test_gif_svg_and_missing_type_get_415(self):
        for data, header in ((GIF, "image/gif"), (SVG, "image/svg+xml"), (png_bytes(5, 5), "text/plain")):
            resp = self.upload("avatar", data, header, self.cookie)
            self.assert_error(resp, 415, "image_type_unsupported")
        resp = self.request("PUT", "/api/profile/avatar", raw=png_bytes(5, 5), headers=self.cookie)
        self.assert_error(resp, 415, "image_type_unsupported")
        resp = self.upload("avatar", GIF, "image/png", self.cookie)   # заголовок png, а внутри gif
        self.assert_error(resp, 415, "image_type_unsupported")
        self.assertEqual(self.image_count(), 0)

    def test_bytes_that_do_not_match_header_are_400(self):
        resp = self.upload("avatar", jpeg_bytes(20, 20), "image/png", self.cookie)
        self.assert_error(resp, 400, "image_invalid")
        self.assertEqual(list(resp.body["fields"]), ["image"])
        self.assertEqual(self.image_count(), 0)

    def test_avatar_limit_is_2_mib(self):
        base = png_bytes(10, 10)
        exact = sized(base, 2 * MIB)
        self.assertEqual(self.upload("avatar", exact, "image/png", self.cookie).status, 200)
        too_big = sized(base, 2 * MIB + 1)
        resp = self.upload("avatar", too_big, "image/png", self.cookie)
        self.assert_error(resp, 413, "avatar_too_large")

    def test_banner_limit_is_4_mib(self):
        base = webp_vp8x(10, 10)
        self.assertEqual(self.upload("banner", sized(base, 4 * MIB), "image/webp", self.cookie).status, 200)
        resp = self.upload("banner", sized(base, 4 * MIB + 1), "image/webp", self.cookie)
        self.assert_error(resp, 413, "banner_too_large")

    def test_five_mib_avatar_gets_413_not_a_dropped_connection(self):
        resp = self.upload("avatar", sized(png_bytes(10, 10), 5 * MIB), "image/png", self.cookie)
        self.assert_error(resp, 413, "avatar_too_large")
        self.assertEqual(self.image_count(), 0)

    def test_side_limits_over_http(self):
        self.assertEqual(self.upload("avatar", png_bytes(2048, 2048), "image/png", self.cookie).status, 200)
        resp = self.upload("avatar", png_bytes(2049, 100), "image/png", self.cookie)
        self.assert_error(resp, 400, "image_invalid")
        self.assertEqual(self.upload("banner", png_bytes(4096, 1), "image/png", self.cookie).status, 200)
        resp = self.upload("banner", png_bytes(1, 4097), "image/png", self.cookie)
        self.assert_error(resp, 400, "image_invalid")

    def test_upload_needs_session_and_bot_or_admin_token_is_refused(self):
        data = png_bytes(10, 10)
        for headers in ({}, {"Authorization": f"Bearer {SHOP_BOT}"}, {"Authorization": f"Bearer {TOKEN}"}):
            resp = self.upload("avatar", data, "image/png", headers)
            self.assert_error(resp, 401, "login_required")
            self.assertNotIn("user", resp.body)
        self.assertEqual(self.image_count(), 0)

    def test_replacing_keeps_one_row_and_changes_version(self):
        first = self.upload("avatar", png_bytes(10, 10), "image/png", self.cookie).body["user"]["avatar_v"]
        second = self.upload("avatar", jpeg_bytes(12, 12), "image/jpeg", self.cookie).body["user"]["avatar_v"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.image_count(), 1)
        self.assertEqual(self.image_row("annapetrova", "avatar")[0], "image/jpeg")

    def test_delete_removes_image_and_is_repeatable(self):
        self.upload("avatar", png_bytes(10, 10), "image/png", self.cookie)
        resp = self.request("DELETE", "/api/profile/avatar", headers=self.cookie)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertFalse(resp.body["user"]["has_avatar"])
        self.assertIsNone(resp.body["user"]["avatar_v"])
        again = self.request("DELETE", "/api/profile/avatar", headers=self.cookie)
        self.assertEqual(again.status, 200, again.body)
        self.assertEqual(self.image_count(), 0)
        self.assertEqual(self.request("GET", "/api/users/annapetrova/avatar", headers=self.cookie).status, 404)

    def test_upload_without_session_is_refused_before_body_is_read(self):
        # Шлём только заголовки с большой длиной и ни байта тела. Сервер, читающий тело первым,
        # ждал бы его до таймаута; здесь он обязан ответить 401 сразу.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(b"PUT /api/profile/avatar HTTP/1.1\r\nHost: test\r\n"
                     b"Content-Type: image/png\r\nContent-Length: 1000000\r\n\r\n")
        answer = b""
        while True:                                  # сервер закрывает соединение после ответа
            chunk = sock.recv(4096)
            if not chunk:
                break
            answer += chunk
        self.assertTrue(answer.startswith(b"HTTP/1.") and b" 401 " in answer.split(b"\r\n", 1)[0], answer[:80])
        self.assertIn(b"login_required", answer)
        self.assertEqual(self.image_count(), 0)

    def test_upload_rate_limit_per_user(self):
        self.start_server(admin_token=TOKEN, db_name="limits.db", auth_limits={"image_upload": (2, 3600)})
        user_id = self.make_user("Limited_One")
        cookie = self.session_for(user_id)
        data = png_bytes(10, 10)
        self.assertEqual(self.upload("avatar", data, "image/png", cookie).status, 200)
        self.assertEqual(self.upload("banner", data, "image/png", cookie).status, 200)
        resp = self.upload("avatar", data, "image/png", cookie)
        self.assert_error(resp, 429, "too_many_requests")
        self.assertIn("Retry-After", resp.headers)

    def test_cascade_removes_images_with_user(self):
        self.upload("avatar", png_bytes(10, 10), "image/png", self.cookie)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
            conn.commit()
            left = conn.execute("SELECT COUNT(*) FROM user_images").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(left, 0)


# ---------------------------------------------------------------- сервер: отдача и бэкап

class ImageServeTest(ProfileServerBase):
    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("AnnaPetrova")
        self.cookie = self.session_for(self.user_id)
        self.data = png_bytes(64, 48)
        self.upload("avatar", self.data, "image/png", self.cookie)

    def test_avatar_is_served_with_type_etag_and_private_cache(self):
        resp = self.request("GET", "/api/users/ANNApetrova/avatar", headers=self.cookie)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.raw, self.data)
        self.assertEqual(resp.headers["Content-Type"], "image/png")
        self.assertEqual(resp.headers["Cache-Control"], "private, max-age=3600")
        self.assertEqual(resp.headers["ETag"], f'"{hashlib.sha256(self.data).hexdigest()}"')
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

    def test_matching_etag_gets_304_without_body(self):
        etag = self.request("GET", "/api/users/annapetrova/avatar", headers=self.cookie).headers["ETag"]
        resp = self.request("GET", "/api/users/annapetrova/avatar",
                            headers={**self.cookie, "If-None-Match": etag})
        self.assertEqual(resp.status, 304)
        self.assertEqual(resp.raw, b"")
        self.assertEqual(resp.headers["Cache-Control"], "private, max-age=3600")

    def test_missing_image_or_user_is_404_for_logged_in(self):
        self.assertEqual(self.request("GET", "/api/users/annapetrova/banner", headers=self.cookie).status, 404)
        self.assertEqual(self.request("GET", "/api/users/nobodyhere/avatar", headers=self.cookie).status, 404)

    def test_guest_bot_and_admin_get_401(self):
        for headers in ({}, {"Authorization": f"Bearer {SHOP_BOT}"}, {"Authorization": f"Bearer {TOKEN}"}):
            for kind in ("avatar", "banner"):
                resp = self.request("GET", f"/api/users/annapetrova/{kind}", headers=headers)
                self.assert_error(resp, 401, "login_required")
                self.assertNotEqual(resp.headers.get("Content-Type"), "image/png")

    def test_other_api_answers_stay_no_store(self):
        resp = self.request("GET", "/api/users/annapetrova/profile", headers=self.cookie)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def test_backup_copy_contains_the_image(self):
        resp = self.admin("GET", "/api/admin/backup")
        self.assertEqual(resp.status, 200)
        copy = Path(tempfile.mkdtemp(prefix="zento-img-backup-"))
        self.addCleanup(shutil.rmtree, copy, True)
        backup_path = copy / "backup.db"
        backup_path.write_bytes(resp.raw)
        conn = sqlite3.connect(backup_path)
        try:
            row = conn.execute(
                "SELECT i.content_type, i.data, i.sha256 FROM user_images i"
                " JOIN users u ON u.id = i.user_id WHERE u.nick_lower = 'annapetrova' AND i.kind = 'avatar'"
            ).fetchone()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertEqual(row[0], "image/png")
        self.assertEqual(bytes(row[1]), self.data)
        self.assertEqual(row[2], hashlib.sha256(self.data).hexdigest())


if __name__ == "__main__":
    unittest.main()
