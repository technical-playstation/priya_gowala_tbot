"""
utils/rate_limiter.py - In-memory sliding-window rate limiter.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from config import settings
from logger import get_logger

log = get_logger("rate_limiter")


class RateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(
        self,
        max_calls: int = settings.RATE_LIMIT_MESSAGES,
        window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS,
    ):
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: dict[int, deque] = defaultdict(deque)
        self._lock = Lock()

    def is_allowed(self, chat_id: int) -> bool:
        """Return True if the user is within the rate limit."""
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            dq = self._calls[chat_id]
            # Evict old timestamps
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_calls:
                log.warning("Rate limit hit: chat_id=%s  calls=%d", chat_id, len(dq))
                return False
            dq.append(now)
            return True

    def reset(self, chat_id: int) -> None:
        with self._lock:
            self._calls.pop(chat_id, None)


# Global instance
rate_limiter = RateLimiter()
