"""Owner bot: command dispatch and the long-polling loop. Only the owner's chat is served."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminActionError, block_user, unblock_user
from app.auth.invites import create_invite
from app.models import User
from app.telegram.client import TelegramError
from app.telegram.stats import stats_text, users_text

logger = logging.getLogger("telegram")

AUDIT_IP = "telegram"
POLL_TIMEOUT_S = 30
BACKOFF_MAX_S = 60

HELP_TEXT = (
    "Команды:\n"
    "/stats — сводка за сегодня\n"
    "/users — пользователи, расход за месяц, последняя активность\n"
    "/invite — новая ссылка-приглашение (одноразовая, 48 ч)\n"
    "/block <логин> — заблокировать пользователя\n"
    "/unblock <логин> — разблокировать пользователя\n"
    "/help — этот список"
)
MSG_UNKNOWN = "Неизвестная команда. /help"
MSG_ERROR = "Ошибка, подробности в логе сервера"
MSG_NOT_FOUND = "Пользователь не найден"
MSG_NO_ADMIN = "Нет активного администратора"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BotDeps:
    factory: Callable[[], Any]  # async context manager yielding an AsyncSession
    notifier: Any  # has `async send(text)`
    settings: Any
    owner_chat_id: int
    clock: Callable[[], datetime] = field(default=_utcnow)


async def _oldest_admin(db: AsyncSession) -> User | None:
    return (
        await db.execute(
            select(User)
            .where(User.role == "admin", User.is_active.is_(True))
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _locked_by_username(db: AsyncSession, username: str) -> User | None:
    return (
        await db.execute(
            select(User)
            .where(User.username == username.strip().lower())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _cmd_help(deps: BotDeps, arg: str) -> str:
    return HELP_TEXT


async def _cmd_stats(deps: BotDeps, arg: str) -> str:
    async with deps.factory() as db:
        return await stats_text(db, deps.clock(), ZoneInfo(deps.settings.digest_timezone))


async def _cmd_users(deps: BotDeps, arg: str) -> str:
    async with deps.factory() as db:
        return await users_text(db, ZoneInfo(deps.settings.digest_timezone), deps.clock())


async def _cmd_invite(deps: BotDeps, arg: str) -> str:
    async with deps.factory() as db:
        admin = await _oldest_admin(db)
        if admin is None:
            return MSG_NO_ADMIN
        url = await create_invite(db, admin, deps.settings, AUDIT_IP)
        await db.commit()
    return f"Ссылка-приглашение (одноразовая, 48 ч):\n{url}"


def _toggle(action: Callable[..., Awaitable[None]], cmd: str, done: str):
    async def run(deps: BotDeps, arg: str) -> str:
        if not arg:
            return f"Укажите логин: /{cmd} <логин>"
        async with deps.factory() as db:
            admin = await _oldest_admin(db)
            if admin is None:
                return MSG_NO_ADMIN
            target = await _locked_by_username(db, arg)
            if target is None:
                await db.rollback()
                return MSG_NOT_FOUND
            try:
                await action(db, admin, target, AUDIT_IP)
            except AdminActionError as e:
                await db.rollback()
                return e.message
            await db.commit()
            return f"Пользователь {target.username} {done}"

    return run


COMMANDS: dict[str, Callable[[BotDeps, str], Awaitable[str]]] = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/stats": _cmd_stats,
    "/users": _cmd_users,
    "/invite": _cmd_invite,
    "/block": _toggle(block_user, "block", "заблокирован"),
    "/unblock": _toggle(unblock_user, "unblock", "разблокирован"),
}


def _parse(text: str) -> tuple[str, str]:
    head, _, rest = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


async def handle_update(update: dict, deps: BotDeps) -> None:
    """Serve one update. Never raises; never logs message text."""
    try:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if chat_id != deps.owner_chat_id:
            # WARNING: the app configures no logging, so INFO would not reach the container log.
            logger.warning("telegram: ignored update from chat %s", chat_id)
            return
        name, arg = _parse(message.get("text") or "")
        handler = COMMANDS.get(name)
    except Exception as e:
        logger.warning("telegram: bad update (%s)", type(e).__name__)
        return
    if handler is None:
        reply = MSG_UNKNOWN
    else:
        try:
            reply = await handler(deps, arg)
        except Exception as e:
            logger.warning("telegram: command %s failed (%s)", name, type(e).__name__)
            reply = MSG_ERROR
    try:
        await deps.notifier.send(reply)
    except Exception as e:
        logger.warning("telegram: reply failed (%s)", type(e).__name__)


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake up as soon as `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def run_polling(
    client,
    deps: BotDeps,
    stop: asyncio.Event,
    pause: Callable[[asyncio.Event, float], Awaitable[None]] = _pause,
) -> None:
    """Long-poll getUpdates until `stop` is set. The backlog that piled up before start is skipped
    (no /block executed retroactively); errors back off 1, 2, 4 ... 60 s."""
    skipped = False
    offset: int | None = None
    backoff = 1
    while not stop.is_set():
        try:
            if not skipped:
                # offset=-1 returns only the newest update and forgets the older ones.
                backlog = await client.get_updates(-1, 0)
                ids = [u["update_id"] for u in backlog]
                if ids:
                    offset = max(ids) + 1
                skipped = True
                backoff = 1
                continue
            updates = await client.get_updates(offset, POLL_TIMEOUT_S)
            backoff = 1
            for u in updates:
                offset = max(offset or 0, u["update_id"] + 1)
                await handle_update(u, deps)
            await asyncio.sleep(0)  # an instant empty reply must not starve the event loop
        except Exception as e:
            # TelegramError carries only a short reason (never the URL); anything else: type only.
            reason = str(e) if isinstance(e, TelegramError) else type(e).__name__
            logger.warning("telegram: getUpdates failed (%s)", reason)
            await pause(stop, backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
