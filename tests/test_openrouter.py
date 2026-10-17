import json
import logging
from contextlib import aclosing
from decimal import Decimal

import httpx
import pytest
import respx

from app.llm.openrouter import LLMError, OpenRouterClient, StreamDelta, StreamDone

BASE = "https://or.test/api/v1"
MSGS = [{"role": "user", "content": "secret question"}]


class FakeNotifier:
    def __init__(self, fail=False):
        self.alerts: list[str] = []
        self.fail = fail

    async def alert(self, text: str) -> None:
        self.alerts.append(text)
        if self.fail:
            raise RuntimeError("boom")


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
async def make_client(settings, notifier):
    clients = []

    def _make(**overrides):
        s = settings.model_copy(update={"openrouter_base_url": BASE, **overrides})
        http = httpx.AsyncClient()
        clients.append(http)
        return OpenRouterClient(s, http, notifier)

    yield _make
    for c in clients:
        await c.aclose()


def sse(*chunks, done=True, comments=True) -> bytes:
    lines = []
    if comments:
        lines.append(": OPENROUTER PROCESSING\n\n")
    for c in chunks:
        lines.append(f"data: {json.dumps(c)}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def chunk(content=None, finish=None, usage=None, id="gen-abc123", model="anthropic/claude-x"):
    c = {"id": id, "model": model, "choices": [{"delta": {}, "finish_reason": finish}]}
    if content is not None:
        c["choices"][0]["delta"] = {"role": "assistant", "content": content}
    if usage:
        c["usage"] = usage
    return c


async def collect(client, model="a/b", messages=MSGS):
    return [e async for e in client.stream_chat(model, messages)]


USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17, "cost": 0.0031}


@respx.mock
async def test_stream_parses_deltas_and_usage(make_client):
    body = sse(
        chunk("Привет"),
        chunk(", мир"),
        chunk(None, finish="stop"),
        chunk(None, finish="stop", usage=USAGE),
    )
    route = respx.post(f"{BASE}/chat/completions").respond(200, content=body)
    events = await collect(make_client())
    assert events[:2] == [StreamDelta("Привет"), StreamDelta(", мир")]
    assert len(events) == 3
    assert events[2] == StreamDone(
        model="anthropic/claude-x",
        generation_id="gen-abc123",
        prompt_tokens=12,
        completion_tokens=5,
        cost_usd=Decimal("0.0031"),
        finish_reason="stop",
    )
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer test-openrouter-key"
    assert req.headers["x-title"] == "AI Notes"
    payload = json.loads(req.content)
    assert payload["stream"] is True
    assert payload["model"] == "a/b"
    assert "plugins" not in payload


@respx.mock
async def test_auto_router_plugin_added_when_configured(make_client):
    body = sse(chunk("x"), chunk(None, "stop", USAGE))
    route = respx.post(f"{BASE}/chat/completions").respond(200, content=body)
    allowed = ["anthropic/*", "openai/gpt-5.1"]
    await collect(make_client(auto_allowed_models=allowed), "openrouter/auto")
    payload = json.loads(route.calls.last.request.content)
    assert payload["plugins"] == [{"id": "auto-router", "allowed_models": allowed}]
    # not added for other models, nor when the list is empty
    await collect(make_client(auto_allowed_models=allowed), "a/b")
    assert "plugins" not in json.loads(route.calls.last.request.content)
    await collect(make_client(auto_allowed_models=[]), "openrouter/auto")
    assert "plugins" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_429_maps_to_user_message(make_client, notifier):
    respx.post(f"{BASE}/chat/completions").respond(
        429, json={"error": {"message": "rate limited by provider", "code": 429}}
    )
    with pytest.raises(LLMError) as ei:
        await collect(make_client())
    assert ei.value.status == 429
    assert ei.value.user_message == "Модель перегружена, попробуйте позже"
    assert ei.value.detail == "rate limited by provider"
    assert notifier.alerts == []


@respx.mock
@pytest.mark.parametrize("code", [401, 402, 403])
async def test_key_or_balance_errors_alert(make_client, notifier, code):
    respx.post(f"{BASE}/chat/completions").respond(code, json={"error": {"message": "no credits"}})
    with pytest.raises(LLMError) as ei:
        await collect(make_client())
    assert ei.value.user_message == "Сервис временно недоступен"
    assert notifier.alerts == [f"OpenRouter: ошибка ключа или баланса (HTTP {code})"]


