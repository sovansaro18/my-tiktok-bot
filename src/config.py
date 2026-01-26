import os
import re
from dotenv import load_dotenv

load_dotenv()

# ====== Load Environment Variables ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = os.getenv("ADMIN_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
REPORT_CHANNEL_ID_STR = os.getenv("REPORT_CHANNEL_ID", "-1003569125986")
PORT_STR = os.getenv("PORT", "10000")


# ====== Business Logic Constants ======
# Premium pricing
PREMIUM_PRICE = 3.00

# Free user limits
FREE_DAILY_LIMIT = 2
FREE_MAX_QUALITY = "480p"

# Premium benefits
PREMIUM_MAX_QUALITY = "1080p"

# File constraints
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49MB (Telegram limit is 50MB, we use 49 for safety)
MAX_URL_LENGTH = 2048
DOWNLOAD_TIMEOUT = 300  # 5 minutes in seconds

# URL allowlist (base domains; subdomains allowed)
ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "pinterest.com",
    "pin.it",
)

# Rate limiting
RATE_LIMIT_REQUESTS = 3  # Number of requests
RATE_LIMIT_WINDOW = 10  # Time window in seconds

RATE_LIMIT_MESSAGE_COOLDOWN = 30

SUPPORTED_PLATFORMS = list(ALLOWED_DOMAINS)


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
if not all([BOT_TOKEN, ADMIN_ID]):
    raise ValueError(
        "❌ Missing required environment variables!\n"
        "Please check your .env file has: BOT_TOKEN, ADMIN_ID"
    )

# ====== Validate BOT_TOKEN Format ======
if not validate_bot_token(BOT_TOKEN):
    raise ValueError(
        "❌ Invalid BOT_TOKEN format!\n"
        "Expected format: 123456789:ABCdefGHI-jklMNOpqr_stuvWXYZ"
    )

# ====== Validate MONGO_URI Format (Optional) ======
if MONGO_URI and not validate_mongo_uri(MONGO_URI):
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

# ====== Parse LOG_CHANNEL_ID (Optional) ======
if LOG_CHANNEL_ID:
    try:
        LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)
    except ValueError:
        raise ValueError("❌ LOG_CHANNEL_ID must be a valid integer (channel ID)!")
else:
    LOG_CHANNEL_ID = None

# ====== Parse REPORT_CHANNEL_ID ======
try:
    REPORT_CHANNEL_ID = int(REPORT_CHANNEL_ID_STR)
except ValueError:
    raise ValueError("❌ REPORT_CHANNEL_ID must be a valid integer (channel ID)!")


# ====== Helper Functions ======
def format_price(price: float) -> str:
    """Format price consistently."""
    return f"${price:.2f}"