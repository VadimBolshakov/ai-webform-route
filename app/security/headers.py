from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
    "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

COMMON = [
    (b"content-security-policy", CSP.encode()),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
]
PRIVATE = [
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"cache-control", b"no-store"),
]


def _is_private(path: str) -> bool:
    return path.startswith("/a/") or path.startswith("/app")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = COMMON + (PRIVATE if _is_private(scope["path"]) else [])
        names = {k for k, _ in extra}

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": headers + extra}
            await send(message)

        await self.app(scope, receive, send_wrapped)


def _origin(value: str) -> str | None:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


class OriginCheckMiddleware:
    SAFE = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app: ASGIApp, base_url: str) -> None:
        self.app = app
        self.allowed = _origin(base_url)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in self.SAFE:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        raw = headers.get(b"origin") or headers.get(b"referer")
        if raw is None or _origin(raw.decode("latin-1")) != self.allowed:
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", b"9"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.app(scope, receive, send)
