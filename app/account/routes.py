import asyncio
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.service import (
    AccountLocked,
    AdminForbidden,
    WrongPassword,
    check_password,
    delete_account,
    export_user_data,
    money,
    spend_breakdown,
    validate_preferences,
)
from app.auth.deps import AuthContext, current_auth
from app.auth.service import LOCK_THRESHOLD, audit, consume_totp, register_failure
from app.auth.sessions import (
    clear_session_cookie,
    create_session,
    revoke,
    revoke_all,
    set_session_cookie,
    utcnow,
)
from app.billing.service import month_spend
from app.chat.service import enabled_models
from app.db import get_db
from app.files.upload import BodyTooLarge, MalformedForm, UnsupportedForm, parse_urlencoded
from app.models import AuthSession, User
from app.notify.base import Notifier, get_notifier
from app.security.crypto import decrypt, encrypt
from app.security.csrf import require_csrf, verify_csrf
from app.security.passwords import MAX_LENGTH, hash_password, validate_password, verify_password
from app.security.totp import new_totp_secret, provisioning_qr_svg
from app.web.layout import page_context
from app.web.request_info import client_ip

router = APIRouter(prefix="/app/settings")

MAX_CODE = 16


async def _page(
    request: Request,
    db: AsyncSession,
    ctx: AuthContext,
    user: User,
    error: str | None = None,
    status: int = 200,
):
    qr = None
    key = request.app.state.settings.fernet_key
    if not user.totp_enabled and user.totp_secret_enc:
        qr = provisioning_qr_svg(decrypt(user.totp_secret_enc, key), user.username)
    return request.app.state.templates.TemplateResponse(
        request,
        "settings_2fa.html",
        {
            **await page_context(db, ctx, None),
            "enabled": user.totp_enabled,
            "qr_svg": qr,
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
    # Секрет не пересоздаётся при каждой загрузке: только если его ещё нет.
    if not user.totp_enabled and not user.totp_secret_enc:
        user.totp_secret_enc = encrypt(new_totp_secret(), request.app.state.settings.fernet_key)
        user.totp_last_step = None
        await db.commit()
    return await _page(request, db, ctx, user)


@router.post("/2fa/regenerate")
async def twofa_regenerate(
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, ctx.user.id)
    if user.totp_enabled:
        raise HTTPException(status_code=400)
    user.totp_secret_enc = encrypt(new_totp_secret(), request.app.state.settings.fernet_key)
    user.totp_last_step = None
    await db.commit()
    return RedirectResponse("/app/settings/2fa", status_code=303)


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
        return await _page(request, db, ctx, user, "Неверный код", 400)
    user.totp_enabled = True
    ip = client_ip(request)
    # Остальные сессии могли быть открыты без второго фактора — отзываем; текущей — новый ID.
    await revoke_all(db, user.id, except_id=ctx.session.id)
    raw, _ = await create_session(db, user, ip, request.headers.get("user-agent", ""))
    await revoke(db, ctx.session)
    await audit(db, "totp_enabled", user, ip)
    await db.commit()
    resp = RedirectResponse("/app/settings/2fa", status_code=303)
    set_session_cookie(resp, raw, request.app.state.settings)
    return resp


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
    if user.locked_until is not None and user.locked_until > utcnow():
        return await _page(request, db, ctx, user, "Неверный пароль или код", 400)
    pw_ok = len(password) <= MAX_LENGTH and await asyncio.to_thread(
        verify_password, user.password_hash, password
    )
    # Код (и его шаг) расходуем только при верном пароле.
    code_ok = pw_ok and len(code) <= MAX_CODE and await consume_totp(db, user, code, request.app.state.settings.fernet_key)
    if not (pw_ok and code_ok):
        await audit(db, "totp_disable_fail", user, ip)
        await register_failure(db, user, ip, notifier)
        if user.failed_logins >= LOCK_THRESHOLD:
            await revoke(db, ctx.session)
            await audit(db, "login_fail", user, ip, reason="totp_disable")
            await db.commit()
            resp = RedirectResponse("/", status_code=303)
            clear_session_cookie(resp)
            return resp
        return await _page(request, db, ctx, user, "Неверный пароль или код", 400)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    await audit(db, "totp_disabled", user, ip)
    await db.commit()
    return RedirectResponse("/app/settings/2fa", status_code=303)


# --- настройки ------------------------------------------------------------------------------

MAX_FORM_BYTES = 64 * 1024
UA_SHOWN = 120
NOTICES = {
    "prefs": "Настройки сохранены",
    "password": "Пароль изменён, остальные устройства отключены",
    "sessions": "Устройства обновлены",
}
MSG_WRONG_PASSWORD = "Неверный пароль"


async def _form(request: Request, ctx: AuthContext, *, body: bool = True) -> dict[str, str]:
    """Небольшая urlencoded-форма с ограничением размера + проверка CSRF."""
    header = request.headers.get("x-csrf-token")
    if header and not body:
        verify_csrf(header, ctx.session)
        return {}
    try:
        fields = await parse_urlencoded(request, max_bytes=MAX_FORM_BYTES)
    except UnsupportedForm:
        if not body:
            verify_csrf(None, ctx.session)  # без токена — 403, а не 415
        raise HTTPException(status_code=415) from None
    except BodyTooLarge:
        raise HTTPException(status_code=413) from None
    except MalformedForm:
        raise HTTPException(status_code=400) from None
    verify_csrf(header or fields.get("csrf_token"), ctx.session)
    return fields


async def _settings_page(
    request: Request,
    db: AsyncSession,
    ctx: AuthContext,
    *,
    status: int = 200,
    errors: dict[str, str] | None = None,
    prefs: dict[str, str] | None = None,
    notice: str | None = None,
):
    user = ctx.user
    now = utcnow()
    rows = (
        await db.execute(
            select(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.expires_at > now,
                AuthSession.totp_pending.is_(False),
            )
            .order_by(AuthSession.last_seen.desc(), AuthSession.id.desc())
            .limit(50)
        )
    ).scalars()
    sessions = [
        {
            "id": s.id,
            "ip": s.ip,
            "ua": (s.user_agent or "")[:UA_SHOWN] or "—",
            "last_seen": s.last_seen,
            "current": s.id == ctx.session.id,
        }
        for s in rows
    ]
    context = {
        **await page_context(db, ctx, None),
        "prefs": prefs
        or {
            "answer_language": user.answer_language,
            "theme": user.theme,
            "system_prompt": user.system_prompt,
            "default_model": user.default_model,
        },
        "errors": errors or {},
        "notice": NOTICES.get(notice or ""),
        "sessions": sessions,
        "month_total": money(await month_spend(db, user.id)),
        "breakdown": [(m, money(v)) for m, v in await spend_breakdown(db, user.id)],
        "totp_enabled": user.totp_enabled,
        "can_delete": user.role != "admin",
    }
    return request.app.state.templates.TemplateResponse(
        request, "settings.html", context, status_code=status
    )


@router.get("")
async def settings_page(
    request: Request,
    saved: str = "",
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    return await _settings_page(request, db, ctx, notice=saved)


@router.get("/sessions")
async def sessions_page(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    return await _settings_page(request, db, ctx)


@router.post("/preferences")
async def update_preferences(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    fields = await _form(request, ctx)
    models = await enabled_models(db)
    values, error = validate_preferences(fields, {m.model_id for m in models})
    if values is None:
        echo = {
            k: fields.get(k, "")
            for k in ("answer_language", "theme", "system_prompt", "default_model")
        }
        return await _settings_page(
            request, db, ctx, status=400, errors={"prefs": error or ""}, prefs=echo
        )
    user = ctx.user
    user.answer_language = values["answer_language"]
    user.theme = values["theme"]
    user.system_prompt = values["system_prompt"]
    user.default_model = values["default_model"]
    await db.commit()
    return RedirectResponse("/app/settings?saved=prefs", status_code=303)


async def _password_failure(
    request: Request,
    db: AsyncSession,
    ctx: AuthContext,
    section: str,
    exc: Exception,
):
    """Общая реакция на AccountLocked/WrongPassword: при блокировке сессия отзывается."""
    if isinstance(exc, WrongPassword) and exc.locked:
        await revoke(db, ctx.session)
        await audit(db, "login_fail", ctx.user, client_ip(request), reason=section)
        await db.commit()
        resp = RedirectResponse("/", status_code=303)
        clear_session_cookie(resp)
        return resp
    return await _settings_page(
        request, db, ctx, status=400, errors={section: MSG_WRONG_PASSWORD}
    )


@router.post("/password")
async def change_password(
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    fields = await _form(request, ctx)
    user = await _locked_user(db, ctx.user.id)
    ip = client_ip(request)
    try:
        await check_password(db, user, fields.get("current_password", ""), ip, notifier)
    except (AccountLocked, WrongPassword) as exc:
        return await _password_failure(request, db, ctx, "password", exc)
    new = fields.get("new_password", "")
    problems = validate_password(new, user.username)
    if new != fields.get("new_password2", ""):
        problems.append("Новые пароли не совпадают.")
    if problems:
        return await _settings_page(
            request, db, ctx, status=400, errors={"password": " ".join(problems)}
        )
    user.password_hash = await asyncio.to_thread(hash_password, new)
    if not user.totp_enabled:
        user.failed_logins = 0
        user.locked_until = None
    await revoke_all(db, user.id, except_id=ctx.session.id)
    raw, _ = await create_session(db, user, ip, request.headers.get("user-agent", ""))
    await revoke(db, ctx.session)  # ротация ID текущей сессии
    await audit(db, "password_changed", user, ip)
    await db.commit()
    resp = RedirectResponse("/app/settings?saved=password", status_code=303)
    set_session_cookie(resp, raw, request.app.state.settings)
    return resp


@router.post("/sessions/revoke-others")
async def revoke_others(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    await _form(request, ctx, body=False)
    await revoke_all(db, ctx.user.id, except_id=ctx.session.id)
    await audit(db, "sessions_revoked", ctx.user, client_ip(request))
    await db.commit()
    return RedirectResponse("/app/settings?saved=sessions", status_code=303)


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(
    session_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    await _form(request, ctx, body=False)
    target = await db.get(AuthSession, session_id)
    if target is None or target.user_id != ctx.user.id:
        raise HTTPException(status_code=404)
    if target.id == ctx.session.id:
        raise HTTPException(status_code=400)
    await revoke(db, target)
    await audit(db, "session_revoked", ctx.user, client_ip(request))
    await db.commit()
    return RedirectResponse("/app/settings?saved=sessions", status_code=303)


@router.get("/export")
async def export_data(
    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    data = await export_user_data(db, ctx.user)
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=my-data.json",
            "Cache-Control": "no-store",
        },
    )


@router.post("/delete")
async def delete_my_account(
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
):
    fields = await _form(request, ctx)
    if ctx.user.role == "admin":
        return await _settings_page(
            request, db, ctx, status=400,
            errors={"delete": "Аккаунт администратора удалить нельзя"},
        )
    if fields.get("confirm") != "1":
        return await _settings_page(
            request, db, ctx, status=400,
            errors={"delete": "Подтвердите, что понимаете: удаление необратимо"},
        )
    try:
        await delete_account(
            db, ctx.user, fields.get("password", ""), ip=client_ip(request), notifier=notifier
        )
    except AdminForbidden:
        raise HTTPException(status_code=400) from None
    except (AccountLocked, WrongPassword) as exc:
        return await _password_failure(request, db, ctx, "delete", exc)
    resp = RedirectResponse("/", status_code=303)
    clear_session_cookie(resp)
    return resp
