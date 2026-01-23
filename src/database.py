import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError
from src.config import MONGO_URI

# កំណត់ការ Log សម្រាប់ Database
logger = logging.getLogger(__name__)

class Database:
    def __init__(self, uri: str):
        """
        Initialize MongoDB connection with Motor (Async).
        Motor handles connection pooling automatically.
        """
        try:
            self.client = AsyncIOMotorClient(uri)
            self.db = self.client['downloader_bot']
            self.users = self.db['users']
            logger.info("✅ Connected to MongoDB successfully.")
        except Exception as e:
            logger.critical(f"❌ Failed to connect to MongoDB: {e}")
            raise e

    async def get_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        """
        Get user data. If user doesn't exist, create a new one.
        Returns: (user_data, is_new_user)
        """
        try:
            user = await self.users.find_one({"user_id": user_id})
            
            if user:
                return user, False
            
            # Create new user structure
            new_user = {
                "user_id": user_id,
                "status": "free",
                "downloads_count": 0,
                "joined_date": datetime.now(timezone.utc)
            }
            await self.users.insert_one(new_user)
            logger.info(f"🆕 New user created: {user_id}")
            return new_user, True

        except PyMongoError as e:
            logger.error(f"⚠️ Database error in get_user: {e}")
            # Return a safe default to prevent bot crash
            return {"user_id": user_id, "status": "free", "downloads_count": 10}, False

    async def increment_download(self, user_id: int) -> bool:
        """
        Increment download count for a user.
        Returns: True if successful.
        """
        try:
            await self.users.update_one(
                {"user_id": user_id},
                {"$inc": {"downloads_count": 1}}
            )
            return True
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to increment download for {user_id}: {e}")
            return False

    async def set_premium(self, user_id: int) -> bool:
        """
        Upgrade a user to premium status.
        """
        try:
            result = await self.users.update_one(
                {"user_id": user_id},
                {"$set": {"status": "premium"}}
            )
            if result.modified_count > 0:
                logger.info(f"💎 User {user_id} upgraded to PREMIUM.")
                return True
            return False
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to set premium for {user_id}: {e}")
            return False

    async def count_users(self) -> Dict[str, int]:
        """
        Get statistics about users.
        """
        try:
            total_users = await self.users.count_documents({})
            premium_users = await self.users.count_documents({"status": "premium"})
            return {
                "total": total_users,
                "premium": premium_users,
                "free": total_users - premium_users
            }
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to count users: {e}")
            return {"total": 0, "premium": 0, "free": 0}

    async def close(self):
        """Close the database connection."""
        self.client.close()
        logger.info("🔒 MongoDB connection closed.")

# Create a global instance
try:
    db = Database(MONGO_URI)
except Exception:
    # Handle case where config might be missing during initial setup
    db = None