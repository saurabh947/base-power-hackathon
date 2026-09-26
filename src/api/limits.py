"""In-process rate limits (docs/API.md). One API process serves the one shared fleet,
so global limits live in memory.

- /run/start and /run/reset: one call per 5 s each (global).
- /availability: 10 calls per second (global).
- Wrong presenter passcodes: 5 per minute per IP; after that the IP is blocked until
  the window frees up, even with the right passcode.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from typing import Callable


def retry_after(seconds: float) -> int:
    return max(1, int(math.ceil(seconds)))


class SlidingWindow:
    """At most `limit` events per `window_s` seconds, per key."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self.clock = clock
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._events[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str = "") -> float | None:
        """Seconds until the next event is allowed, or None if one is allowed now."""
        with self._lock:
            now = self.clock()
            q = self._prune(key, now)
            if len(q) >= self.limit:
                return q[0] + self.window_s - now
            return None

    def record(self, key: str = "") -> None:
        with self._lock:
            self._events[key].append(self.clock())

    def hit(self, key: str = "") -> float | None:
        """Check and record in one step: None if allowed (and counted), else seconds to wait."""
        with self._lock:
            now = self.clock()
            q = self._prune(key, now)
            if len(q) >= self.limit:
                return q[0] + self.window_s - now
            q.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class Limits:
    def __init__(self, cfg: dict | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        cfg = cfg or {}
        self.run_start = SlidingWindow(1, float(cfg.get("run_start_s", 5)), clock)
        self.run_reset = SlidingWindow(1, float(cfg.get("run_reset_s", 5)), clock)
        self.availability = SlidingWindow(int(cfg.get("availability_per_s", 10)), 1.0, clock)
        self.wrong_passcode = SlidingWindow(int(cfg.get("wrong_passcode_per_min", 5)), 60.0, clock)

    def reset(self) -> None:
        for lim in (self.run_start, self.run_reset, self.availability, self.wrong_passcode):
            lim.reset()
