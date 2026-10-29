import json
import logging

import httpx
import pytest
import respx

from app.telegram.client import TelegramClient

TOKEN = "123456:SECRET-token-value"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def sleeper():
    return Sleeper()


@pytest.fixture
def client(http, sleeper):
    return TelegramClient(TOKEN, http, sleep=sleeper)


@respx.mock
async def test_send_message_ok(client):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await client.send_message(42, "hello") is True
    body = json.loads(route.calls.last.request.content)
    assert body == {"chat_id": 42, "text": "hello", "disable_web_page_preview": True}
    assert "parse_mode" not in body


@respx.mock
async def test_send_splits_long_text(client):
    text = "".join(f"{i:04d} " + "x" * 94 + "\n" for i in range(90))  # 9000 chars
    assert len(text) == 9000
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await client.send_message(1, text) is True
    parts = [json.loads(c.request.content)["text"] for c in route.calls]
    assert len(parts) == 3
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == text


@respx.mock
async def test_send_retries_once_on_5xx(client):
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"ok": True})]
    )
    assert await client.send_message(1, "x") is True
    assert route.call_count == 2


@respx.mock
async def test_send_honours_retry_after(client, sleeper):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.send_message(1, "x") is True
    assert sleeper.calls == [2]
    assert route.call_count == 2


@respx.mock
async def test_send_never_raises(client):
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    assert await client.send_message(1, "x") is False
    assert route.call_count == 2


@respx.mock
async def test_token_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await client.send_message(1, "x")
    respx.post(URL).mock(return_value=httpx.Response(500))
    await client.send_message(1, "x")
    respx.post(URL).mock(side_effect=httpx.ConnectError(f"cannot reach {URL}"))
    await client.send_message(1, "x")
    respx.post(URL).mock(
        return_value=httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
    )
    await client.send_message(1, "x")
    assert caplog.records
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


@respx.mock
async def test_send_splits_single_long_line(client):
    text = "y" * 9000
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await client.send_message(1, text) is True
    parts = [json.loads(c.request.content)["text"] for c in route.calls]
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    assert "".join(parts) == text


@respx.mock
async def test_blank_parts_are_skipped(client):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await client.send_message(1, "   " + chr(10) * 2) is True
    assert route.call_count == 0


@respx.mock
async def test_retry_after_is_capped(client, sleeper):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 500}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.send_message(1, "x") is True
    assert sleeper.calls == [30]


def test_client_does_not_lower_stricter_logger_level(http):
    lg = logging.getLogger("httpx")
    old = lg.level
    try:
        lg.setLevel(logging.ERROR)
        TelegramClient(TOKEN, http)
        assert lg.level == logging.ERROR
    finally:
        lg.setLevel(old)
