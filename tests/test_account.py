import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.deps import COOKIE_NAME
from app.auth.sessions import create_session
from app.chat.context import build_messages
from app.models import AllowedModel, AuditLog, AuthSession, Chat, Message, Usage, User
from tests.fakes import FakeNotifier
from tests.test_auth import PASSWORD, make_user
from tests.test_chat import make_chat, signed_in

S = "/app/settings"
NEW_PW = "Another-Strong-Pass-77"


@pytest.fixture(autouse=True)
def notifier(app):
    n = FakeNotifier()
    app.state.notifier = n
    return n


def prefs(**kw):
    d = {"answer_language": "ru", "theme": "system", "system_prompt": "", "default_model": "openrouter/auto"}
    return d | kw


async def post(client, path, csrf, **data):
    return await client.post(S + path, data={"csrf_token": csrf, **data})


async def test_settings_page_renders_sections(client, db):
    await signed_in(client, db)
    r = await client.get(S)
    assert r.status_code == 200
    for needle in ("Ответы", "Смена пароля", "Активные устройства", "Расход", "Удаление аккаунта"):
        assert needle in r.text
    assert 'data-theme="system"' in r.text
    assert "style=" not in r.text.lower() and " onclick" not in r.text.lower()


async def test_update_preferences_and_invalid_values(client, db):
    u, csrf = await signed_in(client, db)
    db.add(AllowedModel(model_id="v/m", display_name="M", enabled=True))
    db.add(AllowedModel(model_id="v/off", display_name="Off", enabled=False))
    await db.commit()
    r = await post(client, "/preferences", csrf, **prefs(
        answer_language="en", theme="dark", system_prompt="Будь кратким", default_model="v/m"))
    assert r.status_code == 303
    await db.refresh(u)
    assert (u.answer_language, u.theme, u.system_prompt, u.default_model) == ("en", "dark", "Будь кратким", "v/m")
    assert 'data-theme="dark"' in (await client.get(S)).text
    for bad in (
        prefs(answer_language="de"), prefs(theme="pink"), prefs(system_prompt="x" * 2001),
        prefs(default_model="v/off"), prefs(default_model="nope/x"),
    ):
        r = await post(client, "/preferences", csrf, **bad)
        assert r.status_code == 400 and 'role="alert"' in r.text
    await db.refresh(u)
    assert u.theme == "dark" and u.default_model == "v/m"
    assert (await post(client, "/preferences", "bad", **prefs())).status_code == 403
    nul = await post(client, "/preferences", csrf, **prefs(system_prompt="a\x00b"))
    assert nul.status_code == 303
    await db.refresh(u)
    assert u.system_prompt == "ab"


async def test_language_applied_to_system_prompt(client, db):
    u, csrf = await signed_in(client, db)
    await post(client, "/preferences", csrf, **prefs(answer_language="en"))
    await db.refresh(u)
    msgs = build_messages(u, [], "hi", 1000)
    assert "Answer in English." in msgs[0]["content"]


async def test_change_password_revokes_other_sessions(client, db):
    u, csrf = await signed_in(client, db)
    _, other = await create_session(db, u, "2.2.2.2", "Other")
    await db.commit()
    old_cookie = client.cookies[COOKIE_NAME]
    r = await post(client, "/password", csrf, current_password=PASSWORD, new_password=NEW_PW, new_password2=NEW_PW)
    assert r.status_code == 303
    assert client.cookies[COOKIE_NAME] != old_cookie
    rows = (await db.execute(select(AuthSession).where(AuthSession.user_id == u.id))).scalars().all()
    assert len(rows) == 1 and rows[0].id != other.id
    assert (await client.get(S)).status_code == 200
    assert "password_changed" in (await db.execute(select(AuditLog.event))).scalars().all()
    await db.refresh(u)
    from app.security.passwords import verify_password
    assert verify_password(u.password_hash, NEW_PW)


