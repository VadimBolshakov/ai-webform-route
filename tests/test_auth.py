from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import Depends
from sqlalchemy import func, select

from app.auth.deps import COOKIE_NAME, current_user, require_admin
from app.auth.sessions import create_session, get_session, revoke_all, touch
from app.cli import create_admin
from app.models import AuditLog, AuthSession, User
from app.security.passwords import hash_password
from app.security.tokens import hash_token

PASSWORD = "Correct-Horse-42"
LOGIN = "/a/test-login-token"


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def alert(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def notifier(app):
    n = FakeNotifier()
    app.state.notifier = n
    return n


async def make_user(db, username="alice", role="user", **kw) -> User:
    u = User(
        username=username,
        password_hash=hash_password(PASSWORD),
        role=role,
        monthly_limit_usd=Decimal("5"),
        **kw,
    )
    db.add(u)
    await db.commit()
    return u


async def login(client, username="alice", password=PASSWORD):
    return await client.post(LOGIN, data={"username": username, "password": password})


async def test_login_success_sets_cookie(client, db):
    await make_user(db)
    r = await login(client)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    low = cookie.lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in low
    assert "domain" not in low
    raw = r.cookies[COOKIE_NAME]
    row = (await db.execute(select(AuthSession))).scalar_one()
    assert row.id_hash == hash_token(raw) and raw not in (row.id_hash, row.csrf_token)


async def test_login_form_get_and_wrong_token_404(client):
    r = await client.get(LOGIN)
    assert r.status_code == 200
    assert 'action="/a/test-login-token"' in r.text and "<script" not in r.text
    ref = await client.get("/nonexistent")
    for method in (client.get, client.post):
        r = await method("/a/wrong-token")
        assert r.status_code == 404 and r.content == ref.content


async def test_login_wrong_password_generic_error(client, db):
    await make_user(db)
    bad_pw = await login(client, password="nope-nope-nope")
    no_user = await login(client, username="ghost")
    assert bad_pw.status_code == no_user.status_code == 401
    assert "Неверный логин или пароль" in bad_pw.text
    assert bad_pw.text == no_user.text
    assert COOKIE_NAME not in bad_pw.headers.get("set-cookie", "")


async def test_overlong_password_is_failure_without_hashing(client, db, monkeypatch):
    await make_user(db)
    calls = []
    monkeypatch.setattr(
        "app.auth.service.verify_password", lambda h, p: calls.append(p) or True
    )
    r = await login(client, password="x" * 257)
    assert r.status_code == 401 and not calls
    u = (await db.execute(select(User))).scalar_one()
    assert u.failed_logins == 1


async def test_lockout_after_5_failures(client, db, notifier):
    await make_user(db)
    for _ in range(5):
        assert (await login(client, password="bad-bad-bad-bad")).status_code == 401
    user = (await db.execute(select(User))).scalar_one()
    assert user.failed_logins == 5 and user.locked_until > datetime.now(timezone.utc)
    r = await login(client)  # correct password, but the account is locked
    assert r.status_code == 401 and COOKIE_NAME not in r.headers.get("set-cookie", "")
    assert len(notifier.sent) == 1
    assert "alice" in notifier.sent[0] and PASSWORD not in notifier.sent[0]
    assert "bad-bad-bad-bad" not in notifier.sent[0]


async def test_lockout_backoff_grows(client, db):
    await make_user(db)
    u = (await db.execute(select(User))).scalar_one()
    u.failed_logins = 6
    await db.commit()
    now = datetime.now(timezone.utc)
    await login(client, password="bad-bad-bad-bad")
    await db.refresh(u)
    assert u.failed_logins == 7
    delta = u.locked_until - now
    assert timedelta(minutes=3) < delta <= timedelta(minutes=4, seconds=5)


async def test_lockout_expires(client, db, notifier):
    await make_user(db)
    for _ in range(5):
        await login(client, password="bad-bad-bad-bad")
    u = (await db.execute(select(User))).scalar_one()
    u.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    r = await login(client)
    assert r.status_code == 303
    await db.refresh(u)
    assert u.failed_logins == 0 and u.locked_until is None


async def test_rehash_on_login(client, db):
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    await make_user(db)
    u = (await db.execute(select(User))).scalar_one()
    u.password_hash = weak
    await db.commit()
    assert (await login(client)).status_code == 303
    await db.refresh(u)
    assert u.password_hash != weak


async def test_ip_rate_limit(client, db, app):
    await make_user(db)
    for _ in range(20):
        await login(client, password="bad-bad-bad-bad")
    r = await login(client)  # 21st attempt from the IP, even with the correct password
    assert r.status_code == 429 and "Неверный логин или пароль" in r.text
    events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "limit_reached" in events


async def _session_cookie(client, db, **user_kw):
    u = await make_user(db, **user_kw)
    r = await login(client, username=u.username)
    assert r.status_code == 303
    return u


async def test_session_valid_then_expired_is_404(client, db):
    u = await _session_cookie(client, db)
    s = (await db.execute(select(AuthSession))).scalar_one()
    raw = client.cookies[COOKIE_NAME]
    assert await get_session(db, raw) is not None
    s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    assert await get_session(db, raw) is None
    ref = await client.get("/nonexistent")
    r = await client.post("/app/logout", headers={"X-CSRF-Token": s.csrf_token})
    assert r.status_code == 404 and r.content == ref.content
    assert u.id


async def test_blocked_user_session_is_404(client, db):
    u = await _session_cookie(client, db)
    s = (await db.execute(select(AuthSession))).scalar_one()
    u.is_active = False
    await db.commit()
    r = await client.post("/app/logout", headers={"X-CSRF-Token": s.csrf_token})
    assert r.status_code == 404
    assert await get_session(db, client.cookies[COOKIE_NAME]) is None


async def test_login_blocked_user_rejected(client, db):
    await make_user(db, is_active=False)
    assert (await login(client)).status_code == 401


async def test_logout_requires_csrf_and_revokes_session(client, db):
    await _session_cookie(client, db)
    s = (await db.execute(select(AuthSession))).scalar_one()
    assert (await client.post("/app/logout")).status_code == 403
    assert (await client.post("/app/logout", headers={"X-CSRF-Token": "bad"})).status_code == 403
    r = await client.post("/app/logout", headers={"X-CSRF-Token": s.csrf_token})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    r = await client.post("/app/logout", headers={"X-CSRF-Token": s.csrf_token})
    assert r.status_code == 404


async def test_old_cookie_ignored_and_revoked_on_new_login(client, db):
    await make_user(db)
    await login(client)
    old_raw = client.cookies[COOKIE_NAME]
    client.cookies.set(COOKIE_NAME, "attacker-chosen-value")
    r = await login(client)
    new_raw = r.cookies[COOKIE_NAME]
    assert new_raw != "attacker-chosen-value" and new_raw != old_raw
    client.cookies.set(COOKIE_NAME, old_raw)
    assert await get_session(db, old_raw) is not None  # someone else's valid cookie is not revoked

    client.cookies.set(COOKIE_NAME, old_raw)
    r = await login(client)  # logging in with a valid old cookie revokes it
    assert await get_session(db, old_raw) is None


async def test_audit_written_on_login(client, db):
    await make_user(db)
    await login(client, password="bad-bad-bad-bad")
    await login(client)
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    assert [r.event for r in rows] == ["login_fail", "login_ok"]
    assert rows[1].user_label == "alice" and rows[1].ip
    assert "bad-bad-bad-bad" not in str(rows[0].details)


async def test_touch_extends_not_more_than_every_10_min(db):
    u = await make_user(db)
    _, s = await create_session(db, u, "1.2.3.4", "ua")
    exp = s.expires_at
    await touch(db, s)
    assert s.expires_at == exp
    s.last_seen = datetime.now(timezone.utc) - timedelta(minutes=11)
    s.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    await touch(db, s)
    assert s.expires_at > datetime.now(timezone.utc) + timedelta(days=6)


async def test_revoke_all_except(db):
    u = await make_user(db)
    _, a = await create_session(db, u, "1.1.1.1", "")
    _, b = await create_session(db, u, "1.1.1.1", "")
    await revoke_all(db, u.id, except_id=a.id)
    ids = (await db.execute(select(AuthSession.id))).scalars().all()
    assert ids == [a.id]
    await revoke_all(db, u.id)
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    assert b.id


async def test_admin_without_totp_redirected_to_2fa(app, client, db):
    @app.get("/app/probe")
    async def probe(user: User = Depends(current_user)):
        return {"u": user.username}

    @app.get("/app/settings/2fa")
    async def twofa(user: User = Depends(current_user)):
        return {"ok": True}

    await _session_cookie(client, db, username="boss", role="admin")
    r = await client.get("/app/probe")
    assert r.status_code == 303 and r.headers["location"] == "/app/settings/2fa"
    assert (await client.get("/app/settings/2fa")).status_code == 200
    boss = (await db.execute(select(User))).scalar_one()
    boss.totp_enabled = True
    await db.commit()
    assert (await client.get("/app/probe")).json() == {"u": "boss"}


async def test_totp_pending_session_is_404_except_totp_path(app, client, db):
    @app.get("/app/probe")
    async def probe(user: User = Depends(current_user)):
        return {"u": user.username}

    @app.get("/a/test-login-token/totp")
    async def totp(user: User = Depends(current_user)):
        return {"ok": True}

    u = await make_user(db)
    raw, _ = await create_session(db, u, "1.1.1.1", "", totp_pending=True)
    await db.commit()
    client.cookies.set(COOKIE_NAME, raw)
    assert (await client.get("/app/probe")).status_code == 404
    assert (await client.get("/a/test-login-token/totp")).status_code == 200


async def test_require_admin_non_admin_404(app, client, db):
    @app.get("/app/adm")
    async def adm(user: User = Depends(require_admin)):
        return {"ok": True}

    await _session_cookie(client, db)
    assert (await client.get("/app/adm")).status_code == 404


async def test_405_on_private_paths_is_neutral_404(client):
    ref = await client.get("/nonexistent")
    r = await client.get("/app/logout")  # POST-only route
    assert r.status_code == 404 and r.content == ref.content
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    r = await client.put(LOGIN)
    assert r.status_code == 404 and r.content == ref.content


async def test_create_admin(db):
    u = await create_admin(db, "Root", "Correct-Horse-42")
    assert u.username == "root" and u.role == "admin" and u.monthly_limit_usd == 50
    assert u.password_hash.startswith("$argon2id$")
    with pytest.raises(ValueError):
        await create_admin(db, "root", "Correct-Horse-42")
    with pytest.raises(ValueError):
        await create_admin(db, "other", "short")
    events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert events == ["admin_created"]


async def test_touch_commits_when_extending(db, monkeypatch):
    u = await make_user(db)
    _, s = await create_session(db, u, "1.2.3.4", "ua")
    s.last_seen = datetime.now(timezone.utc) - timedelta(minutes=11)
    calls = []
    real = db.commit

    async def spy():
        calls.append(1)
        await real()

    monkeypatch.setattr(db, "commit", spy)
    await touch(db, s)
    assert calls == [1]
    await touch(db, s)  # too early - no write
    assert calls == [1]


async def test_sliding_expiry_persisted_on_read_request(app, client, db):
    @app.get("/app/probe")
    async def probe(user: User = Depends(current_user)):
        return {"ok": True}

    await _session_cookie(client, db)
    s = (await db.execute(select(AuthSession))).scalar_one()
    s.last_seen = datetime.now(timezone.utc) - timedelta(minutes=30)
    s.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    await db.commit()
    sid = s.id
    assert (await client.get("/app/probe")).status_code == 200
    await db.rollback()  # whatever is not committed disappears
    db.expunge_all()
    fresh = await db.get(AuthSession, sid)
    assert fresh.expires_at > datetime.now(timezone.utc) + timedelta(days=6)


async def test_overlong_fields_wrong_token_is_neutral_404(client):
    ref = await client.get("/nonexistent")
    r = await client.post("/a/wrong-token", data={"username": "u" * 1025, "password": "p"})
    assert r.status_code == 404 and r.content == ref.content


async def test_overlong_username_valid_token_generic_error_no_echo(client, db):
    await make_user(db)
    r = await login(client, username="u" * 1025, password="secret-pass-echo")
    assert r.status_code == 401 and "Неверный логин или пароль" in r.text
    assert "uuuu" not in r.text and "secret-pass-echo" not in r.text


async def test_validation_error_on_private_path_is_neutral_404(app, client):
    @app.get("/app/needs-int")
    async def needs_int(n: int):
        return {"n": n}

    ref = await client.get("/nonexistent")
    r = await client.get("/app/needs-int?n=abc")
    assert r.status_code == 404 and r.content == ref.content


async def test_lock_persists_when_notifier_raises(client, db, app):
    class Boom:
        async def alert(self, text: str) -> None:
            raise RuntimeError("down")

    app.state.notifier = Boom()
    await make_user(db)
    for _ in range(5):
        r = await login(client, password="bad-bad-bad-bad")
        assert r.status_code == 401
    await db.rollback()
    u = (await db.execute(select(User))).scalar_one()
    assert u.failed_logins == 5 and u.locked_until is not None
    assert (await login(client)).status_code == 401


async def test_ip_rate_limit_audited_once_per_window(client, db, app):
    for _ in range(25):
        await login(client, "nobody", "bad-bad-bad-bad")
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.event == "limit_reached"))
    ).scalars().all()
    assert len(rows) == 1 and rows[0].user_id is None and rows[0].details == {"scope": "login"}


def test_limiter_first_denial_once_per_window(monkeypatch):
    from app.security import ratelimit

    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    lim = ratelimit.SlidingWindowLimiter(1, 60)
    assert lim.hit("ip") and not lim.hit("ip")
    assert lim.first_denial("ip") is True
    assert lim.first_denial("ip") is False
    assert lim.first_denial("other") is True
    now[0] += 61
    assert lim.first_denial("ip") is True
