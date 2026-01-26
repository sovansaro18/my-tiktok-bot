from __future__ import annotations


class BotError(Exception):
    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message or "❌ មានបញ្ហាមួយ។ សូមព្យាយាមម្តងទៀត។"


class InvalidUrlError(BotError):
    pass


class UnsupportedPlatformError(BotError):
    pass


class RateLimitedError(BotError):
    pass


class DbUnavailableError(BotError):
    pass