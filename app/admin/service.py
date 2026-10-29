"""Admin actions shared by the admin UI and the owner bot. The caller locks the target row and commits."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
from app.auth.sessions import revoke_all
from app.models import User

MSG_SELF_BLOCK = "Нельзя заблокировать свою учётную запись"
MSG_ADMIN_BLOCK = "Нельзя заблокировать администратора"


class AdminActionError(Exception):
    """A refused admin action; the message is safe to show to the admin."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def block_user(db: AsyncSession, actor: User, target: User, ip: str) -> None:
    if target.id == actor.id:
        raise AdminActionError(MSG_SELF_BLOCK)
    if target.role == "admin":
        raise AdminActionError(MSG_ADMIN_BLOCK)
    target.is_active = False
    await revoke_all(db, target.id)
    await audit(db, "user_blocked", actor, ip, target_id=target.id)


async def unblock_user(db: AsyncSession, actor: User, target: User, ip: str) -> None:
    target.is_active = True
    await audit(db, "user_unblocked", actor, ip, target_id=target.id)
