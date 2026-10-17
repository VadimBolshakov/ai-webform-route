from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
_hasher = PasswordHasher()


@lru_cache(maxsize=1)
def _breached() -> frozenset[str]:
    path = Path(__file__).with_name("breached.txt")
    with path.open(encoding="utf-8", errors="ignore") as f:
        return frozenset(line.strip().lower() for line in f if line.strip())


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(hash: str, pw: str) -> bool:
    try:
        return _hasher.verify(hash, pw)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(hash: str) -> bool:
    return _hasher.check_needs_rehash(hash)


def validate_password(pw: str, username: str) -> list[str]:
    errors: list[str] = []
    if len(pw) < MIN_LENGTH:
        errors.append(f"Пароль должен быть не короче {MIN_LENGTH} символов.")
    if pw.lower() == username.lower():
        errors.append("Пароль не должен совпадать с логином.")
    if pw.lower() in _breached():
        errors.append("Этот пароль есть в списках утёкших паролей, выберите другой.")
    return errors
