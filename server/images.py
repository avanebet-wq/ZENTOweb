"""Картинки профиля: проверка по сигнатуре и чтение размеров из заголовков файла.

Только стандартная библиотека. Без SQL и без HTTP: сохраняет картинку store.save_image,
а отвечает за неё app.py. Принимаем только PNG, JPEG и WEBP. SVG (он может нести скрипты),
GIF и всё остальное отклоняем. Картинку не декодируем: читаем только заголовки.
"""

from __future__ import annotations

import hashlib
import struct

AVATAR_MAX_BYTES = 2 * 1024 * 1024
BANNER_MAX_BYTES = 4 * 1024 * 1024
AVATAR_MAX_SIDE = 2048
BANNER_MAX_SIDE = 4096
MAX_BYTES = {"avatar": AVATAR_MAX_BYTES, "banner": BANNER_MAX_BYTES}
MAX_SIDE = {"avatar": AVATAR_MAX_SIDE, "banner": BANNER_MAX_SIDE}

ALLOWED_TYPES = ("image/png", "image/jpeg", "image/webp")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
# Маркеры JPEG с размерами кадра: SOF0..SOF15, кроме DHT (C4), JPG (C8) и DAC (CC).
JPEG_SOF_MARKERS = frozenset(m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC))


class ImageError(Exception):
    """Картинка не подходит. code — код ответа (см. app.py), fields — ошибки полей."""

    def __init__(self, code, fields=None, message=None):
        super().__init__(code)
        self.code = code
        self.fields = fields
        self.message = message


def header_type(value):
    """Тип из заголовка Content-Type (без параметров), если он из списка разрешённых. Иначе None."""
    if not isinstance(value, str):
        return None
    media = value.split(";", 1)[0].strip().lower()
    return media if media in ALLOWED_TYPES else None


def sniff(data: bytes):
    """Тип по первым байтам файла: image/png, image/jpeg, image/webp или None."""
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(JPEG_SIGNATURE):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def png_size(data: bytes):
    """Ширина и высота из заголовка IHDR. None, если заголовка нет или он короткий."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def jpeg_size(data: bytes):
    """Ширина и высота из первого кадра SOF. None, если кадра нет до начала данных (SOS) или файл испорчен."""
    position = 2
    end = len(data)
    while position < end:
        if data[position] != 0xFF:
            return None
        while position < end and data[position] == 0xFF:      # байты-заполнители перед маркером
            position += 1
        if position >= end:
            return None
        marker = data[position]
        position += 1
        if marker in (0x00, 0xD9, 0xDA):                       # пусто, конец файла, начало данных до кадра
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:           # маркеры без длины
            continue
        if position + 2 > end:
            return None
        (length,) = struct.unpack(">H", data[position:position + 2])
        if length < 2 or position + length > end:
            return None
        if marker in JPEG_SOF_MARKERS:
            if length < 8:
                return None
            height, width = struct.unpack(">HH", data[position + 3:position + 7])
            return width, height
        position += length
    return None


def webp_size(data: bytes):
    """Ширина и высота из заголовка VP8 (с потерями), VP8L (без потерь) или VP8X (расширенный)."""
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    return None


SIZE_READERS = {"image/png": png_size, "image/jpeg": jpeg_size, "image/webp": webp_size}


def inspect(data: bytes, header: str, kind: str) -> dict:
    """Проверяет картинку для вида kind ('avatar' или 'banner').

    header — тип из Content-Type (уже прошёл header_type). Возвращает dict:
    content_type, width, height, sha256. При отказе бросает ImageError."""
    if not data:
        raise ImageError("image_invalid", {"image": "файл пустой"}, "Картинка пустая")
    found = sniff(data)
    if found is None:
        raise ImageError("image_type_unsupported")
    if found != header:
        raise ImageError("image_invalid", {"image": "тип файла не совпадает с заголовком"},
                         "Тип файла не совпадает с заголовком запроса")
    size = SIZE_READERS[found](data)
    if size is None or size[0] < 1 or size[1] < 1:
        raise ImageError("image_invalid", {"image": "не удаётся прочитать размер картинки"},
                         "Картинка повреждена")
    width, height = size
    limit = MAX_SIDE[kind]
    if width > limit or height > limit:
        raise ImageError("image_invalid", {"image": f"сторона картинки не больше {limit} px"},
                         f"Сторона картинки больше {limit} px")
    return {
        "content_type": found,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
