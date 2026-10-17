import time

import pyotp
import pytest
from fastapi import Depends
from sqlalchemy import func, select

from app.auth.deps import COOKIE_NAME, current_user
from app.models import AuditLog, AuthSession, User
from app.security.crypto import decrypt, encrypt
from app.security.totp import match_totp_step
from tests.test_auth import LOGIN, PASSWORD, login, make_user

TOTP = f"{LOGIN}/totp"


@pytest.fixture(autouse=True)
def probe(app):
    @app.get("/app/probe")
    async def _probe(user: User = Depends(current_user)):
        return {"u": user.username}


async def totp_user(db, settings, username="alice", role="user"):
    secret = pyotp.random_base32()
    u = await make_user(db, username, role, totp_secret_enc=encrypt(secret, settings.fernet_key), totp_enabled=True)
    return u, secret


async def csrf(db) -> str:
    return (
        await db.execute(select(AuthSession).order_by(AuthSession.id.desc()).limit(1))
    ).scalar_one().csrf_token


async def submit(client, db, code):
    return await client.post(TOTP, data={"code": code, "csrf_token": await csrf(db)})


async def test_totp_login_requires_code(client, db, settings):
    _, secret = await totp_user(db, settings)
    r = await login(client)
    assert r.status_code == 303 and r.headers["location"] == TOTP
    assert (await client.get("/app/probe")).status_code == 404
    assert (await client.get(TOTP)).status_code == 200
    pending_sid = client.cookies[COOKIE_NAME]
    r = await submit(client, db, pyotp.TOTP(secret).now())
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert client.cookies[COOKIE_NAME] != pending_sid  # ротация ID
    rows = (await db.execute(select(AuthSession))).scalars().all()
    assert len(rows) == 1 and rows[0].totp_pending is False
    assert (await client.get("/app/probe")).status_code == 200
    ev = (await db.execute(select(AuditLog.event))).scalars().all()
    assert ev.count("login_ok") == 1


async def test_totp_code_with_spaces_accepted(client, db, settings):
    _, secret = await totp_user(db, settings)
    await login(client)
    code = pyotp.TOTP(secret).now()
    r = await submit(client, db, f"{code[:3]} {code[3:]}")
    assert r.status_code == 303 and r.headers["location"] == "/app"


async def test_totp_wrong_code_5_times_revokes(client, db, settings):
    await totp_user(db, settings)
    await login(client)
    for _ in range(4):
        r = await submit(client, db, "000000")
        assert r.status_code == 401 and "Неверный код" in r.text
    r = await submit(client, db, "000000")
    assert r.status_code == 303 and r.headers["location"] == LOGIN
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    assert (await client.get(TOTP)).status_code == 404
    ev = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "login_fail" in ev


async def test_totp_failures_not_reset_by_new_password_login(client, db, settings):
    u, _ = await totp_user(db, settings)
    await login(client)
    for _ in range(4):
        await submit(client, db, "000000")
    # Новый вход по паролю не обнуляет счётчик: следующий неверный код блокирует.
    await login(client)
    r = await submit(client, db, "000000")
    assert r.status_code == 303
    await db.refresh(u)
    assert u.locked_until is not None


async def test_totp_replay_rejected_on_login(client, db, settings):
    _, secret = await totp_user(db, settings)
    await login(client)
    code = pyotp.TOTP(secret).now()
    assert (await submit(client, db, code)).headers["location"] == "/app"
    client.cookies.clear()
    await login(client)
    r = await submit(client, db, code)
    assert r.status_code == 401
    # Код следующего шага принимается.
    nxt = pyotp.TOTP(secret).at(time.time() + 30)
    r = await submit(client, db, nxt)
    assert r.status_code == 303 and r.headers["location"] == "/app"


