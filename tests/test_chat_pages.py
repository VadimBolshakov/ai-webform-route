import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.models import AllowedModel, Chat, Message, Usage
from tests.test_auth import make_user
from tests.test_chat import make_chat, signed_in

STATIC = Path(__file__).parent.parent / "app" / "web" / "static"

XSS_IMG = "<img src=x onerror=alert(1)>"
XSS_SCRIPT = "<script>alert(1)</script>"


def sidebar_ids(html: str) -> list[int]:
    m = re.search(r'<nav id="chat-list".*?</nav>', html, re.S)
    assert m, "sidebar not found"
    return [int(x) for x in re.findall(r'data-chat-id="(\d+)"', m.group(0))]


def listed_titles(html: str) -> list[str]:
    return re.findall(r'<a class="result-link"[^>]*>([^<]*)</a>', html)


async def chats_with_times(db, user, n: int) -> list[Chat]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    chats = []
    for i in range(n):
        c = Chat(user_id=user.id, title=f"Чат {i}", updated_at=base + timedelta(hours=i))
        db.add(c)
        chats.append(c)
    await db.commit()
    return chats


async def test_chat_page_lists_last_10_chats(client, db):
    u, _ = await signed_in(client, db)
    chats = await chats_with_times(db, u, 12)
    chats[0].pinned = True  # the oldest, but pinned
    await db.commit()
    r = await client.get(f"/app/chats/{chats[5].id}")
    assert r.status_code == 200
    ids = sidebar_ids(r.text)
    newest = [c.id for c in reversed(chats[1:])][:9]
    assert ids == [chats[0].id, *newest]
    assert 'href="/app/chats/all"' in r.text


async def test_sidebar_only_own_chats(client, db):
    u, _ = await signed_in(client, db)
    other = await make_user(db, "bob")
    mine = await make_chat(db, u, "мой")
    await make_chat(db, other, "чужой")
    r = await client.get(f"/app/chats/{mine.id}")
    assert sidebar_ids(r.text) == [mine.id]
    assert "чужой" not in r.text


async def test_chat_page_escapes_message_html(client, db):
    u, _ = await signed_in(client, db)
    chat = await make_chat(db, u, XSS_SCRIPT)
    db.add_all(
        [
            Message(chat_id=chat.id, role="user", content=XSS_IMG,
                    attachments=[{"name": XSS_IMG, "kind": "image"}]),
            Message(chat_id=chat.id, role="assistant", content=XSS_IMG + XSS_SCRIPT,
                    model=XSS_SCRIPT),
        ]
    )
    await db.commit()
    r = await client.get(f"/app/chats/{chat.id}")
    assert r.status_code == 200
    html = r.text
    assert XSS_IMG not in html and XSS_SCRIPT not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # Messages are in <template class="md">, the text is escaped.
    assert '<template class="md">&lt;img src=x onerror=alert(1)&gt;</template>' in html
    # Strict CSP: only external scripts, no inline styles or on* attributes.
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag
    assert re.search(r"<script\b[^>]*>\s*[^<\s]", html) is None  # no inline code
    for tag in re.findall(r"<[a-zA-Z][^>]*>", html):  # real tags only
        assert re.search(r"\s(style|on[a-z]+)=", tag) is None, tag
    assert "javascript:" not in html


