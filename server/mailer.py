"""Отправка писем через Brevo (HTTP API). Только стандартная библиотека.

Настройки читаются из переменных окружения при каждой отправке:
  BREVO_API_KEY — ключ Brevo (заголовок api-key);
  MAIL_FROM — адрес отправителя;
  MAIL_FROM_NAME — имя отправителя (по умолчанию ZENTO).
Ключ, адрес отправителя и адрес получателя не попадают ни в ответы, ни в логи, ни в тексты ошибок.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 10

RESET_SUBJECT = "Код для восстановления пароля ZENTO"
RESET_TEXT = ("Ваш код: {code}. Он действует 10 минут. "
              "Если вы не запрашивали код, просто проигнорируйте письмо.")


class MailError(Exception):
    """Письмо не отправлено. Текст ошибки наш и безопасный: ни ключа, ни адреса в нём нет."""


def _settings():
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip()
    sender_name = os.environ.get("MAIL_FROM_NAME", "").strip() or "ZENTO"
    if not api_key or not sender:
        raise MailError("почта не настроена")
    return api_key, sender, sender_name


def is_configured() -> bool:
    """Есть ли ключ и адрес отправителя. Сам ключ не возвращается."""
    try:
        _settings()
    except MailError:
        return False
    return True


def send_reset_code(to_address: str, code: str) -> None:
    """Отправляет письмо с кодом восстановления. Любой сбой — MailError."""
    api_key, sender, sender_name = _settings()
    payload = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": to_address}],
        "subject": RESET_SUBJECT,
        "textContent": RESET_TEXT.format(code=code),
    }
    request = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            response.read()
    except urllib.error.HTTPError:
        raise MailError("Brevo ответил ошибкой") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise MailError("нет связи с Brevo") from None
    if not 200 <= status < 300:
        raise MailError("Brevo ответил ошибкой")
