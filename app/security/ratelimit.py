import time
from collections import deque


class SlidingWindowLimiter:
    """In-memory sliding window; safe within a single asyncio event loop."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def _prune(self, q: deque[float], cutoff: float) -> None:
        while q and q[0] <= cutoff:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Раз в окно удаляет ключи, которые давно не использовались."""
        if now - self._last_sweep < self.window_s:
            return
        self._last_sweep = now
        cutoff = now - self.window_s
        for key in list(self._hits):
            q = self._hits[key]
            self._prune(q, cutoff)
            if not q:
                del self._hits[key]

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        self._sweep(now)
        q = self._hits.get(key)
        if q is not None:
            self._prune(q, now - self.window_s)
            if not q:
                del self._hits[key]
                q = None
        if q is None:
            q = self._hits.setdefault(key, deque())
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
