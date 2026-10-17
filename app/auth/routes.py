import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import COOKIE_NAME, AuthContext, current_auth
from app.auth.service import audit, authenticate
from app.auth.sessions import create_session, get_session, revoke
from app.db import get_db
from app.notify.base import Notifier, get_notifier
from app.security.csrf import require_csrf

router = APIRouter()

GENERIC_ERROR = "Неверный логин или пароль"
MAX_FIELD = 1024


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _check_token(request: Request, token: str) -> None:
    expected = request.app.state.settings.login_path_token
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=404)


def _form(request: Request, token: str, error: str | None = None, status: int = 200):
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"token": token, "error": error}, status_code=status
    )


@router.get("/a/{token}")
async def login_form(request: Request, token: str):
    _check_token(request, token)
    return _form(request, token)


@router.post("/a/{token}")
async def login_submit(
    request: Request,
    token: str,
    username: str = Form(""),
    password: str = Form(""),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    _check_token(request, token)
    ip = client_ip(request)
    if len(username) > MAX_FIELD or len(password) > MAX_FIELD:
        username, password = "", ""  # заведомо неверно; не логируем и не хешируем огромный ввод
    if not request.app.state.login_limiter.hit(ip):
        await audit(db, "limit_reached", None, ip, scope="login")
        await db.commit()
        return _form(request, token, GENERIC_ERROR, 429)

    user = await authenticate(db, username, password, ip, notifier)
    if user is None:
        await audit(db, "login_fail", None, ip, username=username.strip().lower()[:64])
        await db.commit()
        return _form(request, token, GENERIC_ERROR, 401)

    # Старая cookie игнорируется (фиксация сессии невозможна); валидную старую отзываем.
    old = await get_session(db, request.cookies.get(COOKIE_NAME))
    if old is not None:
        await revoke(db, old)
    raw, _ = await create_session(db, user, ip, request.headers.get("user-agent", ""))
    await audit(db, "login_ok", user, ip)
    await db.commit()

    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=7 * 24 * 3600,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return resp


@router.post("/app/logout")
async def logout(
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    await revoke(db, ctx.session)
    await audit(db, "logout", ctx.user, client_ip(request))
    await db.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return resp
