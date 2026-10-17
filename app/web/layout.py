"""Контекст общего шаблона (боковая панель, меню пользователя, композер)."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext
from app.billing.service import month_spend
from app.chat.service import AUTO_MODEL, enabled_models, recent_chats


def money(value: Decimal) -> str:
    return f"{value:.2f}"


async def page_context(db: AsyncSession, ctx: AuthContext, current_id: int | None) -> dict:
    """Everything the shared layout (sidebar, user menu, composer) needs."""
    user = ctx.user
    models = await enabled_models(db)
    selected = user.default_model if any(m.model_id == user.default_model for m in models) else AUTO_MODEL
    return {
        "user": user,
        "is_admin": user.role == "admin",
        "csrf_token": ctx.session.csrf_token,
        "chats": await recent_chats(db, user),
        "current_id": current_id,
        "models": models,
        "auto_model": AUTO_MODEL,
        "selected_model": selected,
        "spend": money(await month_spend(db, user.id)),
        "limit": money(user.monthly_limit_usd),
    }
