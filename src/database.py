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
        ស្វែងរកទិន្នន័យអ្នកប្រើប្រាស់។ ប្រសិនបើគ្មាន នឹងបង្កើតថ្មីភ្លាមៗ។
        Returns: (user_data, is_new_user)
        """
        try:
            user = await self.users.find_one({"user_id": user_id})
            
            if user:
                return user, False
            
            # រចនាសម្ព័ន្ធទិន្នន័យសម្រាប់អ្នកប្រើថ្មី
            new_user = {
                "user_id": user_id,
                "status": "free",
                "downloads_count": 0,
                "last_download_date": datetime.now(timezone.utc),
                "joined_at": datetime.now(timezone.utc)
            }
            await self.users.insert_one(new_user)
            return new_user, True
        except PyMongoError as e:
            logger.error(f"⚠️ Error in get_user: {e}")
            return {}, False

    async def increment_download(self, user_id: int) -> bool:
        """
        បូកចំនួនទាញយក និង Reset ជាស្វ័យប្រវត្តិប្រសិនបើឆ្លងដល់ថ្ងៃថ្មី។
        នេះជាបច្ចេកទេស Atomic Update ដើម្បីការពារ Data Inconsistency។
        """
        try:
            now = datetime.now(timezone.utc)
            # កំណត់ម៉ោង 00:00:00 នៃថ្ងៃនេះសម្រាប់ផ្ទៀងផ្ទាត់ការ Reset ប្រចាំថ្ងៃ
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            await self.users.update_one(
                {"user_id": user_id},
                [
                    {"$set": {
                        "downloads_count": {
                            "$cond": {
                                # បើកាលបរិច្ឆេទចុងក្រោយតូចជាងថ្ងៃនេះ (ឆ្លងថ្ងៃថ្មី) ឱ្យរាប់ពី 1 វិញ
                                "if": {"$lt": ["$last_download_date", today_start]},
                                "then": 1,
                                "else": {"$add": ["$downloads_count", 1]}
                            }
                        },
                        "last_download_date": now
                    }}
                ]
            )
            return True
        except PyMongoError as e:
            logger.error(f"⚠️ Failed to increment download for {user_id}: {e}")
            return False

    async def set_premium(self, user_id: int) -> bool:
        """
        តម្លើងឋានៈអ្នកប្រើប្រាស់ទៅជា PREMIUM។
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
        ទាញយកស្ថិតិអ្នកប្រើប្រាស់សរុប។
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
        """បិទការភ្ជាប់ទៅកាន់ Database។"""
        if self.client:
            self.client.close()
            logger.info("🔒 MongoDB connection closed.")

# បង្កើត Instance សម្រាប់ប្រើប្រាស់ជាសកល
try:
    db = Database(MONGO_URI)
except Exception:
    db = None