@respx.mock
async def test_alert_failure_is_swallowed(settings, caplog):
    respx.post(f"{BASE}/chat/completions").respond(402, json={})
    s = settings.model_copy(update={"openrouter_base_url": BASE})
    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(s, http, FakeNotifier(fail=True))
        with caplog.at_level(logging.ERROR), pytest.raises(LLMError) as ei:
            await collect(client)
    assert ei.value.status == 402
    assert any("alert" in r.getMessage().lower() for r in caplog.records)


@respx.mock
async def test_5xx_and_timeout_map_to_generic(make_client):
    respx.post(f"{BASE}/chat/completions").respond(503, text="down")
    with pytest.raises(LLMError) as ei:
        await collect(make_client())
    assert ei.value.user_message == "Ошибка модели, попробуйте ещё раз"

    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(LLMError) as ei:
        await collect(make_client())
    assert ei.value.user_message == "Ошибка модели, попробуйте ещё раз"
    assert ei.value.status is None


@respx.mock
async def test_error_inside_stream(make_client):
    body = sse(
        chunk("часть"),
        {
            "id": "gen-1",
            "error": {"code": 502, "message": "upstream exploded"},
            "choices": [{"delta": {"content": ""}, "finish_reason": "error"}],
        },
    )
    respx.post(f"{BASE}/chat/completions").respond(200, content=body)
    client = make_client()
    got = []
    with pytest.raises(LLMError) as ei:
        async for ev in client.stream_chat("a/b", MSGS):
            got.append(ev)
    assert got == [StreamDelta("часть")]
    assert ei.value.user_message == "Ошибка модели, попробуйте ещё раз"
    assert ei.value.detail == "upstream exploded"
    assert "upstream exploded" not in ei.value.user_message


@respx.mock
async def test_truncated_stream_raises(make_client):
    respx.post(f"{BASE}/chat/completions").respond(200, content=sse(chunk("x"), done=False))
    with pytest.raises(LLMError):
        await collect(make_client())


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for p in self.parts:
            yield p

    async def aclose(self):
        self.closed = True


@respx.mock
async def test_early_exit_closes_stream(make_client):
    stream = TrackedStream(
        [f"data: {json.dumps(chunk(t))}\n\n".encode() for t in ("a", "b", "c", "d")]
    )
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, stream=stream))
    client = make_client()
    async with aclosing(client.stream_chat("a/b", MSGS)) as gen:
        async for ev in gen:
            assert ev == StreamDelta("a")
            break
    assert stream.closed


@respx.mock
async def test_errors_do_not_leak_secrets(make_client):
    respx.post(f"{BASE}/chat/completions").respond(500, text="oops")
    with pytest.raises(LLMError) as ei:
        await collect(make_client())
    text = repr(ei.value) + str(ei.value) + ei.value.user_message + ei.value.detail
    assert "test-openrouter-key" not in text
    assert "secret question" not in text


@respx.mock
async def test_complete(make_client):
    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        json={
            "id": "gen-9",
            "model": "m/x",
            "choices": [
                {"message": {"role": "assistant", "content": "Заголовок"}, "finish_reason": "stop"}
            ],
            "usage": USAGE,
        },
    )
    text, done = await make_client().complete("m/x", MSGS, max_tokens=20)
    assert text == "Заголовок"
    assert done.cost_usd == Decimal("0.0031")
    assert done.generation_id == "gen-9"
    payload = json.loads(route.calls.last.request.content)
    assert payload["max_tokens"] == 20
    assert not payload.get("stream")


@respx.mock
async def test_generation_cost(make_client):
    route = respx.get(f"{BASE}/generation").respond(200, json={"data": {"total_cost": 0.0042}})
    assert await make_client().get_generation_cost("gen-1") == Decimal("0.0042")
    assert route.calls.last.request.url.params["id"] == "gen-1"
    respx.get(f"{BASE}/generation").respond(404, json={})
    assert await make_client().get_generation_cost("gen-2") is None


@respx.mock
async def test_get_model_info(make_client):
    respx.get(f"{BASE}/models").respond(
        200,
        json={
            "data": [
                {"id": "a/text", "name": "Text", "architecture": {"input_modalities": ["text"]}},
                {
                    "id": "a/vis",
                    "name": "Vision",
                    "architecture": {"input_modalities": ["text", "image"]},
                },
            ]
        },
    )
    client = make_client()
    assert await client.get_model_info("a/vis") == {"name": "Vision", "supports_images": True}
    assert await client.get_model_info("a/text") == {"name": "Text", "supports_images": False}
    assert await client.get_model_info("nope/x") is None
