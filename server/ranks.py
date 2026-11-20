"""Ранги покупателей и опыт (XP). Только данные и простая математика, без SQL и HTTP.

15 уровней. Порог уровня n: 30 * (n - 1)^2 XP, то есть каждый следующий ранг требует всё больше:
2-й уровень с 30 XP, 5-й с 480, 10-й с 2430, 15-й с 5880. Ежедневный вход даёт 10 XP,
значит, высший ранг только за входы — около 590 дней, а с сообщениями и лайками быстрее.
"""

from __future__ import annotations

MAX_LEVEL = 15

# Название ранга по уровню (индекс 0 = уровень 1).
NAMES = (
    "Новичок", "Гость", "Знакомый", "Участник", "Постоянный",
    "Активист", "Бывалый", "Знаток", "Мастер", "Эксперт",
    "Наставник", "Хранитель", "Легенда", "Титан", "Бессмертный",
)

# За что начисляется опыт: amount за одно действие, cap — потолок в сутки (по Киеву).
XP_RULES = {
    "login": {"amount": 10, "cap": 10},    # ежедневный вход, раз в сутки
    "message": {"amount": 2, "cap": 20},   # сообщение в чате (чат появится позже)
    "like": {"amount": 1, "cap": 10},      # лайк (лайки появятся позже)
}


def threshold(level: int) -> int:
    """Сколько XP нужно, чтобы получить уровень."""
    return 30 * (level - 1) ** 2


def level_for(xp: int) -> int:
    level = 1
    while level < MAX_LEVEL and xp >= threshold(level + 1):
        level += 1
    return level


def rank_for(xp: int) -> dict:
    """Всё о ранге для показа: уровень, название, XP, границы текущего уровня и следующий порог."""
    xp = max(0, int(xp))
    level = level_for(xp)
    last = level >= MAX_LEVEL
    return {
        "level": level,
        "name": NAMES[level - 1],
        "max_level": MAX_LEVEL,
        "xp": xp,
        "floor": threshold(level),
        "next_at": None if last else threshold(level + 1),
    }
