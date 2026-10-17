import httpx
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
    "referrer-policy": "no-referrer",
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
