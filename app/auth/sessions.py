from datetime import datetime, timedelta, timezone

from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AuthSession, User
from app.security.tokens import hash_token, new_token

COOKIE_NAME = "__Host-sid"
SESSION_TTL = timedelta(days=7)
PENDING_TTL = timedelta(minutes=10)  # 2FA code entry window; not extended
TOUCH_INTERVAL = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def set_session_cookie(response: Response, raw_id: str, settings: Settings) -> None:
    # settings does not affect the lifetime yet: the session TTL is the SESSION_TTL constant (see create_session).
    response.set_cookie(
        COOKIE_NAME,
        raw_id,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")


async def create_session(
    db: AsyncSession, user: User, ip: str, ua: str, totp_pending: bool = False
) -> tuple[str, AuthSession]:
    raw = new_token()
    s = AuthSession(
        id_hash=hash_token(raw),
        user_id=user.id,
        csrf_token=new_token(),
        expires_at=utcnow() + (PENDING_TTL if totp_pending else SESSION_TTL),
        ip=ip[:45],
        user_agent=ua[:512],
        totp_pending=totp_pending,
    )
    db.add(s)
    await db.flush()
    return raw, s


async def get_session(db: AsyncSession, raw_id: str | None) -> AuthSession | None:
    if not raw_id:
        return None
    row = (
        await db.execute(
            select(AuthSession)
            .join(User, User.id == AuthSession.user_id)
            .where(
                AuthSession.id_hash == hash_token(raw_id),
                AuthSession.expires_at > utcnow(),
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row


async def touch(db: AsyncSession, s: AuthSession) -> None:
    if s.totp_pending:
        return  # the intermediate session lives a fixed 10 minutes
    now = utcnow()
    if now - s.last_seen < TOUCH_INTERVAL:
        return
    s.last_seen = now
    s.expires_at = now + SESSION_TTL
    await db.commit()  # otherwise the sliding lifetime would roll back on requests without a write


async def revoke(db: AsyncSession, s: AuthSession) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.id == s.id))
    await db.flush()


async def revoke_all(db: AsyncSession, user_id: int, except_id: int | None = None) -> None:
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if except_id is not None:
        stmt = stmt.where(AuthSession.id != except_id)
    await db.execute(stmt)
    await db.flush()
