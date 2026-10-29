import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

ALERTS_PER_WINDOW = 20
WINDOW_S = 3600


class _Sender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> bool: ...


class TelegramNotifier:
    """Owner notifier: alerts are rate-limited (20/hour, extras dropped and counted)."""

    def __init__(
        self, client: _Sender, chat_id: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._clock = clock
        self._sent: deque[float] = deque()
        self._dropped = 0

    def _prune(self) -> None:
        cutoff = self._clock() - WINDOW_S
        while self._sent and self._sent[0] <= cutoff:
            self._sent.popleft()

    async def alert(self, text: str) -> None:
        await self.flush_dropped()
        self._prune()
        if len(self._sent) >= ALERTS_PER_WINDOW:
            self._dropped += 1
            return
        self._sent.append(self._clock())
        await self._client.send_message(self._chat_id, text)

    async def send(self, text: str) -> None:
        """No rate limit: digests and command replies."""
        await self._client.send_message(self._chat_id, text)

    async def flush_dropped(self) -> None:
        """Report dropped alerts once the window has room again."""
        self._prune()
        if self._dropped and len(self._sent) < ALERTS_PER_WINDOW:
            n, self._dropped = self._dropped, 0
            self._sent.append(self._clock())
            await self._client.send_message(self._chat_id, f"Пропущено уведомлений: {n}")
