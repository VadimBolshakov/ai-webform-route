import hmac
import io
import time

import pyotp
import segno

ISSUER = "AI Notes"


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    if not (code.isascii() and code.isdigit()):
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def match_totp_step(
    secret: str, code: str, last_step: int | None = None, now: float | None = None
) -> int | None:
    """Шаг времени, которому соответствует код (окно ±1), либо None.

    Шаги <= last_step отвергаются: код нельзя использовать повторно.
    """
    if not (code.isascii() and code.isdigit()):
        return None
    totp = pyotp.TOTP(secret)
    current = int((time.time() if now is None else now) // totp.interval)
    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue
        expected = totp.at(step * totp.interval)
        if hmac.compare_digest(expected.encode(), code.encode()):
            return step
    return None


def provisioning_qr_svg(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", xmldecl=False, nl=False, scale=4)
    return buf.getvalue().decode()
