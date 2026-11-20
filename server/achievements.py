"""Достижения покупателей: коды, каталог и путь к картинкам.

Только данные, без SQL и без HTTP. Таблицу achievements заполняет store.seed_achievements()
(INSERT OR IGNORE), выдачу и список для профиля делает store.py. Порядок показа в профиле —
порядок этого каталога.
"""

from __future__ import annotations

FIRST_PURCHASE = "first_purchase"
FIRST_LOGIN = "first_login"
PROFILE_PHOTO = "profile_photo"
PROFILE_BIO = "profile_bio"

# Картинки лежат в assets/achievements/ и отдаются статикой сайта.
IMAGE_URL_PREFIX = "/assets/achievements/"

# Редкости от самой простой к самой редкой. tier 1..8 нужен интерфейсу: чем выше, тем заметнее название.
RARITIES = (
    {"key": "common", "name": "Common", "tier": 1},
    {"key": "uncommon", "name": "Uncommon", "tier": 2},
    {"key": "rare", "name": "Rare", "tier": 3},
    {"key": "mythical", "name": "Mythical", "tier": 4},
    {"key": "legendary", "name": "Legendary", "tier": 5},
    {"key": "ancient", "name": "Ancient", "tier": 6},
    {"key": "exceedingly_rare", "name": "Exceedingly Rare", "tier": 7},
    {"key": "immortal", "name": "Immortal", "tier": 8},
)
RARITY_BY_KEY = {item["key"]: item for item in RARITIES}

CATALOG = (
    {
        "code": FIRST_PURCHASE,
        "title": "Первая покупка",
        "description": "Награда за первый заказ в магазине ZENTO.",
        "how_to": "Сделайте первую покупку в магазине",
        "image": "first_purchase.svg",
        "rarity": "mythical",
    },
    {
        "code": FIRST_LOGIN,
        "title": "Первый вход",
        "description": "Награда за регистрацию на сайте ZENTO.",
        "how_to": "Зарегистрируйтесь на сайте",
        "image": "first_login.svg",
        "rarity": "common",
    },
    {
        "code": PROFILE_PHOTO,
        "title": "Фото профиля",
        "description": "Награда за фото в профиле.",
        "how_to": "Загрузите фото в профиль",
        "image": "profile_photo.svg",
        "rarity": "rare",
    },
    {
        "code": PROFILE_BIO,
        "title": "О себе",
        "description": "Награда за описание в профиле.",
        "how_to": "Напишите описание профиля",
        "image": "profile_bio.svg",
        "rarity": "uncommon",
    },
)

CODES = frozenset(item["code"] for item in CATALOG)