async def test_chat_page_shell(client, db):
    u, csrf = await signed_in(client, db)
    u.theme = "dark"
    await db.commit()
    chat = await make_chat(db, u, "Мой чат")
    db.add(Usage(user_id=u.id, user_label=u.username, model="m", status="ok",
                 cost_usd=Decimal("1.2")))
    await db.commit()
    r = await client.get(f"/app/chats/{chat.id}")
    html = r.text
    assert f'hx-headers=\'{{"X-CSRF-Token": "{csrf}"}}\'' in html
    assert '<html lang="ru" data-theme="dark">' in html
    assert '"includeIndicatorStyles": false' in html and '"allowEval": false' in html
    assert f'data-chat-id-current="{chat.id}"' in html
    assert "Расход: $1.20 / $5.00" in html
    assert 'href="/app/settings"' in html
    assert 'href="/app/admin"' not in html
    # Order: libraries, then chat.js.
    scripts = re.findall(r'<script src="([^"]+)"></script>', html)
    assert scripts == [
        "/static/vendor/htmx.min.js",
        "/static/vendor/marked.umd.js",
        "/static/vendor/purify.min.js",
        "/static/vendor/highlight.min.js",
        "/static/chat.js",
    ]
    # Model answers are outside htmx processing (data-hx-*/hx-* in Markdown must not fire).
    assert re.search(r'<div id="messages"[^>]*\shx-disable[\s>]', html)
    assert r.headers["cache-control"] == "no-store"


async def test_admin_sees_admin_link(client, db):
    # For an admin without 2FA everything is redirected to /app/settings/2fa - we enable the flag.
    u, _ = await signed_in(client, db, "boss")
    u.role, u.totp_enabled = "admin", True
    await db.commit()
    chat = await make_chat(db, u)
    r = await client.get(f"/app/chats/{chat.id}")
    assert 'href="/app/admin"' in r.text


async def test_foreign_chat_page_404(client, db):
    await signed_in(client, db)
    other = await make_user(db, "bob")
    foreign = await make_chat(db, other)
    ref = await client.get("/nonexistent")
    for path in (f"/app/chats/{foreign.id}", "/app/chats/999999"):
        r = await client.get(path)
        assert r.status_code == 404 and r.content == ref.content


async def test_app_opens_last_chat(client, db):
    u, _ = await signed_in(client, db)
    chats = await chats_with_times(db, u, 3)
    r = await client.get("/app")
    assert r.status_code == 303 and r.headers["location"] == f"/app/chats/{chats[-1].id}"


async def test_app_empty_state(client, db):
    await signed_in(client, db)
    r = await client.get("/app")
    assert r.status_code == 200
    assert "Новый чат" in r.text and 'data-chat-id-current=""' in r.text
    assert 'id="composer"' in r.text


async def test_create_chat_htmx_redirect(client, db):
    u, csrf = await signed_in(client, db)
    r = await client.post("/app/chats", headers={"X-CSRF-Token": csrf, "HX-Request": "true"})
    chat = (await db.execute(select(Chat))).scalar_one()
    assert r.status_code == 204 and r.headers["hx-redirect"] == f"/app/chats/{chat.id}"


async def test_model_selector_lists_enabled_only(client, db):
    u, _ = await signed_in(client, db)
    db.add_all(
        [
            AllowedModel(model_id="vendor/on", display_name="Включённая", enabled=True),
            AllowedModel(model_id="vendor/fav", display_name="Любимая", enabled=True),
            AllowedModel(model_id="vendor/off", display_name="Выключенная", enabled=False),
        ]
    )
    u.default_model = "vendor/fav"
    await db.commit()
    chat = await make_chat(db, u)
    html = (await client.get(f"/app/chats/{chat.id}")).text
    select_html = re.search(r'<select name="model".*?</select>', html, re.S).group(0)
    values = re.findall(r'<option value="([^"]*)"', select_html)
    assert values == ["openrouter/auto", "vendor/on", "vendor/fav"]  # by name
    assert '<option value="vendor/fav" selected>' in select_html
    assert "vendor/off" not in html


async def test_model_selector_falls_back_to_auto(client, db):
    u, _ = await signed_in(client, db)
    db.add(AllowedModel(model_id="vendor/off", display_name="X", enabled=False))
    u.default_model = "vendor/off"
    await db.commit()
    chat = await make_chat(db, u)
    html = (await client.get(f"/app/chats/{chat.id}")).text
    assert '<option value="openrouter/auto" selected>' in html


