from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.invites import create_invite
from app.auth.sessions import COOKIE_NAME, create_session, touch
from app.auth.usernames import validate_username
from app.cli import create_admin
from app.models import AuditLog, AuthSession, Invite, User
from app.security.passwords import hash_password

PW = "Correct-Horse-42"
BASE = "/a/test-login-token"


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


async def make_admin(db) -> User:
    u = User(
        username="boss",
        password_hash=hash_password(PW),
        role="admin",
        monthly_limit_usd=Decimal("50"),
    )
    db.add(u)
    await db.commit()
    return u


async def new_invite(db, settings) -> tuple[str, str]:
    admin = await make_admin(db) if not (await db.execute(select(User))).first() else (
        await db.execute(select(User).where(User.username == "boss"))
    ).scalar_one()
    url = await create_invite(db, admin, settings)
    await db.commit()
    return url.rsplit("/", 1)[1], url


def form(**kw):
    data = {"username": "Newbie", "password": PW, "password2": PW, "accepted": "1"}
    data.update(kw)
    return data


async def test_create_invite_url_and_audit(db, settings):
    raw, url = await new_invite(db, settings)
    assert url == f"https://localhost:8443/a/test-login-token/join/{raw}"
    inv = (await db.execute(select(Invite))).scalar_one()
    delta = inv.expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=47, minutes=55) < delta <= timedelta(hours=48)
    events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "invite_created" in events


async def test_redeem_invite_creates_user_and_logs_in(client, db, settings, notifier):
    raw, _ = await new_invite(db, settings)
    r = await client.get(f"{BASE}/join/{raw}")
    assert r.status_code == 200 and "Политика конфиденциальности" in r.text
    r = await client.post(f"{BASE}/join/{raw}", data=form())
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low
    assert "path=/" in low and f"max-age={7 * 24 * 3600}" in low
    u = (await db.execute(select(User).where(User.username == "newbie"))).scalar_one()
    assert u.role == "user" and u.monthly_limit_usd == settings.default_monthly_limit_usd
    assert u.policy_accepted_at is not None and not u.totp_enabled
    s = (await db.execute(select(AuthSession).where(AuthSession.user_id == u.id))).scalar_one()
    assert not s.totp_pending
    inv = (await db.execute(select(Invite))).scalar_one()
    assert inv.used_by_id == u.id and inv.used_at is not None
    events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "invite_redeemed" in events
    assert notifier.sent == ["Новый пользователь: newbie"]


async def test_invite_single_use(client, db, settings):
    raw, _ = await new_invite(db, settings)
    assert (await client.post(f"{BASE}/join/{raw}", data=form())).status_code == 303
    client.cookies.clear()
    assert (await client.get(f"{BASE}/join/{raw}")).status_code == 404
    r = await client.post(f"{BASE}/join/{raw}", data=form(username="other"))
    assert r.status_code == 404
    assert "<form" not in r.text


async def test_invite_expired(client, db, settings):
    raw, _ = await new_invite(db, settings)
    inv = (await db.execute(select(Invite))).scalar_one()
    inv.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    assert (await client.get(f"{BASE}/join/{raw}")).status_code == 404
    assert (await client.post(f"{BASE}/join/{raw}", data=form())).status_code == 404


async def test_unknown_invite_and_wrong_token_404(client, db, settings):
    raw, _ = await new_invite(db, settings)
    assert (await client.get(f"{BASE}/join/nope")).status_code == 404
    assert (await client.post(f"{BASE}/join/nope", data=form())).status_code == 404
    assert (await client.get(f"/a/wrong/join/{raw}")).status_code == 404
    assert (await client.post(f"/a/wrong/join/{raw}", data=form())).status_code == 404


async def test_invite_requires_policy_acceptance(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(accepted=""))
    assert r.status_code == 400 and "политику" in r.text
    assert (await db.execute(select(User).where(User.username == "newbie"))).first() is None
    assert (await client.get(f"{BASE}/join/{raw}")).status_code == 200  # инвайт не сгорел


async def test_invite_weak_password_rejected(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(password="short", password2="short"))
    assert r.status_code == 400 and "не короче" in r.text
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")
    assert (await client.get(f"{BASE}/join/{raw}")).status_code == 200


async def test_password_mismatch(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(password2=PW + "x"))
    assert r.status_code == 400 and "Пароли не совпадают" in r.text
    assert PW not in r.text


async def test_invite_username_taken(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(username="BOSS"))
    assert r.status_code == 400 and "занят" in r.text
    assert (await client.get(f"{BASE}/join/{raw}")).status_code == 200


@pytest.mark.parametrize("name", ["ab", "a" * 33, "bad name", "юзер", "a/b", "na\nme"])
async def test_invite_bad_username_friendly_error(client, db, settings, name):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(username=name))
    assert r.status_code == 400 and "Логин:" in r.text


async def test_username_is_lowercased_and_stripped(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.post(f"{BASE}/join/{raw}", data=form(username="  MiXed.Name  "))
    assert r.status_code == 303
    assert (await db.execute(select(User).where(User.username == "mixed.name"))).scalar_one()


async def test_only_hash_stored(db, settings):
    raw, url = await new_invite(db, settings)
    hashes = (await db.execute(select(Invite.token_hash))).scalars().all()
    assert hashes and all(raw not in h and len(h) == 64 for h in hashes)


async def test_join_page_referrer_same_origin_and_private_headers(client, db, settings):
    raw, _ = await new_invite(db, settings)
    r = await client.get(f"{BASE}/join/{raw}")
    # same-origin: токен инвайта не уходит в Referer на чужие сайты.
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["cache-control"] == "no-store"
    assert f'href="{BASE}/privacy"' in r.text


async def test_privacy_page(client):
    r = await client.get(f"{BASE}/privacy")
    assert r.status_code == 200
    for needle in ("OpenRouter", "3 месяцев", "доступ к серверу", "удалить аккаунт"):
        assert needle in r.text
    assert (await client.get("/a/wrong/privacy")).status_code == 404


def test_validate_username():
    assert validate_username("good_name-1.x") == []
    assert validate_username("ab") and validate_username("Upper") and validate_username("x\n")


async def test_create_admin_rejects_bad_username(db):
    with pytest.raises(ValueError):
        await create_admin(db, "a b", PW)
    with pytest.raises(ValueError):
        await create_admin(db, "ab", PW)


async def test_pending_session_short_ttl_and_not_extended(db):
    u = await make_admin(db)
    _, s = await create_session(db, u, "1.1.1.1", "ua", totp_pending=True)
    now = datetime.now(timezone.utc)
    assert timedelta(minutes=9) < s.expires_at - now <= timedelta(minutes=10)
    s.last_seen = now - timedelta(minutes=30)
    before = s.expires_at
    await touch(db, s)
    assert s.expires_at == before
    _, full = await create_session(db, u, "1.1.1.1", "ua")
    assert full.expires_at - now > timedelta(days=6)
