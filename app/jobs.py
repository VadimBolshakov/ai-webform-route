"""Background jobs (APScheduler). Functions take a session/client factory - tests call them directly."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select

from app.auth.sessions import utcnow
from app.billing.service import month_spend, notify_thresholds
from app.models import AuditLog, AuthSession, Invite, Usage, User
from app.telegram.notifier import TelegramNotifier
from app.telegram.stats import collect, format_digest, period_window

logger = logging.getLogger(__name__)

INVITE_GRACE = timedelta(days=30)
LOGIN_FAIL_RETENTION = timedelta(days=30)
BACKFILL_WINDOW = timedelta(days=1)
BACKFILL_BATCH = 50


async def cleanup_expired(factory) -> dict[str, int]:
    """Cleans expired sessions, long-expired invites, anonymous login_fail and limit_reached."""
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
        limits = await db.execute(
            delete(AuditLog).where(
                AuditLog.event == "limit_reached",
                AuditLog.user_id.is_(None),
                AuditLog.created_at < now - LOGIN_FAIL_RETENTION,
            )
        )
        await db.commit()
    return {
        "sessions": sessions.rowcount,
        "invites": invites.rowcount,
        "login_fail": fails.rowcount,
        "limit_reached": limits.rowcount,
    }


async def backfill_costs(factory, llm, notifier) -> int:
    """Backfills cost_usd for records where the cost did not arrive in the stream."""
    updated = 0
    last_id = 0
    while True:
        # Short read session: the transaction does not hang around during HTTP requests.
        async with factory() as db:
            cands = (
                await db.execute(
                    select(Usage.id, Usage.generation_id, Usage.user_id)
                    .where(
                        Usage.cost_usd.is_(None),
                        Usage.generation_id.is_not(None),
                        Usage.created_at > utcnow() - BACKFILL_WINDOW,
                        Usage.id > last_id,
                    )
                    .order_by(Usage.id)
                    .limit(BACKFILL_BATCH)
                )
            ).all()
            await db.rollback()
        if not cands:
            return updated
        last_id = cands[-1].id

        costs: dict[int, Decimal] = {}
        for row in cands:
            try:
                cost = await llm.get_generation_cost(row.generation_id)
            except Exception as exc:  # one bad record must not break the whole batch
                logger.warning("backfill_costs: lookup failed (%s)", type(exc).__name__)
                continue
            if cost is not None:
                costs[row.id] = cost
        if not costs:
            continue

        async with factory() as db:
            user_ids = {r.user_id for r in cands if r.id in costs and r.user_id is not None}
            before = {uid: await month_spend(db, uid) for uid in user_ids}
            res = await db.execute(select(Usage).where(Usage.id.in_(costs), Usage.cost_usd.is_(None)))
            changed = 0
            for row in res.scalars():
                row.cost_usd = costs[row.id]
                changed += 1
            await db.flush()
            for uid in user_ids:
                user = await db.get(User, uid)
                if user is not None:
                    await notify_thresholds(db, notifier, user, before[uid], await month_spend(db, uid))
            await db.commit()
            updated += changed


async def send_digest(factory, notifier, settings, now: datetime | None = None) -> str | None:
    """Daily digest for the previous local day; on Mondays the weekly one (previous Mon-Sun)."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.digest_timezone)
    weekly = now.astimezone(tz).weekday() == 0
    start, end, label = period_window("week" if weekly else "day", now, tz)
    async with factory() as db:
        stats = await collect(db, start, end)
        await db.rollback()
    prefix = "Сводка за неделю" if weekly else "Сводка за"
    text = format_digest(f"{prefix} {label}", stats, weekly)
    await notifier.send(text)
    return text


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

    async def _digest() -> None:
        try:
            await send_digest(factory, app.state.notifier, app.state.settings)
        except Exception as exc:
            logger.warning("send_digest failed (%s)", type(exc).__name__)

    async def _flush_dropped() -> None:
        notifier = app.state.notifier
        if isinstance(notifier, TelegramNotifier):
            try:
                await notifier.flush_dropped()
            except Exception as exc:
                logger.warning("flush_dropped failed (%s)", type(exc).__name__)

    scheduler = AsyncIOScheduler(timezone="UTC")
    opts = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(_cleanup, "interval", hours=6, id="cleanup_expired", **opts)
    scheduler.add_job(_backfill, "interval", minutes=5, id="backfill_costs", **opts)
    if isinstance(app.state.notifier, TelegramNotifier):
        scheduler.add_job(_flush_dropped, "interval", minutes=10, id="flush_dropped", **opts)
    settings = app.state.settings
    if settings.telegram_configured:
        hour, minute = settings.digest_time.split(":")
        scheduler.add_job(
            _digest,
            CronTrigger(
                hour=int(hour), minute=int(minute), timezone=ZoneInfo(settings.digest_timezone)
            ),
            id="telegram_digest",
            misfire_grace_time=3600,
            **opts,
        )
    return scheduler
