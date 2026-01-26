from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple, List

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import MONGO_URI

logger = logging.getLogger(__name__)


class BaseDatabase:
    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        raise NotImplementedError

    async def set_premium(self, user_id: int) -> bool:
        raise NotImplementedError

    async def count_users(self) -> Dict[str, int]:
        raise NotImplementedError

    async def list_users(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def total_downloads(self) -> int:
        raise NotImplementedError

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def _default_user(user_id: int) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "status": "free",
        "joined_date": datetime.now(timezone.utc),
        "daily_download_count": 0,
        "last_download_date": None,
    }


class MongoDatabase(BaseDatabase):
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client["downloader_bot"]
        self.users = self.db["users"]
        logger.info("✅ Connected to MongoDB")

    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        try:
            user = await self.users.find_one({"user_id": user_id})
            if user:
                return user, False
            new_user = _default_user(user_id)
            await self.users.insert_one(new_user)
            logger.info(f"🆕 New user created: {user_id}")
            return new_user, True
        except PyMongoError as e:
            logger.error(f"⚠️ Database error in get_user: {e}")
            return _default_user(user_id), False

    async def set_premium(self, user_id: int) -> bool:
        try:
            result = await self.users.update_one(
                {"user_id": user_id},
                {"$set": {"status": "premium"}},
                upsert=True,
            )
            return bool(getattr(result, "modified_count", 0) or getattr(result, "upserted_id", None))
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to set premium for {user_id}: {e}")
            return False

    async def count_users(self) -> Dict[str, int]:
        try:
            total_users = await self.users.count_documents({})
            premium_users = await self.users.count_documents({"status": "premium"})
            return {
                "total": total_users,
                "premium": premium_users,
                "free": total_users - premium_users,
            }
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to count users: {e}")
            return {"total": 0, "premium": 0, "free": 0}

    async def list_users(self) -> List[Dict[str, Any]]:
        try:
            return await self.users.find({}, {"_id": 0, "user_id": 1}).to_list(length=None)
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to list users: {e}")
            return []

    async def total_downloads(self) -> int:
        try:
            pipeline = [{"$group": {"_id": None, "total": {"$sum": "$daily_download_count"}}}]
            result = await self.users.aggregate(pipeline).to_list(length=1)
            return int(result[0].get("total", 0)) if result else 0
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to aggregate total downloads: {e}")
            return 0

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            user, _ = await self.get_user(user_id)
            if user.get("status") == "premium":
                return user

            last_dt = user.get("last_download_date")
            if not last_dt or getattr(last_dt, "date", lambda: None)() != now.date():
                await self.users.update_one(
                    {"user_id": user_id},
                    {"$set": {"last_download_date": now, "daily_download_count": 1}},
                    upsert=True,
                )
            else:
                await self.users.update_one(
                    {"user_id": user_id},
                    {"$inc": {"daily_download_count": 1}, "$set": {"last_download_date": now}},
                    upsert=True,
                )
            updated, _ = await self.get_user(user_id)
            return updated
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to record download for {user_id}: {e}")
            fallback = _default_user(user_id)
            fallback["last_download_date"] = now
            fallback["daily_download_count"] = 1
            return fallback

    async def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            return


class NullDatabase(BaseDatabase):
    def __init__(self):
        self._users: Dict[int, Dict[str, Any]] = {}
        logger.warning("⚠️ MongoDB unavailable. Running with in-memory fallback (limits may reset on restart).")

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

    async def total_downloads(self) -> int:
        return int(sum(u.get("daily_download_count", 0) for u in self._users.values()))

    async def record_download(self, user_id: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        user, _ = await self.get_user(user_id)
        if user.get("status") == "premium":
            return user
        last_dt = user.get("last_download_date")
        if not last_dt or getattr(last_dt, "date", lambda: None)() != now.date():
            user["daily_download_count"] = 1
        else:
            user["daily_download_count"] = int(user.get("daily_download_count", 0)) + 1
        user["last_download_date"] = now
        return user

    async def close(self) -> None:
        return


if MONGO_URI:
    try:
        db: BaseDatabase = MongoDatabase(MONGO_URI)
    except Exception as e:
        logger.critical(f"❌ Failed to connect to MongoDB: {e}")
        db = NullDatabase()
else:
    db = NullDatabase()