async def test_rename_pin_delete(client, db):
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, "Старое")
    db.add(Message(chat_id=chat.id, role="user", content="x"))
    await db.commit()
    h = {"X-CSRF-Token": csrf, "HX-Request": "true"}

    r = await client.post(f"/app/chats/{chat.id}/rename", data={"title": "  Новое\x00 имя  "}, headers=h)
    assert r.status_code == 200
    assert sidebar_ids(r.text) == [chat.id] and "Новое имя" in r.text
    await db.refresh(chat)
    assert chat.title == "Новое имя"

    r = await client.post(f"/app/chats/{chat.id}/rename", data={"title": "я" * 300}, headers=h)
    await db.refresh(chat)
    assert r.status_code == 200 and chat.title == "я" * 200

    r = await client.post(f"/app/chats/{chat.id}/rename", data={"title": "   "}, headers=h)
    await db.refresh(chat)
    assert r.status_code == 400 and chat.title == "я" * 200

    r = await client.post(f"/app/chats/{chat.id}/pin", headers=h)
    await db.refresh(chat)
    assert r.status_code == 200 and chat.pinned is True and "Открепить" in r.text
    r = await client.post(f"/app/chats/{chat.id}/pin", headers=h)
    await db.refresh(chat)
    assert chat.pinned is False

    r = await client.post(f"/app/chats/{chat.id}/delete", headers=h)
    assert r.status_code == 200 and sidebar_ids(r.text) == []
    assert (await db.execute(select(Chat))).scalars().all() == []
    assert (await db.execute(select(Message))).scalars().all() == []


async def test_delete_current_chat_redirects(client, db):
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    keep = await make_chat(db, u)
    h = {"X-CSRF-Token": csrf, "HX-Request": "true",
         "HX-Current-URL": f"https://localhost:8443/app/chats/{chat.id}"}
    r = await client.post(f"/app/chats/{chat.id}/delete", headers=h)
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/app"
    h["HX-Current-URL"] = f"https://localhost:8443/app/chats/{keep.id}"
    r = await client.post(f"/app/chats/{keep.id}/pin", headers=h)
    assert r.status_code == 200 and 'class="chat-item current' in r.text


async def test_actions_from_all_chats_page_refresh(client, db):
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u)
    h = {"X-CSRF-Token": csrf, "HX-Request": "true",
         "HX-Current-URL": "https://localhost:8443/app/chats/all?q=x"}
    r = await client.post(f"/app/chats/{chat.id}/pin", headers=h)
    assert r.status_code == 204 and r.headers["hx-refresh"] == "true"


async def test_chat_actions_foreign_404_and_csrf(client, db):
    u, csrf = await signed_in(client, db)
    other = await make_user(db, "bob")
    foreign = await make_chat(db, other, "чужой")
    mine = await make_chat(db, u, "мой")
    h = {"X-CSRF-Token": csrf}
    for action in ("rename", "pin", "delete"):
        r = await client.post(f"/app/chats/{foreign.id}/{action}", data={"title": "x"}, headers=h)
        assert r.status_code == 404
        r = await client.post(f"/app/chats/{mine.id}/{action}", data={"title": "x"})
        assert r.status_code == 403
        r = await client.post(
            f"/app/chats/{mine.id}/{action}", data={"title": "x", "csrf_token": csrf}
        )
        assert r.status_code == 403  # header only
    await db.refresh(foreign)
    await db.refresh(mine)
    assert foreign.title == "чужой" and not foreign.pinned
    assert mine.title == "мой" and not mine.pinned


async def test_rename_rejects_non_urlencoded_and_large_body(client, db):
    u, csrf = await signed_in(client, db)
    chat = await make_chat(db, u, "мой")
    h = {"X-CSRF-Token": csrf}
    r = await client.post(f"/app/chats/{chat.id}/rename", files={"title": (None, "x")}, headers=h)
    assert r.status_code == 404  # multipart is cut off by BodyLimitMiddleware (neutral 404)
    r = await client.post(
        f"/app/chats/{chat.id}/rename", data={"title": "x" * 20_000}, headers=h
    )
    assert r.status_code == 413
    await db.refresh(chat)
    assert chat.title == "мой"