async def test_change_password_validation_and_lockout(client, db, notifier):
    u, csrf = await signed_in(client, db)
    r = await post(client, "/password", csrf, current_password=PASSWORD, new_password=NEW_PW, new_password2="different-one-1")
    assert r.status_code == 400
    r = await post(client, "/password", csrf, current_password=PASSWORD, new_password="short", new_password2="short")
    assert r.status_code == 400
    for _ in range(4):
        r = await post(client, "/password", csrf, current_password="wrong-wrong-wrong", new_password=NEW_PW, new_password2=NEW_PW)
        assert r.status_code == 400
    r = await post(client, "/password", csrf, current_password="wrong-wrong-wrong", new_password=NEW_PW, new_password2=NEW_PW)
    assert r.status_code == 303 and r.headers["location"] == "/"
    await db.refresh(u)
    assert u.locked_until is not None
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    assert any("неудачных" in m for m in notifier.sent)


async def test_password_refused_while_locked(client, db):
    u, csrf = await signed_in(client, db)
    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.commit()
    r = await post(client, "/password", csrf, current_password=PASSWORD, new_password=NEW_PW, new_password2=NEW_PW)
    assert r.status_code == 400
    await db.refresh(u)
    from app.security.passwords import verify_password
    assert verify_password(u.password_hash, PASSWORD)


async def test_sessions_list_and_revoke_other_session(client, db):
    u, csrf = await signed_in(client, db)
    bob = await make_user(db, "bob")
    _, mine = await create_session(db, u, "2.2.2.2", "<b>Agent</b>" + "x" * 300)
    _, theirs = await create_session(db, bob, "3.3.3.3", "Bob")
    await db.commit()
    r = await client.get(S + "/sessions")
    assert "2.2.2.2" in r.text and "это устройство" in r.text
    assert "<b>Agent" not in r.text and "&lt;b&gt;Agent" in r.text
    assert "x" * 108 in r.text and "x" * 109 not in r.text  # усечение до 120 символов
    assert "3.3.3.3" not in r.text
    assert (await post(client, f"/sessions/{theirs.id}/revoke", csrf)).status_code == 404
    assert (await db.get(AuthSession, theirs.id)) is not None
    assert (await post(client, f"/sessions/{mine.id}/revoke", csrf)).status_code == 303
    assert (await db.get(AuthSession, mine.id)) is None
    assert (await post(client, "/sessions/999999/revoke", csrf)).status_code == 404
    assert (await client.post(S + f"/sessions/{theirs.id}/revoke")).status_code == 403


async def test_revoke_others(client, db):
    u, csrf = await signed_in(client, db)
    await create_session(db, u, "2.2.2.2", "A")
    await create_session(db, u, "4.4.4.4", "B")
    await db.commit()
    assert (await post(client, "/sessions/revoke-others", csrf)).status_code == 303
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 1
    assert (await client.get(S)).status_code == 200


