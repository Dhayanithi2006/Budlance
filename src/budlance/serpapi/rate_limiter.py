"""In-process asynchronous rate limiter for SerpApi calls."""

import asyncio
import time
from collections import deque


class AsyncRateLimiter:
    """Sliding-window rate limiter with concurrency throttling."""

    def __init__(
        self,
        max_calls_per_minute: int = 30,
        max_concurrent: int = 5,
    ) -> None:
        self.max_calls_per_minute = max_calls_per_minute
        self.window_seconds = 60.0
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def acquire(self) -> None:
        """Wait until a call slot is available within the rate window."""
        await self._semaphore.acquire()
        async with self._lock:
            now = time.monotonic()

            # Evict timestamps older than the sliding window
            while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.max_calls_per_minute:
                # Sleep until the oldest call expires from the window
                sleep_needed = self.window_seconds - (now - self._timestamps[0])
                if sleep_needed > 0:
                    await asyncio.sleep(sleep_needed)
                    now = time.monotonic()
                    while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
                        self._timestamps.popleft()

            self._timestamps.append(time.monotonic())

    def release(self) -> None:
        """Release a concurrency slot."""
        self._semaphore.release()
