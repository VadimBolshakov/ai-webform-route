import re

USERNAME_RE = re.compile(r"[a-z0-9_.-]{3,32}")


def validate_username(name: str) -> list[str]:
    """Проверяет уже нормализованный (strip + lower) логин; то же правило, что CHECK в БД."""
    if USERNAME_RE.fullmatch(name):
        return []
    return ["Логин: от 3 до 32 символов, только латинские буквы a-z, цифры и _ . -"]
