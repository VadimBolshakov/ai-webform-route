import logging
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext, current_auth
from app.auth.invites import create_invite
from app.auth.service import audit
from app.auth.sessions import revoke_all, utcnow
from app.billing.service import month_start
from app.db import get_db
from app.models import AllowedModel, AuthSession, Invite, Usage, User
from app.security.csrf import require_csrf
from app.web.layout import money, page_context
from app.web.request_info import client_ip

logger = logging.getLogger(__name__)

MAX_LIMIT = Decimal("1000")
MAX_DECIMALS = 4
MAX_LIMIT_INPUT = 64
MODEL_ID_RE = re.compile(r"^[a-z0-9._-]+/[a-z0-9._:-]+$")
MAX_MODEL_ID = 200
INVITES_SHOWN = 50

MSG_BAD_LIMIT = "Лимит должен быть числом от 0 до 1000 не более чем с 4 знаками после запятой"
MSG_BAD_MODEL_ID = "Некорректный идентификатор модели (ожидается вид vendor/model)"
MSG_MODEL_EXISTS = "Такая модель уже добавлена"
MSG_MODEL_NOT_FOUND = "Модель не найдена в OpenRouter"
MSG_OR_DOWN = "OpenRouter недоступен"
MSG_SELF_BLOCK = "Нельзя заблокировать свою учётную запись"
MSG_ADMIN_BLOCK = "Нельзя заблокировать администратора"

STATUS_LABELS = {"active": "активен", "used": "использован", "expired": "просрочен / отозван"}


async def admin_auth(ctx: AuthContext = Depends(current_auth)) -> AuthContext:
    if ctx.user.role != "admin":
        raise HTTPException(status_code=404)
    return ctx


router = APIRouter(prefix="/app/admin")


def parse_limit(raw: str) -> Decimal | None:
    if len(raw) > MAX_LIMIT_INPUT:
        return None
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0 or value > MAX_LIMIT:
        return None
    if value.as_tuple().exponent < -MAX_DECIMALS:
        return None
    return value.quantize(Decimal(1).scaleb(-MAX_DECIMALS))


def invite_status(invite: Invite, now) -> str:
    if invite.used_at is not None:
        return "used"
    return "expired" if invite.expires_at <= now else "active"


async def _users_view(db: AsyncSession, me_id: int) -> list[dict]:
    last_seen = (
        select(AuthSession.user_id, func.max(AuthSession.last_seen).label("seen"))
        .group_by(AuthSession.user_id)
        .subquery()
    )
    spend = (
        select(Usage.user_id, func.sum(func.coalesce(Usage.cost_usd, 0)).label("total"))
        .where(Usage.created_at >= month_start(utcnow()), Usage.user_id.is_not(None))
        .group_by(Usage.user_id)
        .subquery()
    )
    rows = await db.execute(
        select(User, last_seen.c.seen, spend.c.total)
        .outerjoin(last_seen, last_seen.c.user_id == User.id)
        .outerjoin(spend, spend.c.user_id == User.id)
        .order_by(User.id)
    )
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "spend": money(Decimal(total or 0)),
            "limit": f"{u.monthly_limit_usd:.4f}".rstrip("0").rstrip("."),
            "last_seen": seen.strftime("%d.%m.%Y %H:%M") if seen else "—",
            "is_self": u.id == me_id,
        }
        for u, seen, total in rows
    ]


async def _page(
    request: Request,
    db: AsyncSession,
    ctx: AuthContext,
    *,
    status: int = 200,
    error: str | None = None,
    invite_url: str | None = None,
):
    now = utcnow()
    invites = (
        await db.execute(select(Invite).order_by(Invite.id.desc()).limit(INVITES_SHOWN))
    ).scalars()
    invite_rows = []
    for i in invites:
        st = invite_status(i, now)
        invite_rows.append(
            {
                "id": i.id,
                "status": st,
                "status_label": STATUS_LABELS[st],
                "expires_at": i.expires_at.strftime("%d.%m.%Y %H:%M"),
            }
        )
    all_models = (
        await db.execute(select(AllowedModel).order_by(AllowedModel.display_name, AllowedModel.id))
    ).scalars()
    return request.app.state.templates.TemplateResponse(
        request,
        "admin.html",
        {
            **await page_context(db, ctx, None),
            "users": await _users_view(db, ctx.user.id),
            "invites": invite_rows,
            "all_models": list(all_models),
            "error": error,
            "invite_url": invite_url,
        },
        status_code=status,
    )


def _back() -> RedirectResponse:
    return RedirectResponse("/app/admin", status_code=303)


