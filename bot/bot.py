"""Telegram-бот магазина ZENTO.

Команды: /start, /catalog (товары и цены), /categories (категории).
Товары берёт с сайта через API магазина. Только стандартная библиотека Python 3.13.

Переменные окружения:
  BOT_TOKEN        токен от @BotFather (обязательно)
  SHOP_API_URL     адрес сайта, например https://zentoweb-production.up.railway.app (обязательно)
  SHOP_BOT_TOKEN   токен бота для API магазина (необязательно; у него нет прав админа)

Токены никогда не печатаются в лог и не попадают в ответы бота.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT = 25        # секунд: Telegram держит запрос, пока нет новых сообщений
HTTP_TIMEOUT = 35        # больше POLL_TIMEOUT, чтобы не оборвать ожидание
RETRY_DELAY = 5          # пауза после ошибки связи, секунд
MESSAGE_LIMIT = 3500     # Telegram не принимает сообщения длиннее 4096 символов
MAX_CATALOG_ITEMS = 300

HELP_TEXT = (
    "Здравствуйте! Это магазин ZENTO.\n\n"
    "/catalog — все товары и цены\n"
    "/categories — категории товаров\n\n"
    "Заказы через бота пока не оформляются: оформите заказ на сайте."
)
UNAVAILABLE_TEXT = "Магазин временно недоступен. Попробуйте чуть позже."


class ConfigError(Exception):
    pass


class ShopError(Exception):
    pass


class TelegramError(Exception):
    pass


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def load_config(env) -> dict:
    token = env.get("BOT_TOKEN", "").strip()
    shop_url = env.get("SHOP_API_URL", "").strip().rstrip("/")
    shop_token = env.get("SHOP_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN не задан")
    if not shop_url.startswith(("https://", "http://")):
        raise ConfigError("SHOP_API_URL должен начинаться с https://")
    return {"bot_token": token, "shop_url": shop_url, "shop_token": shop_token or None}


# ---------------------------------------------------------------- тексты ответов

def format_price(value) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₴"


def stock_text(stock) -> str:
    return "в наличии" if int(stock) > 0 else "нет в наличии"


def chunk_lines(lines, header):
    """Склеивает строки в сообщения, каждое короче лимита Telegram."""
    messages = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate) > MESSAGE_LIMIT:
            messages.append(current)
            current = line
        else:
            current = candidate
    messages.append(current)
    return messages


def catalog_messages(products) -> list[str]:
    if not products:
        return ["Товаров пока нет."]
    lines = [
        f"{item['name']} — {format_price(item['price'])}, {stock_text(item['stock'])}"
        for item in products[:MAX_CATALOG_ITEMS]
    ]
    return chunk_lines(lines, "Товары ZENTO:")


def categories_text(products) -> str:
    counts: dict[str, int] = {}
    for item in products:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    if not counts:
        return "Категорий пока нет."
    lines = [f"{name} — {count}" for name, count in sorted(counts.items())]
    return "Категории ZENTO:\n" + "\n".join(lines)


# ---------------------------------------------------------------- связь с Telegram и с сайтом

class Telegram:
    def __init__(self, token, opener=None):
        self.base = f"{TELEGRAM_API}/bot{token}/"
        self.opener = opener or urllib.request.urlopen

    def _call(self, method, params):
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(self.base + method, data=data)
        with self.opener(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise TelegramError(method)
        return body["result"]

    def get_updates(self, offset):
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params)

    def send(self, chat_id, text):
        self._call("sendMessage", {"chat_id": chat_id, "text": text})


class Shop:
    def __init__(self, base_url, token=None, opener=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def products(self) -> list:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base + "/api/products", headers=headers)
        with self.opener(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, list):
            raise ShopError("неожиданный ответ магазина")
        return data


# ---------------------------------------------------------------- обработка сообщений

def command_of(text: str) -> str:
    parts = (text or "").strip().split()
    if not parts:
        return ""
    # В группах Telegram пишет команду как /catalog@ИмяБота — убираем часть после @.
    return parts[0].lower().split("@")[0]


def reply_for(text: str, shop: Shop) -> list[str]:
    command = command_of(text)
    if command == "/catalog":
        return catalog_messages(shop.products())
    if command == "/categories":
        return [categories_text(shop.products())]
    return [HELP_TEXT]


def handle_update(update: dict, telegram: Telegram, shop: Shop) -> bool:
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return False
    try:
        replies = reply_for(message.get("text") or "", shop)
    except (OSError, ValueError, ShopError):
        replies = [UNAVAILABLE_TEXT]
    for text in replies:
        telegram.send(chat_id, text)
    return True


def run(config, telegram=None, shop=None, sleep=time.sleep, max_loops=None):
    """Бесконечно спрашивает Telegram о новых сообщениях (max_loops нужен только тестам)."""
    telegram = telegram or Telegram(config["bot_token"])
    shop = shop or Shop(config["shop_url"], config["shop_token"])
    offset = None
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            updates = telegram.get_updates(offset)
        except (OSError, ValueError, TelegramError) as exc:
            # Печатаем только тип ошибки: в тексте ошибки может быть адрес с токеном.
            log(f"Нет связи с Telegram ({type(exc).__name__}), повтор через {RETRY_DELAY} с")
            sleep(RETRY_DELAY)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update, telegram, shop)
            except (OSError, ValueError, TelegramError) as exc:
                log(f"Не удалось ответить на сообщение ({type(exc).__name__})")


def main() -> int:
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(f"Бот не запущен: {exc}", file=sys.stderr)
        return 1
    log("Бот запущен")
    try:
        run(config)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
