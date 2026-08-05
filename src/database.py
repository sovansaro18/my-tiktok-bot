from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from src.config import SUPABASE_URI

logger = logging.getLogger(__name__)


def _ipv4_dsn(uri: str) -> str:
    """Rewrite a postgres DSN so the host is an IPv4 address.

    Railway containers typically have IPv4-only routing, but DNS may return
    AAAA records first — causing ``[Errno 101] Network is unreachable``.
    Resolving the hostname to A records before connecting sidesteps that.
    """
    from urllib.parse import urlparse, urlunparse, quote_plus

    parsed = urlparse(uri)
    host = parsed.hostname
    if not host:
        return uri
    try:
        # Resolve to IPv4 only; fall back to original host on failure.
        resolved = socket.getaddrinfo(host, None, socket.AF_INET)
        if not resolved:
            return uri
        ipv4 = resolved[0][4][0]
    except (socket.gaierror, OSError):
        return uri

    # Rebuild netloc preserving user:pass@ and port.
    netloc = ipv4
    if parsed.username:
        creds = quote_plus(parsed.username)
        if parsed.password:
            creds += ":" + quote_plus(parsed.password)
        netloc = f"{creds}@{ipv4}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _default_user(user_id: int) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "status": "free",
        "is_active": True,
        "joined_date": datetime.now(timezone.utc),
        "daily_download_count": 0,
        "last_download_date": None,
    }


class BaseDatabase:
    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        raise NotImplementedError

    async def set_premium(self, user_id: int) -> bool:
        raise NotImplementedError

    async def count_users(self) -> Dict[str, int]:
        raise NotImplementedError

    async def list_users(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def set_user_active(self, user_id: int, active: bool) -> bool:
        raise NotImplementedError

    async def list_active_users(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def count_active_users(self) -> int:
        raise NotImplementedError

    async def total_downloads(self) -> int:
        raise NotImplementedError

    async def total_active_downloads(self) -> int:
        raise NotImplementedError

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SupabaseDatabase(BaseDatabase):
    """PostgreSQL (Supabase) data layer backed by asyncpg."""

    def __init__(self, uri: str):
        self._uri = uri
        self._pool: Optional[asyncpg.Pool] = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            dsn = _ipv4_dsn(self._uri)
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                    server_settings={"application_name": "telegram-bot"},
                )
                # Verify connectivity
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                logger.info("✅ Connected to Supabase (PostgreSQL)")
            except Exception as e:
                logger.critical(f"❌ Failed to connect to Supabase: {e}")
                raise
        return self._pool

    @staticmethod
    def _row_to_user(row: asyncpg.Record) -> Dict[str, Any]:
        joined = row.get("joined_date")
        last_dl = row.get("last_download_date")
        if joined is not None and joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        if last_dl is not None and last_dl.tzinfo is None:
            last_dl = last_dl.replace(tzinfo=timezone.utc)
        return {
            "user_id": row["user_id"],
            "status": row["status"],
            "is_active": row["is_active"],
            "joined_date": joined,
            "daily_download_count": row["daily_download_count"],
            "last_download_date": last_dl,
        }

    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, status, is_active, joined_date, "
                    "daily_download_count, last_download_date "
                    "FROM users WHERE user_id = $1",
                    user_id,
                )
                if row:
                    return self._row_to_user(row), False
                # Insert new user
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    user_id,
                )
                logger.info(f"🆕 New user created: {user_id}")
                return _default_user(user_id), True
        except Exception as e:
            logger.error(f"⚠️ Database error in get_user: {e}")
            return _default_user(user_id), False

    async def set_premium(self, user_id: int) -> bool:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE users SET status = 'premium' WHERE user_id = $1",
                    user_id,
                )
                return result.endswith("1") or "INSERT" in result
        except Exception as e:
            logger.error(f"⚠️ Failed to set premium for {user_id}: {e}")
            return False

    async def count_users(self) -> Dict[str, int]:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM users")
                premium = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE status = 'premium'"
                )
            return {
                "total": total or 0,
                "premium": premium or 0,
                "free": (total or 0) - (premium or 0),
            }
        except Exception as e:
            logger.error(f"⚠️ Failed to count users: {e}")
            return {"total": 0, "premium": 0, "free": 0}

    async def list_users(self) -> List[Dict[str, Any]]:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT user_id FROM users")
            return [{"user_id": r["user_id"]} for r in rows]
        except Exception as e:
            logger.error(f"⚠️ Failed to list users: {e}")
            return []

    async def set_user_active(self, user_id: int, active: bool) -> bool:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (user_id, is_active) VALUES ($1, $2) "
                    "ON CONFLICT (user_id) DO UPDATE SET is_active = $2",
                    user_id,
                    active,
                )
            return True
        except Exception as e:
            logger.error(f"⚠️ Failed to set active={active} for {user_id}: {e}")
            return False

    async def list_active_users(self) -> List[Dict[str, Any]]:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id FROM users WHERE is_active = true"
                )
            return [{"user_id": r["user_id"]} for r in rows]
        except Exception as e:
            logger.error(f"⚠️ Failed to list active users: {e}")
            return []

    async def count_active_users(self) -> int:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE is_active = true"
                )
            return count or 0
        except Exception as e:
            logger.error(f"⚠️ Failed to count active users: {e}")
            return 0

    async def total_downloads(self) -> int:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COALESCE(SUM(daily_download_count), 0) FROM users"
                )
            return int(total or 0)
        except Exception as e:
            logger.error(f"⚠️ Failed to aggregate total downloads: {e}")
            return 0

    async def total_active_downloads(self) -> int:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COALESCE(SUM(daily_download_count), 0) "
                    "FROM users WHERE is_active = true"
                )
            return int(total or 0)
        except Exception as e:
            logger.error(f"⚠️ Failed to aggregate active downloads: {e}")
            return 0

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        pool = await self._ensure_pool()
        now = datetime.now(timezone.utc)
        try:
            async with pool.acquire() as conn:
                # Upsert user if missing
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    user_id,
                )
                row = await conn.fetchrow(
                    "SELECT user_id, status, is_active, joined_date, "
                    "daily_download_count, last_download_date "
                    "FROM users WHERE user_id = $1",
                    user_id,
                )
                user = self._row_to_user(row)

                if user.get("status") == "premium":
                    return user

                last_dt = user.get("last_download_date")
                same_day = (
                    last_dt is not None
                    and last_dt.date() == now.date()
                )
                if same_day:
                    await conn.execute(
                        "UPDATE users SET daily_download_count = "
                        "daily_download_count + 1, last_download_date = $1 "
                        "WHERE user_id = $2",
                        now,
                        user_id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET daily_download_count = 1, "
                        "last_download_date = $1 WHERE user_id = $2",
                        now,
                        user_id,
                    )

                row = await conn.fetchrow(
                    "SELECT user_id, status, is_active, joined_date, "
                    "daily_download_count, last_download_date "
                    "FROM users WHERE user_id = $1",
                    user_id,
                )
                return self._row_to_user(row)
        except Exception as e:
            logger.error(f"⚠️ Failed to record download for {user_id}: {e}")
            fallback = _default_user(user_id)
            fallback["last_download_date"] = now
            fallback["daily_download_count"] = 1
            return fallback

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
                logger.info("✅ Closed Supabase connection pool")
            except Exception as e:
                logger.error(f"Error closing connection pool: {e}")
            finally:
                self._pool = None