async def test_spend_section(client, db):
    u, _ = await signed_in(client, db)
    for model, cost in (("a/x", "0.5"), ("a/x", "0.25"), ("b/y", "1.1")):
        db.add(Usage(user_id=u.id, user_label=u.username, model=model, status="ok", cost_usd=Decimal(cost)))
    db.add(Usage(user_id=u.id, user_label=u.username, model="old/z", status="ok", cost_usd=Decimal("9"),
                 created_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    await db.commit()
    html = (await client.get(S)).text
    assert "$1.85 из $5.00" in html and "$1.10" in html and "$0.75" in html and "old/z" not in html
    assert html.index("b/y") < html.index("a/x")


async def test_export_contains_only_own_data(client, db):
    u, _ = await signed_in(client, db)
    bob = await make_user(db, "bob")
    mine = await make_chat(db, u, "мой чат")
    theirs = await make_chat(db, bob, "чужой чат")
    db.add_all([
        Message(chat_id=mine.id, role="user", content="привет", attachments=[{"name": "a.txt", "kind": "text", "text": "t"}]),
        Message(chat_id=mine.id, role="assistant", content="ответ", model="v/m"),
        Message(chat_id=theirs.id, role="user", content="секрет-боба"),
        Usage(user_id=u.id, user_label="alice", model="v/m", status="ok", prompt_tokens=3, completion_tokens=4, cost_usd=Decimal("0.0123")),
        Usage(user_id=bob.id, user_label="bob", model="v/m", status="ok", cost_usd=Decimal("7")),
    ])
    await db.commit()
    r = await client.get(S + "/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == "attachment; filename=my-data.json"
    assert r.headers["cache-control"] == "no-store"
    assert "секрет-боба" not in r.text and "bob" not in r.text
    data = json.loads(r.text)
    assert data["user"]["username"] == "alice"
    assert data["user"]["settings"]["answer_language"] == "ru"
    assert [c["title"] for c in data["chats"]] == ["мой чат"]
    msgs = data["chats"][0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert set(msgs[0]) == {"role", "content", "attachments", "model", "created_at"}
    assert data["usage"] == [{"model": "v/m", "prompt_tokens": 3, "completion_tokens": 4,
                              "cost_usd": "0.012300", "created_at": data["usage"][0]["created_at"]}]
    assert datetime.fromisoformat(data["usage"][0]["created_at"]).utcoffset() == timedelta(0)


async def test_delete_account_requires_password(client, db):
    u, csrf = await signed_in(client, db)
    r = await post(client, "/delete", csrf, password="wrong-wrong-wrong", confirm="1")
    assert r.status_code == 400
    assert await db.get(User, u.id) is not None
    assert (await client.post(S + "/delete", data={"password": PASSWORD})).status_code == 403
    for _ in range(3):
        await post(client, "/delete", csrf, password="wrong-wrong-wrong", confirm="1")
    r = await post(client, "/delete", csrf, password="wrong-wrong-wrong", confirm="1")
    assert r.status_code == 303 and r.headers["location"] == "/"
    await db.refresh(u)
    assert u.locked_until is not None


async def test_delete_account_anonymizes_usage(client, db, notifier):
    u, csrf = await signed_in(client, db)
    uid = u.id
    chat = await make_chat(db, u, "x")
    db.add(Message(chat_id=chat.id, role="user", content="hi"))
    db.add(Usage(user_id=uid, user_label="alice", chat_id=chat.id, model="v/m", status="ok"))
    db.add(AuditLog(event="login_ok", user_id=uid, user_label="alice"))
    await db.commit()
    r = await post(client, "/delete", csrf, password=PASSWORD, confirm="1")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert COOKIE_NAME in r.headers["set-cookie"]
    label = f"deleted-user-{uid}"
    assert (await db.scalar(select(func.count()).select_from(User).where(User.id == uid))) == 0
    assert (await db.scalar(select(func.count()).select_from(Chat))) == 0
    assert (await db.scalar(select(func.count()).select_from(AuthSession))) == 0
    assert (await db.execute(select(Usage.user_label))).scalars().all() == [label]
    labels = (await db.execute(select(AuditLog.event, AuditLog.user_label))).all()
    assert ("account_deleted", label) in labels
    assert all("alice" != lab for _, lab in labels)
    assert notifier.sent[-1] == f"Аккаунт удалён: {label}"
    assert (await client.get(S)).status_code == 404


async def test_delete_alert_failure_is_logged_not_raised(client, db, app):
    u, csrf = await signed_in(client, db)

    class Boom:
        async def alert(self, text):
            raise RuntimeError("x")

    app.state.notifier = Boom()
    r = await post(client, "/delete", csrf, password=PASSWORD, confirm="1")
    assert r.status_code == 303
    assert await db.get(User, u.id) is None


async def test_admin_cannot_delete_self(client, db):
    from tests.test_auth import login as _login

    admin = await make_user(db, "boss", "admin", totp_enabled=True, totp_secret_enc="x")
    await _login(client, "boss")
    s = (await db.execute(select(AuthSession))).scalars().first()
    s.totp_pending = False
    await db.commit()
    r = await post(client, "/delete", s.csrf_token, password=PASSWORD)
    assert r.status_code == 400
    assert await db.get(User, admin.id) is not None


async def test_twofa_secret_stable_and_regenerate(client, db):
    u, csrf = await signed_in(client, db)
    await client.get(S + "/2fa")
    await db.refresh(u)
    first = u.totp_secret_enc
    assert first
    await client.get(S + "/2fa")
    await db.refresh(u)
    assert u.totp_secret_enc == first
    assert (await client.post(S + "/2fa/regenerate")).status_code == 403
    assert (await post(client, "/2fa/regenerate", csrf)).status_code == 303
    await db.refresh(u)
    assert u.totp_secret_enc != first


async def test_delete_requires_confirm(client, db):
    u, csrf = await signed_in(client, db)
    r = await post(client, "/delete", csrf, password=PASSWORD)
    assert r.status_code == 400 and "необратимо" in r.text
    assert await db.get(User, u.id) is not None


async def test_delete_removes_username_from_audit(client, db):
    from tests.test_auth import LOGIN

    u, csrf = await signed_in(client, db)
    uid = u.id
    # неудачный вход (известное имя) и вход с неизвестным именем
    await client.post(LOGIN, data={"username": "alice", "password": "wrong-wrong-wrong"})
    # устаревшая строка: без user_id, но с именем
    db.add(AuditLog(event="login_fail", user_id=None, user_label="alice", details={"username": "alice"}))
    await db.commit()
    unknown = (await db.execute(select(AuditLog).where(AuditLog.event == "login_fail"))).scalars().all()
    assert any(a.user_id == uid and "username" not in a.details for a in unknown)
    r = await post(client, "/delete", csrf, password=PASSWORD, confirm="1")
    assert r.status_code == 303
    for a in (await db.execute(select(AuditLog))).scalars():
        await db.refresh(a)
        assert a.user_label != "alice"
        assert "alice" not in a.details.values()
        assert "username" not in a.details


async def test_delete_blanks_ip_on_audit_rows(client, db):
    u, csrf = await signed_in(client, db)
    bob = await make_user(db, "bob")
    db.add_all([
        AuditLog(event="login_ok", user_id=u.id, user_label="alice", ip="5.5.5.5"),
        AuditLog(event="login_fail", user_id=None, user_label="alice", ip="6.6.6.6",
                 details={"username": "alice"}),
        AuditLog(event="login_ok", user_id=bob.id, user_label="bob", ip="7.7.7.7"),
        AuditLog(event="login_fail", user_id=None, user_label="carol", ip="8.8.8.8"),
    ])
    await db.commit()
    r = await post(client, "/delete", csrf, password=PASSWORD, confirm="1")
    assert r.status_code == 303
    db.expunge_all()
    rows = (await db.execute(select(AuditLog.user_label, AuditLog.ip))).all()
    by_label: dict[str, set[str]] = {}
    for label, ip in rows:
        by_label.setdefault(label, set()).add(ip)
    assert by_label[f"deleted-user-{u.id}"] == {""}  # включая саму запись account_deleted
    assert by_label["bob"] == {"7.7.7.7"} and by_label["carol"] == {"8.8.8.8"}


async def test_export_includes_sessions_and_own_audit(client, db):
    u, _ = await signed_in(client, db)
    bob = await make_user(db, "bob")
    await create_session(db, u, "2.2.2.2", "Second device")
    await create_session(db, bob, "3.3.3.3", "Bob device")
    db.add_all([
        AuditLog(event="password_changed", user_id=u.id, user_label="alice", ip="4.4.4.4",
                 details={"secret": "x"}),
        AuditLog(event="login_ok", user_id=bob.id, user_label="bob", ip="3.3.3.3"),
    ])
    await db.commit()
    data = json.loads((await client.get(S + "/export")).text)
    assert "3.3.3.3" not in json.dumps(data)
    sessions = data["sessions"]
    assert {s["ip"] for s in sessions} == {"2.2.2.2", "127.0.0.1"}
    assert all(set(s) == {"ip", "user_agent", "created_at", "last_seen"} for s in sessions)
    assert "Second device" in {s["user_agent"] for s in sessions}
    audit = data["audit"]
    assert all(set(a) == {"event", "created_at", "ip"} for a in audit)
    assert {"event": "password_changed", "ip": "4.4.4.4"} in [
        {"event": a["event"], "ip": a["ip"]} for a in audit
    ]
    assert "login_ok" in {a["event"] for a in audit}  # собственный вход при signed_in
