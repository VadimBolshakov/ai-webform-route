from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.service import (
    LimitExceeded,
    check_limit,
    month_spend,
    month_start,
    notify_thresholds,
    record_usage,
)
from app.llm.openrouter import StreamDone
from app.models import AuditLog, Usage, User
from app.security.passwords import hash_password


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def alert(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(text)


async def make_user(db, limit="5", name="alice") -> User:
    u = User(
        username=name,
        password_hash=hash_password("Correct-Horse-42"),
        role="user",
        monthly_limit_usd=Decimal(limit),
    )
    db.add(u)
    await db.flush()
    return u


def add_usage(db, user, cost, created_at=None):
    row = Usage(
        user_id=user.id,
        user_label=user.username,
        model="m",
        status="ok",
        cost_usd=None if cost is None else Decimal(cost),
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    return row


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_month_start():
    assert month_start(utc(2026, 10, 17, 13, 5)) == utc(2026, 10, 1)


def test_month_start_requires_aware():
    with pytest.raises(ValueError):
        month_start(datetime(2026, 10, 17))


async def test_month_spend_only_current_month(db):
    u = await make_user(db)
    now = datetime.now(timezone.utc)
    start = month_start(now)
    add_usage(db, u, "1.5")
    add_usage(db, u, "2", created_at=start)  # first of month 00:00 is included
    add_usage(db, u, "100", created_at=start - timedelta(seconds=1))  # previous month
    await db.flush()
    assert await month_spend(db, u.id) == Decimal("3.5")


async def test_null_cost_counts_zero(db):
    u = await make_user(db)
    add_usage(db, u, None)
    add_usage(db, u, "1")
    await db.flush()
    assert await month_spend(db, u.id) == Decimal("1")


async def test_month_spend_empty_is_zero(db):
    u = await make_user(db)
    assert await month_spend(db, u.id) == Decimal("0")


async def test_check_limit_blocks_at_100(db):
    u = await make_user(db, "5")
    add_usage(db, u, "4.99")
    await db.flush()
    await check_limit(db, u)
    add_usage(db, u, "0.01")
    await db.flush()
    with pytest.raises(LimitExceeded) as ei:
        await check_limit(db, u)
    assert str(ei.value) == "Месячный лимит исчерпан ($5.00 из $5.00)"
    assert ei.value.spend == Decimal("5")
    assert ei.value.limit == Decimal("5")


async def test_check_limit_zero_blocks(db):
    u = await make_user(db, "0")
    with pytest.raises(LimitExceeded):
        await check_limit(db, u)


async def test_thresholds_fire_once(db):
    u = await make_user(db, "5")
    n = FakeNotifier()
    await notify_thresholds(db, n, u, Decimal("3.9"), Decimal("4.1"))
    assert n.sent == ["alice: 80% of the limit used ($4.10 of $5.00)"]
    await notify_thresholds(db, n, u, Decimal("4.1"), Decimal("4.2"))
    assert len(n.sent) == 1


async def test_threshold_jump_both(db):
    u = await make_user(db, "5")
    n = FakeNotifier()
    await notify_thresholds(db, n, u, Decimal("3.0"), Decimal("5.5"), ip="1.2.3.4")
    assert len(n.sent) == 2
    assert "80%" in n.sent[0] and "100%" in n.sent[1]
    rows = (await db.scalars(select(AuditLog).where(AuditLog.event == "limit_reached"))).all()
    assert len(rows) == 1 and rows[0].user_id == u.id and rows[0].ip == "1.2.3.4"


async def test_thresholds_zero_limit_silent(db):
    u = await make_user(db, "0")
    n = FakeNotifier()
    await notify_thresholds(db, n, u, Decimal("0"), Decimal("1"))
    assert n.sent == []


async def test_alert_failure_not_raised(db):
    u = await make_user(db, "5")
    await notify_thresholds(db, FakeNotifier(fail=True), u, Decimal("0"), Decimal("6"))


async def test_record_usage_done(db):
    u = await make_user(db)
    done = StreamDone("m/x", "gen-1", 10, 20, Decimal("0.0123"), "stop")
    row = await record_usage(
        db, user=u, chat_id=None, done=done, model="m/x", latency_ms=42, status="ok"
    )
    assert row.id is not None
    assert (row.user_label, row.generation_id, row.prompt_tokens, row.completion_tokens) == (
        "alice",
        "gen-1",
        10,
        20,
    )
    assert row.cost_usd == Decimal("0.0123") and row.latency_ms == 42


async def test_record_usage_done_unknown_cost_stays_null(db):
    u = await make_user(db)
    done = StreamDone("m", "g", 1, 2, None, "stop")
    row = await record_usage(
        db, user=u, chat_id=None, done=done, model="m", latency_ms=1, status="ok"
    )
    assert row.cost_usd is None


async def test_record_usage_no_done(db):
    u = await make_user(db)
    row = await record_usage(
        db,
        user=u,
        chat_id=None,
        done=None,
        model="m",
        latency_ms=5,
        status="error",
        error="boom",
        generation_id="gen-9",
    )
    assert (row.prompt_tokens, row.completion_tokens, row.cost_usd) == (0, 0, None)
    assert row.generation_id == "gen-9" and row.error == "boom" and row.status == "error"
