from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.jobs import send_digest
from app.models import AuditLog, Chat, Message, Usage
from app.telegram.stats import (
    PeriodStats,
    UserLine,
    collect,
    format_digest,
    period_window,
    stats_text,
    users_text,
)
from tests.test_auth import make_user
from tests.test_jobs import shared

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


def msk(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


def usage(user, at, cost="0.10", model="vendor/a", status="ok", prompt=100, completion=50):
    return Usage(
        user_id=user.id if user else None,
        user_label=user.username if user else "gone",
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=Decimal(cost),
        status=status,
        created_at=at,
    )


async def chat_with_message(db, user, at, content="hello", title="Новый чат", role="user"):
    chat = Chat(user_id=user.id, title=title, created_at=at, updated_at=at)
    db.add(chat)
    await db.flush()
    db.add(Message(chat_id=chat.id, role=role, content=content, created_at=at))
    await db.flush()
    return chat


def test_daily_window_timezone():
    start, end, label = period_window("day", msk(29, 9), MSK)
    assert (start, end) == (msk(28, 0), msk(29, 0))
    assert label == "28.10.2026"


async def test_daily_window_splits_rows_at_moscow_midnight(db):
    u = await make_user(db, "alice")
    db.add_all([usage(u, msk(28, 23, 30), "0.10"), usage(u, msk(29, 0, 30), "0.20")])
    await db.flush()
    start, end, _ = period_window("day", msk(29, 9), MSK)
    assert (await collect(db, start, end)).cost_usd == Decimal("0.10")
    start, end, _ = period_window("day", msk(30, 9), MSK)
    assert (await collect(db, start, end)).cost_usd == Decimal("0.20")


def test_weekly_window_monday():
    now = msk(26, 9)  # Monday 26.10.2026
    assert now.weekday() == 0
    start, end, label = period_window("week", now, MSK)
    assert (start, end) == (msk(19, 0), msk(26, 0))
    assert label == "19.10–25.10.2026"


def test_today_window():
    start, end, label = period_window("today", msk(29, 15), MSK)
    assert (start, end) == (msk(29, 0), msk(30, 0))
    assert label == "сегодня, 29.10.2026"


async def test_collect_counts(db):
    alice = await make_user(db, "alice")
    bob = await make_user(db, "bob")
    await make_user(db, "idle")
    at = msk(28, 12)
    await chat_with_message(db, alice, at)
    await chat_with_message(db, alice, at, content="answer", role="assistant")
    await chat_with_message(db, bob, at)
    db.add_all([
        usage(alice, at, "0.30", model="vendor/a", prompt=1000, completion=200),
        usage(bob, at, "0.10", model="vendor/b", prompt=500, completion=100),
        usage(bob, at, "0.00", model="vendor/b", status="error", prompt=0, completion=0),
        usage(None, at, "0.05", model="vendor/c", prompt=10, completion=5),  # deleted user
        usage(alice, msk(5, 12), "1.00"),  # earlier this month, outside the day
        usage(alice, msk(29, 12), "9.00"),  # after the window
        AuditLog(event="login_fail", details={}, created_at=at),
        AuditLog(event="login_fail", details={}, created_at=at),
        AuditLog(event="invite_redeemed", details={}, created_at=at),
        AuditLog(event="login_fail", details={}, created_at=msk(27, 12)),
    ])
    await db.flush()
    start, end, _ = period_window("day", msk(29, 9), MSK)
    s = await collect(db, start, end)
    assert (s.active_users, s.total_users) == (2, 3)
    assert (s.messages, s.new_chats) == (2, 3)  # assistant messages are not counted
    assert (s.prompt_tokens, s.completion_tokens) == (1510, 305)
    assert s.cost_usd == Decimal("0.45")
    assert s.month_cost_usd == Decimal("1.45")
    assert (s.model_errors, s.failed_logins, s.new_users) == (1, 2, 1)
    assert [(u.username, u.messages, u.cost, u.month_cost, u.limit) for u in s.per_user] == [
        ("alice", 1, Decimal("0.30"), Decimal("1.30"), Decimal("5")),
        ("bob", 1, Decimal("0.10"), Decimal("0.10"), Decimal("5")),
    ]
    assert s.top_models == [
        ("vendor/a", Decimal("0.30")),
        ("vendor/b", Decimal("0.10")),
        ("vendor/c", Decimal("0.05")),
    ]


def fixed_stats() -> PeriodStats:
    return PeriodStats(
        active_users=3,
        total_users=9,
        messages=41,
        new_chats=6,
        prompt_tokens=150100,
        completion_tokens=32300,
        cost_usd=Decimal("0.84"),
        month_cost_usd=Decimal("7.12"),
        per_user=[
            UserLine("alice", 18, Decimal("0.41"), Decimal("3.10"), Decimal("5")),
            UserLine("bob", 15, Decimal("0.30"), Decimal("2.02"), Decimal("5")),
        ],
        model_errors=1,
        failed_logins=4,
        new_users=0,
        top_models=[("vendor/a", Decimal("0.60")), ("vendor/b", Decimal("0.24"))],
    )


def test_digest_format():
    assert format_digest("Сводка за 28.10.2026", fixed_stats(), weekly=False) == (
        "Сводка за 28.10.2026\n"
        "Активных пользователей: 3 из 9\n"
        "Сообщений: 41, новых чатов: 6\n"
        "Токены: 182 400 (вход 150 100 / выход 32 300)\n"
        "Расход: $0.84 · с начала месяца $7.12\n"
        "По пользователям:\n"
        "  alice — 18 сообщ., $0.41 (месяц $3.10 из $5.00)\n"
        "  bob — 15 сообщ., $0.30 (месяц $2.02 из $5.00)\n"
        "Ошибок модели: 1 · неудачных входов: 4 · новых пользователей: 0"
    )


def test_digest_weekly_has_top_models():
    text = format_digest("Сводка за неделю 19.10–25.10.2026", fixed_stats(), weekly=True)
    assert "Топ моделей: vendor/a $0.60, vendor/b $0.24" in text
    assert "Топ моделей" not in format_digest("x", fixed_stats(), weekly=False)


def test_digest_no_activity():
    s = PeriodStats(month_cost_usd=Decimal("7.12"), total_users=9)
    assert format_digest("Сводка за 28.10.2026", s, False) == (
        "Сводка за 28.10.2026: активности не было. С начала месяца $7.12"
    )


async def test_digest_has_no_content(db):
    alice = await make_user(db, "alice")
    at = msk(29, 12)
    await chat_with_message(db, alice, at, content="SECRET-MSG-42", title="SECRET-TITLE-7")
    db.add(usage(alice, at))
    db.add(AuditLog(event="login_fail", ip="203.0.113.77", details={"username": "SECRET-MSG-42"},
                    created_at=at))
    await db.flush()
    now = msk(29, 15)
    text = await stats_text(db, now, MSK)
    assert "alice" in text and "Сообщений: 1" in text
    start, end, label = period_window("today", now, MSK)
    digest = format_digest(label, await collect(db, start, end), weekly=True)
    for out in (text, digest):
        for marker in ("SECRET-MSG-42", "SECRET-TITLE-7", "203.0.113.77"):
            assert marker not in out


async def test_users_text(db):
    alice = await make_user(db, "alice")
    bob = await make_user(db, "bob")
    bob.is_active = False
    db.add_all([
        usage(alice, datetime(2026, 10, 28, 20, 30, tzinfo=UTC), "1.50"),
        usage(alice, datetime(2026, 9, 28, 20, 30, tzinfo=UTC), "4.00"),  # previous month
    ])
    await db.flush()
    text = await users_text(db, MSK, datetime(2026, 10, 29, 12, tzinfo=UTC))
    assert text.splitlines() == [
        "alice — активен, $1.50 из $5.00, последняя активность 28.10 23:30",
        "bob — заблокирован, $0.00 из $5.00, последняя активность —",
    ]


class RecordingNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _Settings:
    digest_timezone = "Europe/Moscow"


async def test_send_digest_monday_sends_weekly(db):
    alice = await make_user(db, "alice")
    await chat_with_message(db, alice, msk(21, 12))
    db.add(usage(alice, msk(21, 12), "0.50"))
    await db.flush()
    n = RecordingNotifier()
    text = await send_digest(shared(db), n, _Settings(), now=msk(26, 9))
    assert n.sent == [text]
    assert text.startswith("Сводка за неделю 19.10–25.10.2026\n")
    assert "Топ моделей: vendor/a $0.50" in text


async def test_send_digest_tuesday_sends_daily(db):
    alice = await make_user(db, "alice")
    await chat_with_message(db, alice, msk(26, 12))
    db.add(usage(alice, msk(26, 12), "0.50"))
    await db.flush()
    n = RecordingNotifier()
    text = await send_digest(shared(db), n, _Settings(), now=msk(27, 9))
    assert text.startswith("Сводка за 26.10.2026\n")
    assert "Топ моделей" not in text
