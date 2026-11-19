"""Тесты Telegram-бота ZENTO. Сеть не используется: Telegram и магазин подменены.

Запуск из корня репозитория:
    python3 -m unittest discover -s bot/tests -v
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

FAKE_TOKEN = "123456:TEST-FAKE-TOKEN-do-not-use"

PRODUCTS = [
    {"id": 1, "name": "Наушники ZENTO Air", "category": "Аудио", "price": 7990, "stock": 24},
    {"id": 2, "name": "Колонка ZENTO Boom", "category": "Аудио", "price": 5490, "stock": 0},
    {"id": 3, "name": "Чехол", "category": "Аксессуары", "price": 299, "stock": 3},
]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_opener(data, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return FakeResponse(json.dumps(data).encode("utf-8"))
    return opener


class FakeTelegram:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.sent = []
        self.offsets = []

    def get_updates(self, offset):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeShop:
    def __init__(self, products=None, error=None):
        self._products = PRODUCTS if products is None else products
        self._error = error

    def products(self):
        if self._error:
            raise self._error
        return self._products


def message(text, chat_id=77, update_id=10):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class ConfigTest(unittest.TestCase):
    def test_token_is_required(self):
        with self.assertRaises(bot.ConfigError):
            bot.load_config({"SHOP_API_URL": "https://shop.example"})

    def test_shop_url_must_be_web_address(self):
        with self.assertRaises(bot.ConfigError):
            bot.load_config({"BOT_TOKEN": FAKE_TOKEN, "SHOP_API_URL": "shop.example"})

    def test_trailing_slash_is_removed_and_shop_token_optional(self):
        config = bot.load_config({"BOT_TOKEN": FAKE_TOKEN, "SHOP_API_URL": "https://shop.example/"})
        self.assertEqual(config["shop_url"], "https://shop.example")
        self.assertIsNone(config["shop_token"])


class FormatTest(unittest.TestCase):
    def test_price_uses_spaces_and_hryvnia_sign(self):
        self.assertEqual(bot.format_price(7990), "7 990 ₴")
        self.assertEqual(bot.format_price(299), "299 ₴")

    def test_catalog_lists_name_price_and_stock(self):
        text = bot.catalog_messages(PRODUCTS)[0]
        self.assertIn("Наушники ZENTO Air — 7 990 ₴, в наличии", text)
        self.assertIn("Колонка ZENTO Boom — 5 490 ₴, нет в наличии", text)

    def test_empty_catalog_has_clear_text(self):
        self.assertEqual(bot.catalog_messages([]), ["Товаров пока нет."])

    def test_long_catalog_is_split_into_short_messages(self):
        many = [dict(PRODUCTS[0], id=i, name="Товар " + "я" * 120) for i in range(100)]
        messages = bot.catalog_messages(many)
        self.assertGreater(len(messages), 1)
        for text in messages:
            self.assertLessEqual(len(text), bot.MESSAGE_LIMIT)

    def test_categories_count_products(self):
        text = bot.categories_text(PRODUCTS)
        self.assertIn("Аудио — 2", text)
        self.assertIn("Аксессуары — 1", text)

    def test_command_ignores_case_arguments_and_bot_name(self):
        self.assertEqual(bot.command_of("/Catalog@ZentoBot please"), "/catalog")
        self.assertEqual(bot.command_of(""), "")


class HandleTest(unittest.TestCase):
    def test_start_shows_help_without_asking_shop(self):
        telegram = FakeTelegram()
        shop = FakeShop(error=AssertionError("магазин не должен вызываться"))
        self.assertTrue(bot.handle_update(message("/start"), telegram, shop))
        self.assertEqual(telegram.sent, [(77, bot.HELP_TEXT)])

    def test_catalog_sends_products(self):
        telegram = FakeTelegram()
        bot.handle_update(message("/catalog"), telegram, FakeShop())
        self.assertIn("Чехол — 299 ₴", telegram.sent[0][1])

    def test_categories_command(self):
        telegram = FakeTelegram()
        bot.handle_update(message("/categories"), telegram, FakeShop())
        self.assertIn("Аудио — 2", telegram.sent[0][1])

    def test_shop_down_gives_friendly_text(self):
        telegram = FakeTelegram()
        bot.handle_update(message("/catalog"), telegram, FakeShop(error=OSError("нет сети")))
        self.assertEqual(telegram.sent, [(77, bot.UNAVAILABLE_TEXT)])

    def test_update_without_chat_is_skipped(self):
        telegram = FakeTelegram()
        self.assertFalse(bot.handle_update({"update_id": 1}, telegram, FakeShop()))
        self.assertEqual(telegram.sent, [])


class ShopClientTest(unittest.TestCase):
    def test_token_is_sent_as_bearer_when_configured(self):
        seen = []
        shop = bot.Shop("https://shop.example", token="shop-bot-token-0123", opener=json_opener(PRODUCTS, seen))
        self.assertEqual(len(shop.products()), 3)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer shop-bot-token-0123")
        self.assertEqual(seen[0].full_url, "https://shop.example/api/products")

    def test_no_authorization_header_without_token(self):
        seen = []
        bot.Shop("https://shop.example", opener=json_opener(PRODUCTS, seen)).products()
        self.assertFalse(seen[0].has_header("Authorization"))

    def test_unexpected_answer_is_an_error(self):
        shop = bot.Shop("https://shop.example", opener=json_opener({"error": "x"}))
        with self.assertRaises(bot.ShopError):
            shop.products()


class TelegramClientTest(unittest.TestCase):
    def test_get_updates_sends_offset_and_timeout(self):
        seen = []
        client = bot.Telegram(FAKE_TOKEN, opener=json_opener({"ok": True, "result": []}, seen))
        self.assertEqual(client.get_updates(42), [])
        self.assertIn(b"offset=42", seen[0].data)
        self.assertEqual(seen[0].full_url, f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates")

    def test_failed_answer_raises(self):
        client = bot.Telegram(FAKE_TOKEN, opener=json_opener({"ok": False}))
        with self.assertRaises(bot.TelegramError):
            client.send(1, "привет")


class RunLoopTest(unittest.TestCase):
    def test_offset_moves_past_handled_updates(self):
        telegram = FakeTelegram(batches=[[message("/start", update_id=10)], [message("/catalog", update_id=11)]])
        bot.run({"bot_token": FAKE_TOKEN}, telegram=telegram, shop=FakeShop(), sleep=lambda s: None, max_loops=3)
        self.assertEqual(telegram.offsets, [None, 11, 12])
        self.assertEqual(len(telegram.sent), 2)

    def test_connection_error_waits_and_keeps_going(self):
        class Flaky(FakeTelegram):
            def get_updates(self, offset):
                self.offsets.append(offset)
                if len(self.offsets) == 1:
                    raise OSError("нет связи")
                return [message("/start", update_id=5)]

        telegram = Flaky()
        waits = []
        bot.run({"bot_token": FAKE_TOKEN}, telegram=telegram, shop=FakeShop(), sleep=waits.append, max_loops=2)
        self.assertEqual(waits, [bot.RETRY_DELAY])
        self.assertEqual(len(telegram.sent), 1)

    def test_token_never_appears_in_logs(self):
        class Broken(FakeTelegram):
            def get_updates(self, offset):
                raise OSError(f"ошибка адреса /bot{FAKE_TOKEN}/getUpdates")

        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            bot.run({"bot_token": FAKE_TOKEN}, telegram=Broken(), shop=FakeShop(),
                    sleep=lambda s: None, max_loops=1)
        self.assertIn("Нет связи с Telegram", output.getvalue())
        self.assertNotIn(FAKE_TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
