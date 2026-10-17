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
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
]
PRIVATE = [
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"cache-control", b"no-store"),
]


def is_private(path: str) -> bool:
    return path == "/app" or path.startswith("/app/") or path.startswith("/a/")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = COMMON + (PRIVATE if is_private(scope["path"]) else [])
        names = {k for k, _ in extra}

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": headers + extra}
            await send(message)

        await self.app(scope, receive, send_wrapped)


DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(value: str) -> str | None:
    """scheme://host[:port] в нижнем регистре; порт по умолчанию для схемы опускается."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname  # urlsplit уже приводит к нижнему регистру
    if ":" in host:
        host = f"[{host}]"
    if port is None or DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


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
