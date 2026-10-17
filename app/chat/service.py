"""Chat turn orchestration: SSE framing, streaming a reply, persisting its outcome, titles.

Post-stream writes use a fresh short-lived session from a session factory: the
request-scoped session is already closed once the StreamingResponse body runs.
Message contents are never logged.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import month_spend, notify_thresholds, record_usage
from app.files.upload import clean_text
from app.llm.openrouter import MSG_MODEL_ERROR, LLMError, OpenRouterClient, StreamDelta, StreamDone
from app.models import AllowedModel, Chat, Message, User

logger = logging.getLogger(__name__)

AUTO_MODEL = "openrouter/auto"
DEFAULT_TITLE = "Новый чат"
TITLE_PROMPT = (
    "Придумай короткий заголовок (3–5 слов) для диалога, который начинается так. "
    "Ответь только заголовком без кавычек."
)
TITLE_INPUT_CHARS = 1000
TITLE_MAX_CHARS = 200
TITLE_MAX_TOKENS = 20
ERROR_SERIES_ALERT = "Серия ошибок OpenRouter: 3 ошибки подряд за 10 минут"
MAX_ERROR_TEXT = 500
MSG_SAVE_FAILED = "Не удалось сохранить ответ"

SessionFactory = Callable[[], Any]  # () -> async context manager yielding AsyncSession


def get_llm(request: Request) -> OpenRouterClient:
    return request.app.state.llm


async def get_owned_chat(db: AsyncSession, user: User, chat_id: int) -> Chat:
    chat = await db.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=404)
    return chat


SIDEBAR_CHATS = 10
CHATS_PER_PAGE = 50


async def recent_chats(db: AsyncSession, user: User, limit: int = SIDEBAR_CHATS) -> list[Chat]:
    """Sidebar: pinned first, then most recently updated."""
    rows = await db.execute(
        select(Chat)
        .where(Chat.user_id == user.id)
        .order_by(Chat.pinned.desc(), Chat.updated_at.desc(), Chat.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def last_chat(db: AsyncSession, user: User) -> Chat | None:
    return await db.scalar(
        select(Chat)
        .where(Chat.user_id == user.id)
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
        .limit(1)
    )


def _like_pattern(q: str) -> str:
    """Substring pattern with LIKE wildcards taken literally (escape char: backslash)."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_chats(
    db: AsyncSession, user: User, q: str, page: int, per_page: int = CHATS_PER_PAGE
) -> tuple[list[Chat], bool]:
    """One page of the user's chats (title ILIKE q when given). Returns (chats, has_next)."""
    stmt = select(Chat).where(Chat.user_id == user.id)
    if q:
        stmt = stmt.where(Chat.title.ilike(_like_pattern(q), escape="\\"))
    rows = await db.execute(
        stmt.order_by(Chat.updated_at.desc(), Chat.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
    )
    chats = list(rows.scalars())
    return chats[:per_page], len(chats) > per_page


async def enabled_models(db: AsyncSession) -> list[AllowedModel]:
    rows = await db.execute(
        select(AllowedModel)
        .where(AllowedModel.enabled.is_(True))
        .order_by(AllowedModel.display_name, AllowedModel.model_id)
    )
    return list(rows.scalars())


async def chat_messages(db: AsyncSession, chat_id: int) -> list[Message]:
    rows = await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))
    return list(rows.scalars())


async def model_supports_images(db: AsyncSession, model_id: str) -> bool:
    """Whether the model accepts images; HTTP 400 if it is not allowed at all."""
    if model_id == AUTO_MODEL:
        return True
    row = await db.scalar(
        select(AllowedModel).where(AllowedModel.model_id == model_id, AllowedModel.enabled.is_(True))
    )
    if row is None:
        raise HTTPException(status_code=400, detail="Модель недоступна")
    return row.supports_images


# --- SSE ---------------------------------------------------------------------------------

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def sse_response(body: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)


def error_response(message: str) -> StreamingResponse:
    async def body():
        yield sse("error", {"message": message})

    return sse_response(body())


# --- background tasks ----------------------------------------------------------------------


