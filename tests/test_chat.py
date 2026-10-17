import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends
from sqlalchemy import select

from app.auth.deps import current_user
from app.chat.service import DEFAULT_TITLE, Turn, get_owned_chat, stream_reply
from app.db import get_db, get_session_factory
from app.llm.openrouter import LLMError, StreamDelta
from app.models import AllowedModel, AuthSession, Chat, Message, Usage, User
from app.security.ratelimit import SlidingWindowLimiter
from tests.fakes import HANG, FakeLLM, FakeNotifier, drain_background, ok_script
from tests.fakes import done as done_event
from tests.test_auth import login, make_user

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def notifier(app):
    n = FakeNotifier()
    app.state.notifier = n
    return n


@pytest.fixture(autouse=True)
def probe(app):
    @app.get("/app/probe-chat/{chat_id}")
    async def _probe(chat_id: int, user: User = Depends(current_user), db=Depends(get_db)):
        chat = await get_owned_chat(db, user, chat_id)
        return {"id": chat.id}


def use_llm(app, *scripts, **kw) -> FakeLLM:
    llm = FakeLLM(*scripts, **kw)
    app.state.llm = llm
    return llm


async def signed_in(client, db, username="alice") -> tuple[User, str]:
    u = await make_user(db, username)
    await login(client, username)
    s = (
        await db.execute(
            select(AuthSession).where(AuthSession.user_id == u.id).order_by(AuthSession.id.desc())
        )
    ).scalars().first()
    return u, s.csrf_token


async def make_chat(db, user, title=DEFAULT_TITLE) -> Chat:
    chat = Chat(user_id=user.id, title=title)
    db.add(chat)
    await db.commit()
    return chat


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def send(client, chat_id, csrf, text="Привет", model="openrouter/auto", files=None):
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    # Поля — частями без filename, чтобы тело всегда было multipart.
    parts = [("text", (None, text)), ("model", (None, model)), *(files or [])]
    return await client.post(f"/app/chats/{chat_id}/messages", files=parts, headers=headers)


async def raw_send(client, chat_id, csrf, body: bytes):
    return await client.post(
        f"/app/chats/{chat_id}/messages",
        content=body,
        headers={"X-CSRF-Token": csrf, "Content-Type": "multipart/form-data; boundary=BND"},
    )


async def messages(db, chat_id) -> list[Message]:
    return (
        await db.execute(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
        )
    ).scalars().all()


async def usages(db) -> list[Usage]:
    return (await db.execute(select(Usage).order_by(Usage.id))).scalars().all()


