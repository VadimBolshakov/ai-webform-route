from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import COOKIE_NAME, get_session, touch
from app.db import get_db
from app.models import AuthSession, User

TWO_FA_PATH = "/app/settings/2fa"
LOGOUT_PATH = "/app/logout"


@dataclass
class AuthContext:
    user: User
    session: AuthSession


async def current_auth(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    s = await get_session(db, request.cookies.get(COOKIE_NAME))
    if s is None:
        raise HTTPException(status_code=404)
    user = await db.get(User, s.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404)
    path = request.url.path
    token = request.app.state.settings.login_path_token
    if s.totp_pending:
        if path != f"/a/{token}/totp":
            raise HTTPException(status_code=404)
    elif (
        user.role == "admin"
        and not user.totp_enabled
        and not (path == TWO_FA_PATH or path.startswith(TWO_FA_PATH + "/") or path == LOGOUT_PATH)
    ):
        raise HTTPException(status_code=303, headers={"Location": TWO_FA_PATH})
    await touch(db, s)
    return AuthContext(user=user, session=s)


async def optional_auth(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AuthContext | None:
    """Like current_auth, but returns None instead of a neutral 404 (no/expired session)."""
    try:
        return await current_auth(request, db)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


async def current_user(ctx: AuthContext = Depends(current_auth)) -> User:
    return ctx.user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=404)
    return user
