"""Колесо фортуны: призы и выбор приза. Результат выбирает только сервер, браузер его не считает.

Призы виртуальные: без денежной ценности, без скидок и промокодов, без автоматического эффекта на заказ.
Одна прокрутка в сутки на пользователя (день по Киеву) — это правило хранит store.py (таблица roulette_spins).

Только стандартная библиотека.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

# Сегменты колеса. weight — относительный вес (сумма не обязана быть 100).
PRIZES = [
    {"id": "again", "label": "Попробуйте завтра", "weight": 40},
    {"id": "sticker", "label": "Стикер ZENTO", "weight": 15},
    {"id": "badge", "label": "Значок «Удачливый»", "weight": 14},
    {"id": "postcard", "label": "Открытка ZENTO", "weight": 12},
    {"id": "tea", "label": "Бонус: чашка чая от ZENTO (при встрече)", "weight": 10},
    {"id": "tip", "label": "Совет дня от ZENTO", "weight": 9},
]

# Случайность для розыгрыша: системный генератор ОС, не random.random().
_SYSTEM_RANDOM = secrets.SystemRandom()


def now() -> datetime:
    """Текущее время в UTC. Тесты подменяют эту функцию, чтобы проверить смену суток."""
    return datetime.now(timezone.utc)


def public_prizes() -> list[dict]:
    """Призы для браузера: только id и текст. Веса наружу не отдаём."""
    return [{"id": prize["id"], "label": prize["label"]} for prize in PRIZES]


def pick_prize(rng=None) -> dict:
    """Выбирает приз с учётом весов. rng — генератор с методами choices/random (для тестов)."""
    generator = _SYSTEM_RANDOM if rng is None else rng
    prize = generator.choices(PRIZES, weights=[item["weight"] for item in PRIZES], k=1)[0]
    return {"id": prize["id"], "label": prize["label"]}
