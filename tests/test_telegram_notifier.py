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
    assert len(fake.sent) == 20
    assert all(chat == 7 for chat, _ in fake.sent)
    await n.flush_dropped()
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
    clock.now += 3601
    await n.alert("fresh")
    assert [t for _, t in fake.sent[-2:]] == ["Пропущено уведомлений: 2", "fresh"]


async def test_send_bypasses_limit(fake, clock):
    n = TelegramNotifier(fake, 7, clock=clock)
    for i in range(20):
        await n.alert(f"a{i}")
    for i in range(5):
        await n.send(f"digest{i}")
    assert len(fake.sent) == 25
    await n.alert("extra")
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
