"""Request body cap + multipart ban (BodyLimitMiddleware)."""

import pytest
from starlette.requests import Request

from tests.fakes import drain_background, ok_script
from tests.test_chat import events, make_chat, send, signed_in, use_llm

FIVE_MB = b"a" * (5 * 1024 * 1024)


@pytest.fixture
def form_spy(monkeypatch):
    calls = []
    real = Request.form

    def spy(self, *a, **kw):
        calls.append(self.url.path)
        return real(self, *a, **kw)

    monkeypatch.setattr(Request, "form", spy)
    return calls


@pytest.fixture
async def ref404(client):
    return (await client.get("/nonexistent")).content


async def test_big_multipart_to_login_is_neutral_404_unparsed(client, form_spy, ref404):
    r = await client.post("/a/WRONG", files={"f": ("x.bin", FIVE_MB)})
    assert r.status_code == 404 and r.content == ref404
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert form_spy == []


async def test_big_urlencoded_to_login_is_neutral_404_unparsed(client, form_spy, ref404):
    r = await client.post("/a/test-login-token", data={"username": "u", "password": "p" * 70_000})
    assert r.status_code == 404 and r.content == ref404
    assert form_spy == []


async def test_big_body_without_content_length_counted(client, form_spy, ref404):
    async def chunks():
        for _ in range(10):
            yield b"x" * 10_000

    r = await client.post(
        "/a/test-login-token",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 404 and r.content == ref404
    assert form_spy == []


async def test_small_multipart_rejected_everywhere_but_messages(client, db, form_spy, ref404):
    _, csrf = await signed_in(client, db)
    form_spy.clear()  # the login itself (urlencoded) parses the form
    r = await client.post(
        "/app/settings/preferences", files={"csrf_token": (None, csrf), "theme": (None, "dark")}
    )
    assert r.status_code == 404 and r.content == ref404
    r = await client.post("/a/test-login-token", files={"username": (None, "alice")})
    assert r.status_code == 404 and r.content == ref404
    assert form_spy == []


async def test_public_paths_get_413_and_415(client):
    r = await client.post("/healthz", content=b"x" * (64 * 1024 + 1))
    assert r.status_code == 413
    r = await client.post("/healthz", files={"a": (None, "b")})
    assert r.status_code == 415


async def test_large_body_to_settings_rejected(client, db, ref404):
    u, csrf = await signed_in(client, db)
    r = await client.post(
        "/app/settings/preferences",
        data={"csrf_token": csrf, "system_prompt": "x" * 70_000, "theme": "dark"},
    )
    assert r.status_code == 404 and r.content == ref404
    await db.refresh(u)
    assert u.theme == "system"


async def test_body_at_cap_still_accepted(client, db):
    u, csrf = await signed_in(client, db)
    pad = "x" * (60 * 1024)
    r = await client.post(
        "/app/settings/preferences",
        data={"csrf_token": csrf, "answer_language": "ru", "theme": "dark",
              "system_prompt": "", "default_model": "openrouter/auto", "pad": pad},
    )
    assert r.status_code == 303
    await db.refresh(u)
    assert u.theme == "dark"


async def test_messages_endpoint_accepts_large_multipart(app, client, db, monkeypatch):
    llm = use_llm(app, ok_script("ok"))
    monkeypatch.setattr(app.state.settings, "context_token_budget", 10_000_000)
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, title="t")
    big = b"word " * (200 * 1024)  # 1 MB of text: more than 64 KB, less than the attachment limit
    r = await send(client, chat.id, csrf, text="hi", files=[("files", ("big.txt", big))])
    assert r.status_code == 200
    assert "error" not in [n for n, _ in events(r.text)]
    assert len(llm.calls) == 1
    await drain_background(app)


async def test_duplicate_content_type_rejected(client, form_spy, ref404):
    # The first header (read by Starlette) is multipart, the last one is harmless.
    dup = [
        ("content-type", "multipart/form-data; boundary=x"),
        ("content-type", "text/plain"),
    ]
    r = await client.post("/a/test-login-token", content=b"--x--\r\n", headers=dup)
    assert r.status_code == 404 and r.content == ref404
    r = await client.post("/healthz", content=b"--x--\r\n", headers=dup)
    assert r.status_code == 415
    assert form_spy == []
