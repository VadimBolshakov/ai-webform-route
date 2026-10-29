"""Usage statistics and digest texts for the owner bot. Metadata only: logins, numbers, models,
sums - never message content, chat titles, file names or IPs."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import month_start
from app.models import AuditLog, AuthSession, Chat, Message, Usage, User

ZERO = Decimal(0)


@dataclass
class UserLine:
    username: str
    messages: int
    cost: Decimal
    month_cost: Decimal
    limit: Decimal


@dataclass
class PeriodStats:
    active_users: int = 0
    total_users: int = 0
    messages: int = 0
    new_chats: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = ZERO
    month_cost_usd: Decimal = ZERO
    per_user: list[UserLine] = field(default_factory=list)
    model_errors: int = 0
    failed_logins: int = 0
    new_users: int = 0
    top_models: list[tuple[str, Decimal]] = field(default_factory=list)


def _local_midnight(day, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)


def period_window(
    kind: Literal["day", "week", "today"], now: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime, str]:
    """UTC [start, end) and a label. day = previous calendar day in tz, week = previous Mon-Sun,
    today = from local midnight to the next local midnight."""
    today = now.astimezone(tz).date()
    if kind == "today":
        return (
            _local_midnight(today, tz),
            _local_midnight(today + timedelta(days=1), tz),
            f"сегодня, {today:%d.%m.%Y}",
        )
    if kind == "day":
        day = today - timedelta(days=1)
        return _local_midnight(day, tz), _local_midnight(today, tz), f"{day:%d.%m.%Y}"
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(days=7)
    last = monday - timedelta(days=1)
    return (
        _local_midnight(first, tz),
        _local_midnight(monday, tz),
        f"{first:%d.%m}–{last:%d.%m.%Y}",
    )


async def collect(db: AsyncSession, start: datetime, end: datetime) -> PeriodStats:
    s = PeriodStats()
    s.total_users = await db.scalar(select(func.count(User.id))) or 0

    msgs = {
        r.user_id: r.n
        for r in (
            await db.execute(
                select(Chat.user_id, func.count(Message.id).label("n"))
                .join(Chat, Chat.id == Message.chat_id)
                .where(Message.role == "user", Message.created_at >= start, Message.created_at < end)
                .group_by(Chat.user_id)
            )
        ).all()
    }
    s.messages = sum(msgs.values())
    s.new_chats = (
        await db.scalar(
            select(func.count(Chat.id)).where(Chat.created_at >= start, Chat.created_at < end)
        )
        or 0
    )

    in_period = (Usage.created_at >= start, Usage.created_at < end)
    cost_expr = func.coalesce(func.sum(func.coalesce(Usage.cost_usd, 0)), 0)
    tot = (
        await db.execute(
            select(
                func.coalesce(func.sum(Usage.prompt_tokens), 0),
                func.coalesce(func.sum(Usage.completion_tokens), 0),
                cost_expr,
                func.count().filter(Usage.status == "error"),
            ).where(*in_period)
        )
    ).one()
    s.prompt_tokens, s.completion_tokens = int(tot[0]), int(tot[1])
    s.cost_usd, s.model_errors = Decimal(tot[2]), int(tot[3])

    # Calendar month (UTC, as in billing) up to the end of the period.
    mstart = month_start(end - timedelta(microseconds=1))
    s.month_cost_usd = Decimal(
        await db.scalar(select(cost_expr).where(Usage.created_at >= mstart, Usage.created_at < end))
    )

    per_cost = {
        r.user_id: Decimal(r.c)
        for r in (
            await db.execute(
                select(Usage.user_id, cost_expr.label("c"))
                .where(*in_period, Usage.user_id.is_not(None))
                .group_by(Usage.user_id)
            )
        ).all()
    }
    ids = set(per_cost) | set(msgs)
    s.active_users = len(ids)
    if ids:
        month_cost = {
            r.user_id: Decimal(r.c)
            for r in (
                await db.execute(
                    select(Usage.user_id, cost_expr.label("c"))
                    .where(Usage.created_at >= mstart, Usage.created_at < end, Usage.user_id.in_(ids))
                    .group_by(Usage.user_id)
                )
            ).all()
        }
        users = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
        s.per_user = sorted(
            (
                UserLine(
                    u.username,
                    msgs.get(u.id, 0),
                    per_cost.get(u.id, ZERO),
                    month_cost.get(u.id, ZERO),
                    u.monthly_limit_usd,
                )
                for u in users
            ),
            key=lambda x: (-x.cost, x.username),
        )

    for event, attr in (("login_fail", "failed_logins"), ("invite_redeemed", "new_users")):
        n = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.event == event, AuditLog.created_at >= start, AuditLog.created_at < end
            )
        )
        setattr(s, attr, n or 0)

    s.top_models = [
        (r.model, Decimal(r.c))
        for r in (
            await db.execute(
                select(Usage.model, cost_expr.label("c"))
                .where(*in_period)
                .group_by(Usage.model)
                .order_by(cost_expr.desc(), Usage.model)
                .limit(3)
            )
        ).all()
    ]
    return s


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _usd(x: Decimal) -> str:
    return f"${x:.2f}"


def format_digest(title: str, s: PeriodStats, weekly: bool) -> str:
    if s.messages == 0:
        return f"{title}: активности не было. С начала месяца {_usd(s.month_cost_usd)}"
    lines = [
        title,
        f"Активных пользователей: {s.active_users} из {s.total_users}",
        f"Сообщений: {s.messages}, новых чатов: {s.new_chats}",
        f"Токены: {_num(s.prompt_tokens + s.completion_tokens)} "
        f"(вход {_num(s.prompt_tokens)} / выход {_num(s.completion_tokens)})",
        f"Расход: {_usd(s.cost_usd)} · с начала месяца {_usd(s.month_cost_usd)}",
    ]
    if weekly and s.top_models:
        lines.append("Топ моделей: " + ", ".join(f"{m} {_usd(c)}" for m, c in s.top_models))
    if s.per_user:
        lines.append("По пользователям:")
        for u in s.per_user:
            lines.append(
                f"  {u.username} — {u.messages} сообщ., {_usd(u.cost)} "
                f"(месяц {_usd(u.month_cost)} из {_usd(u.limit)})"
            )
    lines.append(
        f"Ошибок модели: {s.model_errors} · неудачных входов: {s.failed_logins} "
        f"· новых пользователей: {s.new_users}"
    )
    return "\n".join(lines)


async def stats_text(db: AsyncSession, now: datetime, tz: ZoneInfo) -> str:
    """/stats: the daily layout for today (from local midnight)."""
    start, end, label = period_window("today", now, tz)
    return format_digest(f"Сводка за {label}", await collect(db, start, end), weekly=False)


async def users_text(
    db: AsyncSession, tz: ZoneInfo | None = None, now: datetime | None = None
) -> str:
    tz = tz or ZoneInfo("Europe/Moscow")
    now = now or datetime.now(timezone.utc)
    spend = {
        r.user_id: Decimal(r.c)
        for r in (
            await db.execute(
                select(
                    Usage.user_id,
                    func.coalesce(func.sum(func.coalesce(Usage.cost_usd, 0)), 0).label("c"),
                )
                .where(Usage.created_at >= month_start(now), Usage.user_id.is_not(None))
                .group_by(Usage.user_id)
            )
        ).all()
    }
    # Last activity = the later of the last usage row and the last session hit (as on the admin page).
    last: dict[int, datetime] = {}
    for stmt in (
        select(Usage.user_id, func.max(Usage.created_at).label("last_at"))
        .where(Usage.user_id.is_not(None))
        .group_by(Usage.user_id),
        select(AuthSession.user_id, func.max(AuthSession.last_seen).label("last_at")).group_by(
            AuthSession.user_id
        ),
    ):
        for r in (await db.execute(stmt)).all():
            if r.last_at is not None and (r.user_id not in last or r.last_at > last[r.user_id]):
                last[r.user_id] = r.last_at
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    if not users:
        return "Пользователей нет"
    lines = []
    for u in users:
        t = last.get(u.id)
        seen = t.astimezone(tz).strftime("%d.%m %H:%M") if t else "—"
        state = "активен" if u.is_active else "заблокирован"
        lines.append(
            f"{u.username} — {state}, {_usd(spend.get(u.id, ZERO))} из "
            f"{_usd(u.monthly_limit_usd)}, последняя активность {seen}"
        )
    return "\n".join(lines)
