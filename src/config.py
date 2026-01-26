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


# ====== Business Logic Constants ======
# Premium pricing
PREMIUM_PRICE = 1.99  # USD
PREMIUM_ORIGINAL_PRICE = 3.00  # USD
PREMIUM_DISCOUNT_SLOTS = 15  # Total discount slots available

# Free user limits
FREE_TRIAL_DAYS = 7  # Trial period in days
FREE_DAILY_LIMIT = 2  # Downloads per day after trial
FREE_MAX_QUALITY = "480p"  # Maximum quality for free users

# Premium benefits
PREMIUM_MAX_QUALITY = "1080p"

# File constraints
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49MB (Telegram limit is 50MB, we use 49 for safety)
MAX_URL_LENGTH = 2048
DOWNLOAD_TIMEOUT = 300  # 5 minutes in seconds

# Rate limiting
RATE_LIMIT_REQUESTS = 3  # Number of requests
RATE_LIMIT_WINDOW = 10  # Time window in seconds

# Supported platforms
SUPPORTED_PLATFORMS = [
    'youtube.com', 'youtu.be', 'www.youtube.com', 'm.youtube.com',
    'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com',
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com',
    'instagram.com', 'www.instagram.com',
    'pinterest.com', 'www.pinterest.com', 'pin.it',
]


# ====== Validation Functions ======
def validate_bot_token(token: str) -> bool:
    """Validate Telegram Bot Token format."""
    if not token:
        return False
    pattern = r'^\d+:[A-Za-z0-9_-]+$'
    return bool(re.match(pattern, token))


def validate_mongo_uri(uri: str) -> bool:
    """Validate MongoDB URI format (basic check)."""
    if not uri:
        return False
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

try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)
except ValueError:
    raise ValueError("❌ LOG_CHANNEL_ID must be a valid integer (channel ID)!")


# ====== Helper Functions ======
def get_discount_percentage() -> int:
    """Calculate discount percentage."""
    return int(((PREMIUM_ORIGINAL_PRICE - PREMIUM_PRICE) / PREMIUM_ORIGINAL_PRICE) * 100)


def format_price(price: float) -> str:
    """Format price consistently."""
    return f"${price:.2f}"