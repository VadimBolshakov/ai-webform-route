import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger("telegram")
# Same logger and level as LogNotifier: every alert stays in the server log even when Telegram
# is down, the token is revoked or the alert is dropped by the rate limit.
alert_logger = logging.getLogger("notify")

ALERTS_PER_WINDOW = 20
WINDOW_S = 3600
MAX_IN_FLIGHT = 5
SEND_TIMEOUT_S = 15


class _Sender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> bool: ...


class TelegramNotifier:
    """Owner notifier: alerts are rate-limited (20/hour, extras dropped and counted) and
    fire-and-forget, so a slow Telegram never blocks the request that raised the alert."""

    def __init__(
        self, client: _Sender, chat_id: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._clock = clock
        self._sent: deque[float] = deque()
        self._dropped = 0
        self._tasks: set[asyncio.Task] = set()

    def _prune(self) -> None:
        cutoff = self._clock() - WINDOW_S
        while self._sent and self._sent[0] <= cutoff:
            self._sent.popleft()

    def _take_dropped(self) -> int:
        """Claim the dropped count if the window has room (the summary uses one slot)."""
        self._prune()
        if self._dropped and len(self._sent) < ALERTS_PER_WINDOW:
            n, self._dropped = self._dropped, 0
            self._sent.append(self._clock())
            return n
        return 0

    async def _send_summary(self, n: int) -> None:
        ok = False
        try:
            ok = await self._client.send_message(self._chat_id, f"Пропущено уведомлений: {n}")
        finally:
            if not ok:
                self._dropped += n

    async def alert(self, text: str) -> None:
        """Rate-limit accounting is synchronous; the send runs in a background task."""
        alert_logger.warning(text)
        if len(self._tasks) >= MAX_IN_FLIGHT:
            self._dropped += 1
            return
        summary = self._take_dropped()
        self._prune()
        if len(self._sent) >= ALERTS_PER_WINDOW:
            self._dropped += 1
            alert_text = None
        else:
            self._sent.append(self._clock())
            alert_text = text
        if not summary and alert_text is None:
            return
        task = asyncio.create_task(self._deliver(summary, alert_text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, summary: int, text: str | None) -> None:
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                if summary:
                    await self._send_summary(summary)
                if text is not None:
                    await self._client.send_message(self._chat_id, text)
        except Exception as e:  # never surface to anyone; type only (no URL/token)
            logger.warning("telegram: alert delivery failed (%s)", type(e).__name__)

    async def drain(self) -> None:
        """Wait for in-flight alerts (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def send(self, text: str) -> None:
        """No rate limit: digests and command replies."""
        await self._client.send_message(self._chat_id, text)

    async def flush_dropped(self) -> None:
        """Report dropped alerts once the window has room again (also called by the scheduler)."""
        n = self._take_dropped()
        if n:
            await self._send_summary(n)
