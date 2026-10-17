import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
from app.auth.sessions import utcnow
from app.auth.usernames import validate_username
from app.config import Settings
from app.models import Invite, User
from app.security.passwords import hash_password, validate_password
from app.security.tokens import hash_token, new_token

INVITE_TTL = timedelta(hours=48)
INVALID_MSG = "Ссылка недействительна"


class InviteError(Exception):
    """Ошибка регистрации; текст безопасен для показа пользователю."""


class InviteInvalid(InviteError):
    """Инвайт неизвестен, использован или просрочен (отвечаем нейтральным 404)."""


async def create_invite(db: AsyncSession, admin: User, settings: Settings, ip: str = "") -> str:
    raw = new_token()
    db.add(
        Invite(
            token_hash=hash_token(raw),
            created_by_id=admin.id,
            expires_at=utcnow() + INVITE_TTL,
        )
    )
    await db.flush()
    await audit(db, "invite_created", admin, ip)
    return f"{settings.base_url.rstrip('/')}/a/{settings.login_path_token}/join/{raw}"


async def get_valid_invite(db: AsyncSession, raw: str, lock: bool = False) -> Invite | None:
    stmt = select(Invite).where(
        Invite.token_hash == hash_token(raw),
        Invite.used_at.is_(None),
        Invite.expires_at > utcnow(),
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def redeem_invite(
    db: AsyncSession,
    raw: str,
    username: str,
    password: str,
    accepted: bool,
    settings: Settings,
    ip: str = "",
) -> User:
    """Создаёт пользователя и гасит инвайт; commit и алерт — на вызывающем."""
    invite = await get_valid_invite(db, raw, lock=True)
    if invite is None:
        raise InviteInvalid(INVALID_MSG)

    username = username.strip().lower()
    errors = validate_username(username)
    if not errors:
        errors = validate_password(password, username)
    if not accepted:
        errors.append("Необходимо принять политику конфиденциальности.")
    if errors:
        raise InviteError(" ".join(errors))
    if (await db.execute(select(User.id).where(User.username == username))).first():
        raise InviteError("Этот логин уже занят.")

    pw_hash = await asyncio.to_thread(hash_password, password)
    now = utcnow()
    user = User(
        username=username,
        password_hash=pw_hash,
        role="user",
        monthly_limit_usd=settings.default_monthly_limit_usd,
        policy_accepted_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:  # гонка за логин между двумя инвайтами
        raise InviteError("Этот логин уже занят.") from None
    invite.used_by_id = user.id
    invite.used_at = now
    await audit(db, "invite_redeemed", user, ip, invite_id=invite.id)
    return user
