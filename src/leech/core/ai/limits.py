from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    calls: int
    per: float = 60.0
    _hits: deque = field(default_factory=deque)

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._hits and now - self._hits[0] >= self.per:
                self._hits.popleft()
            if len(self._hits) < self.calls:
                self._hits.append(now)
                return
            await asyncio.sleep(self.per - (now - self._hits[0]))

    def used(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._hits if now - t < self.per)


@dataclass
class DailyCounter:
    limit: int
    count: int = 0
    day: str = ""
    _cooldown_until: float = 0.0

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))

    def record(self) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.count = today, 0
        self.count += 1

    def remaining(self) -> int:
        return (
            self.limit if self._today() != self.day else max(0, self.limit - self.count)
        )

    def trip(self, seconds: float) -> None:
        self._cooldown_until = time.monotonic() + seconds

    def cooling(self) -> bool:
        return time.monotonic() < self._cooldown_until