async def test_totp_csrf_required(client, db, settings):
    _, secret = await totp_user(db, settings)
    await login(client)
    r = await client.post(TOTP, data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 403


def test_match_totp_step_window_and_last_step():
    secret = pyotp.random_base32()
    t = 1_700_000_000.0
    code = pyotp.TOTP(secret).at(t)
    step = int(t // 30)
    assert match_totp_step(secret, code, None, now=t) == step
    assert match_totp_step(secret, code, step, now=t) is None
    assert match_totp_step(secret, code, step - 1, now=t) == step
    assert match_totp_step(secret, code, None, now=t + 200) is None


async def test_admin_without_totp_redirected_to_setup(client, db):
    await make_user(db, "boss", "admin")
    await login(client, "boss")
    r = await client.get("/app/probe")
    assert r.status_code == 303 and r.headers["location"] == "/app/settings/2fa"
    assert (await client.get("/app/settings/2fa")).status_code == 200


async def test_twofa_exemption_is_exact_path(app, client, db):
    @app.get("/app/settings/2fa-evil")
    async def evil(user: User = Depends(current_user)):
        return {"ok": True}

    await make_user(db, "boss", "admin")
    await login(client, "boss")
    r = await client.get("/app/settings/2fa-evil")
    assert r.status_code == 303 and r.headers["location"] == "/app/settings/2fa"


async def test_enable_totp_flow(client, db, settings):
    u = await make_user(db)
    await login(client)
    r = await client.get("/app/settings/2fa")
    assert r.status_code == 200 and "<svg" in r.text and "style=" not in r.text.lower() and "<style" not in r.text.lower()
    await db.refresh(u)
    assert u.totp_enabled is False and u.totp_secret_enc
    secret = decrypt(u.totp_secret_enc, settings.fernet_key)
    assert secret not in u.totp_secret_enc
    token = await csrf(db)
    bad = await client.post("/app/settings/2fa/enable", data={"code": "000000", "csrf_token": token})
    assert bad.status_code == 400
    assert (await client.post("/app/settings/2fa/enable", data={"code": "1"})).status_code == 403
    code = pyotp.TOTP(secret).now()
    r = await client.post("/app/settings/2fa/enable", data={"code": code, "csrf_token": token})
    assert r.status_code == 303
    await db.refresh(u)
    assert u.totp_enabled is True and u.totp_last_step is not None
    assert "totp_enabled" in (await db.execute(select(AuditLog.event))).scalars().all()
    # Тот же код повторно (для disable) отвергается как повтор.
    r = await client.post(
        "/app/settings/2fa/disable",
        data={"code": code, "password": PASSWORD, "csrf_token": token},
    )
    assert r.status_code == 400
    await db.refresh(u)
    assert u.totp_enabled is True


async def test_disable_totp_requires_password_and_fresh_code(client, db, settings):
    u, secret = await totp_user(db, settings)
    await login(client)
    await submit(client, db, pyotp.TOTP(secret).now())
    token = await csrf(db)
    nxt = pyotp.TOTP(secret).at(time.time() + 30)
    r = await client.post(
        "/app/settings/2fa/disable",
        data={"code": nxt, "password": "wrong-wrong-wrong", "csrf_token": token},
    )
    assert r.status_code == 400
    await db.refresh(u)
    assert u.totp_enabled is True
    r = await client.post(
        "/app/settings/2fa/disable",
        data={"code": nxt, "password": PASSWORD, "csrf_token": token},
    )
    assert r.status_code == 303
    await db.refresh(u)
    assert u.totp_enabled is False and u.totp_secret_enc is None


async def test_admin_cannot_disable_totp(client, db, settings):
    _, secret = await totp_user(db, settings, "boss", "admin")
    await login(client, "boss")
    await submit(client, db, pyotp.TOTP(secret).now())
    token = await csrf(db)
    r = await client.post(
        "/app/settings/2fa/disable",
        data={
            "code": pyotp.TOTP(secret).at(time.time() + 30),
            "password": PASSWORD,
            "csrf_token": token,
        },
    )
    assert r.status_code == 400
    boss = (await db.execute(select(User))).scalar_one()
    await db.refresh(boss)
    assert boss.totp_enabled is True


async def test_disable_lockout_after_5_failures(client, db, settings):
    u, secret = await totp_user(db, settings)
    await login(client)
    await submit(client, db, pyotp.TOTP(secret).now())
    token = await csrf(db)
    nxt = pyotp.TOTP(secret).at(time.time() + 30)
    bad = {"code": "000000", "password": "wrong-wrong-wrong", "csrf_token": token}
    for _ in range(4):
        r = await client.post("/app/settings/2fa/disable", data=bad)
        assert r.status_code == 400
    r = await client.post("/app/settings/2fa/disable", data=bad)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    await db.refresh(u)
    assert u.locked_until is not None
    # Использованная сессия отозвана: верные данные уже не проходят.
    good = {"code": nxt, "password": PASSWORD, "csrf_token": token}
    assert (await client.post("/app/settings/2fa/disable", data=good)).status_code == 404
    await db.refresh(u)
    assert u.totp_enabled is True


async def test_disable_refused_while_locked(client, db, settings):
    from datetime import timedelta

    from app.auth.sessions import create_session, utcnow

    u, secret = await totp_user(db, settings)
    raw, s = await create_session(db, u, "1.1.1.1", "")
    u.locked_until = utcnow() + timedelta(minutes=5)
    await db.commit()
    client.cookies.set(COOKIE_NAME, raw)
    r = await client.post(
        "/app/settings/2fa/disable",
        data={
            "code": pyotp.TOTP(secret).at(time.time() + 30),
            "password": PASSWORD,
            "csrf_token": s.csrf_token,
        },
    )
    assert r.status_code == 400
    await db.refresh(u)
    assert u.totp_enabled is True
