from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext, current_auth
from app.billing.service import LimitExceeded, check_limit, month_spend
from app.chat.context import build_messages, history_content, user_content
from app.chat.service import (
    DEFAULT_TITLE,
    SessionFactory,
    Turn,
    error_response,
    get_llm,
    get_owned_chat,
    model_supports_images,
    sse_response,
    stream_reply,
)
from app.db import get_db, get_session_factory
from app.files.extract import AttachmentError, extract_attachment
from app.files.upload import MalformedForm, ParsedForm, parse_form
from app.llm.openrouter import OpenRouterClient
from app.models import Chat, Message, User
from app.security.csrf import require_csrf, verify_csrf
from app.web.request_info import client_ip

router = APIRouter(prefix="/app/chats")

MAX_FILES = 5
MAX_TEXT_CHARS = 32_000
MSG_RATE = "Слишком много запросов, подождите минуту"
MSG_NO_IMAGES = "Выбранная модель не принимает изображения"
MSG_EMPTY = "Пустое сообщение"
MSG_TOO_LONG = f"Сообщение длиннее {MAX_TEXT_CHARS} символов"
MSG_NOTHING_TO_RETRY = "Нечего повторять"


async def _history(db: AsyncSession, chat_id: int) -> list[Message]:
    return list(
        (
            await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))
        ).scalars()
    )


async def _precheck(request: Request, db: AsyncSession, user: User, model: str):
    """Rate limit → monthly limit → model. Returns (error_response | None, supports_images)."""
    if not request.app.state.chat_limiter.hit(str(user.id)):
        return error_response(MSG_RATE), False
    try:
        await check_limit(db, user)
    except LimitExceeded as exc:
        return error_response(str(exc)), False
    return None, await model_supports_images(db, model)


async def _start(
    request: Request,
    db: AsyncSession,
    user: User,
    chat: Chat,
    llm: OpenRouterClient,
    factory: SessionFactory,
    *,
    model: str,
    history: list[Message],
    content,
    first_user_text: str,
) -> Response:
    settings = request.app.state.settings
    turn = Turn(
        user_id=user.id,
        chat_id=chat.id,
        model=model,
        messages=build_messages(user, history, content, settings.context_token_budget),
        before=await month_spend(db, user.id),
        limit=user.monthly_limit_usd,
        first_reply=chat.title == DEFAULT_TITLE and not any(m.role == "assistant" for m in history),
        first_user_text=first_user_text,
        ip=client_ip(request),
    )
    return sse_response(stream_reply(request, llm, factory, turn))


@router.post("")
async def create_chat(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    await require_csrf(request, ctx.session)
    chat = Chat(user_id=ctx.user.id)
    db.add(chat)
    await db.commit()
    return RedirectResponse(f"/app/chats/{chat.id}", status_code=303)


async def _read_form(request: Request, ctx: AuthContext) -> ParsedForm:
    header = request.headers.get("x-csrf-token")
    if header:
        verify_csrf(header, ctx.session)
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
    try:
        form = await parse_form(request, max_file_bytes=max_bytes, max_files=MAX_FILES)
    except MalformedForm:
        if not header:
            raise HTTPException(status_code=403)
        raise HTTPException(status_code=400)
    if not header:
        verify_csrf(form.fields.get("csrf_token"), ctx.session)
    return form


@router.post("/{chat_id}/messages")
async def send_message(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    llm: OpenRouterClient = Depends(get_llm),
    factory: SessionFactory = Depends(get_session_factory),
):
    user = ctx.user
    chat = await get_owned_chat(db, user, chat_id)
    form = await _read_form(request, ctx)
    model = form.fields.get("model") or user.default_model
    error, supports_images = await _precheck(request, db, user, model)
    if error is not None:
        return error

    text = form.fields.get("text", "")
    if form.error:
        return error_response(form.error)
    if len(text) > MAX_TEXT_CHARS:
        return error_response(MSG_TOO_LONG)
    if not text.strip() and not form.files:
        return error_response(MSG_EMPTY)
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
    try:
        attachments = [
            await extract_attachment(f.name, f.data, max_bytes=max_bytes) for f in form.files
        ]
    except AttachmentError as exc:
        return error_response(str(exc))
    if not supports_images and any(a.kind == "image" for a in attachments):
        return error_response(MSG_NO_IMAGES)

    history = await _history(db, chat.id)
    stored = [
        {"name": a.name, "kind": "text", "text": a.text}
        if a.kind == "text"
        else {"name": a.name, "kind": "image"}
        for a in attachments
    ]
    db.add(Message(chat_id=chat.id, role="user", content=text, attachments=stored))
    await db.commit()  # сообщение пользователя переживёт любой исход стрима
    title_text = text.strip() or ", ".join(a.name for a in attachments)
    return await _start(
        request, db, user, chat, llm, factory,
        model=model, history=history, content=user_content(text, attachments),
        first_user_text=title_text,
    )


@router.post("/{chat_id}/retry")
async def retry(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
    llm: OpenRouterClient = Depends(get_llm),
    factory: SessionFactory = Depends(get_session_factory),
):
    user = ctx.user
    chat = await get_owned_chat(db, user, chat_id)
    await require_csrf(request, ctx.session)
    form = await request.form()
    model = form.get("model") if isinstance(form.get("model"), str) else None
    model = model or user.default_model
    error, _ = await _precheck(request, db, user, model)
    if error is not None:
        return error

    msgs = await _history(db, chat.id)
    if msgs and msgs[-1].role == "assistant":
        await db.delete(msgs.pop())
    if not msgs or msgs[-1].role != "user":
        await db.rollback()
        return error_response(MSG_NOTHING_TO_RETRY)
    last = msgs.pop()
    await db.commit()
    # Изображения не хранятся — при повторе модель получает только «[изображение: имя]».
    return await _start(
        request, db, user, chat, llm, factory,
        model=model, history=msgs, content=history_content(last),
        first_user_text=last.content,
    )
