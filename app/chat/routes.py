import re
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext, current_auth
from app.billing.service import LimitExceeded, check_limit, month_spend
from app.chat.context import build_messages, history_content, user_content
from app.chat.service import (
    DEFAULT_TITLE,
    SessionFactory,
    Turn,
    chat_messages,
    error_response,
    get_llm,
    get_owned_chat,
    last_chat,
    model_supports_images,
    recent_chats,
    search_chats,
    sse_response,
    stream_reply,
)
from app.db import get_db, get_session_factory
from app.files.extract import AttachmentError, extract_attachment
from app.files.upload import (
    BodyTooLarge,
    MalformedForm,
    ParsedForm,
    UnsupportedForm,
    clean_text,
    parse_form,
    parse_urlencoded,
)
from app.llm.openrouter import OpenRouterClient
from app.models import Chat, Message, User
from app.security.csrf import verify_csrf
from app.web.layout import page_context
from app.web.request_info import client_ip

# Все POST здесь требуют заголовок X-CSRF-Token: тело никогда не разбирается Starlette
# (его парсер пишет крупные файлы во временные файлы на диск). HTMX шлёт заголовок из
# hx-headers на <body>, chat.js — сам.
router = APIRouter(prefix="/app")

MAX_FILES = 5
MAX_TEXT_CHARS = 32_000
MAX_TITLE_CHARS = 200
MAX_RENAME_BODY = 4096
MAX_PAGE = 100_000
MSG_RATE = "Слишком много запросов, подождите минуту"
MSG_NO_IMAGES = "Выбранная модель не принимает изображения"
MSG_EMPTY = "Пустое сообщение"
MSG_TOO_LONG = f"Сообщение длиннее {MAX_TEXT_CHARS} символов"
MSG_NOTHING_TO_RETRY = "Нечего повторять"

_CHAT_PATH = re.compile(r"/app/chats/(\d+)")


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


@router.post("/chats")
async def create_chat(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    _require_csrf_header(request, ctx)
    chat = Chat(user_id=ctx.user.id)
    db.add(chat)
    await db.commit()
    url = f"/app/chats/{chat.id}"
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _require_csrf_header(request: Request, ctx: AuthContext) -> None:
    verify_csrf(request.headers.get("x-csrf-token"), ctx.session)


async def _read_form(request: Request, ctx: AuthContext) -> ParsedForm:
    _require_csrf_header(request, ctx)
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
    try:
        return await parse_form(request, max_file_bytes=max_bytes, max_files=MAX_FILES)
    except UnsupportedForm:
        raise HTTPException(status_code=415)
    except BodyTooLarge:
        raise HTTPException(status_code=413)
    except MalformedForm:
        raise HTTPException(status_code=400)


@router.post("/chats/{chat_id}/messages")
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
    model = clean_text(form.fields.get("model", "")).strip()[:200] or user.default_model
    error, supports_images = await _precheck(request, db, user, model)
    if error is not None:
        return error

    text = clean_text(form.fields.get("text", ""))
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
    for a in attachments:
        if a.text is not None:
            a.text = clean_text(a.text)
    if not supports_images and any(a.kind == "image" for a in attachments):
        return error_response(MSG_NO_IMAGES)

    history = await chat_messages(db, chat.id)
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


@router.post("/chats/{chat_id}/retry")
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
    _require_csrf_header(request, ctx)
    # Модель — из query (?model=), тело не читается.
    model = clean_text(request.query_params.get("model", "")).strip()[:200] or user.default_model
    error, _ = await _precheck(request, db, user, model)
    if error is not None:
        return error

    msgs = await chat_messages(db, chat.id)
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


# --- pages ---------------------------------------------------------------------------------


def _render(request: Request, name: str, context: dict, status_code: int = 200):
    return request.app.state.templates.TemplateResponse(
        request, name, context, status_code=status_code
    )


@router.get("")
async def app_home(
    request: Request, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)
):
    chat = await last_chat(db, ctx.user)
    if chat is not None:
        return RedirectResponse(f"/app/chats/{chat.id}", status_code=303)
    context = await page_context(db, ctx, None)
    return _render(request, "chat.html", {**context, "chat": None, "messages": []})


