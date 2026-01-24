import time
import logging
from typing import Callable, Dict, Any, Awaitable, List
from aiogram import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Rate limit message cooldown to avoid spamming the user
RATE_LIMIT_MESSAGE_COOLDOWN = 30  # seconds


class RateLimitMiddleware(BaseMiddleware):
    """
    Rate limiting middleware to prevent spam/abuse.
    Tracks requests per user and limits them within a time window.
    """
    
    def __init__(self, limit: int = 3, window: int = 10):
        """
        Initialize rate limiter.
        
        Args:
            limit: Maximum number of requests allowed
            window: Time window in seconds
        """
        self.limit = limit
        self.window = window
        self.user_requests: Dict[int, List[float]] = {}
        self.last_rate_limit_message: Dict[int, float] = {}  # Track when we last sent rate limit message

    def _cleanup_old_entries(self, current_time: float) -> None:
        """
        Cleanup old entries to prevent memory leak.
        Remove users who haven't made requests in the last minute.
        """
        cleanup_threshold = current_time - 60  # 1 minute
        users_to_remove = [
            user_id for user_id, timestamps in self.user_requests.items()
            if not timestamps or max(timestamps) < cleanup_threshold
        ]
        for user_id in users_to_remove:
            del self.user_requests[user_id]
            if user_id in self.last_rate_limit_message:
                del self.last_rate_limit_message[user_id]

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        """Process incoming message and apply rate limiting."""
        
        # Skip rate limiting for non-user events
        if not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        current_time = time.time()

        # Periodic cleanup to prevent memory leak
        if len(self.user_requests) > 1000:
            self._cleanup_old_entries(current_time)

        # Initialize user's request list if not exists
        if user_id not in self.user_requests:
            self.user_requests[user_id] = []

        # Filter out old requests outside the window
        self.user_requests[user_id] = [
            t for t in self.user_requests[user_id] 
            if current_time - t < self.window
        ]

        # Check if rate limit exceeded
        if len(self.user_requests[user_id]) >= self.limit:
            logger.warning(f"🚫 Rate limit exceeded for user {user_id}")
            
            # Send rate limit message (with cooldown to avoid spam)
            last_message_time = self.last_rate_limit_message.get(user_id, 0)
            if current_time - last_message_time > RATE_LIMIT_MESSAGE_COOLDOWN:
                try:
                    wait_time = int(self.window - (current_time - min(self.user_requests[user_id])))
                    await event.answer(
                        f"⏳ <b>Slow down!</b>\n\n"
                        f"You're sending messages too fast.\n"
                        f"Please wait <b>{max(1, wait_time)} seconds</b> before trying again.",
                        parse_mode="HTML"
                    )
                    self.last_rate_limit_message[user_id] = current_time
                except Exception as e:
                    logger.error(f"Failed to send rate limit message: {e}")
            
            return  # Block the request

        # Record this request
        self.user_requests[user_id].append(current_time)

        # Process the handler with error handling
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"Error in handler: {e}")
            raise