import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext, current_auth
from app.auth.routes import client_ip
from app.auth.service import audit, consume_totp, register_failure
from app.db import get_db
from app.models import User
from app.notify.base import Notifier, get_notifier
from app.security.crypto import decrypt, encrypt
from app.security.csrf import require_csrf
from app.security.passwords import MAX_LENGTH, verify_password
from app.security.totp import new_totp_secret, provisioning_qr_svg

router = APIRouter(prefix="/app/settings")

MAX_CODE = 16


def _page(
    request: Request, ctx: AuthContext, user: User, error: str | None = None, status: int = 200
):
    qr = None
    key = request.app.state.settings.fernet_key
    if not user.totp_enabled and user.totp_secret_enc:
        qr = provisioning_qr_svg(decrypt(user.totp_secret_enc, key), user.username)
    return request.app.state.templates.TemplateResponse(
        request,
        "settings_2fa.html",
        {
            "enabled": user.totp_enabled,
            "is_admin": user.role == "admin",
            "qr_svg": qr,
            "csrf_token": ctx.session.csrf_token,
            "error": error,
        },
        status_code=status,
    )


async def _locked_user(db: AsyncSession, user_id: int) -> User:
    return (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@router.get("/2fa")
async def twofa_page(
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    user = ctx.user
    if not user.totp_enabled:
        user.totp_secret_enc = encrypt(new_totp_secret(), request.app.state.settings.fernet_key)
        user.totp_last_step = None
        await db.commit()
    return _page(request, ctx, user)


@router.post("/2fa/enable")
async def twofa_enable(
    request: Request,
    code: str = Form(""),
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, ctx.user.id)
    if user.totp_enabled or not user.totp_secret_enc:
        raise HTTPException(status_code=400)
    if len(code) > MAX_CODE or not await consume_totp(db, user, code, request.app.state.settings.fernet_key):
        return _page(request, ctx, user, "Неверный код", 400)
    user.totp_enabled = True
    await audit(db, "totp_enabled", user, client_ip(request))
    await db.commit()
    return RedirectResponse("/app/settings/2fa", status_code=303)


@router.post("/2fa/disable")
async def twofa_disable(
    request: Request,
    password: str = Form(""),
    code: str = Form(""),
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, ctx.user.id)
    if user.role == "admin" or not user.totp_enabled:
        raise HTTPException(status_code=400)
    ip = client_ip(request)
    pw_ok = len(password) <= MAX_LENGTH and await asyncio.to_thread(
        verify_password, user.password_hash, password
    )
    # Код (и его шаг) расходуем только при верном пароле.
    code_ok = pw_ok and len(code) <= MAX_CODE and await consume_totp(db, user, code, request.app.state.settings.fernet_key)
    if not (pw_ok and code_ok):
        await audit(db, "totp_disable_fail", user, ip)
        await register_failure(db, user, ip, notifier)
        return _page(request, ctx, user, "Неверный пароль или код", 400)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    await audit(db, "totp_disabled", user, ip)
    await db.commit()
    return RedirectResponse("/app/settings/2fa", status_code=303)
