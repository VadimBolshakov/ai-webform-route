import pytest
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from app.security.csrf import require_csrf

EXPECTED = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "strict-transport-security": "max-age=31536000",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), geolocation=(), microphone=()",
}


async def test_decoy_page(client):
    r = await client.get("/")
    assert r.status_code == 200
    low = r.text.replace("/a/test-login-token", "").lower()
    assert "войти" not in low and "login" not in low
    assert 'href="/a/test-login-token"' in r.text
    assert "style=" not in low and "<script" not in low
    for k, v in EXPECTED.items():
        assert r.headers[k] == v


async def test_app_paths_404_without_session(client):
    ref = await client.get("/nonexistent")
    assert ref.status_code == 404
    for p in ("/app", "/app/chats/1", "/app/admin"):
        r = await client.get(p)
        assert r.status_code == 404
        assert r.content == ref.content


async def test_http_exception_404_uses_shared_page(app, client):
    @app.get("/boom")
    async def boom():
        raise HTTPException(status_code=404)

    ref = await client.get("/nonexistent")
    r = await client.get("/boom")
    assert r.status_code == 404 and r.content == ref.content


async def test_post_without_origin_forbidden(app, settings):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=settings.base_url) as c:
        r = await c.post("/app/x")
        assert r.status_code == 403
        r = await c.post("/app/x", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/app/x", headers={"Referer": "https://localhost:8443/foo"})
        assert r.status_code == 404  # origin ok, route absent
        r = await c.get("/app/x")
        assert r.status_code == 404


async def test_private_paths_noindex(client):
    for p in ("/a/whatever", "/app", "/app/zzz"):
        r = await client.get(p)
        assert r.status_code == 404
        assert r.headers["x-robots-tag"] == "noindex, nofollow"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-frame-options"] == "DENY"
    r = await client.get("/")
    assert "x-robots-tag" not in r.headers


async def test_private_prefix_is_exact(client):
    for p in ("/application", "/apple", "/a", "/about"):
        r = await client.get(p)
        assert "x-robots-tag" not in r.headers
    for p in ("/app", "/app/", "/a/x"):
        assert (await client.get(p)).headers["x-robots-tag"] == "noindex, nofollow"


async def test_robots_and_static(client):
    r = await client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /a/" in r.text and "Disallow: /app/" in r.text
    assert (await client.get("/static/app.css")).status_code == 200


class _Sess:
    csrf_token = "tok123"


class _Req:
    def __init__(self, headers=None, form=None):
        self.headers = Headers(headers or {})
        self._form = form or {}

    async def form(self):
        return self._form


async def test_require_csrf():
    await require_csrf(_Req({"X-CSRF-Token": "tok123"}), _Sess())
    await require_csrf(_Req(form={"csrf_token": "tok123"}), _Sess())
    for req in (_Req({"X-CSRF-Token": "bad"}), _Req(form={"csrf_token": "bad"}), _Req()):
        with pytest.raises(HTTPException) as e:
            await require_csrf(req, _Sess())
        assert e.value.status_code == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",  # http only for localhost
        "https://example.com/app",  # no path
        "https://example.com?x=1",
        "https://example.com#f",
        "ftp://example.com",
        "example.com",
        "https://",
        "https://example.com:99999",  # port out of range
        "https://example.com:abc",
        "https://user:pw@example.com",  # no userinfo
        "https://user@example.com",
    ],
)
def test_base_url_rejected(settings, url):
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "base_url": url}, _env_file=None)


@pytest.mark.parametrize(
    "url",
    ["https://example.com", "https://example.com/", "http://localhost:8000", "http://127.0.0.1:8000"],
)
def test_base_url_accepted(settings, url):
    from app.config import Settings

    assert Settings(**{**settings.model_dump(), "base_url": url}, _env_file=None).base_url


def test_origin_default_ports_normalised():
    from app.security.headers import _origin

    assert _origin("https://Host.example:443") == _origin("https://host.example")
    assert _origin("http://localhost:80/x") == "http://localhost"
    assert _origin("https://host.example:8443") == "https://host.example:8443"
    assert _origin("http://host.example:443") == "http://host.example:443"


async def test_origin_with_explicit_default_port_allowed(settings):
    from app.config import Settings
    from app.main import create_app

    s = Settings(**{**settings.model_dump(), "base_url": "https://chat.example"}, _env_file=None)
    application = create_app(s)
    async with AsyncClient(transport=ASGITransport(app=application), base_url=s.base_url) as c:
        r = await c.post("/healthz", headers={"Origin": "https://chat.example:443"})
        assert r.status_code == 404  # passed the Origin check; 405 is masked as 404
        r = await c.post("/healthz", headers={"Origin": "https://chat.example:444"})
        assert r.status_code == 403
    await application.state.engine.dispose()


def test_title_model_default_and_not_empty(settings, monkeypatch):
    from pydantic import ValidationError

    from app.config import Settings

    base = {k: v for k, v in settings.model_dump().items() if k != "title_model"}
    with pytest.raises(ValidationError):
        Settings(**base, title_model="", _env_file=None)
    monkeypatch.setenv("TITLE_MODEL", "")  # empty string in .env -> default value
    assert Settings(**base, _env_file=None).title_model == "openai/gpt-4o-mini"


def test_origin_check_denies_all_when_allowed_unparseable():
    from app.security.headers import OriginCheckMiddleware

    mw = OriginCheckMiddleware(app=None, base_url="https://example.com:abc")
    assert mw.allowed is None
    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    scope = {"type": "http", "method": "POST", "headers": [(b"origin", b"https://x:abc")]}
    asyncio.run(mw(scope, None, send))
    assert sent[0]["status"] == 403


async def test_decoy_answers_head_and_hides_framework(client):
    r = await client.head("/")
    assert r.status_code == 200 and r.content == b""
    ref = (await client.get("/nonexistent")).content
    for method in ("post", "put", "delete"):
        r = await client.request(method.upper(), "/", headers={"Origin": "https://localhost:8443"})
        assert r.status_code == 404 and r.content == ref
    assert "server" not in {k.lower() for k in (await client.get("/")).headers}
