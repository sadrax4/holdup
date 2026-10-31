"""Sliding-window rate limits for chat actions and web console requests."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    warn: bool = False  # first refusal since the key was last allowed: worth telling the person once


class RateLimiter:
    """At most ``limit`` actions per ``window_s`` seconds for each key.

    Refused actions do not count against the window. Keys that have been
    quiet for a whole window are forgotten, and at most ``max_keys`` are
    tracked (least recently used first out), so memory stays bounded.
    """

    def __init__(
        self,
        limit: int,
        window_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._refused: dict[str, int] = {}

    def hit(self, key: str) -> Decision:
        """Count one action for ``key`` if it is within the limit."""
        now = self.clock()
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        self._hits[key] = hits
        if len(hits) >= self.limit:
            refused = self._refused.get(key, 0) + 1
            self._refused[key] = refused
            return Decision(allowed=False, warn=refused == 1)
        hits.append(now)
        self._refused.pop(key, None)
        self._evict()
        return Decision(allowed=True)

    def wait(self, key: str) -> float:
        """Seconds until ``key`` may act again; 0 when it may now. Counts nothing."""
        hits = self._hits.get(key)
        if not hits:
            return 0.0
        now = self.clock()
        recent = [t for t in hits if t > now - self.window_s]
        if len(recent) < self.limit:
            return 0.0
        return max(0.0, recent[0] + self.window_s - now)

    def _evict(self) -> None:
        while len(self._hits) > self.max_keys:
            key, _ = self._hits.popitem(last=False)
            self._refused.pop(key, None)
