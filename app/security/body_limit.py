"""Request body cap and multipart ban, applied before any route parses a body.

Starlette's form parser spools multipart files >1 MB to disk and reads urlencoded bodies
without a limit — and FastAPI `Form()` params run it before the handler's own checks
(login token, session). So every request body is read here first, capped at
MAX_BODY_BYTES, and multipart is refused, except on the chat messages endpoint, which
parses its multipart body itself, in memory, with its own limits.

Rejections on private paths (/a/*, /app*) are the neutral 404 page — no route-existence
leak; elsewhere 413 / 415.
"""

import re
from collections.abc import Callable

from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.security.headers import is_private

MAX_BODY_BYTES = 64 * 1024
_SELF_LIMITED = re.compile(r"/app/chats/\d+/messages")


def _exempt(scope: Scope) -> bool:
    return scope["method"] == "POST" and _SELF_LIMITED.fullmatch(scope["path"]) is not None


def _content_type(headers: dict[bytes, bytes]) -> str:
    return headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0].strip().lower()


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, not_found: Callable[[Scope], Response]) -> None:
        self.app = app
        self.not_found = not_found

    async def _reject(self, scope: Scope, receive: Receive, send: Send, status: int) -> None:
        if is_private(scope["path"]):
            response = self.not_found(scope)
        elif status == 413:
            response = PlainTextResponse("Request Entity Too Large", status_code=413)
        else:
            response = PlainTextResponse("Unsupported Media Type", status_code=415)
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Несколько Content-Type: middleware и Starlette могли бы прочитать разные значения.
        if sum(1 for k, _ in scope["headers"] if k.lower() == b"content-type") > 1:
            await self._reject(scope, receive, send, 415)
            return
        if _exempt(scope):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        if _content_type(headers) == "multipart/form-data":
            await self._reject(scope, receive, send, 415)
            return
        declared = headers.get(b"content-length", b"").decode("latin-1")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await self._reject(scope, receive, send, 413)
            return

        # Тело читается целиком (не больше лимита) и отдаётся приложению одним сообщением.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # клиент отключился, не дослав тело — отвечать некому
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                await self._reject(scope, receive, send, 413)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
