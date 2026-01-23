import time
import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)

class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 3, window: int = 10):

        self.limit = limit
        self.window = window
        self.user_requests: Dict[int, list[float]] = {}

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:

        if not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        current_time = time.time()

        if user_id not in self.user_requests:
            self.user_requests[user_id] = []

        self.user_requests[user_id] = [
            t for t in self.user_requests[user_id] 
            if current_time - t < self.window
        ]

        if len(self.user_requests[user_id]) >= self.limit:
            logger.warning(f"🚫 Rate limit exceeded for user {user_id}")
            return 

        self.user_requests[user_id].append(current_time)

        return await handler(event, data)