def spawn(state, coro: Coroutine) -> asyncio.Task:
    """create_task with a strong reference on app.state (so the task is not GC'd)."""
    task = asyncio.create_task(coro)
    state.background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        state.background_tasks.discard(t)
        if not t.cancelled() and (exc := t.exception()) is not None:
            # Тип и traceback без текста сообщений (у движка hide_parameters=True).
            logger.error("background task failed: %s", type(exc).__name__, exc_info=exc)

    task.add_done_callback(_done)
    return task


async def shielded(state, coro: Coroutine):
    """Run `coro` to completion even if the caller is cancelled (e.g. client disconnect)."""
    return await asyncio.shield(spawn(state, coro))


class ErrorSeries:
    """Consecutive OpenRouter errors within a window; a success resets the series."""

    def __init__(self, threshold: int = 3, window_s: float = 600, clock=time.monotonic) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self._clock = clock
        self._times: deque[float] = deque()

    def success(self) -> None:
        self._times.clear()

    def failure(self) -> bool:
        """Record an error; True when the series reached the threshold (then it restarts)."""
        now = self._clock()
        self._times.append(now)
        while self._times and self._times[0] <= now - self.window_s:
            self._times.popleft()
        if len(self._times) >= self.threshold:
            self._times.clear()
            return True
        return False


# --- one turn ------------------------------------------------------------------------------


@dataclass
class Turn:
    user_id: int
    chat_id: int
    model: str
    messages: list[dict]
    before: Decimal  # потрачено за месяц до запроса
    limit: Decimal
    first_reply: bool
    first_user_text: str
    ip: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _finish(
    state,
    factory: SessionFactory,
    turn: Turn,
    *,
    text: str,
    done: StreamDone | None,
    status: str,
    latency_ms: int,
    error: str | None = None,
    generation_id: str | None = None,
) -> tuple[int | None, Decimal]:
    """Persist assistant message + usage in a short transaction. Returns (message_id, spend)."""
    text = clean_text(text)
    after = turn.before + ((done.cost_usd or Decimal(0)) if done else Decimal(0))
    async with factory() as s:
        user = await s.get(User, turn.user_id)
        chat = await s.get(Chat, turn.chat_id)
        if user is None:
            return None, after
        msg = None
        if chat is not None and (text or status == "ok"):
            msg = Message(
                chat_id=chat.id,
                role="assistant",
                content=text,
                model=done.model if done and done.model else turn.model,
                interrupted=status != "ok",
            )
            s.add(msg)
        if chat is not None:
            chat.updated_at = _now()
        await record_usage(
            s,
            user=user,
            chat_id=chat.id if chat is not None else None,
            done=done,
            model=(done.model if done and done.model else turn.model),
            latency_ms=latency_ms,
            status=status,
            error=error,
            generation_id=generation_id,
        )
        if done is not None:
            await notify_thresholds(s, state.notifier, user, turn.before, after, turn.ip)
        await s.commit()
        return (msg.id if msg is not None else None), after


async def _alert(state, text: str) -> None:
    try:
        await state.notifier.alert(text)
    except Exception:
        logger.exception("failed to send alert")


def _interrupted(state, factory, turn, parts, done, gen_id, latency_ms) -> Coroutine:
    """Partial reply; a StreamDone already received (if any) supplies cost and generation id."""
    return _finish(
        state, factory, turn, text="".join(parts), done=done, status="interrupted",
        latency_ms=latency_ms, generation_id=done.generation_id if done else gen_id,
    )


async def _persist_quietly(state, coro: Coroutine) -> None:
    """Shielded persistence whose failure is logged, never raised (cancellation still is)."""
    try:
        await shielded(state, coro)
    except Exception:
        logger.error("chat outcome not persisted")  # traceback логирует spawn()


