import time
from collections import deque


class SlidingWindowLimiter:
    """In-memory sliding window; safe within a single asyncio event loop."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        cutoff = now - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
