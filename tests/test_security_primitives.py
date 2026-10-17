import pyotp
from cryptography.fernet import Fernet

from app.security.crypto import decrypt, encrypt
from app.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.security.ratelimit import SlidingWindowLimiter
from app.security.tokens import hash_token, new_token
from app.security.totp import new_totp_secret, provisioning_qr_svg, verify_totp


def test_hash_and_verify():
    h = hash_password("Correct-Horse-42")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "Correct-Horse-42")
    assert not verify_password(h, "wrong")
    assert not verify_password("garbage", "x")
    assert not needs_rehash(h)


def test_validate_password():
    assert any("10" in e for e in validate_password("short", "alice"))
    assert any("утёк" in e for e in validate_password("password123", "alice"))
    assert any("логин" in e for e in validate_password("alicealice1", "alicealice1"))
    assert any("логин" in e for e in validate_password("ALICEALICE1", "alicealice1"))
    assert validate_password("Correct-Horse-42", "alice") == []
    assert len(validate_password("123456", "alice")) == 2


def test_token_hash_stable():
    t = new_token()
    assert new_token() != t
    assert hash_token(t) == hash_token(t)
    assert len(hash_token(t)) == 64
    assert hash_token("a") != hash_token("b")


def test_encrypt_roundtrip():
    key = Fernet.generate_key().decode()
    c = encrypt("секрет", key=key)
    assert c != "секрет"
    assert decrypt(c, key=key) == "секрет"


def test_encrypt_uses_settings_key(monkeypatch):
    from app.config import Settings

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.security.crypto.get_settings", lambda: Settings.model_construct(fernet_key=key)
    )
    assert decrypt(encrypt("x")) == "x"


def test_totp():
    secret = new_totp_secret()
    assert verify_totp(secret, pyotp.TOTP(secret).now())
    assert not verify_totp(secret, "000000" if pyotp.TOTP(secret).now() != "000000" else "111111")
    assert not verify_totp(secret, "abc")


def test_qr_svg():
    svg = provisioning_qr_svg(new_totp_secret(), "alice")
    assert svg.lstrip().startswith("<svg")
    assert "<?xml" not in svg


def test_limiter(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.security.ratelimit.time.monotonic", lambda: now[0])
    lim = SlidingWindowLimiter(limit=2, window_s=60)
    assert lim.hit("a") is True
    assert lim.hit("a") is True
    assert lim.hit("a") is False
    assert lim.hit("b") is True
    now[0] += 61
    assert lim.hit("a") is True


def test_limiter_prunes_memory(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.security.ratelimit.time.monotonic", lambda: now[0])
    lim = SlidingWindowLimiter(limit=1000, window_s=10)
    for _ in range(50):
        lim.hit("a")
    now[0] += 11
    lim.hit("a")
    assert len(lim._hits["a"]) == 1
