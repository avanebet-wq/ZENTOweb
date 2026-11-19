"""HTTP-сервер ZENTO: статические файлы из корня репозитория и JSON API под /api.

Запуск из корня репозитория:
    ADMIN_TOKEN=... python3 server/app.py --port 8080

Только стандартная библиотека Python 3.13. База: data/store.db (создаётся сама).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "store.db"
DEFAULT_PRODUCTS = ROOT / "data" / "products.json"

MAX_BODY_BYTES = 64 * 1024
# Тело больше лимита читаем и выбрасываем (до 1 МБ), чтобы клиент получил ответ 413, а не обрыв связи.
DISCARD_LIMIT_BYTES = 1024 * 1024

# Эти папки никогда не отдаются как статика: в data/ лежит база с персональными данными.
HIDDEN_DIRS = {"server", "tests", "data", "__pycache__"}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    """Ответ с ошибкой: статус и тело JSON."""

    def __init__(self, status, body):
        super().__init__(status)
        self.status = status
        self.body = body


# ---------------------------------------------------------------- статика

def resolve_static(root: Path, url_path: str):
    """Возвращает (путь к файлу, Content-Type) или None. Не выпускает файлы за пределы root."""
    raw = unquote(url_path)
    if "\x00" in raw or "\\" in raw:
        return None
    if raw == "" or raw.endswith("/"):
        raw += "index.html"
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part.startswith(".") for part in parts):
        return None
    candidate = (root / Path(*parts)).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    # Проверяем уже после разрешения ссылок: симлинк не должен открывать скрытые папки.
    if any(part.startswith(".") or part in HIDDEN_DIRS for part in relative.parts):
        return None
    content_type = STATIC_TYPES.get(candidate.suffix.lower())
    if content_type is None or not candidate.is_file():
        return None
    return candidate, content_type


# ---------------------------------------------------------------- обработчики API

def _db(handler):
    return store.connect(handler.server.db_path)


def _run(handler, func, *args):
    """Открывает базу на один запрос и переводит ошибки store в HTTP-ответы."""
    conn = _db(handler)
    try:
        return func(conn, *args)
    except store.ValidationError as exc:
        raise ApiError(400, {"error": exc.message, "fields": exc.fields}) from None
    except store.NotFound:
        raise ApiError(404, {"error": "not found"}) from None
    finally:
        conn.close()


def _path_int(text):
    number = store._as_int(text)
    if number is None:
        raise ApiError(404, {"error": "not found"})
    return number


def _products_list(handler, params, query):
    category = (query.get("category") or "").strip() or None
    search = (query.get("q") or "").strip() or None
    if search and len(search) > store.MAX_SEARCH_LEN:
        raise ApiError(400, {"error": "query too long", "fields": {"q": "слишком длинный запрос"}})
    return 200, _run(handler, lambda c: store.list_products(c, category=category, search=search))


def _product_get(handler, params, query):
    product = _run(handler, store.get_product, params["id"])
    if product is None:
        raise ApiError(404, {"error": "not found"})
    return 200, product


def _order_create(handler, params, query):
    payload = handler._read_json()
    return 201, _run(handler, store.create_order, payload)


def _event_create(handler, params, query):
    payload = handler._read_json()
    _run(handler, store.record_event, payload)
    return 204, None


def _admin_stats(handler, params, query):
    return 200, _run(handler, store.get_stats)


def _admin_orders(handler, params, query):
    return 200, _run(handler, store.list_orders)


def _admin_order_patch(handler, params, query):
    order_id = _path_int(params["id"])
    payload = handler._read_json()
    if not isinstance(payload, dict):
        raise ApiError(400, {"error": "Проверьте заполненные поля", "fields": {"body": "ожидается объект"}})
    if "status" not in payload:
        raise ApiError(400, {"error": "Проверьте заполненные поля", "fields": {"status": "обязательное поле"}})
    return 200, _run(handler, store.update_order_status, order_id, payload["status"])


def _admin_customers(handler, params, query):
    return 200, _run(handler, store.list_customers)


def _admin_products(handler, params, query):
    return 200, _run(handler, lambda c: store.list_products(c, admin=True))


def _admin_product_create(handler, params, query):
    payload = handler._read_json()
    return 201, _run(handler, store.create_product, payload)


def _admin_product_patch(handler, params, query):
    payload = handler._read_json()
    return 200, _run(handler, store.update_product, params["id"], payload)


ROUTES = [
    ("GET", re.compile(r"^/api/products$"), _products_list),
    ("GET", re.compile(r"^/api/products/(?P<id>[^/]+)$"), _product_get),
    ("POST", re.compile(r"^/api/orders$"), _order_create),
    ("POST", re.compile(r"^/api/events$"), _event_create),
    ("GET", re.compile(r"^/api/admin/stats$"), _admin_stats),
    ("GET", re.compile(r"^/api/admin/orders$"), _admin_orders),
    ("PATCH", re.compile(r"^/api/admin/orders/(?P<id>[^/]+)$"), _admin_order_patch),
    ("GET", re.compile(r"^/api/admin/customers$"), _admin_customers),
    ("GET", re.compile(r"^/api/admin/products$"), _admin_products),
    ("POST", re.compile(r"^/api/admin/products$"), _admin_product_create),
    ("PATCH", re.compile(r"^/api/admin/products/(?P<id>[^/]+)$"), _admin_product_patch),
]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ZENTO"
    sys_version = ""

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        try:
            # Тело читаем сразу, даже если маршрут откажет (например 503): иначе сокет закроется с непрочитанными данными.
            self._prefetch_body()
            if path == "/api" or path.startswith("/api/"):
                status, body = self._route_api(method, path, query)
                self._send_json(status, body)
            elif method == "GET":
                self._send_static(path)
            else:
                raise ApiError(405, {"error": "method not allowed"})
        except ApiError as exc:
            self._send_json(exc.status, exc.body)
        except Exception:
            # В лог пишем только место ошибки, без тела запроса и заголовков.
            print(f"Внутренняя ошибка: {method} {path}", file=sys.stderr)
            traceback.print_exc()
            self._send_json(500, {"error": "internal error"})

    def _route_api(self, method, path, query):
        if path == "/api/admin" or path.startswith("/api/admin/"):
            if not self.server.admin_token:
                raise ApiError(503, {"error": "admin disabled"})
            if not self._is_admin():
                raise ApiError(401, {"error": "unauthorized"})
        path_matched = False
        for route_method, pattern, func in ROUTES:
            match = pattern.match(path)
            if match is None:
                continue
            path_matched = True
            if route_method != method:
                continue
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            return func(self, params, query)
        if path_matched:
            raise ApiError(405, {"error": "method not allowed"})
        raise ApiError(404, {"error": "not found"})

    def _is_admin(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        expected = self.server.admin_token.encode("utf-8")
        return hmac.compare_digest(token.strip().encode("utf-8"), expected)

    def _prefetch_body(self):
        """Читает тело запроса один раз. Слишком большое тело не сохраняем."""
        self._body = b""
        self._body_state = "ok"
        if self.headers.get("Transfer-Encoding"):
            self._body_state = "no-length"
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body_state = "no-length"
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._body_state = "bad-length"
            return
        if length < 0:
            self._body_state = "bad-length"
            return
        if length > MAX_BODY_BYTES:
            self._body_state = "too-large"
            if length <= DISCARD_LIMIT_BYTES:
                self.rfile.read(length)
            else:
                self.close_connection = True
            return
        self._body = self.rfile.read(length) if length else b""

    def _read_json(self):
        state = getattr(self, "_body_state", "no-length")
        if state == "no-length":
            raise ApiError(411, {"error": "length required"})
        if state == "bad-length":
            raise ApiError(400, {"error": "bad content-length"})
        if state == "too-large":
            raise ApiError(413, {"error": "payload too large"})
        try:
            return json.loads(self._body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, {"error": "invalid json"}) from None

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")

    def _send_json(self, status, body):
        self.send_response(status)
        self._security_headers()
        self.send_header("Cache-Control", "no-store")
        if status == 204 or body is None:
            self.end_headers()
            return
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, path):
        found = resolve_static(self.server.static_root, path)
        if found is None:
            raise ApiError(404, {"error": "not found"})
        file_path, content_type = found
        data = file_path.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # Ошибки, которые отдаёт сам http.server (например 501), тоже в JSON.
        self._send_json(code, {"error": message or "error"})

    def log_request(self, code="-", size="-"):
        # Пишем метод, путь без строки запроса и статус. Токен, тело и IP не пишем.
        if getattr(self.server, "quiet", False):
            return
        print(f"{self.command or '-'} {urlsplit(self.path).path} {code}", file=sys.stderr)

    def log_message(self, format, *args):
        # Стандартные сообщения содержат заголовки запроса, поэтому они отключены.
        pass


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, db_path, admin_token, static_root=ROOT, quiet=False):
        self.db_path = Path(db_path)
        self.admin_token = admin_token or None
        self.static_root = Path(static_root).resolve()
        self.quiet = quiet
        super().__init__(address, Handler)


def create_server(host, port, *, db_path=DEFAULT_DB, products_path=DEFAULT_PRODUCTS,
                  admin_token=None, static_root=ROOT, quiet=False) -> AppServer:
    """Готовит базу (создаёт и при необходимости заполняет) и возвращает сервер, который ещё не запущен."""
    store.init_db(db_path, products_path)
    return AppServer(
        (host, port),
        db_path=db_path,
        admin_token=admin_token,
        static_root=static_root,
        quiet=quiet,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сервер магазина ZENTO")
    parser.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="порт (по умолчанию 8080)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="файл базы SQLite")
    parser.add_argument("--products", default=str(DEFAULT_PRODUCTS), help="JSON с товарами для первого заполнения")
    args = parser.parse_args(argv)

    admin_token = os.environ.get("ADMIN_TOKEN", "").strip() or None
    if admin_token is None:
        print("ADMIN_TOKEN не задан: админка отключена (ответ 503).", file=sys.stderr)

    try:
        server = create_server(
            args.host, args.port,
            db_path=args.db, products_path=args.products, admin_token=admin_token,
        )
    except ValueError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1

    print(f"ZENTO запущен: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
