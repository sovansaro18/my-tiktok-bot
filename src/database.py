from motor.motor_asyncio import AsyncIOMotorClient
from src.config import MONGO_URI

class Database:
    def __init__(self):
        # បង្កើត Connection ទៅ MongoDB (Asynchronous)
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client['downloader_bot']
        self.users = self.db['users']

    async def get_user(self, user_id: int):
        """
        ទាញយកព័ត៌មាន User។
        Return: (user_dict, is_new_boolean)
        - is_new = True: បើជា User ថ្មីដែលទើបតែបង្កើត
        - is_new = False: បើជា User ចាស់
        """
        user = await self.users.find_one({"user_id": user_id})
        
        if not user:
            # បង្កើត User ថ្មីបើរកមិនឃើញ
            new_user = {
                "user_id": user_id,
                "status": "free",
                "downloads_count": 0,
                "joined_date": None # អាចដាក់ datetime.now() បើចង់
            }
            await self.users.insert_one(new_user)
            return new_user, True  # ត្រឡប់ True ដើម្បីប្រាប់ថាជា User ថ្មី
            
        return user, False # ត្រឡប់ False ព្រោះជា User ចាស់

    async def increment_download(self, user_id: int):
        """បន្ថែមចំនួន Download +1"""
        await self.users.update_one(
            {"user_id": user_id},
            {"$inc": {"downloads_count": 1}}
        )

    async def set_premium(self, user_id: int):
        """ដំឡើងសិទ្ធិជា Premium"""
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"status": "premium"}}
        )
    
    async def count_users(self):
        """រាប់ចំនួន User សរុប (សម្រាប់ Admin Stats នៅថ្ងៃក្រោយ)"""
        total = await self.users.count_documents({})
        premium = await self.users.count_documents({"status": "premium"})
        return total, premium

# បង្កើត Object តែមួយសម្រាប់ប្រើពេញគម្រោង
db = Database()