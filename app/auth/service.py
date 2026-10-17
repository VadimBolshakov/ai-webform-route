import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import utcnow
from app.models import AuditLog, User
from app.notify.base import Notifier
from app.security.totp import match_totp_step
from app.security.crypto import decrypt
from app.security.passwords import (
    MAX_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
)

logger = logging.getLogger(__name__)
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
    notifier: Notifier,
) -> User | None:
    user = (
        await db.execute(select(User).where(User.username == username.strip().lower()).with_for_update())
    ).scalar_one_or_none()

    if len(password) > MAX_LENGTH:
        # Не хешируем: ограничиваем работу Argon2; считаем обычной неудачей.
        ok = False
    else:
        ok = await asyncio.to_thread(
            verify_password, user.password_hash if user else _DUMMY_HASH, password
        )
    if user is None:
        return None

    now = utcnow()
    if user.locked_until is not None and user.locked_until > now:
        return None
    if not user.is_active:
        return None

    if not ok:
        await register_failure(db, user, ip, notifier)
        return None

    if not user.totp_enabled:
        # С 2FA счётчик сбрасывается только после верного кода (иначе пароль даёт новые 5 попыток).
        user.failed_logins = 0
        user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = await asyncio.to_thread(hash_password, password)
    await db.flush()
    return user


async def register_failure(db: AsyncSession, user: User, ip: str, notifier: Notifier) -> None:
    """Неудачная попытка (пароль или код): счётчик, блокировка, алерт."""
    user.failed_logins += 1
    if user.failed_logins >= LOCK_THRESHOLD:
        minutes = min(2 ** (user.failed_logins - LOCK_THRESHOLD), 60)
        user.locked_until = utcnow() + timedelta(minutes=minutes)
    await db.commit()  # счётчик и блокировка сохраняются до любых побочных эффектов
    if user.failed_logins == LOCK_THRESHOLD:
        try:
            await notifier.alert(f"Серия неудачных входов: {user.username}, IP {ip}")
        except Exception:
            logger.exception("notifier.alert failed")


async def consume_totp(db: AsyncSession, user: User, code: str, fernet_key: str) -> bool:
    """Проверяет код и атомарно фиксирует шаг; повтор того же (или более раннего) шага отвергается."""
    if not user.totp_secret_enc:
        return False
    secret = decrypt(user.totp_secret_enc, fernet_key)
    step = match_totp_step(secret, code.replace(" ", ""), user.totp_last_step)
    if step is None:
        return False
    res = await db.execute(
        update(User)
        .where(User.id == user.id, (User.totp_last_step.is_(None)) | (User.totp_last_step < step))
        .values(totp_last_step=step)
    )
    if res.rowcount != 1:
        return False
    user.totp_last_step = step
    return True