@router.get("")
async def admin_page(
    request: Request, ctx: AuthContext = Depends(admin_auth), db: AsyncSession = Depends(get_db)
):
    return await _page(request, db, ctx)


@router.post("/invites")
async def new_invite(
    request: Request, ctx: AuthContext = Depends(admin_auth), db: AsyncSession = Depends(get_db)
):
    await require_csrf(request, ctx.session)
    url = await create_invite(db, ctx.user, request.app.state.settings, client_ip(request))
    await db.commit()
    return await _page(request, db, ctx, invite_url=url)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    invite_id: int,
    request: Request,
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404)
    if invite.used_at is not None:
        raise HTTPException(status_code=400)
    invite.expires_at = utcnow()
    await audit(db, "invite_revoked", ctx.user, client_ip(request), invite_id=invite.id)
    await db.commit()
    return _back()


async def _locked_user(db: AsyncSession, user_id: int) -> User:
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404)
    return user


@router.post("/users/{user_id}/limit")
async def set_limit(
    user_id: int,
    request: Request,
    limit: str = Form(""),
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, user_id)
    value = parse_limit(limit)
    if value is None:
        return await _page(request, db, ctx, status=400, error=MSG_BAD_LIMIT)
    old = user.monthly_limit_usd
    user.monthly_limit_usd = value
    await audit(
        db, "limit_changed", ctx.user, client_ip(request),
        target_id=user.id, old=str(old), new=str(value),
    )
    await db.commit()
    return _back()


@router.post("/users/{user_id}/block")
async def block_user(
    user_id: int,
    request: Request,
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, user_id)
    if user.id == ctx.user.id:
        return await _page(request, db, ctx, status=400, error=MSG_SELF_BLOCK)
    if user.role == "admin":
        return await _page(request, db, ctx, status=400, error=MSG_ADMIN_BLOCK)
    user.is_active = False
    await revoke_all(db, user.id)
    await audit(db, "user_blocked", ctx.user, client_ip(request), target_id=user.id)
    await db.commit()
    return _back()


@router.post("/users/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    request: Request,
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    user = await _locked_user(db, user_id)
    user.is_active = True
    await audit(db, "user_unblocked", ctx.user, client_ip(request), target_id=user.id)
    await db.commit()
    return _back()


@router.post("/models")
async def add_model(
    request: Request,
    model_id: str = Form(""),
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    model_id = model_id.strip()
    if len(model_id) > MAX_MODEL_ID or not MODEL_ID_RE.fullmatch(model_id):
        return await _page(request, db, ctx, status=400, error=MSG_BAD_MODEL_ID)
    if await db.scalar(select(AllowedModel.id).where(AllowedModel.model_id == model_id)):
        return await _page(request, db, ctx, status=400, error=MSG_MODEL_EXISTS)
    try:
        info = await request.app.state.llm.get_model_info(model_id)
        if info is not None:
            name = str(info["name"])[:200] or model_id
            supports_images = bool(info["supports_images"])
    except Exception as exc:  # LLMError и любые сбои разбора ответа
        logger.warning("get_model_info failed: %s", type(exc).__name__)
        return await _page(request, db, ctx, status=502, error=MSG_OR_DOWN)
    if info is None:
        return await _page(request, db, ctx, status=400, error=MSG_MODEL_NOT_FOUND)
    try:
        async with db.begin_nested():
            db.add(AllowedModel(model_id=model_id, display_name=name, supports_images=supports_images))
            await db.flush()
    except IntegrityError:
        return await _page(request, db, ctx, status=400, error=MSG_MODEL_EXISTS)
    await audit(db, "model_added", ctx.user, client_ip(request), model=model_id)
    await db.commit()
    return _back()


@router.post("/models/{model_pk}/toggle")
async def toggle_model(
    model_pk: int,
    request: Request,
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    model = await db.get(AllowedModel, model_pk)
    if model is None:
        raise HTTPException(status_code=404)
    model.enabled = not model.enabled
    await audit(
        db, "model_toggled", ctx.user, client_ip(request), model=model.model_id, enabled=model.enabled
    )
    await db.commit()
    return _back()


@router.post("/models/{model_pk}/delete")
async def delete_model(
    model_pk: int,
    request: Request,
    ctx: AuthContext = Depends(admin_auth),
    db: AsyncSession = Depends(get_db),
):
    await require_csrf(request, ctx.session)
    model = await db.get(AllowedModel, model_pk)
    if model is None:
        raise HTTPException(status_code=404)
    await db.execute(delete(AllowedModel).where(AllowedModel.id == model_pk))
    await audit(db, "model_deleted", ctx.user, client_ip(request), model=model.model_id)
    await db.commit()
    return _back()
