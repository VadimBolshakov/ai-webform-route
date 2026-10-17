from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from app.auth.sessions import create_session, utcnow
from app.models import AllowedModel, AuditLog, AuthSession, Invite, User
from tests.test_auth import login, make_user
from tests.test_chat import signed_in

A = "/app/admin"
OR = "https://openrouter.ai/api/v1"


async def admin_in(client, db, username="boss"):
    admin = await make_user(db, username, "admin", totp_enabled=True, totp_secret_enc="x")
    await login(client, username)
    s = (
        await db.execute(
            select(AuthSession).where(AuthSession.user_id == admin.id).order_by(AuthSession.id.desc())
        )
    ).scalars().first()
    s.totp_pending = False
    await db.commit()
    return admin, s.csrf_token


async def post(client, path, csrf, **data):
    return await client.post(A + path, data={"csrf_token": csrf, **data})


async def events(db, name):
    return (await db.execute(select(AuditLog).where(AuditLog.event == name))).scalars().all()


async def test_admin_routes_404_for_user(client, db):
    _, csrf = await signed_in(client, db)
    assert (await client.get(A)).status_code == 404
    for path in ("/invites", "/models", "/users/1/limit", "/users/1/block", "/models/1/toggle"):
        assert (await post(client, path, csrf)).status_code == 404


async def test_admin_routes_404_without_session(client):
    assert (await client.get(A)).status_code == 404
    assert (await client.post(A + "/invites", data={})).status_code == 404


async def test_admin_page_renders_and_csrf_required(client, db):
    admin, csrf = await admin_in(client, db)
    bob = await make_user(db, "bob")
    _, s = await create_session(db, bob, "1.1.1.1", "UA")
    await db.commit()
    r = await client.get(A)
    assert r.status_code == 200
    for needle in ("boss", "bob", "Создать приглашение", "Добавить модель"):
        assert needle in r.text
    assert "style=" not in r.text.lower() and " onclick" not in r.text.lower()
    assert r.headers["cache-control"] == "no-store"
    # без CSRF — 403
    assert (await client.post(A + "/invites", data={})).status_code == 403
    assert (await client.post(f"{A}/users/{bob.id}/block", data={"csrf_token": "bad"})).status_code == 403


async def test_create_and_revoke_invite(client, db):
    admin, csrf = await admin_in(client, db)
    r = await post(client, "/invites", csrf)
    assert r.status_code == 200
    assert "/a/test-login-token/join/" in r.text and "Копировать" in r.text
    assert r.headers["cache-control"] == "no-store"
    inv = (await db.execute(select(Invite))).scalar_one()
    assert "активен" in (await client.get(A)).text
    # повторная загрузка страницы ссылку не показывает
    assert "/join/" not in (await client.get(A)).text
    r = await post(client, f"/invites/{inv.id}/revoke", csrf)
    assert r.status_code == 303
    await db.refresh(inv)
    assert inv.expires_at <= utcnow()
    assert "просрочен" in (await client.get(A)).text
    assert len(await events(db, "invite_created")) == 1
    assert len(await events(db, "invite_revoked")) == 1
    inv.used_at = utcnow()
    await db.commit()
    assert (await post(client, f"/invites/{inv.id}/revoke", csrf)).status_code == 400
    assert (await post(client, "/invites/99999/revoke", csrf)).status_code == 404


@pytest.mark.parametrize(
    "value,ok",
    [("10", True), ("0", True), ("1000", True), ("2.5", True), ("0.0001", True),
     ("1000.0001", False), ("-1", False), ("abc", False), ("", False), ("NaN", False),
     ("Infinity", False), ("1.00001", False), ("1001", False)],
)
async def test_set_limit_validation(client, db, value, ok):
    admin, csrf = await admin_in(client, db)
    bob = await make_user(db, "bob")
    r = await post(client, f"/users/{bob.id}/limit", csrf, limit=value)
    await db.refresh(bob)
    if ok:
        assert r.status_code == 303
        assert str(bob.monthly_limit_usd.normalize()) == str(Decimal(value).normalize())
        assert len(await events(db, "limit_changed")) == 1
    else:
        assert r.status_code == 400 and "Лимит" in r.text
        assert bob.monthly_limit_usd == 5


async def test_set_limit_unknown_user_404(client, db):
    _, csrf = await admin_in(client, db)
    assert (await post(client, "/users/99999/limit", csrf, limit="1")).status_code == 404


