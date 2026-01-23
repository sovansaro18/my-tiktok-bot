import os
from dotenv import load_dotenv

# Load ទិន្នន័យពី .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = os.getenv("ADMIN_ID")

# ត្រួតពិនិត្យមើលថាមានទិន្នន័យគ្រប់គ្រាន់ឬអត់
if not all([BOT_TOKEN, MONGO_URI, ADMIN_ID]):
    raise ValueError("❌ សូមពិនិត្យមើល file .env របស់អ្នកឡើងវិញ! ទិន្នន័យមិនគ្រប់គ្រាន់។")

# បំប្លែង ADMIN_ID ទៅជាលេខ (Integer)
try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    raise ValueError("❌ ADMIN_ID នៅក្នុង .env ត្រូវតែជាលេខសុទ្ធ!")

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")

# បន្ថែមការ Check
if not LOG_CHANNEL_ID:
    raise ValueError("❌ សូមដាក់ LOG_CHANNEL_ID នៅក្នុង .env")