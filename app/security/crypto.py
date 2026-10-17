from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet(key: str | None) -> Fernet:
    return Fernet(key or get_settings().fernet_key)


def encrypt(s: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(s.encode()).decode()


def decrypt(s: str, key: str | None = None) -> str:
    return _fernet(key).decrypt(s.encode()).decode()
