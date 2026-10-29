import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select

from app.auth.sessions import create_session
from app.models import AuditLog, AuthSession, Invite
from app.telegram.bot import BotDeps, handle_update, run_polling
from app.telegram.client import TelegramClient
from app.telegram.stats import stats_text, users_text
from tests.test_auth import make_user
from tests.test_jobs import shared

OWNER = 777
STRANGER = 999
TOKEN = "123456:SECRET-token-value"
UPDATES_URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
MSK = ZoneInfo("Europe/Moscow")


class RecordingNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def deps(db, notifier, settings):
    return BotDeps(shared(db), notifier, settings, OWNER, clock=lambda: NOW)


def update(text: str, chat_id: int = OWNER, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}, "text": text},
    }


async def say(deps, text: str, chat_id: int = OWNER) -> list[str]:
    before = len(deps.notifier.sent)
    await handle_update(update(text, chat_id), deps)
    return deps.notifier.sent[before:]


async def events(db, event: str) -> list[AuditLog]:
    return list((await db.execute(select(AuditLog).where(AuditLog.event == event))).scalars())


async def test_help(deps):
    (reply,) = await say(deps, "/help")
    for cmd in ("/stats", "/users", "/invite", "/block", "/unblock"):
        assert cmd in reply
    # case-insensitive, @botname suffix stripped
    assert await say(deps, "/HELP@my_aichat_bot") == [reply]


async def test_unknown_command(deps):
    assert await say(deps, "/nope") == ["Неизвестная команда. /help"]
    assert await say(deps, "hello") == ["Неизвестная команда. /help"]


async def test_stats_command(db, deps):
    await make_user(db, "alice")
    (reply,) = await say(deps, "/stats")
    assert reply == await stats_text(db, NOW, MSK)
    assert reply.startswith("Сводка за сегодня, 29.10.2026")


async def test_users_command(db, deps):
    await make_user(db, "alice")
    (reply,) = await say(deps, "/users")
    assert reply == await users_text(db, MSK, NOW)
    assert reply.startswith("alice — активен")


async def test_invite_command(db, deps, settings):
    await make_user(db, "boss2", "admin", is_active=False)
    boss = await make_user(db, "boss", "admin")
    (reply,) = await say(deps, "/invite")
    assert f"/a/{settings.login_path_token}/join/" in reply
    assert len((await db.execute(select(Invite))).scalars().all()) == 1
    (row,) = await events(db, "invite_created")
    assert row.ip == "telegram" and row.user_id == boss.id


async def test_block_and_unblock(db, deps):
    boss = await make_user(db, "boss", "admin")
    alice = await make_user(db, "alice")
    await create_session(db, alice, "1.1.1.1", "UA")
    await db.commit()
    (reply,) = await say(deps, "/block Alice")
    assert "alice" in reply and "заблокирован" in reply
    await db.refresh(alice)
    assert alice.is_active is False
    left = (await db.execute(select(AuthSession).where(AuthSession.user_id == alice.id))).all()
    assert left == []
    (row,) = await events(db, "user_blocked")
    assert (row.ip, row.user_id, row.details["target_id"]) == ("telegram", boss.id, alice.id)

    (reply,) = await say(deps, "/unblock alice")
    assert "разблокирован" in reply
    await db.refresh(alice)
    assert alice.is_active is True
    (row,) = await events(db, "user_unblocked")
    assert (row.ip, row.details["target_id"]) == ("telegram", alice.id)


async def test_block_admin_refused(db, deps):
    await make_user(db, "boss", "admin")
    other = await make_user(db, "boss2", "admin")
    (reply,) = await say(deps, "/block boss2")
    assert reply == "Нельзя заблокировать администратора"
    await db.refresh(other)
    assert other.is_active is True
    assert await events(db, "user_blocked") == []


async def test_block_unknown_user(db, deps):
    await make_user(db, "boss", "admin")
    assert await say(deps, "/block ghost") == ["Пользователь не найден"]
    assert await say(deps, "/unblock ghost") == ["Пользователь не найден"]
    assert await say(deps, "/block") == ["Укажите логин: /block <логин>"]


async def test_command_error_is_contained(db, notifier, settings, caplog):
    def broken_factory():
        raise RuntimeError("SECRET-DB-DETAIL")

    deps = BotDeps(broken_factory, notifier, settings, OWNER, clock=lambda: NOW)
    with caplog.at_level(logging.WARNING, logger="telegram"):
        assert await say(deps, "/stats") == ["Ошибка, подробности в логе сервера"]
    assert "RuntimeError" in caplog.text
    assert "SECRET-DB-DETAIL" not in caplog.text