async def stream_reply(
    request: Request, llm: OpenRouterClient, factory: SessionFactory, turn: Turn
) -> AsyncIterator[bytes]:
    """SSE body: deltas, then `done` or `error`. Persists the outcome in every case.

    Errors here carry "retry": true — the user message is already saved, so POST …/retry
    can regenerate the answer. Pre-stream errors (error_response) have no such flag.
    """
    state = request.app.state
    parts: list[str] = []
    gen_id: str | None = None
    done: StreamDone | None = None
    failure: LLMError | None = None
    disconnected = False
    t0 = time.monotonic()

    def latency() -> int:
        return int((time.monotonic() - t0) * 1000)

    try:
        try:
            async with aclosing(llm.stream_chat(turn.model, turn.messages)) as events:
                async for ev in events:
                    if isinstance(ev, StreamDone):
                        done = ev
                        continue
                    if isinstance(ev, StreamDelta):
                        gen_id = ev.generation_id or gen_id
                        if ev.text:
                            parts.append(ev.text)
                            yield sse("delta", {"t": ev.text})
                    if await request.is_disconnected():
                        disconnected = True
                        break
        except LLMError as exc:
            failure = exc
            gen_id = exc.generation_id or gen_id
    except (asyncio.CancelledError, GeneratorExit):
        # Клиент ушёл: сохраняем то, что успели получить; стоимость дотянет фоновая задача.
        await _persist_quietly(state, _interrupted(state, factory, turn, parts, done, gen_id, latency()))
        raise

    text = "".join(parts)
    if disconnected:
        await _persist_quietly(state, _interrupted(state, factory, turn, parts, done, gen_id, latency()))
        return

    if failure is None and done is None:
        failure = LLMError(None, MSG_MODEL_ERROR, "stream ended without usage", gen_id)

    if failure is not None:
        logger.warning("chat stream failed: status=%s", failure.status)
        await _persist_quietly(
            state,
            _finish(state, factory, turn, text=text, done=None, status="error",
                    latency_ms=latency(), generation_id=gen_id,
                    error=(failure.detail or failure.user_message)[:MAX_ERROR_TEXT]),
        )
        if state.llm_errors.failure():
            await _alert(state, ERROR_SERIES_ALERT)
        yield sse("error", {"message": failure.user_message, "retry": True})
        return

    try:
        message_id, after = await shielded(
            state,
            _finish(state, factory, turn, text=text, done=done, status="ok", latency_ms=latency()),
        )
    except Exception:
        logger.error("chat reply not persisted")  # traceback логирует spawn()
        yield sse("error", {"message": MSG_SAVE_FAILED, "retry": True})
        return
    state.llm_errors.success()
    if turn.first_reply:
        spawn(state, generate_title(state, llm, factory, turn))
    yield sse(
        "done",
        {
            "model": done.model or turn.model,
            "prompt_tokens": done.prompt_tokens,
            "completion_tokens": done.completion_tokens,
            "cost_usd": None if done.cost_usd is None else f"{done.cost_usd:f}",
            "message_id": message_id,
            "spend": f"{after:.2f}",
            "limit": f"{turn.limit:.2f}",
        },
    )


# --- titles --------------------------------------------------------------------------------


def clean_title(raw: str) -> str:
    title = " ".join(clean_text(raw).replace("\r", " ").replace("\n", " ").split())
    title = title.strip("\"'«»„“”‘’` ").strip()
    return title[:TITLE_MAX_CHARS]


async def generate_title(state, llm: OpenRouterClient, factory: SessionFactory, turn: Turn) -> None:
    """Name the chat from its first user message; usage is recorded either way."""
    model = state.settings.title_model
    prompt = f"{TITLE_PROMPT}\n\n{turn.first_user_text[:TITLE_INPUT_CHARS]}"
    t0 = time.monotonic()
    title, done, error = "", None, None
    try:
        raw, done = await llm.complete(model, [{"role": "user", "content": prompt}], TITLE_MAX_TOKENS)
        title = clean_title(raw)
    except LLMError as exc:
        error = (exc.detail or exc.user_message)[:MAX_ERROR_TEXT]
    except Exception:
        logger.exception("title generation failed")
        error = "internal error"
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        async with factory() as s:
            user = await s.get(User, turn.user_id)
            chat = await s.get(Chat, turn.chat_id)
            if user is None:
                return
            if chat is not None and title and chat.title == DEFAULT_TITLE:
                chat.title = title
            before = await month_spend(s, user.id) if done is not None else Decimal(0)
            await record_usage(
                s,
                user=user,
                chat_id=chat.id if chat is not None else None,
                done=done,
                model=(done.model if done and done.model else model),
                latency_ms=latency_ms,
                status="ok" if done is not None else "error",
                error=error,
            )
            if done is not None and done.cost_usd:
                await notify_thresholds(
                    s, state.notifier, user, before, before + done.cost_usd, turn.ip
                )
            await s.commit()
    except Exception:
        logger.exception("failed to persist chat title")
