import time
from collections import deque


class SlidingWindowLimiter:
    """In-memory sliding window; safe within a single asyncio event loop."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._denied: dict[str, float] = {}  # key -> time of the denial that was already reported
        self._last_sweep = time.monotonic()

    def _prune(self, q: deque[float], cutoff: float) -> None:
        while q and q[0] <= cutoff:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Once per window removes keys that have not been used for a long time."""
        if now - self._last_sweep < self.window_s:
            return
        self._last_sweep = now
        cutoff = now - self.window_s
        for key in list(self._hits):
            q = self._hits[key]
            self._prune(q, cutoff)
            if not q:
                del self._hits[key]
        for key in [k for k, t in self._denied.items() if t <= cutoff]:
            del self._denied[key]

    def first_denial(self, key: str) -> bool:
        """True for the first denial per key per window - so we do not write an audit record for each one."""
        now = time.monotonic()
        last = self._denied.get(key)
        if last is not None and now - last < self.window_s:
            return False
        self._denied[key] = now
        return True

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
