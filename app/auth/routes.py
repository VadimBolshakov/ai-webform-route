import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import COOKIE_NAME, AuthContext, current_auth
from app.auth.service import (
    LOCK_THRESHOLD,
    audit,
    authenticate,
    consume_totp,
    register_failure,
)
from app.auth.sessions import create_session, get_session, revoke, utcnow
from app.db import get_db
from app.models import User
from app.notify.base import Notifier, get_notifier
from app.security.csrf import require_csrf

router = APIRouter()

GENERIC_ERROR = "Неверный логин или пароль"
MAX_FIELD = 1024
MAX_CODE = 16


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


def _set_cookie(resp, raw: str) -> None:
    resp.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=7 * 24 * 3600,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
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
    pending = user.totp_enabled
    raw, _ = await create_session(
        db, user, ip, request.headers.get("user-agent", ""), totp_pending=pending
    )
    if not pending:
        await audit(db, "login_ok", user, ip)
    await db.commit()

    resp = RedirectResponse(f"/a/{token}/totp" if pending else "/app", status_code=303)
    _set_cookie(resp, raw)
    return resp


def _totp_form(
    request: Request, ctx: AuthContext, token: str, error: str | None = None, status: int = 200
):
    return request.app.state.templates.TemplateResponse(
        request,
        "totp.html",
        {"token": token, "error": error, "csrf_token": ctx.session.csrf_token},
        status_code=status,
    )


def _drop_session_redirect(token: str):
    resp = RedirectResponse(f"/a/{token}", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return resp


@router.get("/a/{token}/totp")
async def totp_form(request: Request, token: str, ctx: AuthContext = Depends(current_auth)):
    _check_token(request, token)
    if not ctx.session.totp_pending:
        raise HTTPException(status_code=404)
    return _totp_form(request, ctx, token)


@router.post("/a/{token}/totp")
async def totp_submit(
    request: Request,
    token: str,
    code: str = Form(""),
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    _check_token(request, token)
    if not ctx.session.totp_pending:
        raise HTTPException(status_code=404)
    await require_csrf(request, ctx.session)
    ip = client_ip(request)
    user = (
        await db.execute(
            select(User)
            .where(User.id == ctx.user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if user.locked_until is not None and user.locked_until > utcnow():
        await revoke(db, ctx.session)
        await db.commit()
        return _drop_session_redirect(token)

    if len(code) <= MAX_CODE and await consume_totp(db, user, code, request.app.state.settings.fernet_key):
        user.failed_logins = 0
        user.locked_until = None
        # Ротация ID сессии: после повышения привилегий старый идентификатор бесполезен.
        await revoke(db, ctx.session)
        raw, _ = await create_session(db, user, ip, request.headers.get("user-agent", ""))
        await audit(db, "login_ok", user, ip)
        await db.commit()
        resp = RedirectResponse("/app", status_code=303)
        _set_cookie(resp, raw)
        return resp

    await audit(db, "totp_fail", user, ip)
    await register_failure(db, user, ip, notifier)
    if user.failed_logins >= LOCK_THRESHOLD:
        await revoke(db, ctx.session)
        await audit(db, "login_fail", user, ip, reason="totp")
        await db.commit()
        return _drop_session_redirect(token)
    return _totp_form(request, ctx, token, "Неверный код", 401)


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