async def test_sidebar_fragment(client, db):
    u, _ = await signed_in(client, db)
    chat = await make_chat(db, u, "Фрагмент")
    r = await client.get(f"/app/chats/sidebar?current={chat.id}")
    assert r.status_code == 200 and sidebar_ids(r.text) == [chat.id]
    assert "<html" not in r.text and 'class="chat-item current' in r.text


async def test_all_chats_search(client, db):
    u, _ = await signed_in(client, db)
    other = await make_user(db, "bob")
    await make_chat(db, u, "Python asyncio")
    await make_chat(db, u, "Рецепт борща")
    await make_chat(db, u, "заметки PYTHON")
    await make_chat(db, u, "100% готово")
    await make_chat(db, other, "python чужой")
    r = await client.get("/app/chats/all", params={"q": "python"})
    assert r.status_code == 200
    assert sorted(listed_titles(r.text)) == ["Python asyncio", "заметки PYTHON"]
    r = await client.get("/app/chats/all", params={"q": "%"})
    assert listed_titles(r.text) == ["100% готово"]  # % and _ are searched literally
    r = await client.get("/app/chats/all", params={"q": "_"})
    assert listed_titles(r.text) == []
    r = await client.get("/app/chats/all", params={"q": "<b>"})
    assert "&lt;b&gt;" in r.text and "<b>" not in r.text
    r = await client.get("/app/chats/all")
    assert len(listed_titles(r.text)) == 4


async def test_all_chats_pagination(client, db):
    u, _ = await signed_in(client, db)
    chats = await chats_with_times(db, u, 55)
    r = await client.get("/app/chats/all")
    titles = listed_titles(r.text)
    assert len(titles) == 50 and titles[0] == "Чат 54"
    assert 'href="/app/chats/all?page=2"' in r.text
    r = await client.get("/app/chats/all", params={"page": 2})
    assert listed_titles(r.text) == [f"Чат {i}" for i in range(4, -1, -1)]
    assert "page=3" not in r.text
    r = await client.get("/app/chats/all", params={"page": 2, "q": "Чат"})
    assert 'href="/app/chats/all?page=1&amp;q=%D0%A7%D0%B0%D1%82"' in r.text
    for bad in ("0", "-1", "abc", "99999999999999999999"):
        assert (await client.get("/app/chats/all", params={"page": bad})).status_code == 404
    assert len(chats) == 55


async def test_pages_require_session(client):
    for p in ("/app", "/app/chats/all", "/app/chats/sidebar", "/app/chats/1"):
        assert (await client.get(p)).status_code == 404


def test_chat_js_sanitizer_options():
    """Pin the DOMPurify setup: model HTML must not carry htmx attributes, ids or spoofed classes."""
    js = (STATIC / "chat.js").read_text(encoding="utf-8")
    for needle in (
        "ALLOW_DATA_ATTR: false",
        "SANITIZE_NAMED_PROPS: true",
        'FORBID_ATTR: ["style", "id", "name"]',
        '"uponSanitizeAttribute"',
        'startsWith("hx-")',
        'startsWith("data-hx-")',
        "/^(language-|hljs)/",
        "ignoreUnescapedHTML: true",
    ):
        assert needle in js, needle
    assert "innerHTML = DOMPurify.sanitize(" in js
    # The only innerHTML assignment is the DOMPurify result.
    assert js.count("innerHTML") == 1


def test_vendor_versions_listed():
    versions = (STATIC / "vendor" / "VERSIONS.txt").read_text(encoding="utf-8")
    for name in ("htmx.min.js", "marked.umd.js", "purify.min.js", "highlight.min.js",
                 "hljs-light.css", "hljs-dark.css"):
        assert (STATIC / "vendor" / name).is_file() and name in versions
    assert "marked.min.js" not in versions