async def test_create_chat(client, db):
    u, csrf = await signed_in(client, db)
    r = await client.post("/app/chats", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 303
    chat = (await db.execute(select(Chat))).scalar_one()
    assert chat.user_id == u.id and chat.title == DEFAULT_TITLE
    assert r.headers["location"] == f"/app/chats/{chat.id}"


async def test_send_message_streams_and_persists(app, client, db):
    llm = use_llm(app, ok_script("При", "вет!", cost="0.0123"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="Мой чат")
    r = await send(client, chat.id, csrf)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-accel-buffering"] == "no"
    ev = events(r.text)
    assert ev[:2] == [("delta", {"t": "При"}), ("delta", {"t": "вет!"})]
    name, data = ev[2]
    assert name == "done" and len(ev) == 3
    msgs = await messages(db, chat.id)
    assert [(m.role, m.content) for m in msgs] == [("user", "Привет"), ("assistant", "Привет!")]
    assert msgs[1].model == "vendor/model-a" and msgs[1].interrupted is False
    assert data == {
        "model": "vendor/model-a",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": "0.0123",
        "message_id": msgs[1].id,
        "spend": "0.01",
        "limit": "5.00",
    }
    [usage] = await usages(db)
    assert usage.status == "ok" and usage.cost_usd == Decimal("0.0123")
    assert usage.generation_id == "gen-1" and usage.chat_id == chat.id
    await db.refresh(chat)
    assert chat.updated_at > chat.created_at
    model, sent = llm.calls[0]
    assert model == "openrouter/auto"
    assert sent[0]["role"] == "system" and sent[-1] == {"role": "user", "content": "Привет"}
    await drain_background(app)
    assert llm.complete_calls == []  # заголовок уже задан пользователем


async def test_history_sent_to_model(app, client, db):
    llm = use_llm(app, ok_script("Первый"), ok_script("Второй"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    await send(client, chat.id, csrf, text="раз")
    await drain_background(app)
    await send(client, chat.id, csrf, text="два")
    sent = llm.calls[1][1]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "раз" and sent[2]["content"] == "Первый"


async def test_other_users_chat_404(app, client, db):
    use_llm(app)
    bob = await make_user(db, "bob")
    bobs = await make_chat(db, bob)
    _, csrf = await signed_in(client, db)
    assert (await client.get(f"/app/probe-chat/{bobs.id}")).status_code == 404
    assert (await client.get("/app/probe-chat/999999")).status_code == 404
    assert (await send(client, bobs.id, csrf)).status_code == 404
    assert (await send(client, 999999, csrf)).status_code == 404
    r = await client.post(f"/app/chats/{bobs.id}/retry", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404
    assert await messages(db, bobs.id) == []


async def test_limit_exceeded_returns_error_event(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    db.add(Usage(user_id=u.id, user_label=u.username, model="m", status="ok", cost_usd=Decimal("5")))
    await db.commit()
    chat = await make_chat(db, u)
    r = await send(client, chat.id, csrf)
    assert r.status_code == 200
    [(name, data)] = events(r.text)
    assert name == "error" and data["message"].startswith("Месячный лимит исчерпан")
    assert llm.calls == [] and await messages(db, chat.id) == []


async def test_rate_limit(app, client, db):
    llm = use_llm(app)
    app.state.chat_limiter = SlidingWindowLimiter(2, 60)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    for _ in range(2):
        assert events((await send(client, chat.id, csrf)).text)[-1][0] == "done"
    r = await send(client, chat.id, csrf)
    [(name, data)] = events(r.text)
    assert name == "error" and "Слишком много запросов" in data["message"]
    assert len(llm.calls) == 2


async def test_default_rate_limit_from_settings(app, settings):
    assert app.state.chat_limiter.limit == settings.chat_rate_per_min == 20
    assert app.state.chat_limiter.window_s == 60


async def test_disallowed_model_400(app, client, db):
    llm = use_llm(app)
    db.add(AllowedModel(model_id="vendor/off", display_name="Off", enabled=False))
    await db.commit()
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    assert (await send(client, chat.id, csrf, model="vendor/unknown")).status_code == 400
    assert (await send(client, chat.id, csrf, model="vendor/off")).status_code == 400
    assert llm.calls == [] and await messages(db, chat.id) == []


async def test_allowed_model_used(app, client, db):
    llm = use_llm(app)
    db.add(AllowedModel(model_id="vendor/on", display_name="On", enabled=True))
    await db.commit()
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    r = await send(client, chat.id, csrf, model="vendor/on")
    assert events(r.text)[-1][0] == "done"
    assert llm.calls[0][0] == "vendor/on"


async def test_image_with_non_vision_model_error(app, client, db):
    llm = use_llm(app)
    db.add(AllowedModel(model_id="vendor/text", display_name="T", enabled=True, supports_images=False))
    await db.commit()
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    photo = (FIXTURES / "photo.jpg").read_bytes()
    r = await send(client, chat.id, csrf, model="vendor/text", files=[("files", ("p.jpg", photo))])
    [(name, data)] = events(r.text)
    assert name == "error" and data["message"] == "Выбранная модель не принимает изображения"
    assert llm.calls == [] and await messages(db, chat.id) == []
    # auto — изображения разрешены
    r = await send(client, chat.id, csrf, files=[("files", ("p.jpg", photo))])
    assert events(r.text)[-1][0] == "done"
    content = llm.calls[0][1][-1]["content"]
    assert content[0] == {"type": "text", "text": "Привет"}
    assert content[1]["image_url"]["url"].startswith("data:image/")
    [user_msg, _] = await messages(db, chat.id)
    assert user_msg.attachments == [{"name": "p.jpg", "kind": "image"}]


async def test_text_attachment_inlined_and_stored(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    files = [("files[]", ("notes.txt", "строка из файла".encode()))]
    r = await send(client, chat.id, csrf, text="что тут?", files=files)
    assert events(r.text)[-1][0] == "done"
    content = llm.calls[0][1][-1]["content"]
    assert content == '<file name="notes.txt">\nстрока из файла\n</file>\n\nчто тут?'
    [user_msg, _] = await messages(db, chat.id)
    assert user_msg.content == "что тут?"
    assert user_msg.attachments == [{"name": "notes.txt", "kind": "text", "text": "строка из файла"}]


async def test_attachment_error_event(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    r = await send(client, chat.id, csrf, files=[("files", ("scan.pdf", (FIXTURES / "scan.pdf").read_bytes()))])
    [(name, data)] = events(r.text)
    assert name == "error" and data["message"] == "В PDF нет текстового слоя"
    assert llm.calls == [] and await messages(db, chat.id) == []


async def test_upload_limits(app, client, db, monkeypatch):
    llm = use_llm(app)
    monkeypatch.setattr(app.state.settings, "max_upload_mb", 1)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    big = b"a" * (1024 * 1024 + 1)
    [(name, data)] = events((await send(client, chat.id, csrf, files=[("files", ("big.txt", big))])).text)
    assert name == "error" and "big.txt" in data["message"] and "1 МБ" in data["message"]
    six = [("files", (f"f{i}.txt", b"x")) for i in range(6)]
    [(name, data)] = events((await send(client, chat.id, csrf, files=six)).text)
    assert name == "error" and data["message"] == "Не больше 5 файлов в сообщении"
    [(name, data)] = events((await send(client, chat.id, csrf, text="я" * 32001)).text)
    assert name == "error" and data["message"] == "Сообщение длиннее 32000 символов"
    [(name, data)] = events((await send(client, chat.id, csrf, text="  ")).text)
    assert name == "error" and data["message"] == "Пустое сообщение"
    assert "retry" not in data  # сообщение не сохранено — повторять через /retry нечего
    assert llm.calls == [] and await messages(db, chat.id) == []


async def _asgi_post(app, client, path, csrf, data, on_receive_wait):
    """Drive the ASGI app directly so the client can 'disconnect' mid-stream."""
    req = client.build_request("POST", path, data=data, files=[("files", ("x.txt", b"x"))],
                               headers={"X-CSRF-Token": csrf})
    body = req.read()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower(), v) for k, v in req.headers.raw],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8443),
    }
    sent_body = False
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await on_receive_wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return b"".join(chunks).decode()


async def test_stream_disconnect_saves_partial(app, client, db):
    llm = use_llm(app, [StreamDelta("Прив", "gen-77"), StreamDelta("ет", "gen-77"), HANG])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    out = await _asgi_post(app, client, f"/app/chats/{chat.id}/messages", csrf,
                           {"text": "Привет"}, llm.hanging.wait)
    await drain_background(app)
    assert [e for e, _ in events(out)] == ["delta", "delta"]
    assert llm.closed == 1
    msgs = await messages(db, chat.id)
    assert [(m.role, m.content, m.interrupted) for m in msgs] == [
        ("user", "Привет", False),
        ("assistant", "Привет", True),
    ]
    [usage] = await usages(db)
    assert usage.status == "interrupted" and usage.generation_id == "gen-77"
    assert usage.cost_usd is None
    assert llm.complete_calls == []  # нет заголовка для прерванного ответа


async def test_disconnect_detected_between_deltas(app, db, settings):
    u = await make_user(db)
    chat = await make_chat(db, u)
    llm = FakeLLM(ok_script("раз", "два", "три", gen="gen-5"))
    checks = 0

    async def is_disconnected():
        nonlocal checks
        checks += 1
        return checks >= 2

    request = SimpleNamespace(app=app, is_disconnected=is_disconnected)
    turn = Turn(user_id=u.id, chat_id=chat.id, model="openrouter/auto",
                messages=[{"role": "user", "content": "x"}], before=Decimal(0),
                limit=Decimal(5), first_reply=True, first_user_text="x", ip="")
    factory = app.dependency_overrides[get_session_factory]()
    out = [chunk async for chunk in stream_reply(request, llm, factory, turn)]
    await drain_background(app)
    assert len(out) == 2 and llm.closed == 1
    [msg] = await messages(db, chat.id)
    assert msg.content == "раздва" and msg.interrupted is True
    [usage] = await usages(db)
    assert usage.status == "interrupted" and usage.generation_id == "gen-5"


async def test_llm_error_saves_partial_and_usage_error(app, client, db, notifier):
    err = LLMError(503, "Ошибка модели, попробуйте ещё раз", "upstream boom", "gen-err")
    llm = use_llm(app, [StreamDelta("Част", "gen-err"), err])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    r = await send(client, chat.id, csrf)
    ev = events(r.text)
    assert ev == [("delta", {"t": "Част"}), ("error", {"message": "Ошибка модели, попробуйте ещё раз", "retry": True})]
    msgs = await messages(db, chat.id)
    assert [(m.role, m.content, m.interrupted) for m in msgs] == [
        ("user", "Привет", False),
        ("assistant", "Част", True),
    ]
    [usage] = await usages(db)
    assert usage.status == "error" and usage.generation_id == "gen-err"
    assert usage.error and "upstream boom" in usage.error
    await drain_background(app)
    assert llm.complete_calls == []
    assert notifier.sent == []


async def test_llm_error_without_text_saves_no_message(app, client, db):
    use_llm(app, [LLMError(429, "Модель перегружена, попробуйте позже")])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    ev = events((await send(client, chat.id, csrf)).text)
    assert ev == [("error", {"message": "Модель перегружена, попробуйте позже", "retry": True})]
    assert [m.role for m in await messages(db, chat.id)] == ["user"]
    assert [x.status for x in await usages(db)] == ["error"]


async def test_error_series_alert(app, client, db, notifier):
    err = lambda: [LLMError(500, "Ошибка модели, попробуйте ещё раз", "x")]  # noqa: E731
    use_llm(app, err(), err(), ok_script("ok"), err(), err(), err())
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    for _ in range(5):
        await send(client, chat.id, csrf)
    assert notifier.sent == []  # успех сбросил серию
    await send(client, chat.id, csrf)
    assert notifier.sent == ["Серия ошибок OpenRouter: 3 ошибки подряд за 10 минут"]


async def test_title_generated_after_first_reply(app, client, db, settings):
    llm = use_llm(app, ok_script("Ответ"), ok_script("Ещё"), title='«Про котов»\n')
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    await send(client, chat.id, csrf, text="Расскажи про котов " + "я" * 2000)
    await drain_background(app)
    await db.refresh(chat)
    assert chat.title == "Про котов"
    [(model, msgs, max_tokens)] = llm.complete_calls
    assert model == settings.title_model and max_tokens == 20
    prompt = msgs[-1]["content"]
    assert prompt.startswith("Придумай короткий заголовок (3–5 слов)")
    assert "Расскажи про котов" in prompt and len(prompt) < 1200
    rows = await usages(db)
    assert [(x.status, x.model) for x in rows] == [("ok", "vendor/model-a"), ("ok", settings.title_model)]
    assert rows[1].cost_usd == Decimal("0.0001") and rows[1].chat_id == chat.id
    await send(client, chat.id, csrf, text="ещё")
    await drain_background(app)
    assert len(llm.complete_calls) == 1  # только после первого ответа


async def test_title_error_keeps_default(app, client, db):
    llm = use_llm(app, title_error=LLMError(500, "Ошибка модели, попробуйте ещё раз", "x"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    await send(client, chat.id, csrf)
    await drain_background(app)
    await db.refresh(chat)
    assert chat.title == DEFAULT_TITLE and len(llm.complete_calls) == 1
    assert [x.status for x in await usages(db)] == ["ok", "error"]


async def test_retry_replaces_last_answer(app, client, db):
    llm = use_llm(app, ok_script("Плохой"), ok_script("Хороший", gen="gen-2"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    photo = (FIXTURES / "photo.jpg").read_bytes()
    files = [("files", ("a.txt", "текст".encode())), ("files", ("p.jpg", photo))]
    await send(client, chat.id, csrf, text="Вопрос", files=files)
    r = await client.post(f"/app/chats/{chat.id}/retry", headers={"X-CSRF-Token": csrf})
    ev = events(r.text)
    assert ev[0] == ("delta", {"t": "Хороший"}) and ev[-1][0] == "done"
    msgs = await messages(db, chat.id)
    assert [(m.role, m.content) for m in msgs] == [("user", "Вопрос"), ("assistant", "Хороший")]
    assert ev[-1][1]["message_id"] == msgs[1].id
    sent = llm.calls[1][1]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[-1]["content"] == (
        "<file name=\"a.txt\">\nтекст\n</file>\n\n[изображение: p.jpg]\n\nВопрос"
    )
    assert [x.status for x in await usages(db)] == ["ok", "ok"]


async def test_retry_after_error_without_answer(app, client, db):
    llm = use_llm(app, [LLMError(500, "Ошибка модели, попробуйте ещё раз")], ok_script("Да"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    await send(client, chat.id, csrf, text="Вопрос")
    r = await client.post(f"/app/chats/{chat.id}/retry", headers={"X-CSRF-Token": csrf})
    assert events(r.text)[-1][0] == "done"
    assert [(m.role, m.content) for m in await messages(db, chat.id)] == [
        ("user", "Вопрос"),
        ("assistant", "Да"),
    ]
    assert llm.calls[1][1][-1]["content"] == "Вопрос"


async def test_retry_empty_chat_error(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    r = await client.post(f"/app/chats/{chat.id}/retry", headers={"X-CSRF-Token": csrf})
    [(name, data)] = events(r.text)
    assert name == "error" and data["message"] == "Нечего повторять"
    assert llm.calls == []


async def test_csrf_required(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    assert (await client.post("/app/chats")).status_code == 403
    assert (await send(client, chat.id, None)).status_code == 403
    assert (await send(client, chat.id, "wrong")).status_code == 403
    assert (await client.post(f"/app/chats/{chat.id}/retry")).status_code == 403
    assert llm.calls == [] and await messages(db, chat.id) == []
    # токен только в заголовке: поле формы не принимается (тело не разбирается Starlette)
    r = await client.post(
        f"/app/chats/{chat.id}/messages",
        files=[("text", (None, "hi")), ("csrf_token", (None, csrf)), ("files", ("a.txt", b"a"))],
    )
    assert r.status_code == 403
    assert (await client.post("/app/chats", data={"csrf_token": csrf})).status_code == 403
    r = await client.post(f"/app/chats/{chat.id}/retry", data={"csrf_token": csrf})
    assert r.status_code == 403
    assert llm.calls == [] and await messages(db, chat.id) == []


async def test_unauthenticated_404(app, client, db):
    use_llm(app)
    u = await make_user(db)
    chat = await make_chat(db, u)
    assert (await client.post("/app/chats")).status_code == 404
    assert (await send(client, chat.id, "x")).status_code == 404


async def test_non_multipart_rejected(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    r = await client.post(f"/app/chats/{chat.id}/messages", data={"text": "hi"},
                          headers={"X-CSRF-Token": csrf})
    assert r.status_code == 415
    assert llm.calls == [] and await messages(db, chat.id) == []


async def test_body_too_large_rejected(app, client, db, monkeypatch):
    use_llm(app)
    monkeypatch.setattr(app.state.settings, "max_upload_mb", 1)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    huge = [("files", (f"f{i}.txt", b"a" * (1024 * 1024))) for i in range(9)]
    r = await send(client, chat.id, csrf, files=huge)
    assert r.status_code == 413
    assert await messages(db, chat.id) == []


async def test_control_chars_stripped(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    # httpx экранирует управляющие символы в filename — тело собираем вручную.
    body = (
        '--BND\r\nContent-Disposition: form-data; name="text"\r\n\r\n'
        "При\x00вет\x1b\n\tок\r\n"
        '--BND\r\nContent-Disposition: form-data; name="files"; filename="a\x00b\x07.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nxy\r\n--BND--\r\n"
    ).encode()
    r = await raw_send(client, chat.id, csrf, body)
    assert r.status_code == 200 and events(r.text)[-1][0] == "done"
    [user_msg, _] = await messages(db, chat.id)
    assert user_msg.content == "Привет\n\tок"
    assert user_msg.attachments == [{"name": "ab.txt", "kind": "text", "text": "xy"}]
    assert llm.calls[0][1][-1]["content"].endswith("Привет\n\tок")


def test_engine_hides_parameters(app):
    assert app.state.engine.sync_engine.hide_parameters is True


async def test_empty_file_input_ignored(app, client, db):
    llm = use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    # браузер шлёт пустое поле файла как filename="" без данных (httpx так не умеет)
    body = (
        '--BND\r\nContent-Disposition: form-data; name="text"\r\n\r\nтолько текст\r\n'
        '--BND\r\nContent-Disposition: form-data; name="files"; filename=""\r\n'
        "Content-Type: application/octet-stream\r\n\r\n\r\n--BND--\r\n"
    ).encode()
    r = await raw_send(client, chat.id, csrf, body)
    assert events(r.text)[-1][0] == "done"
    [user_msg, _] = await messages(db, chat.id)
    assert user_msg.attachments == []
    assert llm.calls[0][1][-1]["content"] == "только текст"


async def test_retry_model_from_query(app, client, db):
    llm = use_llm(app)
    db.add(AllowedModel(model_id="vendor/on", display_name="On", enabled=True))
    await db.commit()
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    await send(client, chat.id, csrf)
    r = await client.post(f"/app/chats/{chat.id}/retry?model=vendor/on",
                          headers={"X-CSRF-Token": csrf})
    assert events(r.text)[-1][0] == "done"
    assert llm.calls[1][0] == "vendor/on"


def _failing_factory(app):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    app.dependency_overrides[get_session_factory] = lambda: Boom


async def test_persistence_failure_on_error_path_still_emits_error(app, client, db, caplog):
    use_llm(app, [StreamDelta("Част", "g"), LLMError(500, "Ошибка модели, попробуйте ещё раз")])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    _failing_factory(app)
    r = await send(client, chat.id, csrf)
    assert events(r.text) == [
        ("delta", {"t": "Част"}),
        ("error", {"message": "Ошибка модели, попробуйте ещё раз", "retry": True}),
    ]
    await drain_background(app)
    assert "background task failed: RuntimeError" in caplog.text
    assert "Част" not in caplog.text and "Привет" not in caplog.text


async def test_persistence_failure_on_ok_path_emits_error(app, client, db):
    use_llm(app)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    _failing_factory(app)
    r = await send(client, chat.id, csrf)
    assert events(r.text)[-1] == ("error", {"message": "Не удалось сохранить ответ", "retry": True})


async def test_persistence_failure_on_disconnect_does_not_raise(app, client, db, caplog):
    llm = use_llm(app, [StreamDelta("Прив", "g"), HANG])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    _failing_factory(app)
    out = await _asgi_post(app, client, f"/app/chats/{chat.id}/messages", csrf,
                           {"text": "Привет"}, llm.hanging.wait)
    await drain_background(app)
    assert [e for e, _ in events(out)] == ["delta"]
    assert "background task failed: RuntimeError" in caplog.text


async def test_interrupt_after_done_uses_done(app, client, db):
    llm = use_llm(app, [StreamDelta("Всё", "gen-a"), done_event(gen="gen-final", cost="0.002"), HANG])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    await _asgi_post(app, client, f"/app/chats/{chat.id}/messages", csrf,
                     {"text": "Привет"}, llm.hanging.wait)
    await drain_background(app)
    [usage] = await usages(db)
    assert usage.status == "interrupted" and usage.generation_id == "gen-final"
    assert usage.cost_usd == Decimal("0.002") and usage.prompt_tokens == 10
    msgs = await messages(db, chat.id)
    assert msgs[-1].content == "Всё" and msgs[-1].interrupted is True


async def test_cost_fixed_point(app, client, db):
    use_llm(app, ok_script("x", cost="1E-7"))
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    data = events((await send(client, chat.id, csrf)).text)[-1][1]
    assert data["cost_usd"] == "0.0000001" and data["spend"] == "0.00"


async def test_reply_and_title_control_chars_stripped(app, client, db):
    use_llm(app, ok_script("От\x00в", "ет\x07\n\tок"), title="Ко\x00ты\x1b")
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    r = await send(client, chat.id, csrf)
    assert events(r.text)[-1][0] == "done"
    await drain_background(app)
    [_, reply] = await messages(db, chat.id)
    assert reply.content == "Ответ\n\tок"
    await db.refresh(chat)
    assert chat.title == "Коты"


async def test_interrupted_reply_control_chars_stripped(app, client, db):
    use_llm(app, [StreamDelta("ча\x00сть", "gen-1"), LLMError(500, "Ошибка", "x")])
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    await send(client, chat.id, csrf)
    [_, reply] = await messages(db, chat.id)
    assert reply.content == "часть" and reply.interrupted
