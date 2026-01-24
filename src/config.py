import os
import re
from dotenv import load_dotenv

load_dotenv()

# ====== Load Environment Variables ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = os.getenv("ADMIN_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT_STR = os.getenv("PORT", "10000")


# ====== Validation Functions ======
def validate_bot_token(token: str) -> bool:
    """
    Validate Telegram Bot Token format.
    Format: {number}:{alphanumeric-string}
    """
    if not token:
        return False
    # Telegram bot token format: digits:alphanumeric_with_dashes
    pattern = r'^\d+:[A-Za-z0-9_-]+$'
    return bool(re.match(pattern, token))


def validate_mongo_uri(uri: str) -> bool:
    """
    Validate MongoDB URI format (basic check).
    """
    if not uri:
        return False
    # Basic check for MongoDB URI formats
    valid_prefixes = ['mongodb://', 'mongodb+srv://']
    return any(uri.startswith(prefix) for prefix in valid_prefixes)


# ====== Validate Required Fields ======
if not all([BOT_TOKEN, MONGO_URI, ADMIN_ID]):
    raise ValueError(
        "❌ Missing required environment variables!\n"
        "Please check your .env file has: BOT_TOKEN, MONGO_URI, ADMIN_ID"
    )

# ====== Validate BOT_TOKEN Format ======
if not validate_bot_token(BOT_TOKEN):
    raise ValueError(
        "❌ Invalid BOT_TOKEN format!\n"
        "Expected format: 123456789:ABCdefGHI-jklMNOpqr_stuvWXYZ"
    )

# ====== Validate MONGO_URI Format ======
if not validate_mongo_uri(MONGO_URI):
    raise ValueError(
        "❌ Invalid MONGO_URI format!\n"
        "Expected: mongodb:// or mongodb+srv:// URI"
    )

# ====== Parse ADMIN_ID ======
try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    raise ValueError("❌ ADMIN_ID must be a valid integer (Telegram user ID)!")

# ====== Parse PORT ======
try:
    PORT = int(PORT_STR)
    if PORT < 1 or PORT > 65535:
        raise ValueError("Port out of range")
except ValueError:
    raise ValueError(f"❌ PORT must be a valid integer between 1-65535, got: {PORT_STR}")

# ====== Validate LOG_CHANNEL_ID ======
if not LOG_CHANNEL_ID:
    raise ValueError(
        "❌ Missing LOG_CHANNEL_ID in .env!\n"
        "Please add your Telegram channel ID for logging."
    )

# Try to parse LOG_CHANNEL_ID (can be negative for channels)
try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)
except ValueError:
    raise ValueError("❌ LOG_CHANNEL_ID must be a valid integer (channel ID)!")