async def test_foreign_chat_ignored(db, deps, caplog):
    await make_user(db, "boss", "admin")
    alice = await make_user(db, "alice")
    with caplog.at_level(logging.INFO, logger="telegram"):
        assert await say(deps, "/block alice SECRET-TEXT", chat_id=STRANGER) == []
    await db.refresh(alice)
    assert alice.is_active is True
    assert f"telegram: ignored update from chat {STRANGER}" in caplog.text
    assert "SECRET-TEXT" not in caplog.text


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_backlog_skipped_on_start(db, deps, http):
    await make_user(db, "boss", "admin")
    alice = await make_user(db, "alice")
    stop = asyncio.Event()
    payloads: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(200, json={"ok": True, "result": [update("/block alice", update_id=10)]})
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    respx.post(UPDATES_URL).mock(side_effect=handler)
    await asyncio.wait_for(run_polling(TelegramClient(TOKEN, http), deps, stop), 5)
    assert (payloads[0]["offset"], payloads[0]["timeout"]) == (-1, 0)
    assert (payloads[1]["offset"], payloads[1]["timeout"]) == (11, 30)
    assert payloads[1]["allowed_updates"] == ["message"]
    await db.refresh(alice)
    assert alice.is_active is True
    assert deps.notifier.sent == []


@respx.mock
async def test_polling_handles_updates_and_advances_offset(db, deps, http):
    stop = asyncio.Event()
    payloads: list[dict] = []
    responses = [
        [],
        [update("/help", update_id=5), update("/help", chat_id=STRANGER, update_id=6)],
        [],
    ]

    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == len(responses):
            stop.set()
        return httpx.Response(200, json={"ok": True, "result": responses[len(payloads) - 1]})

    respx.post(UPDATES_URL).mock(side_effect=handler)
    await asyncio.wait_for(run_polling(TelegramClient(TOKEN, http), deps, stop), 5)
    assert "offset" not in payloads[1]  # empty backlog: no offset yet
    assert payloads[2]["offset"] == 7
    assert len(deps.notifier.sent) == 1 and "/stats" in deps.notifier.sent[0]


@respx.mock
async def test_polling_backoff_on_error(deps, http, caplog):
    stop = asyncio.Event()
    pauses: list[float] = []

    async def pause(_stop, seconds):
        pauses.append(seconds)
        if len(pauses) == 8:
            stop.set()

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 4:  # one success resets the backoff
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(502)

    respx.post(UPDATES_URL).mock(side_effect=handler)
    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(
            run_polling(TelegramClient(TOKEN, http), deps, stop, pause=pause), 5
        )
    assert pauses == [1, 2, 4, 1, 2, 4, 8, 16]
    assert "HTTP 502" in caplog.text
    assert TOKEN not in caplog.text


async def test_backoff_caps_at_60(deps):
    stop = asyncio.Event()
    pauses: list[float] = []

    class Down:
        async def get_updates(self, offset, timeout):
            from app.telegram.client import TelegramError

            raise TelegramError("ConnectError")

    async def pause(_stop, seconds):
        pauses.append(seconds)
        if len(pauses) == 9:
            stop.set()

    await asyncio.wait_for(run_polling(Down(), deps, stop, pause=pause), 5)
    assert pauses == [1, 2, 4, 8, 16, 32, 60, 60, 60]


async def test_polling_stops_promptly_while_backing_off(deps):
    stop = asyncio.Event()

    class Down:
        async def get_updates(self, offset, timeout):
            from app.telegram.client import TelegramError

            stop.set()
            raise TelegramError("ConnectError")

    # The default pause waits on the stop event, so the loop exits without sleeping 1 s.
    await asyncio.wait_for(run_polling(Down(), deps, stop), 0.5)


@respx.mock
async def test_lifespan_starts_and_stops_polling(settings):
    from app.main import create_app

    started = asyncio.Event()

    def handler(request):
        started.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    respx.post(UPDATES_URL).mock(side_effect=handler)
    cfg = settings.model_copy(
        update={"telegram_bot_token": TOKEN, "telegram_owner_chat_id": OWNER, "enable_jobs": True}
    )
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 5)
        task = app.state.telegram_polling
        assert not task.done()
    assert task.done()


async def test_lifespan_no_polling_without_jobs(settings):
    from app.main import create_app

    cfg = settings.model_copy(update={"telegram_bot_token": TOKEN, "telegram_owner_chat_id": OWNER})
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.telegram_polling is None
