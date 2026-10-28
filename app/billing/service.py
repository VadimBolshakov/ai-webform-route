import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
from app.llm.openrouter import StreamDone
from app.models import Usage, User
from app.notify.base import Notifier

logger = logging.getLogger(__name__)

WARN_RATIO = Decimal("0.8")


class LimitExceeded(Exception):
    def __init__(self, spend: Decimal, limit: Decimal) -> None:
        super().__init__(f"Месячный лимит исчерпан (${spend:.2f} из ${limit:.2f})")
        self.spend = spend
        self.limit = limit


def month_start(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("month_start requires an aware datetime")
    now = now.astimezone(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


async def month_spend(db: AsyncSession, user_id: int) -> Decimal:
    start = month_start(datetime.now(timezone.utc))
    total = await db.scalar(
        select(func.coalesce(func.sum(func.coalesce(Usage.cost_usd, 0)), 0)).where(
            Usage.user_id == user_id, Usage.created_at >= start
        )
    )
    return Decimal(total)


async def check_limit(db: AsyncSession, user: User) -> None:
    spend = await month_spend(db, user.id)
    limit = user.monthly_limit_usd
    if spend >= limit:
        raise LimitExceeded(spend, limit)


async def record_usage(
    db: AsyncSession,
    *,
    user: User,
    chat_id: int | None,
    done: StreamDone | None,
    model: str,
    latency_ms: int,
    status: str,
    error: str | None = None,
    generation_id: str | None = None,
) -> Usage:
    row = Usage(
        user_id=user.id,
        user_label=user.username,
        chat_id=chat_id,
        model=model,
        generation_id=done.generation_id if done else generation_id,
        prompt_tokens=done.prompt_tokens if done else 0,
        completion_tokens=done.completion_tokens if done else 0,
        cost_usd=done.cost_usd if done else None,
        latency_ms=latency_ms,
        status=status,
        error=error,
    )
    db.add(row)
    await db.flush()
    return row


async def _alert(notifier: Notifier, text: str) -> None:
    try:
        await notifier.alert(text)
    except Exception:
        logger.exception("failed to send limit alert")


async def notify_thresholds(
    db: AsyncSession,
    notifier: Notifier,
    user: User,
    before: Decimal,
    after: Decimal,
    ip: str = "",
) -> None:
    limit = user.monthly_limit_usd
    if limit <= 0:
        return
    if before < limit * WARN_RATIO <= after:
        await _alert(
            notifier,
            f"{user.username}: 80% of the limit used (${after:.2f} of ${limit:.2f})",
        )
    if before < limit <= after:
        await _alert(
            notifier,
            f"{user.username}: 100% of the limit used (${after:.2f} of ${limit:.2f})",
        )
        await audit(db, "limit_reached", user, ip, spend=str(after), limit=str(limit))
