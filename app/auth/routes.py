import hmac
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext, current_auth, optional_auth
from app.auth.invites import InviteError, InviteInvalid, get_valid_invite, redeem_invite
from app.auth.service import (
    LOCK_THRESHOLD,
    audit,
    authenticate,
    consume_totp,
    register_failure,
)
from app.auth.sessions import (
    COOKIE_NAME,
    clear_session_cookie,
    create_session,
    get_session,
    revoke,
    set_session_cookie,
    utcnow,
)
from app.db import get_db
from app.models import User
from app.notify.base import Notifier, get_notifier
from app.security.csrf import require_csrf
from app.web.request_info import client_ip

router = APIRouter()
logger = logging.getLogger(__name__)

GENERIC_ERROR = "Неверный логин или пароль"
MAX_FIELD = 1024
MAX_CODE = 16


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
    limiter = request.app.state.login_limiter
    if not limiter.hit(ip):
        if limiter.first_denial(ip):  # одна запись на IP за окно, не на каждую попытку
            await audit(db, "limit_reached", None, ip, scope="login")
            await db.commit()
        return _form(request, token, GENERIC_ERROR, 429)

    user = await authenticate(db, username, password, ip, notifier)
    if user is None:
        uname = username.strip().lower()[:64]
        known = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
        if known is not None:
            # Существующий аккаунт: привязка по user_id, имя в details не пишем.
            await audit(db, "login_fail", known, ip)
        else:
            await audit(db, "login_fail", None, ip, username=uname)
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
    set_session_cookie(resp, raw, request.app.state.settings)
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
    clear_session_cookie(resp)
    return resp


# Без действующей промежуточной сессии (истекли 10 минут, отозвана) — назад ко входу,
# а не голый 404: токен входа верный, скрывать нечего.
@router.get("/a/{token}/totp")
async def totp_form(
    request: Request, token: str, ctx: AuthContext | None = Depends(optional_auth)
):
    _check_token(request, token)
    if ctx is None:
        return _drop_session_redirect(token)
    if not ctx.session.totp_pending:
        raise HTTPException(status_code=404)
    return _totp_form(request, ctx, token)


@router.post("/a/{token}/totp")
async def totp_submit(
    request: Request,
    token: str,
    code: str = Form(""),
    ctx: AuthContext | None = Depends(optional_auth),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    _check_token(request, token)
    if ctx is None:
        return _drop_session_redirect(token)
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
        set_session_cookie(resp, raw, request.app.state.settings)
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
    clear_session_cookie(resp)
    return resp


def _join_form(
    request: Request,
    token: str,
    invite: str,
    username: str = "",
    error: str | None = None,
    status: int = 200,
):
    return request.app.state.templates.TemplateResponse(
        request,
        "join.html",
        {"token": token, "invite": invite, "username": username, "error": error},
        status_code=status,
    )


@router.get("/a/{token}/join/{invite}")
async def join_form(request: Request, token: str, invite: str, db: AsyncSession = Depends(get_db)):
    _check_token(request, token)
    if await get_valid_invite(db, invite) is None:
        raise HTTPException(status_code=404)
    return _join_form(request, token, invite)


@router.post("/a/{token}/join/{invite}")
async def join_submit(
    request: Request,
    token: str,
    invite: str,
    username: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    accepted: str = Form(""),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    _check_token(request, token)
    if await get_valid_invite(db, invite) is None:
        raise HTTPException(status_code=404)
    ip = client_ip(request)
    shown = username.strip()[:64]
    if len(username) > MAX_FIELD or len(password) > MAX_FIELD or len(password2) > MAX_FIELD:
        return _join_form(request, token, invite, "", "Слишком длинное значение.", 400)
    if password != password2:
        return _join_form(request, token, invite, shown, "Пароли не совпадают.", 400)
    try:
        user = await redeem_invite(
            db, invite, username, password, bool(accepted), request.app.state.settings, ip
        )
    except InviteInvalid:
        raise HTTPException(status_code=404) from None
    except InviteError as e:
        await db.rollback()
        return _join_form(request, token, invite, shown, str(e), 400)

    old = await get_session(db, request.cookies.get(COOKIE_NAME))
    if old is not None:
        await revoke(db, old)
    raw, _ = await create_session(db, user, ip, request.headers.get("user-agent", ""))
    await audit(db, "login_ok", user, ip)
    await db.commit()
    try:
        await notifier.alert(f"Новый пользователь: {user.username}")
    except Exception:
        logger.exception("notifier.alert failed")

    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, raw, request.app.state.settings)
    return resp


@router.get("/a/{token}/privacy")
async def privacy(request: Request, token: str):
    _check_token(request, token)
    return request.app.state.templates.TemplateResponse(request, "privacy.html", {"token": token})