async def test_block_user_revokes_sessions(client, db):
    admin, csrf = await admin_in(client, db)
    bob = await make_user(db, "bob")
    await create_session(db, bob, "1.1.1.1", "UA")
    await create_session(db, bob, "1.1.1.2", "UA")
    await db.commit()
    r = await post(client, f"/users/{bob.id}/block", csrf)
    assert r.status_code == 303
    await db.refresh(bob)
    assert bob.is_active is False
    left = (await db.execute(select(AuthSession).where(AuthSession.user_id == bob.id))).scalars().all()
    assert left == []
    assert len(await events(db, "user_blocked")) == 1
    assert "Разблокировать" in (await client.get(A)).text
    r = await post(client, f"/users/{bob.id}/unblock", csrf)
    assert r.status_code == 303
    await db.refresh(bob)
    assert bob.is_active is True
    assert len(await events(db, "user_unblocked")) == 1


async def test_admin_cannot_block_self(client, db):
    admin, csrf = await admin_in(client, db)
    r = await post(client, f"/users/{admin.id}/block", csrf)
    assert r.status_code == 400
    await db.refresh(admin)
    assert admin.is_active is True


async def test_admin_cannot_block_other_admin(client, db):
    _, csrf = await admin_in(client, db)
    other = await make_user(db, "boss2", "admin")
    r = await post(client, f"/users/{other.id}/block", csrf)
    assert r.status_code == 400
    await db.refresh(other)
    assert other.is_active is True


async def test_last_activity_shown(client, db):
    admin, _ = await admin_in(client, db)
    bob = await make_user(db, "bob")
    page = (await client.get(A)).text
    assert "—" in page  # у bob сессий нет
    _, s = await create_session(db, bob, "1.1.1.1", "UA")
    await db.commit()
    assert s.last_seen.strftime("%d.%m.%Y %H:%M") in (await client.get(A)).text


@respx.mock
async def test_add_model_uses_openrouter_info(client, db):
    _, csrf = await admin_in(client, db)
    respx.get(f"{OR}/models").respond(
        200,
        json={"data": [
            {"id": "vendor/vision-1", "name": "Vision One",
             "architecture": {"input_modalities": ["text", "image"]}},
        ]},
    )
    r = await post(client, "/models", csrf, model_id="vendor/vision-1")
    assert r.status_code == 303
    m = (await db.execute(select(AllowedModel))).scalar_one()
    assert (m.model_id, m.display_name, m.supports_images, m.enabled) == (
        "vendor/vision-1", "Vision One", True, True)
    # дубликат
    r = await post(client, "/models", csrf, model_id="vendor/vision-1")
    assert r.status_code == 400 and "уже добавлена" in r.text
    # toggle + delete
    assert (await post(client, f"/models/{m.id}/toggle", csrf)).status_code == 303
    await db.refresh(m)
    assert m.enabled is False
    assert (await post(client, f"/models/{m.id}/delete", csrf)).status_code == 303
    assert (await db.execute(select(AllowedModel))).first() is None
    assert (await post(client, "/models/9999/toggle", csrf)).status_code == 404


@respx.mock
async def test_add_unknown_model_error(client, db):
    _, csrf = await admin_in(client, db)
    respx.get(f"{OR}/models").respond(200, json={"data": []})
    r = await post(client, "/models", csrf, model_id="vendor/nope")
    assert r.status_code == 400 and "Модель не найдена в OpenRouter" in r.text
    assert (await db.execute(select(AllowedModel))).first() is None


@pytest.mark.parametrize("bad", ["", "novendor", "A/B", "a/b c", "a/b/c", "../x/y", "x/" + "y" * 250])
async def test_add_model_rejects_bad_id_without_calling_openrouter(client, db, bad):
    _, csrf = await admin_in(client, db)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{OR}/models").respond(200, json={"data": []})
        r = await post(client, "/models", csrf, model_id=bad)
        assert r.status_code == 400
        assert not route.called


@pytest.mark.parametrize(
    "responder",
    [
        lambda route: route.respond(500, json={}),
        lambda route: route.respond(200, content=b"not json"),
        lambda route: route.respond(200, json={"data": [{"id": "vendor/x", "architecture": "bad"}]}),
        lambda route: setattr(route, "side_effect", httpx.ConnectError("x")),
    ],
)
async def test_add_model_openrouter_errors_no_500(client, db, responder):
    _, csrf = await admin_in(client, db)
    with respx.mock:
        responder(respx.get(f"{OR}/models"))
        r = await post(client, "/models", csrf, model_id="vendor/x")
    assert r.status_code == 502 and "OpenRouter недоступен" in r.text
    assert (await db.execute(select(AllowedModel))).first() is None
