import asyncio
import logging
import time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.notify.base import LogNotifier
from app.telegram.notifier import TelegramNotifier


class FakeClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake():
    return FakeClient()


async def test_alert_rate_limit(fake, clock):
    n = TelegramNotifier(fake, 7, clock=clock)
    for i in range(25):
        await n.alert(f"a{i}")
        await n.drain()
    await n.drain()
    assert len(fake.sent) == 20
    assert all(chat == 7 for chat, _ in fake.sent)
    await n.flush_dropped()
    await n.drain()
    assert len(fake.sent) == 20  # window not freed yet
    clock.now += 3601
    await n.flush_dropped()
    assert fake.sent[-1][1] == "Пропущено уведомлений: 5"
    assert len(fake.sent) == 21
    await n.flush_dropped()
    assert len(fake.sent) == 21  # counter reset


async def test_next_alert_flushes_dropped(fake, clock):
    n = TelegramNotifier(fake, 7, clock=clock)
    for i in range(22):
        await n.alert(f"a{i}")
        await n.drain()
    clock.now += 3601
    await n.alert("fresh")
    await n.drain()
    assert [t for _, t in fake.sent[-2:]] == ["Пропущено уведомлений: 2", "fresh"]


async def test_send_bypasses_limit(fake, clock):
    n = TelegramNotifier(fake, 7, clock=clock)
    for i in range(20):
        await n.alert(f"a{i}")
        await n.drain()
    for i in range(5):
        await n.send(f"digest{i}")
    assert len(fake.sent) == 25
    await n.alert("extra")
    await n.drain()
    assert len(fake.sent) == 25


async def test_create_app_picks_notifier(settings):
    app = create_app(
        settings.model_copy(update={"telegram_bot_token": "1:abc", "telegram_owner_chat_id": 5})
    )
    assert isinstance(app.state.notifier, TelegramNotifier)
    assert app.state.telegram is not None
    await app.state.engine.dispose()

    for kw in ({"telegram_bot_token": "1:abc"}, {"telegram_owner_chat_id": 5}, {}):
        app = create_app(settings.model_copy(update=kw))
        assert isinstance(app.state.notifier, LogNotifier)
        await app.state.engine.dispose()


def test_settings_validation(settings):
    base = settings.model_dump()
    for bad in (
        {"digest_timezone": "Mars/Olympus"},
        {"digest_time": "25:00"},
        {"digest_time": "9:00"},
        {"digest_time": "09:60"},
        {"digest_time": "noon"},
    ):
        with pytest.raises(ValidationError):
            Settings(**{**base, **bad}, _env_file=None)
    ok = Settings(**{**base, "digest_timezone": "Asia/Tokyo", "digest_time": "07:30"}, _env_file=None)
    assert ok.digest_time == "07:30"


class SlowClient:
    def __init__(self, fail=None):
        self.started = 0
        self.fail = fail

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.started += 1
        if self.fail:
            raise self.fail
        await asyncio.sleep(0.3)
        return True


async def test_alert_returns_immediately_when_telegram_is_slow():
    client = SlowClient()
    n = TelegramNotifier(client, 7)
    t0 = time.perf_counter()
    await n.alert("x")
    assert time.perf_counter() - t0 < 0.1
    await n.drain()
    assert client.started == 1


async def test_alert_in_flight_is_bounded():
    client = SlowClient()
    n = TelegramNotifier(client, 7)
    for _ in range(8):
        await n.alert("x")
    await n.drain()
    assert client.started == 5  # the other 3 were counted as dropped


async def test_alert_task_exceptions_are_swallowed_and_logged(caplog):
    caplog.set_level(logging.DEBUG)
    n = TelegramNotifier(SlowClient(fail=RuntimeError("https://api.telegram.org/bot1:SECRET/x")), 7)
    await n.alert("x")
    await n.drain()
    assert "RuntimeError" in caplog.text
    assert "SECRET" not in caplog.text


async def test_alert_timeout_is_swallowed(monkeypatch, caplog):
    import app.telegram.notifier as mod

    monkeypatch.setattr(mod, "SEND_TIMEOUT_S", 0.05)
    n = TelegramNotifier(SlowClient(), 7)
    await n.alert("x")
    await n.drain()
    assert "TimeoutError" in caplog.text


async def test_failed_summary_restores_dropped_count(clock):
    class Flaky:
        def __init__(self):
            self.ok = False
            self.sent = []

        async def send_message(self, chat_id, text):
            self.sent.append(text)
            return self.ok

    c = Flaky()
    n = TelegramNotifier(c, 7, clock=clock)
    for i in range(23):
        await n.alert(f"a{i}")
        await n.drain()
    await n.drain()
    clock.now += 3601
    await n.flush_dropped()  # fails
    c.ok = True
    await n.flush_dropped()
    assert c.sent[-1] == "Пропущено уведомлений: 3"
