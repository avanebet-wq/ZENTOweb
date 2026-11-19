"""Капча-пазл: картинка режется на 4 части (2x2), их нужно положить на свои места.

Модуль без базы и без HTTP: берёт список картинок, готовит задание и сверяет расстановку.
Часть с номером piece (0 верх-лево, 1 верх-право, 2 низ-лево, 3 низ-право) лежит в ячейке с тем же номером.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

GRID = 2
PIECES = GRID * GRID
MIN_IMAGES = 10
CHALLENGE_TTL = 120     # секунд живёт задание
TICKET_TTL = 300        # секунд живёт билет после правильной сборки
URL_PREFIX = "/assets/captcha/"
IMAGE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.svg")

_random = secrets.SystemRandom()


def load_images(folder, url_prefix: str = URL_PREFIX) -> list:
    """Адреса картинок вида /assets/captcha/NN.svg. Нет папки — пустой список, без ошибки."""
    try:
        names = sorted(p.name for p in Path(folder).iterdir()
                       if p.is_file() and IMAGE_NAME_RE.fullmatch(p.name))
    except OSError:
        return []
    return [url_prefix + name for name in names]


def is_available(images) -> bool:
    return len(images) >= MIN_IMAGES


def new_challenge(images) -> dict:
    """Новое задание: id, правильные ячейки {токен: piece}, картинка и кусочки в случайном порядке."""
    tokens = [secrets.token_urlsafe(12) for _ in range(PIECES)]   # индекс в списке = номер части
    expected = {token: piece for piece, token in enumerate(tokens)}
    tiles = [{"token": token, "piece": piece} for token, piece in expected.items()]
    _random.shuffle(tiles)
    return {
        "challenge_id": secrets.token_urlsafe(24),
        "expected": expected,
        "image": _random.choice(list(images)),
        "tiles": tiles,
    }


def evaluate(expected: dict, placements: dict) -> str:
    """'incomplete' — не все части положены (задание не сгорает);
    'wrong' — хотя бы одна часть не на своём месте (задание сгорает); 'ok' — всё верно.
    placements уже проверен: значения None или целые 0..3, ключи из expected."""
    if any(placements.get(token) is None for token in expected):
        return "incomplete"
    if all(placements[token] == piece for token, piece in expected.items()):
        return "ok"
    return "wrong"
