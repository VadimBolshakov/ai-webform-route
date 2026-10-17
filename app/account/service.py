"""Account settings: preferences, password check with lockout, spend, export, deletion."""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import LOCK_THRESHOLD, audit, register_failure
from app.auth.sessions import utcnow
from app.billing.service import month_start
from app.chat.service import AUTO_MODEL
from app.files.upload import clean_text
from app.models import AuditLog, AuthSession, Chat, Message, Usage, User
from app.notify.base import LogNotifier, Notifier
from app.security.passwords import MAX_LENGTH, verify_password

logger = logging.getLogger(__name__)

MAX_SYSTEM_PROMPT = 2000
LANGUAGES = ("ru", "en")
THEMES = ("light", "dark", "system")


class AccountError(Exception):
    pass


class WrongPassword(AccountError):
    """Неверный пароль; `locked` — достигнут порог блокировки."""

    def __init__(self, locked: bool = False) -> None:
        super().__init__("wrong password")
        self.locked = locked


class AccountLocked(AccountError):
    pass


class AdminForbidden(AccountError):
    pass


def deleted_label(user_id: int) -> str:
    return f"deleted-user-{user_id}"


def money(value: Decimal) -> str:
    return f"{value:.2f}"


def validate_preferences(
    fields: dict[str, str], model_ids: set[str]
) -> tuple[dict[str, str] | None, str | None]:
    """→ (значения, None) или (None, сообщение об ошибке по-русски)."""
    language = fields.get("answer_language", "")
    theme = fields.get("theme", "")
    prompt = clean_text(fields.get("system_prompt", "")).replace("\r\n", "\n").replace("\r", "\n")
    model = clean_text(fields.get("default_model", "")).strip()
    if language not in LANGUAGES:
        return None, "Неверный язык ответа"
    if theme not in THEMES:
        return None, "Неверная тема оформления"
    if len(prompt) > MAX_SYSTEM_PROMPT:
        return None, f"Системный промпт длиннее {MAX_SYSTEM_PROMPT} символов"
    if model != AUTO_MODEL and model not in model_ids:
        return None, "Выбранная модель недоступна"
    return {
        "answer_language": language,
        "theme": theme,
        "system_prompt": prompt,
        "default_model": model,
    }, None


async def check_password(
    db: AsyncSession, user: User, password: str, ip: str, notifier: Notifier
) -> None:
    """Проверяет текущий пароль с семантикой блокировки (как отключение 2FA).

    `user` должен быть заблокирован FOR UPDATE вызывающим. Неудача считается в
    register_failure (коммитит счётчик); при достижении порога — WrongPassword(locked=True).
    """
    if user.locked_until is not None and user.locked_until > utcnow():
        raise AccountLocked()
    ok = len(password) <= MAX_LENGTH and await asyncio.to_thread(
        verify_password, user.password_hash, password
    )
    if ok:
        return
    await audit(db, "password_check_fail", user, ip)
    await register_failure(db, user, ip, notifier)
    raise WrongPassword(locked=user.failed_logins >= LOCK_THRESHOLD)


async def spend_breakdown(db: AsyncSession, user_id: int) -> list[tuple[str, Decimal]]:
    start = month_start(datetime.now(timezone.utc))
    total = func.coalesce(func.sum(func.coalesce(Usage.cost_usd, 0)), 0)
    rows = await db.execute(
        select(Usage.model, total)
        .where(Usage.user_id == user_id, Usage.created_at >= start)
        .group_by(Usage.model)
        .order_by(total.desc(), Usage.model)
    )
    return [(m, Decimal(t)) for m, t in rows.all()]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


async def export_user_data(db: AsyncSession, user: User) -> dict:
    chats = (
        await db.execute(select(Chat).where(Chat.user_id == user.id).order_by(Chat.id))
    ).scalars().all()
    by_chat: dict[int, list[dict]] = {c.id: [] for c in chats}
    msgs = await db.execute(
        select(Message)
        .join(Chat, Chat.id == Message.chat_id)
        .where(Chat.user_id == user.id)
        .order_by(Message.id)
    )
    for m in msgs.scalars():
        by_chat[m.chat_id].append(
            {
                "role": m.role,
                "content": m.content,
                "attachments": m.attachments or [],
                "model": m.model,
                "created_at": _iso(m.created_at),
            }
        )
    usage = (
        await db.execute(select(Usage).where(Usage.user_id == user.id).order_by(Usage.id))
    ).scalars()
    sessions = (
        await db.execute(
            select(AuthSession).where(AuthSession.user_id == user.id).order_by(AuthSession.id)
        )
    ).scalars()
    events = (
        await db.execute(select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.id))
    ).scalars()
    return {
        "user": {
            "username": user.username,
            "created_at": _iso(user.created_at),
            "settings": {
                "answer_language": user.answer_language,
                "theme": user.theme,
                "system_prompt": user.system_prompt,
                "default_model": user.default_model,
            },
        },
        "chats": [
            {"title": c.title, "created_at": _iso(c.created_at), "messages": by_chat[c.id]}
            for c in chats
        ],
        "usage": [
            {
                "model": u.model,
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "cost_usd": None if u.cost_usd is None else str(u.cost_usd),
                "created_at": _iso(u.created_at),
            }
            for u in usage
        ],
        "sessions": [
            {
                "ip": s.ip,
                "user_agent": s.user_agent,
                "created_at": _iso(s.created_at),
                "last_seen": _iso(s.last_seen),
            }
            for s in sessions
        ],
        "audit": [
            {"event": a.event, "created_at": _iso(a.created_at), "ip": a.ip} for a in events
        ],
    }


async def delete_account(
    db: AsyncSession,
    user: User,
    password: str,
    *,
    ip: str = "",
    notifier: Notifier | None = None,
) -> None:
    """Удаляет аккаунт. Админ — запрещено; неверный пароль — WrongPassword (с блокировкой)."""
    notifier = notifier or LogNotifier()
    if user.role == "admin":
        raise AdminForbidden()
    # Строка пользователя блокируется на время проверки пароля и удаления.
    user = (
        await db.execute(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if user.role == "admin":  # роль могла измениться до блокировки строки
        raise AdminForbidden()
    await check_password(db, user, password, ip, notifier)

    uid = user.id
    username = user.username
    label = deleted_label(uid)
    await db.execute(update(Usage).where(Usage.user_id == uid).values(user_label=label))
    # Строки по user_id и «осиротевшие» (без user_id, но с именем): метка, имя в details, IP.
    no_name = AuditLog.details.op("-")("username")
    await db.execute(
        update(AuditLog)
        .where(
            (AuditLog.user_id == uid)
            | (AuditLog.user_id.is_(None) & (AuditLog.user_label == username))
        )
        .values(user_label=label, details=no_name, ip="")
    )
    # IP — персональные данные: у записей удалённого аккаунта не хранится (колонка NOT NULL → "").
    db.add(AuditLog(event="account_deleted", user_id=uid, user_label=label, ip="", details={}))
    await db.flush()
    await db.execute(delete(User).where(User.id == uid))  # каскад: сессии, чаты, сообщения
    db.expunge(user)
    await db.commit()
    try:
        await notifier.alert(f"Аккаунт удалён: {label}")
    except Exception:
        logger.exception("notifier.alert failed")
