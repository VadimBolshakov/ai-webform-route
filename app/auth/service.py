from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import utcnow
from app.models import AuditLog, User
from app.notify.base import Notifier
from app.security.passwords import (
    MAX_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
)

LOCK_THRESHOLD = 5
_DUMMY_HASH = hash_password("dummy-password-for-timing")


async def audit(db: AsyncSession, event: str, user: User | None, ip: str, **details) -> None:
    db.add(
        AuditLog(
            event=event,
            user_id=user.id if user else None,
            user_label=(user.username if user else str(details.get("username", "")))[:64],
            ip=ip[:45],
            details=details,
        )
    )
    await db.flush()


async def authenticate(
    db: AsyncSession,
    username: str,
    password: str,
    ip: str,
    notifier: Notifier | None = None,
) -> User | None:
    user = (
        await db.execute(select(User).where(User.username == username.strip().lower()).with_for_update())
    ).scalar_one_or_none()

    if len(password) > MAX_LENGTH:
        # Не хешируем: ограничиваем работу Argon2; считаем обычной неудачей.
        ok = False
    else:
        ok = verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None:
        return None

    now = utcnow()
    if user.locked_until is not None and user.locked_until > now:
        return None
    if not user.is_active:
        return None

    if not ok:
        user.failed_logins += 1
        if user.failed_logins >= LOCK_THRESHOLD:
            minutes = min(2 ** (user.failed_logins - LOCK_THRESHOLD), 60)
            user.locked_until = now + timedelta(minutes=minutes)
        await db.flush()
        if user.failed_logins == LOCK_THRESHOLD and notifier is not None:
            await notifier.alert(f"Серия неудачных входов: {user.username}, IP {ip}")
        return None

    user.failed_logins = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    await db.flush()
    return user