class NullDatabase(BaseDatabase):
    """In-memory fallback used when SUPABASE_URI is not configured."""

    def __init__(self):
        self._users: Dict[int, Dict[str, Any]] = {}
        logger.warning(
            "⚠️ Supabase unavailable. Running with in-memory fallback "
            "(data resets on restart)."
        )

    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        if user_id in self._users:
            return self._users[user_id], False
        user = _default_user(user_id)
        self._users[user_id] = user
        return user, True

    async def set_premium(self, user_id: int) -> bool:
        user, _ = await self.get_user(user_id)
        user["status"] = "premium"
        return True

    async def count_users(self) -> Dict[str, int]:
        total = len(self._users)
        premium = sum(1 for u in self._users.values() if u.get("status") == "premium")
        return {"total": total, "premium": premium, "free": total - premium}

    async def list_users(self) -> List[Dict[str, Any]]:
        return [{"user_id": u["user_id"]} for u in self._users.values()]

    async def set_user_active(self, user_id: int, active: bool) -> bool:
        user, _ = await self.get_user(user_id)
        user["is_active"] = active
        return True

    async def list_active_users(self) -> List[Dict[str, Any]]:
        return [
            {"user_id": u["user_id"]}
            for u in self._users.values()
            if u.get("is_active", True)
        ]

    async def count_active_users(self) -> int:
        return sum(1 for u in self._users.values() if u.get("is_active", True))

    async def total_downloads(self) -> int:
        return int(sum(u.get("daily_download_count", 0) for u in self._users.values()))

    async def total_active_downloads(self) -> int:
        return int(
            sum(
                u.get("daily_download_count", 0)
                for u in self._users.values()
                if u.get("is_active") is not False
            )
        )

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        user, _ = await self.get_user(user_id)
        if user.get("status") == "premium":
            return user
        last_dt = user.get("last_download_date")
        if not last_dt or last_dt.date() != now.date():
            user["daily_download_count"] = 1
        else:
            user["daily_download_count"] = int(user.get("daily_download_count", 0)) + 1
        user["last_download_date"] = now
        return user

    async def close(self) -> None:
        return


db: BaseDatabase

if SUPABASE_URI:
    db = SupabaseDatabase(SUPABASE_URI)
else:
    db = NullDatabase()
