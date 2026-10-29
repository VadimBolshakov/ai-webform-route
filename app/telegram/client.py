import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_S = 5.0
MAX_PART_CHARS = 4000
RETRY_AFTER_CAP_S = 30.0


class TelegramError(Exception):
    """Carries only a short reason (status code / exception type); never the URL (token in path)."""


def split_text(text: str, limit: int = MAX_PART_CHARS) -> list[str]:
    """Split on line boundaries into parts of at most `limit` chars (long lines are cut hard)."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


class TelegramClient:
    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Request URLs contain the token: make sure httpx never logs them. Only raise the level
        # (never lower an operator's stricter setting). tests/test_telegram_client.py relies on this.
        for name in ("httpx", "httpcore"):
            lg = logging.getLogger(name)
            if lg.level < logging.WARNING:  # NOTSET (0) counts as below
                lg.setLevel(logging.WARNING)
        self._base = f"{API_BASE}/bot{token}/"
        self._http = http
        self._sleep = sleep

    async def send_message(self, chat_id: int, text: str) -> bool:
        """True if every part was accepted. Never raises."""
        ok = True
        for part in split_text(text):
            payload = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if not await self._send_part(payload):
                ok = False
        return ok

    async def _send_part(self, payload: dict) -> bool:
        retried_network = False
        retried_429 = False
        while True:
            try:
                resp = await self._http.post(
                    self._base + "sendMessage", json=payload, timeout=SEND_TIMEOUT_S
                )
            except Exception as e:  # network errors, timeouts: never let them reach callers
                if retried_network:
                    logger.warning("telegram: send failed (%s)", type(e).__name__)
                    return False
                retried_network = True
                continue
            if resp.status_code == 200:
                return True
            if resp.status_code == 429 and not retried_429:
                retried_429 = True
                await self._sleep(self._retry_after(resp))
                continue
            if resp.status_code >= 500 and not retried_network:
                retried_network = True
                continue
            logger.warning("telegram: send failed (HTTP %s)", resp.status_code)
            return False

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            value = float(resp.json()["parameters"]["retry_after"])
        except Exception:
            value = 1.0
        return max(0.0, min(value, RETRY_AFTER_CAP_S))

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        """Long-poll for updates. Raises TelegramError on any failure."""
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            resp = await self._http.post(
                self._base + "getUpdates", json=payload, timeout=timeout + 10
            )
        except Exception as e:
            raise TelegramError(type(e).__name__) from None
        if resp.status_code != 200:
            raise TelegramError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            if not data.get("ok"):
                raise ValueError
            return list(data["result"])
        except Exception:
            raise TelegramError("bad response") from None