@router.get("/chats/all")
async def all_chats(
    request: Request,
    q: str = "",
    page: int = Query(1, ge=1, le=MAX_PAGE),
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    q = clean_text(q).strip()[:MAX_TITLE_CHARS]
    results, has_next = await search_chats(db, ctx.user, q, page)

    def page_url(n: int) -> str:
        return "/app/chats/all?" + urlencode({"page": n, **({"q": q} if q else {})})

    context = await page_context(db, ctx, None)
    return _render(
        request,
        "all_chats.html",
        {
            **context,
            "q": q,
            "results": results,
            "prev_url": page_url(page - 1) if page > 1 else None,
            "next_url": page_url(page + 1) if has_next else None,
        },
    )


@router.get("/chats/sidebar")
async def sidebar(
    request: Request,
    current: int | None = None,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    """Sidebar fragment (chat.js refreshes it after a reply: new title, new order)."""
    return await _sidebar(request, db, ctx.user, current)


@router.get("/chats/{chat_id}")
async def chat_page(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    chat = await get_owned_chat(db, ctx.user, chat_id)
    context = await page_context(db, ctx, chat.id)
    return _render(
        request, "chat.html", {**context, "chat": chat, "messages": await chat_messages(db, chat.id)}
    )


# --- sidebar actions (HTMX) ----------------------------------------------------------------


async def _sidebar(request: Request, db: AsyncSession, user: User, current_id: int | None):
    return _render(
        request, "_chat_list.html", {"chats": await recent_chats(db, user), "current_id": current_id}
    )


async def _after_action(
    request: Request, db: AsyncSession, user: User, deleted_id: int | None = None
) -> Response:
    """Updated sidebar fragment; or tell HTMX to reload/leave the page it was sent from."""
    path = urlsplit(request.headers.get("hx-current-url", "")).path
    if path == "/app/chats/all":
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    m = _CHAT_PATH.fullmatch(path)
    current_id = int(m.group(1)) if m else None
    if deleted_id is not None and current_id == deleted_id:
        return Response(status_code=204, headers={"HX-Redirect": "/app"})
    return await _sidebar(request, db, user, current_id)


@router.post("/chats/{chat_id}/rename")
async def rename_chat(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    _require_csrf_header(request, ctx)
    chat = await get_owned_chat(db, ctx.user, chat_id)
    try:
        fields = await parse_urlencoded(request, max_bytes=MAX_RENAME_BODY)
    except UnsupportedForm:
        raise HTTPException(status_code=415)
    except BodyTooLarge:
        raise HTTPException(status_code=413)
    title = " ".join(clean_text(fields.get("title", "")).split())[:MAX_TITLE_CHARS]
    if not title:
        raise HTTPException(status_code=400)
    chat.title = title
    await db.commit()
    return await _after_action(request, db, ctx.user)


@router.post("/chats/{chat_id}/pin")
async def pin_chat(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    _require_csrf_header(request, ctx)
    chat = await get_owned_chat(db, ctx.user, chat_id)
    chat.pinned = not chat.pinned
    await db.commit()
    return await _after_action(request, db, ctx.user)


@router.post("/chats/{chat_id}/delete")
async def delete_chat(
    chat_id: int,
    request: Request,
    ctx: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_db),
):
    _require_csrf_header(request, ctx)
    chat = await get_owned_chat(db, ctx.user, chat_id)
    await db.delete(chat)  # сообщения — ON DELETE CASCADE, usage.chat_id — SET NULL
    await db.commit()
    return await _after_action(request, db, ctx.user, deleted_id=chat_id)
