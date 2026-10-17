from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.auth.sessions import create_session, utcnow
from app.jobs import backfill_costs, cleanup_expired, make_scheduler
from app.llm.openrouter import LLMError
from app.models import AuditLog, AuthSession, Invite, Usage
from tests.fakes import FakeNotifier
from tests.test_auth import make_user


def shared(db):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


async def test_cleanup_expired(db):
    now = utcnow()
    u = await make_user(db, "alice")
    _, old = await create_session(db, u, "1.1.1.1", "UA")
    _, fresh = await create_session(db, u, "1.1.1.2", "UA")
    old.expires_at = now - timedelta(seconds=1)
    db.add_all([
        Invite(token_hash="a" * 64, created_by_id=u.id, expires_at=now - timedelta(days=31)),
        Invite(token_hash="b" * 64, created_by_id=u.id, expires_at=now - timedelta(days=29)),
        Invite(token_hash="c" * 64, created_by_id=u.id, expires_at=now + timedelta(hours=1)),
    ])
    long_ago = now - timedelta(days=31)
    db.add_all([
        AuditLog(event="login_fail", user_id=None, details={}, created_at=long_ago),  # удалить
        AuditLog(event="login_fail", user_id=None, details={}, created_at=now),  # свежая
        AuditLog(event="login_fail", user_id=u.id, user_label="alice", details={}, created_at=long_ago),
        AuditLog(event="user_blocked", user_id=None, details={}, created_at=long_ago),
        AuditLog(event="limit_reached", user_id=None, details={"scope": "login"}, created_at=long_ago),  # удалить
        AuditLog(event="limit_reached", user_id=None, details={"scope": "login"}, created_at=now),
        AuditLog(event="limit_reached", user_id=u.id, details={}, created_at=long_ago),  # месячный лимит
    ])
    await db.commit()
    res = await cleanup_expired(shared(db))
    assert res == {"sessions": 1, "invites": 1, "login_fail": 1, "limit_reached": 1}
    assert (await db.execute(select(AuthSession.id))).scalars().all() == [fresh.id]
    assert sorted((await db.execute(select(Invite.token_hash))).scalars().all()) == ["b" * 64, "c" * 64]
    left = (await db.execute(select(AuditLog.event, AuditLog.user_id))).all()
    assert len(left) == 5


class FakeCostLLM:
    def __init__(self, costs: dict):
        self.costs = costs
        self.calls: list[str] = []

    async def get_generation_cost(self, gid: str):
        self.calls.append(gid)
        value = self.costs[gid]
        if isinstance(value, Exception):
            raise value
        return value


def usage(user, gid, cost=None, age=timedelta(minutes=10)) -> Usage:
    return Usage(
        user_id=user.id, user_label=user.username, model="v/m", generation_id=gid,
        cost_usd=cost, status="ok", created_at=utcnow() - age,
    )


async def test_backfill_costs(db):
    u = await make_user(db, "alice")  # лимит $5
    other = await make_user(db, "bob")
    rows = {
        "ok": usage(u, "g-ok"),
        "none": usage(u, "g-none"),
        "boom": usage(u, "g-boom"),
        "llmerr": usage(u, "g-llm"),
        "bob": usage(other, "g-bob"),
        "has": usage(u, "g-has", cost=Decimal("0.5")),
        "nogen": usage(u, None),
        "old": usage(u, "g-old", age=timedelta(days=2)),
    }
    # до обновления у alice уже 4.0 -> 80% порог пересечётся (4.0 + 0.5 + 0.2 + ... )
    rows["has"].cost_usd = Decimal("3.5")
    db.add_all(rows.values())
    await db.commit()
    llm = FakeCostLLM({
        "g-ok": Decimal("0.6"),
        "g-none": None,
        "g-boom": ValueError("bad body"),
        "g-llm": LLMError(500, "x"),
        "g-bob": Decimal("0.1"),
    })
    notifier = FakeNotifier()
    n = await backfill_costs(shared(db), llm, notifier)
    assert n == 2
    for r in rows.values():
        await db.refresh(r)
    assert rows["ok"].cost_usd == Decimal("0.6")
    assert rows["bob"].cost_usd == Decimal("0.1")
    assert rows["none"].cost_usd is None and rows["boom"].cost_usd is None
    assert rows["llmerr"].cost_usd is None and rows["nogen"].cost_usd is None
    assert rows["old"].cost_usd is None
    assert sorted(llm.calls) == ["g-bob", "g-boom", "g-llm", "g-none", "g-ok"]
    # alice: 3.5 -> 4.1 пересекает 80% от $5
    assert len(notifier.sent) == 1 and "80%" in notifier.sent[0] and "alice" in notifier.sent[0]
    # повторный запуск не дёргает уже заполненные
    llm.calls.clear()
    assert await backfill_costs(shared(db), llm, notifier) == 0
    assert "g-ok" not in llm.calls


async def test_backfill_notifies_100_percent_and_audits(db):
    u = await make_user(db, "alice")
    db.add_all([usage(u, "g1", cost=Decimal("4.5")), usage(u, "g2")])
    await db.commit()
    notifier = FakeNotifier()
    await backfill_costs(shared(db), FakeCostLLM({"g2": Decimal("1.0")}), notifier)
    assert any("100%" in t for t in notifier.sent)
    events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "limit_reached" in events


async def test_scheduler_jobs_do_not_overlap(app):
    sched = make_scheduler(app)
    sched.start(paused=True)
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {"cleanup_expired", "backfill_costs"}
        for j in jobs.values():
            assert j.max_instances == 1 and j.coalesce is True
    finally:
        sched.shutdown(wait=False)
