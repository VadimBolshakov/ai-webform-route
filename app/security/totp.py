import io

import pyotp
import segno

ISSUER = "AI Notes"


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    if not (code.isascii() and code.isdigit()):
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def provisioning_qr_svg(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", xmldecl=False, nl=False, scale=4)
    return buf.getvalue().decode()
