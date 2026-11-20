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

CATALOG = (
    {
        "code": FIRST_PURCHASE,
        "title": "Первая покупка",
        "description": "Награда за первый заказ в магазине ZENTO.",
        "how_to": "Сделайте первую покупку в магазине",
        "image": "first_purchase.svg",
    },
    {
        "code": FIRST_LOGIN,
        "title": "Первый вход",
        "description": "Награда за регистрацию на сайте ZENTO.",
        "how_to": "Зарегистрируйтесь на сайте",
        "image": "first_login.svg",
    },
    {
        "code": PROFILE_PHOTO,
        "title": "Фото профиля",
        "description": "Награда за фото в профиле.",
        "how_to": "Загрузите фото в профиль",
        "image": "profile_photo.svg",
    },
    {
        "code": PROFILE_BIO,
        "title": "О себе",
        "description": "Награда за описание в профиле.",
        "how_to": "Напишите описание профиля",
        "image": "profile_bio.svg",
    },
)

CODES = frozenset(item["code"] for item in CATALOG)
