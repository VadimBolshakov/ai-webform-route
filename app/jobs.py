"""Фоновые задачи (APScheduler). Функции принимают фабрику сессий/клиентов — тесты вызывают их напрямую."""

import logging
from datetime import timedelta
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select

from app.auth.sessions import utcnow
from app.billing.service import month_spend, notify_thresholds
from app.models import AuditLog, AuthSession, Invite, Usage, User

logger = logging.getLogger(__name__)

INVITE_GRACE = timedelta(days=30)
LOGIN_FAIL_RETENTION = timedelta(days=30)
BACKFILL_WINDOW = timedelta(days=1)
BACKFILL_BATCH = 50


async def cleanup_expired(factory) -> dict[str, int]:
    """Чистит просроченные сессии, давно просроченные инвайты и анонимные login_fail."""
    now = utcnow()
    async with factory() as db:
        sessions = await db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
        invites = await db.execute(delete(Invite).where(Invite.expires_at < now - INVITE_GRACE))
        fails = await db.execute(
            delete(AuditLog).where(
                AuditLog.event == "login_fail",
                AuditLog.user_id.is_(None),
                AuditLog.created_at < now - LOGIN_FAIL_RETENTION,
            )
        )
        await db.commit()
    return {
        "sessions": sessions.rowcount,
        "invites": invites.rowcount,
        "login_fail": fails.rowcount,
    }


async def backfill_costs(factory, llm, notifier) -> int:
    """Догружает cost_usd для записей, где стоимость не пришла в стриме."""
    updated = 0
    last_id = 0
    while True:
        async with factory() as db:
            rows = (
                await db.execute(
                    select(Usage)
                    .where(
                        Usage.cost_usd.is_(None),
                        Usage.generation_id.is_not(None),
                        Usage.created_at > utcnow() - BACKFILL_WINDOW,
                        Usage.id > last_id,
                    )
                    .order_by(Usage.id)
                    .limit(BACKFILL_BATCH)
                )
            ).scalars().all()
            if not rows:
                return updated
            last_id = rows[-1].id

            costs: dict[int, Decimal] = {}
            for row in rows:
                try:
                    cost = await llm.get_generation_cost(row.generation_id)
                except Exception as exc:  # одна плохая запись не должна рушить пачку
                    logger.warning("backfill_costs: lookup failed (%s)", type(exc).__name__)
                    continue
                if cost is not None:
                    costs[row.id] = cost
            if not costs:
                continue

            user_ids = {r.user_id for r in rows if r.id in costs and r.user_id is not None}
            before = {uid: await month_spend(db, uid) for uid in user_ids}
            for row in rows:
                if row.id in costs:
                    row.cost_usd = costs[row.id]
            await db.flush()
            for uid in user_ids:
                user = await db.get(User, uid)
                if user is not None:
                    after = await month_spend(db, uid)
                    await notify_thresholds(db, notifier, user, before[uid], after)
            await db.commit()
            updated += len(costs)


def make_scheduler(app) -> AsyncIOScheduler:
    factory = app.state.sessionmaker

    async def _cleanup() -> None:
        try:
            await cleanup_expired(factory)
        except Exception as exc:
            logger.warning("cleanup_expired failed (%s)", type(exc).__name__)

    async def _backfill() -> None:
        try:
            await backfill_costs(factory, app.state.llm, app.state.notifier)
        except Exception as exc:
            logger.warning("backfill_costs failed (%s)", type(exc).__name__)

    scheduler = AsyncIOScheduler(timezone="UTC")
    opts = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(_cleanup, "interval", hours=6, id="cleanup_expired", **opts)
    scheduler.add_job(_backfill, "interval", minutes=5, id="backfill_costs", **opts)
    return scheduler
