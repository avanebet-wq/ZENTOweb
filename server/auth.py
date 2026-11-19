"""Пароли, сессии и cookie для входа покупателей.

Только стандартная библиотека. Без SQL и без HTTP: это работа с секретами.
Секреты (пароль, токен сессии, билет капчи) наружу не выводятся; в базе хранятся только хеши.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import re
import secrets

SESSION_COOKIE = "zento_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600          # 2592000, совпадает с Max-Age cookie
SESSION_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")   # 32 байта в base64 urlsafe без «=»

SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

# Частые пароли. Сравнение без учёта регистра.
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "passw0rd", "p@ssw0rd",
    "12345678", "123456789", "1234567890", "12341234", "11111111", "00000000",
    "qwerty123", "qwerty12", "qwertyui", "qwerty1234", "1q2w3e4r", "1q2w3e4r5t",
    "abc12345", "abcd1234", "iloveyou1", "letmein1", "qazwsx123", "welcome1",
    "admin123", "zento123", "zento2026", "zento12345", "user1234", "test1234",
})


# ---------------------------------------------------------------- пароль

def password_error(password, nick=""):
    """Текст ошибки для пароля или None, если пароль подходит. Пароль не должен содержать ник."""
    if not isinstance(password, str) or not password:
        return "Укажите пароль"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Пароль короче {PASSWORD_MIN_LENGTH} символов"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Пароль длиннее {PASSWORD_MAX_LENGTH} символов"
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return "Пароль содержит недопустимые символы"
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        return "Нужны и буквы, и цифры"
    if password.casefold() in COMMON_PASSWORDS:
        return "Этот пароль слишком простой, придумайте другой"
    if nick and nick.casefold() in password.casefold():
        return "Пароль не должен содержать ник"
    return None


def new_reset_code() -> str:
    """Шестизначный код для письма восстановления пароля (с ведущими нулями)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@functools.lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Хеш для неизвестного логина: проверка идёт так же долго, как для настоящего пользователя."""
    return hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- токены и cookie

def new_token() -> str:
    """Случайный токен: 32 байта, base64 urlsafe. Наружу отдаётся только он, в базе — его хеш."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_session_token(cookie_header):
    """Достаёт значение zento_session из заголовка Cookie. Нет или странный формат — None."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE and SESSION_TOKEN_RE.fullmatch(value):
            return value
    return None


def session_cookie(token: str, secure: bool) -> str:
    parts = [f"{SESSION_COOKIE}={token}", "HttpOnly", "SameSite=Lax", "Path=/",
             f"Max-Age={SESSION_TTL_SECONDS}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def expired_session_cookie(secure: bool) -> str:
    parts = [f"{SESSION_COOKIE}=", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
