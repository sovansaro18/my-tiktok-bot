import time
import logging
from typing import Callable, Dict, Any, Awaitable, List
from aiogram import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)

from src.config import RATE_LIMIT_MESSAGE_COOLDOWN


class RateLimitMiddleware(BaseMiddleware):
    """
    Rate limiting middleware to prevent spam/abuse.
    
    ✅ FIX 3.3: Periodic cleanup now runs every request (cheap check),
    not just when dict exceeds 1000 entries — prevents memory growth
    on long-running bots.
    """

    # ✅ Cleanup interval: flush stale entries every 5 minutes
    CLEANUP_INTERVAL_SECONDS = 300

    def __init__(self, limit: int = 3, window: int = 10):
        """
        Initialize rate limiter.

        Args:
            limit: Max requests allowed within the window
            window: Time window in seconds
        """
        self.limit = limit
        self.window = window
        self.user_requests: Dict[int, List[float]] = {}
        self.last_rate_limit_message: Dict[int, float] = {}
        # Track when we last ran a full cleanup sweep
        self._last_cleanup_time: float = time.time()

    def _cleanup_old_entries(self, current_time: float) -> None:
        """
        Remove users who have been idle for > 1 minute.
        Called periodically to prevent unbounded memory growth.
        """
        cutoff = current_time - 60  # entries older than 1 minute are stale
        stale_users = [
            uid
            for uid, timestamps in self.user_requests.items()
            if not timestamps or max(timestamps) < cutoff
        ]
        for uid in stale_users:
            del self.user_requests[uid]
            self.last_rate_limit_message.pop(uid, None)

        if stale_users:
            logger.debug(
                f"🧹 Rate limiter: cleaned {len(stale_users)} stale entries"
            )

    def _maybe_run_cleanup(self, current_time: float) -> None:
        """
        ✅ FIX 3.3: Trigger cleanup every CLEANUP_INTERVAL_SECONDS,
        regardless of dict size. Prevents unbounded memory growth.
        """
        if current_time - self._last_cleanup_time >= self.CLEANUP_INTERVAL_SECONDS:
            self._cleanup_old_entries(current_time)
            self._last_cleanup_time = current_time

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        """Process incoming message and apply rate limiting."""

        # Skip for system events without a real user
        if not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        current_time = time.time()

        # ✅ Periodic cleanup — O(n) but runs only every 5 minutes
        self._maybe_run_cleanup(current_time)

        # Initialize request list for new users
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []

        # Slide the window: discard timestamps older than `window` seconds
        self.user_requests[user_id] = [
            t
            for t in self.user_requests[user_id]
            if current_time - t < self.window
        ]

        # Enforce rate limit
        if len(self.user_requests[user_id]) >= self.limit:
            logger.warning(f"🚫 Rate limit exceeded for user {user_id}")

            last_msg_time = self.last_rate_limit_message.get(user_id, 0)
            if current_time - last_msg_time > RATE_LIMIT_MESSAGE_COOLDOWN:
                try:
                    # Calculate remaining wait time
                    oldest = min(self.user_requests[user_id])
                    wait_time = max(1, int(self.window - (current_time - oldest)))

                    await event.answer(
                        f"⏳ <b>សូមបន្តិច...</b>\n\n"
                        f"អ្នកកំពុងផ្ញើសារលឿនពេក។\n"
                        f"សូមរង់ចាំ <b>{wait_time} វិនាទី</b> "
                        f"មុនព្យាយាមម្តងទៀត។",
                        parse_mode="HTML",
                    )
                    self.last_rate_limit_message[user_id] = current_time
                except Exception as e:
                    logger.error(f"Failed to send rate limit message: {e}")

            return  # Block this request

        # Record current request timestamp
        self.user_requests[user_id].append(current_time)

        # Delegate to actual handler
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"Error in handler for user {user_id}: {e}")
            raise