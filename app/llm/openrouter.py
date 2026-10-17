"""OpenRouter client: SSE streaming chat, non-streaming completion, cost/model lookups.

Never logs or raises with the API key, request bodies or message contents.
"""

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.config import Settings
from app.notify.base import Notifier

logger = logging.getLogger("openrouter")

TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
MSG_UNAVAILABLE = "Сервис временно недоступен"
MSG_OVERLOADED = "Модель перегружена, попробуйте позже"
MSG_MODEL_ERROR = "Ошибка модели, попробуйте ещё раз"
ALERT_INTERVAL_S = 30 * 60


@dataclass
class StreamDelta:
    text: str
    generation_id: str | None = None


@dataclass
class StreamDone:
    model: str
    generation_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None  # None = unknown (not reported); 0 = explicitly free
    finish_reason: str


class LLMError(Exception):
    def __init__(
        self,
        status: int | None,
        user_message: str,
        detail: str = "",
        generation_id: str | None = None,
    ):
        super().__init__(user_message)
        self.status = status
        self.user_message = user_message
        self.detail = detail  # provider message only; for logs
        self.generation_id = generation_id


def _provider_message(data) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")
        if isinstance(err, str):
            return err
    return ""


def _parse_cost(usage: dict) -> Decimal | None:
    raw = usage.get("cost")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cost = Decimal(str(raw))
    except InvalidOperation:
        logger.warning("openrouter: non-numeric usage.cost")
        return None
    if not cost.is_finite():
        logger.warning("openrouter: non-finite usage.cost")
        return None
    return cost


def _usage_done(chunk_state: dict, usage: dict | None) -> StreamDone:
    usage = usage or {}
    return StreamDone(
        model=chunk_state.get("model", ""),
        generation_id=chunk_state.get("id", ""),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost_usd=_parse_cost(usage),
        finish_reason=chunk_state.get("finish_reason") or "",
    )


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        notifier: Notifier,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._last_alert: float | None = None
        self._settings = settings
        self._http = http
        self._notifier = notifier

    @property
    def _base(self) -> str:
        return self._settings.openrouter_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.openrouter_api_key}",
            "X-Title": "AI Notes",
        }

    def _payload(self, model: str, messages: list[dict], **extra) -> dict:
        payload = {"model": model, "messages": messages, **extra}
        if model == "openrouter/auto" and self._settings.auto_allowed_models:
            payload["plugins"] = [
                {"id": "auto-router", "allowed_models": list(self._settings.auto_allowed_models)}
            ]
        return payload

    async def _alert(self, text: str) -> None:
        try:
            await self._notifier.alert(text)
        except Exception:
            logger.exception("failed to send alert")

    async def _key_alert(self, code: int) -> None:
        now = self._clock()
        if self._last_alert is not None and now - self._last_alert < ALERT_INTERVAL_S:
            return
        self._last_alert = now
        await self._alert(f"OpenRouter: ошибка ключа или баланса (HTTP {code})")

    async def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            await resp.aread()
            detail = _provider_message(resp.json())
        except Exception:
            detail = ""
        code = resp.status_code
        if code in (401, 402, 403):
            await self._key_alert(code)
            msg = MSG_UNAVAILABLE
        elif code == 429:
            msg = MSG_OVERLOADED
        else:
            msg = MSG_MODEL_ERROR
        logger.warning("openrouter http %s: %s", code, detail)
        raise LLMError(code, msg, detail)

    async def stream_chat(
        self, model: str, messages: list[dict]
    ) -> AsyncIterator[StreamDelta | StreamDone]:
        payload = self._payload(model, messages, stream=True)
        state: dict = {}
        usage: dict | None = None
        finished = False
        try:
            async with self._http.stream(
                "POST",
                f"{self._base}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=TIMEOUT,
            ) as resp:
                await self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        logger.warning("openrouter: unparsable SSE chunk")
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        detail = _provider_message(chunk)
                        logger.warning("openrouter mid-stream error: %s", detail)
                        code = chunk["error"].get("code") if isinstance(chunk["error"], dict) else None
                        code = code if isinstance(code, int) else None
                        msg = MSG_MODEL_ERROR
                        if code in (401, 402, 403):
                            await self._key_alert(code)
                            msg = MSG_UNAVAILABLE
                        elif code == 429:
                            msg = MSG_OVERLOADED
                        raise LLMError(code, msg, detail, chunk.get("id") or state.get("id"))
                    if chunk.get("id"):
                        state["id"] = chunk["id"]
                    if chunk.get("model"):
                        state["model"] = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            state["finish_reason"] = choice["finish_reason"]
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield StreamDelta(text, state.get("id"))
        except httpx.HTTPError as exc:
            logger.warning("openrouter transport error: %s", type(exc).__name__)
            raise LLMError(None, MSG_MODEL_ERROR, type(exc).__name__) from exc
        if not finished:
            raise LLMError(
                None, MSG_MODEL_ERROR, "stream ended without [DONE]", state.get("id")
            )
        yield _usage_done(state, usage)

    async def complete(
        self, model: str, messages: list[dict], max_tokens: int
    ) -> tuple[str, StreamDone]:
        payload = self._payload(model, messages, max_tokens=max_tokens)
        try:
            resp = await self._http.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=TIMEOUT,
            )
            await self._raise_for_status(resp)
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("openrouter transport error: %s", type(exc).__name__)
            raise LLMError(None, MSG_MODEL_ERROR, type(exc).__name__) from exc
        except ValueError as exc:
            raise LLMError(None, MSG_MODEL_ERROR, "invalid JSON") from exc
        if data.get("error"):
            raise LLMError(None, MSG_MODEL_ERROR, _provider_message(data))
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        state = {
            "id": data.get("id", ""),
            "model": data.get("model", model),
            "finish_reason": choices[0].get("finish_reason"),
        }
        return text, _usage_done(state, data.get("usage"))

    async def get_generation_cost(self, generation_id: str) -> Decimal | None:
        try:
            resp = await self._http.get(
                f"{self._base}/generation",
                params={"id": generation_id},
                headers=self._headers(),
                timeout=TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            await self._raise_for_status(resp)
            cost = (resp.json().get("data") or {}).get("total_cost")
        except httpx.HTTPError as exc:
            raise LLMError(None, MSG_MODEL_ERROR, type(exc).__name__) from exc
        return None if cost is None else Decimal(str(cost))

    async def get_model_info(self, model_id: str) -> dict | None:
        try:
            resp = await self._http.get(
                f"{self._base}/models", headers=self._headers(), timeout=TIMEOUT
            )
            await self._raise_for_status(resp)
            models = resp.json().get("data") or []
        except httpx.HTTPError as exc:
            raise LLMError(None, MSG_MODEL_ERROR, type(exc).__name__) from exc
        for m in models:
            if m.get("id") == model_id:
                modalities = (m.get("architecture") or {}).get("input_modalities") or []
                return {"name": m.get("name", model_id), "supports_images": "image" in modalities}
        return None
