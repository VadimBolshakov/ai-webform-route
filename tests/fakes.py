"""Test doubles shared by chat tests."""

import asyncio
from collections import deque
from decimal import Decimal

from app.llm.openrouter import LLMError, StreamDelta, StreamDone

HANG = object()  # in a stream script: hang (wait for cancellation)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def alert(self, text: str) -> None:
        self.sent.append(text)


def done(model="vendor/model-a", gen="gen-1", cost="0.0123", prompt=10, completion=5) -> StreamDone:
    return StreamDone(
        model=model,
        generation_id=gen,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=None if cost is None else Decimal(cost),
        finish_reason="stop",
    )


def ok_script(*texts: str, **kw) -> list:
    return [*(StreamDelta(t, kw.get("gen", "gen-1")) for t in texts), done(**kw)]


class FakeLLM:
    """Scriptable stand-in for OpenRouterClient.

    Each stream_chat call consumes the next script: a list of StreamDelta / StreamDone /
    LLMError (raised) / HANG (block until cancelled).
    """

    def __init__(self, *scripts: list, title: str = "Заголовок чата", title_error: LLMError | None = None):
        self.scripts = deque(scripts)
        self.calls: list[tuple[str, list[dict]]] = []
        self.complete_calls: list[tuple[str, list[dict], int]] = []
        self.closed = 0
        self.hanging = asyncio.Event()
        self.title = title
        self.title_error = title_error

    async def stream_chat(self, model: str, messages: list[dict]):
        self.calls.append((model, messages))
        script = self.scripts.popleft() if self.scripts else ok_script("ok")
        try:
            for item in script:
                if item is HANG:
                    self.hanging.set()
                    await asyncio.Event().wait()
                if isinstance(item, Exception):
                    raise item
                await asyncio.sleep(0)
                yield item
        finally:
            self.closed += 1

    async def complete(self, model: str, messages: list[dict], max_tokens: int):
        self.complete_calls.append((model, messages, max_tokens))
        if self.title_error is not None:
            raise self.title_error
        return self.title, done(model=model, gen="gen-title", cost="0.0001", prompt=30, completion=4)


async def drain_background(app) -> None:
    """Wait for fire-and-forget tasks (persistence after disconnect, title generation)."""
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks), return_exceptions=True)
