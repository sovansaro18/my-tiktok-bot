import asyncio
import logging
import os
from html import escape
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from src.config import ADMIN_ID, LOG_CHANNEL_ID
from src.database import db
from src.downloader import downloader
from src.utils import send_log, safe_remove_file

router = Router()
logger = logging.getLogger(__name__)

# ====== Security: URL Validation ======
ALLOWED_DOMAINS = [
    'youtube.com', 'youtu.be', 'www.youtube.com', 'm.youtube.com',  # YouTube Shorts
    'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com',  # TikTok
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com',  # Facebook
    'instagram.com', 'www.instagram.com',  # Instagram
    'pinterest.com', 'www.pinterest.com', 'pin.it',  # Pinterest
]

MAX_URL_LENGTH = 2048
DOWNLOAD_TIMEOUT = 300  # 5 minutes
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49MB for Telegram

# Free user limits
FREE_TRIAL_DAYS = 7  # First week unlimited
FREE_DAILY_LIMIT = 2  # After trial: 2 downloads/day
FREE_MAX_QUALITY = "480p"  # Max quality for free users


def validate_url(url: str) -> tuple[bool, Optional[str]]:
    """Validate URL for security and supported platforms."""
    if not url:
        return False, "URL is empty"
    
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL too long (max {MAX_URL_LENGTH} characters)"
    
    try:
        parsed = urlparse(url)
        
        if parsed.scheme not in ['http', 'https']:
            return False, "Only HTTP/HTTPS URLs are allowed"
        
        netloc_lower = parsed.netloc.lower()
        
        # Block internal URLs
        if any(blocked in netloc_lower for blocked in ['localhost', '127.0.0.1', '0.0.0.0', '::1', '192.168.', '10.', '172.16.']):
            return False, "Internal URLs are not allowed"
        
        # Check supported platforms
        if not any(domain in netloc_lower for domain in ALLOWED_DOMAINS):
            return False, (
                "វេទិកានេះមិនត្រូវបានគាំទ្រទេ។\n\n"
                "វេទិកាដែលគាំទ្រ:\n"
                "• TikTok\n"
                "• Facebook\n"
                "• YouTube Shorts\n"
                "• Instagram\n"
                "• Pinterest"
            )
        
        return True, None
        
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False, "ទម្រង់ URL មិនត្រឹមត្រូវ"


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Safely delete a message without raising exceptions."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"✅ Deleted message {message_id}")
        return True
    except TelegramBadRequest as e:
        if "message to delete not found" in str(e).lower():
            logger.info(f"ℹ️ Message {message_id} already deleted or not found")
            return True
        elif "message can't be deleted" in str(e).lower():
            logger.warning(f"⚠️ Cannot delete message {message_id} (too old or permission issue)")
            return False
        else:
            logger.error(f"❌ Error deleting message {message_id}: {e}")
            return False
    except Exception as e:
        logger.error(f"❌ Unexpected error deleting message {message_id}: {e}")
        return False


def check_daily_limit(user_data: dict) -> tuple[bool, str]:
    """
    Check if user has exceeded daily download limit.
    
    Returns:
        (can_download, message)
    """
    status = user_data.get("status", "free")
    
    # Premium users: unlimited
    if status == "premium":
        return True, ""
    
    # Check if still in trial period (first 7 days)
    joined_date = user_data.get("joined_date")
    if joined_date:
        days_since_joined = (datetime.now(timezone.utc) - joined_date).days
        
        if days_since_joined < FREE_TRIAL_DAYS:
            # Still in trial - unlimited
            remaining_days = FREE_TRIAL_DAYS - days_since_joined
            return True, f"🎉 រយៈពេលសាកល្បង: នៅសល់ {remaining_days} ថ្ងៃទៀត (ទាញយកមិនកំណត់)"
    
    # After trial: check daily limit
    last_download_date = user_data.get("last_download_date")
    daily_count = user_data.get("daily_download_count", 0)
    
    today = datetime.now(timezone.utc).date()
    
    # Reset counter if new day
    if not last_download_date or last_download_date.date() != today:
        return True, ""
    
    # Check if exceeded daily limit
    if daily_count >= FREE_DAILY_LIMIT:
        return False, (
            f"🚫 <b>អស់ការទាញយកប្រចាំថ្ងៃរបស់អ្នកហើយ!</b>\n\n"
            f"📊 កំណត់សម្រាប់អ្នកប្រើឥតគិតថ្លៃ: {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
            f"⏰ សូមព្យាយាមម្តងទៀតនៅថ្ងៃស្អែក\n\n"
            f"💎 <b>ចង់ប្រើមិនកំណត់?</b>\n"
            f"Upgrade ទៅ Premium តម្លៃត្រឹមតែ $1.99!"
        )
    
    remaining = FREE_DAILY_LIMIT - daily_count
    return True, f"📊 នៅសល់: {remaining}/{FREE_DAILY_LIMIT} ដងសម្រាប់ថ្ងៃនេះ"


def get_usage_notification(user_data: dict) -> dict:
    """Generate usage notification with trial/daily limit info."""
    status = user_data.get("status", "free")
    
    if status == "premium":
        return {
            "text": (
                "✅ <b>ទាញយករួចរាល់!</b>\n\n"
                "💎 <b>សមាជិកពិសេស Premium</b>\n"
                "♾️ ទាញយកបានមិនកំណត់\n"
                "🚀 ល្បឿនលឿនបំផុត\n"
                "🎬 គុណភាព 1080p\n\n"
                "<i>អរគុណសម្រាប់ការជឿទុកចិត្ត!</i>"
            ),
            "keyboard": None
        }
    
    # Free user
    joined_date = user_data.get("joined_date")
    days_since_joined = (datetime.now(timezone.utc) - joined_date).days if joined_date else 999
    
    # Check if in trial period
    if days_since_joined < FREE_TRIAL_DAYS:
        remaining_days = FREE_TRIAL_DAYS - days_since_joined
        text = (
            f"✅ <b>ទាញយករួចរាល់!</b>\n\n"
            f"🎉 <b>រយៈពេលសាកល្បងឥតគិតថ្លៃ</b>\n"
            f"📅 នៅសល់: {remaining_days} ថ្ងៃទៀត\n"
            f"♾️ ទាញយកបានមិនកំណត់ (ក្នុងអំឡុងពេលសាកល្បង)\n"
            f"🎬 គុណភាព: {FREE_MAX_QUALITY}\n\n"
            f"💡 <b>ជូនដំណឹង:</b>\n"
            f"បន្ទាប់ពីរយៈពេលសាកល្បងផុតកំណត់ អ្នកនឹងមានសិទ្ធិ:\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
            f"• គុណភាព {FREE_MAX_QUALITY}\n"
            f"• ល្បឿនមធ្យម\n\n"
            f"💎 <b>ចង់បន្តប្រើមិនកំណត់?</b>\n"
            f"Upgrade ទៅ Premium តម្លៃត្រឹមតែ $1.99!"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💎 មើលអត្ថប្រយោជន៍ Premium",
                callback_data="premium_info"
            )]
        ])
        
        return {"text": text, "keyboard": keyboard}
    
    # After trial - check daily limit
    daily_count = user_data.get("daily_download_count", 0)
    remaining = FREE_DAILY_LIMIT - daily_count
    
    # Progress bar
    filled = int((daily_count / FREE_DAILY_LIMIT) * 5)
    empty = 5 - filled
    progress_bar = "🟩" * filled + "⬜" * empty
    
    text = (
        f"📢 <b>ស្ថានភាពការទាញយក</b>\n\n"
        f"🎞️ <b>ទាញយកថ្ងៃនេះ:</b> {daily_count}/{FREE_DAILY_LIMIT}\n"
        f"📊 <b>នៅសល់:</b> {remaining} ដងទៀត\n"
        f"{progress_bar}\n"
        f"🎬 គុណភាព: {FREE_MAX_QUALITY}\n\n"
    )
    
    if remaining <= 1:
        text += (
            "⚠️ <b>ជិតអស់សិទ្ធិសម្រាប់ថ្ងៃនេះហើយ!</b>\n\n"
            "💎 <b>ចង់ទាញយកបានរហូត?</b>\n"
            "• ទាញយកមិនកំណត់\n"
            "• គុណភាព 1080p\n"
            "• ល្បឿនលឿនបំផុត\n"
            "• តម្លៃ: $1.99 (ពេញមួយជីវិត)\n\n"
            "<i>បង់ម្តង ប្រើរហូត!</i>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💎 ទិញ Premium ឥឡូវនេះ $1.99!",
                callback_data="buy_premium"
            )]
        ])
    else:
        text += (
            "💡 <b>ជម្រើស Premium:</b>\n"
            "• ទាញយកមិនកំណត់\n"
            "• គុណភាព 1080p\n"
            "• តម្លៃ: $1.99 ពេញមួយជីវិត"
        )
        keyboard = None
    
    return {"text": text, "keyboard": keyboard}


class DownloadState(StatesGroup):
    waiting_for_format = State()


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command with detailed welcome message."""
    user_id = message.from_user.id
    user_data, is_new = await db.get_user(user_id)
    
    if is_new:
        await send_log(
            f"🆕 New User Joined: {message.from_user.full_name} (`{user_id}`)",
            bot=message.bot
        )

    status = user_data.get("status", "free")
    
    # Welcome message
    welcome = f"👋 <b>សួស្តី {escape(message.from_user.full_name)}!</b>\n\n"
    
    # Bot capabilities
    welcome += (
        "🤖 <b>អ្វីដែលបតអាចធ្វើបាន:</b>\n"
        "✅ ទាញយកវីដេអូពីវេទិកាល្បីៗ\n"
        "✅ គាំទ្រ: TikTok, Facebook, YouTube Shorts, Instagram, Pinterest\n"
        "✅ ទាញយកជា Video ឬ Audio\n"
        "✅ គុណភាពល្អ (អាស្រ័យលើគណនីរបស់អ្នក)\n\n"
        
        "🚫 <b>កំណត់:</b>\n"
        "❌ មិនគាំទ្រវីដេអូ Private\n"
        "❌ មិនគាំទ្រវីដេអូដែលមាន Copyright\n"
        "❌ ទំហំវីដេអូត្រូវតូចជាង 49MB\n"
        "❌ ត្រឹមតែវីដេអូ Public ប៉ុណ្ណោះ\n\n"
    )
    
    # Show status based on user type
    if status == "premium":
        welcome += (
            "💎 <b>ស្ថានភាពរបស់អ្នក: PREMIUM</b>\n\n"
            "🎁 <b>អត្ថប្រយោជន៍របស់អ្នក:</b>\n"
            "♾️ ទាញយកបានមិនកំណត់\n"
            "🎬 គុណភាព 1080p\n"
            "🚀 ល្បឿនលឿនបំផុត\n"
            "💬 ជំនួយអាទិភាព 24/7\n\n"
            "<i>គ្រាន់តែផ្ញើ link មកខ្ញុំ ហើយខ្ញុំនឹងទាញយកឱ្យអ្នក!</i>"
        )
    else:
        # Check trial status
        joined_date = user_data.get("joined_date")
        days_since_joined = (datetime.now(timezone.utc) - joined_date).days if joined_date else 0
        
        if days_since_joined < FREE_TRIAL_DAYS:
            # In trial
            remaining_days = FREE_TRIAL_DAYS - days_since_joined
            welcome += (
                f"🎉 <b>ស្ថានភាពរបស់អ្នក: រយៈពេលសាកល្បង</b>\n\n"
                f"📅 <b>នៅសល់:</b> {remaining_days} ថ្ងៃទៀត\n\n"
                f"🎁 <b>អត្ថប្រយោជន៍បច្ចុប្បន្ន:</b>\n"
                f"♾️ ទាញយកមិនកំណត់ (ក្នុងអំឡុងពេលសាកល្បង)\n"
                f"🎬 គុណភាព {FREE_MAX_QUALITY}\n"
                f"⚡ ល្បឿនមធ្យម\n\n"
                f"⚠️ <b>បន្ទាប់ពីរយៈពេលសាកល្បង:</b>\n"
                f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
                f"• គុណភាព {FREE_MAX_QUALITY}\n"
                f"• ល្បឿនមធ្យម\n\n"
            )
        else:
            # After trial
            daily_count = user_data.get("daily_download_count", 0)
            remaining = FREE_DAILY_LIMIT - daily_count
            
            welcome += (
                f"🆓 <b>ស្ថានភាពរបស់អ្នក: ឥតគិតថ្លៃ</b>\n\n"
                f"🎁 <b>អត្ថប្រយោជន៍បច្ចុប្បន្ន:</b>\n"
                f"📊 {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (នៅសល់: {remaining})\n"
                f"🎬 គុណភាព {FREE_MAX_QUALITY}\n"
                f"⚡ ល្បឿនមធ្យម\n\n"
            )
        
        # Premium comparison
        welcome += (
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 <b>ប្រៀបធៀប: ឥតគិតថ្លៃ vs Premium</b>\n\n"
            "<b>ឥតគិតថ្លៃ:</b>\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (បន្ទាប់ពីសាកល្បង)\n"
            f"• គុណភាព {FREE_MAX_QUALITY}\n"
            "• ល្បឿនមធ្យម\n\n"
            "<b>Premium ($1.99 ពេញមួយជីវិត):</b>\n"
            "• ទាញយកមិនកំណត់ ♾️\n"
            "• គុណភាព 1080p 🎬\n"
            "• ល្បឿនលឿនបំផុត 🚀\n"
            "• ជំនួយអាទិភាព 💬\n"
            "• គ្មានការបង់ប្រចាំខែ ✅\n\n"
            "<i>បង់ម្តង ប្រើរហូត! តម្លៃសមរម្យបំផុត!</i>"
        )

    await message.answer(welcome, parse_mode="HTML")


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    """Show user plan details."""
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    status = user_data.get("status", "free")
    joined_date = user_data.get("joined_date")
    
    if status == "premium":
        text = (
            f"📊 <b>ព័ត៌មានគណនីរបស់អ្នក</b>\n\n"
            f"👤 ឈ្មោះ: {escape(message.from_user.full_name)}\n"
            f"🏷 ស្ថានភាព: <b>PREMIUM 💎</b>\n\n"
            f"🎁 <b>អត្ថប្រយោជន៍:</b>\n"
            f"♾️ ទាញយកមិនកំណត់\n"
            f"🎬 គុណភាព 1080p\n"
            f"🚀 ល្បឿនលឿនបំផុត\n"
            f"💬 ជំនួយអាទិភាព 24/7\n\n"
            f"<i>សូមអរគុណសម្រាប់ការគាំទ្រ! ❤️</i>"
        )
    else:
        days_since_joined = (datetime.now(timezone.utc) - joined_date).days if joined_date else 0
        
        if days_since_joined < FREE_TRIAL_DAYS:
            # In trial
            remaining_days = FREE_TRIAL_DAYS - days_since_joined
            text = (
                f"📊 <b>ព័ត៌មានគណនីរបស់អ្នក</b>\n\n"
                f"👤 ឈ្មោះ: {escape(message.from_user.full_name)}\n"
                f"🏷 ស្ថានភាព: <b>រយៈពេលសាកល្បង 🎉</b>\n"
                f"📅 នៅសល់: <b>{remaining_days} ថ្ងៃទៀត</b>\n\n"
                f"🎁 <b>អត្ថប្រយោជន៍បច្ចុប្បន្ន:</b>\n"
                f"♾️ ទាញយកមិនកំណត់ (ក្នុងអំឡុងពេលសាកល្បង)\n"
                f"🎬 គុណភាព {FREE_MAX_QUALITY}\n"
                f"⚡ ល្បឿនមធ្យម\n\n"
                f"⚠️ <b>បន្ទាប់ពីសាកល្បង:</b>\n"
                f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
                f"• គុណភាព {FREE_MAX_QUALITY}\n\n"
                f"💎 Upgrade ទៅ Premium តម្លៃត្រឹមតែ $1.99 ពេញមួយជីវិត!"
            )
        else:
            # After trial
            daily_count = user_data.get("daily_download_count", 0)
            remaining = FREE_DAILY_LIMIT - daily_count
            
            text = (
                f"📊 <b>ព័ត៌មានគណនីរបស់អ្នក</b>\n\n"
                f"👤 ឈ្មោះ: {escape(message.from_user.full_name)}\n"
                f"🏷 ស្ថានភាព: <b>ឥតគិតថ្លៃ 🆓</b>\n\n"
                f"🎁 <b>អត្ថប្រយោជន៍បច្ចុប្បន្ន:</b>\n"
                f"📊 {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (នៅសល់: {remaining})\n"
                f"🎬 គុណភាព {FREE_MAX_QUALITY}\n"
                f"⚡ ល្បឿនមធ្យម\n\n"
                f"💎 <b>Upgrade ទៅ Premium:</b>\n"
                f"• ទាញយកមិនកំណត់ ♾️\n"
                f"• គុណភាព 1080p 🎬\n"
                f"• ល្បឿនលឿន 🚀\n"
                f"• តម្លៃ: $1.99 (ពេញមួយជីវិត)\n\n"
                f"<i>បង់ម្តង ប្រើរហូត!</i>"
            )
        
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: Message, state: FSMContext):
    """Handle video URL messages."""
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    # Check daily limit for free users
    can_download, limit_msg = check_daily_limit(user_data)
    
    if not can_download:
        await message.answer(limit_msg, parse_mode="HTML")
        return

    url = message.text.strip()
    
    # Validate URL
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>URL មិនត្រឹមត្រូវ</b>\n\n{error_msg}",
            parse_mode="HTML"
        )
        return
    
    # Store URL and message IDs
    await state.update_data(url=url, url_message_id=message.message_id)
    await state.set_state(DownloadState.waiting_for_format)
    
    # Show format selection
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 វីដេអូ (MP4)", callback_data="fmt_video"),
            InlineKeyboardButton(text="🎵 អូឌីយ៉ូ (M4A)", callback_data="fmt_audio")
        ]
    ])
    
    # Show limit info if available
    info_text = "👇 សូមជ្រើសរើសប្រភេទ:\n\n"
    if limit_msg:
        info_text += f"<i>{limit_msg}</i>"
    
    format_msg = await message.answer(info_text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(format_message_id=format_msg.message_id)


@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    """Handle format selection and download."""
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None
    
    if not url:
        await callback.message.edit_text("⚠️ សម័យផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត។")
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"
    
    progress_msg = await callback.message.edit_text(
        f"⏳ <b>កំពុងទាញយក {download_type.upper()}...</b>\n"
        f"<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML"
    )
    
    # Download with timeout
    try:
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Download timeout for URL: {url}")
        await progress_msg.edit_text(
            "❌ <b>ការទាញយកយូរពេកហើយ</b>\n\n"
            "សូមព្យាយាមជាមួយវីដេអូខ្លីជាងនេះ។",
            parse_mode="HTML"
        )
        await send_log(
            f"⏱ Download Timeout\n"
            f"User: `{callback.from_user.id}`\n"
            f"URL: {url}\n"
            f"Type: {download_type}",
            bot=callback.bot
        )
        await state.clear()
        return
    
    # Handle download errors
    if result["status"] == "error":
        safe_message = escape(result.get('message', 'Unknown error'))
        await progress_msg.edit_text(f"❌ <b>មានបញ្ហា:</b> {safe_message}", parse_mode="HTML")
        
        await send_log(
            f"❌ Download Error\n"
            f"User: {callback.from_user.full_name} (`{callback.from_user.id}`)\n"
            f"URL: {url}\n"
            f"Type: {download_type}\n"
            f"Error: {result.get('message', 'Unknown')}",
            bot=callback.bot
        )
        await state.clear()
        return

    file_path = result["file_path"]
    
    # Check file size
    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            await progress_msg.edit_text(
                f"❌ <b>ឯកសារធំពេកសម្រាប់ Telegram</b>\n\n"
                f"📊 ទំហំ: {file_size / 1024 / 1024:.1f}MB\n"
                f"⚠️ កំណត់: {MAX_FILE_SIZE / 1024 / 1024:.0f}MB\n\n"
                f"សូមព្យាយាមវីដេអូគុណភាពទាបជាង ឬជ្រើសរើសអូឌីយ៉ូ។",
                parse_mode="HTML"
            )
            await safe_remove_file(file_path)
            await state.clear()
            return
    
    # Prepare caption
    safe_title = escape(str(result.get('title', 'Unknown')))
    safe_duration = escape(str(result.get('duration', 0)))
    
    caption = (
        f"✅ <b>ទាញយករួចរាល់!</b>\n"
        f"📌 ចំណងជើង: {safe_title}\n"
        f"⏱ រយៈពេល: {safe_duration}វិ\n"
        f"🤖 តាមរយៈ @ravi_downloader_bot"
    )

    # Upload file
    try:
        await progress_msg.edit_text("📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")
        
        file_input = FSInputFile(file_path)
        
        if download_type == "audio":
            await callback.message.answer_audio(file_input, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer_video(file_input, caption=caption, parse_mode="HTML")
        
        # Cleanup messages
        chat_id = callback.message.chat.id
        
        if url_message_id:
            await safe_delete_message(callback.bot, chat_id, url_message_id)
        
        if format_message_id:
            await safe_delete_message(callback.bot, chat_id, format_message_id)
        
        try:
            await progress_msg.delete()
        except Exception as e:
            logger.warning(f"Could not delete progress message: {e}")
        
        # Update download stats
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        
        # Update daily counter for free users
        if user_data.get("status") != "premium":
            today = datetime.now(timezone.utc)
            last_download_date = user_data.get("last_download_date")
            
            # Reset if new day
            if not last_download_date or last_download_date.date() != today.date():
                await db.users.update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "last_download_date": today,
                            "daily_download_count": 1
                        }
                    }
                )
            else:
                # Increment daily counter
                await db.users.update_one(
                    {"user_id": user_id},
                    {"$inc": {"daily_download_count": 1}}
                )
            
            # Get updated data
            updated_user_data, _ = await db.get_user(user_id)
            notification = get_usage_notification(updated_user_data)
        else:
            # Premium user
            notification = get_usage_notification(user_data)
        
        # Send notification
        await callback.message.answer(
            notification["text"],
            parse_mode="HTML",
            reply_markup=notification["keyboard"]
        )
            
    except TelegramBadRequest as e:
        logger.error(f"Telegram API error: {e}")
        
        error_str = str(e).lower()
        if "file is too big" in error_str or "too large" in error_str:
            error_msg = (
                "❌ <b>ឯកសារធំពេកសម្រាប់ Telegram</b>\n\n"
                "⚠️ Telegram កំណត់: 50MB\n"
                "សូមព្យាយាមវីដេអូគុណភាពទាបជាង។"
            )
        elif "wrong file identifier" in error_str:
            error_msg = "❌ មានបញ្ហាជាមួយទម្រង់ឯកសារ។ សូមព្យាយាមម្តងទៀត។"
        else:
            error_msg = f"❌ មិនអាចបញ្ជូនឯកសារបានទេ។\n\n<code>{escape(str(e)[:200])}</code>"
        
        await callback.message.answer(error_msg, parse_mode="HTML")
        
        await send_log(
            f"❌ Upload Error (Telegram)\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot
        )
        
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        await callback.message.answer(
            f"❌ មានបញ្ហាក្នុងការបញ្ជូនឯកសារ។\n\n"
            f"<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML"
        )
        
        await send_log(
            f"❌ Upload Error (General)\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot
        )
    finally:
        # Always cleanup file
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()


# ====== Admin Commands ======

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Admin: Broadcast message to all users."""
    if message.from_user.id != ADMIN_ID:
        return
    
    text = message.text.replace("/broadcast", "", 1).strip()
    
    if not text:
        await message.answer(
            "⚠️ <b>របៀបប្រើប្រាស់:</b> /broadcast [សារ]\n\n"
            "<b>ឧទាហរណ៍:</b>\n"
            "/broadcast 🔧 បតកំពុងធ្វើថែទាំ 30 នាទី។",
            parse_mode="HTML"
        )
        return
    
    try:
        all_users = await db.users.find({}).to_list(length=None)
        
        total = len(all_users)
        success = 0
        failed = 0
        
        progress_msg = await message.answer(
            f"📢 <b>កំពុងផ្សាយ...</b>\n"
            f"សរុប: {total}\n"
            f"បញ្ជូន: 0",
            parse_mode="HTML"
        )
        
        for idx, user in enumerate(all_users, 1):
            user_id = user.get("user_id")
            
            try:
                broadcast_text = (
                    f"📢 <b>សេចក្តីជូនដំណឹងពីអ្នកគ្រប់គ្រង</b>\n\n"
                    f"{text}\n\n"
                    f"<i>នេះជាសារផ្លូវការពីអ្នកគ្រប់គ្រងបត។</i>"
                )
                
                await message.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    parse_mode="HTML"
                )
                success += 1
                
                if idx % 20 == 0:
                    await asyncio.sleep(1)
                
                if idx % 10 == 0 or idx == total:
                    await progress_msg.edit_text(
                        f"📢 <b>កំពុងផ្សាយ...</b>\n"
                        f"សរុប: {total}\n"
                        f"✅ បញ្ជូន: {success}\n"
                        f"❌ បរាជ័យ: {failed}\n"
                        f"ដំណើរការ: {idx}/{total} ({idx*100//total}%)",
                        parse_mode="HTML"
                    )
                
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to send to {user_id}: {e}")
        
        await progress_msg.edit_text(
            f"✅ <b>ផ្សាយរួចរាល់!</b>\n\n"
            f"📊 សរុប: {total}\n"
            f"✅ ជោគជ័យ: {success}\n"
            f"❌ បរាជ័យ: {failed}",
            parse_mode="HTML"
        )
        
        await send_log(
            f"📢 Broadcast Sent\n"
            f"Success: {success}/{total}",
            bot=message.bot
        )
        
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await message.answer(f"❌ <b>ផ្សាយបរាជ័យ</b>\n\n{escape(str(e))}", parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Admin: View bot statistics."""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        stats = await db.count_users()
        
        pipeline = [
            {"$group": {
                "_id": None,
                "total_downloads": {"$sum": "$daily_download_count"}
            }}
        ]
        
        result = await db.users.aggregate(pipeline).to_list(length=1)
        total_downloads = result[0]["total_downloads"] if result else 0
        
        premium_sold = stats['premium']
        slots_remaining = max(0, 15 - premium_sold)
        revenue = premium_sold * 1.99
        potential = slots_remaining * 1.99
        
        text = (
            f"📊 <b>ស្ថិតិបត</b>\n\n"
            f"👥 អ្នកប្រើសរុប: <b>{stats['total']}</b>\n"
            f"💎 Premium: <b>{stats['premium']}</b>\n"
            f"🆓 ឥតគិតថ្លៃ: <b>{stats['free']}</b>\n\n"
            f"⬇️ ការទាញយកសរុប: <b>{total_downloads}</b>\n"
            f"📈 មធ្យមក្នុងមួយអ្នក: <b>{total_downloads // stats['total'] if stats['total'] > 0 else 0}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>ការលក់ Lifetime Premium:</b>\n"
            f"• តម្លៃ: ${1.99:.2f}\n"
            f"• លក់រួច: <b>{premium_sold}/15</b>\n"
            f"• នៅសល់: <b>{slots_remaining}/15</b>\n"
            f"• ប្រាក់ចំណូល: <b>${revenue:.2f}</b>\n"
            f"• សក្តានុពល: <b>${potential:.2f}</b>\n\n"
            f"<i>ថ្ងៃទី: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await message.answer(f"❌ មានបញ្ហា: {escape(str(e))}", parse_mode="HTML")


@router.message(Command("approve"))
async def cmd_approve(message: Message):
    """Admin: Approve premium for user."""
    if message.from_user.id != ADMIN_ID:
        return

    try:
        target_id = int(message.text.split()[1])
        success = await db.set_premium(target_id)
        
        if success:
            await message.answer(f"✅ អ្នកប្រើ {target_id} ក្លាយជា PREMIUM ហើយ។")
            await message.bot.send_message(
                target_id, 
                "🎉 <b>អបអរសាទរ!</b> គណនីរបស់អ្នកត្រូវបាន Upgrade ទៅជា PREMIUM ហើយ! 💎", 
                parse_mode="HTML"
            )
            await send_log(
                f"👮‍♂️ Admin approved Premium for `{target_id}`",
                bot=message.bot
            )
        else:
            await message.answer("❌ បរាជ័យក្នុងការធ្វើបច្ចុប្បន្នភាពអ្នកប្រើ។ សូមពិនិត្យ ID។")
    except (IndexError, ValueError):
        await message.answer("⚠️ របៀបប្រើប្រាស់: /approve [user_id]")


# ====== Payment Handlers ======

@router.callback_query(F.data == "buy_premium")
async def handle_buy_premium(callback: CallbackQuery):
    """Show payment QR code."""
    
    stats = await db.count_users()
    premium_sold = stats['premium']
    slots_remaining = max(0, 15 - premium_sold)
    
    if slots_remaining == 0:
        await callback.message.edit_text(
            "😢 <b>សូមអភ័យទោស! លក់អស់ហើយ!</b>\n\n"
            "កន្លែងបញ្ចុះតម្លៃទាំង 15 ត្រូវបានទិញអស់ហើយ។\n\n"
            "💬 សូមទាក់ទងអ្នកគ្រប់គ្រងសម្រាប់តម្លៃធម្មតា ឬការផ្តល់ជូនថ្មី។",
            parse_mode="HTML"
        )
        return
    
    payment_qr_path = "payment.jpg"
    
    if not os.path.exists(payment_qr_path):
        await callback.message.edit_text(
            "❌ <b>រកមិនឃើញកូដ QR ទូទាត់ប្រាក់!</b>\n\n"
            "សូមទាក់ទងអ្នកគ្រប់គ្រង។",
            parse_mode="HTML"
        )
        logger.error(f"payment.jpg not found!")
        return
    
    payment_caption = (
        "💳 <b>ទូទាត់ប្រាក់ Premium ពេញមួយជីវិត</b>\n\n"
        f"💎 <b>ចូលប្រើពេញមួយជីវិត:</b> ${1.99:.2f} (បង់តែម្តង)\n"
        f"⚡ <b>កន្លែងនៅសល់:</b> {slots_remaining}/15\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱 <b>របៀបបង់ប្រាក់:</b>\n\n"
        "1️⃣ ស្កេន QR Code ខាងក្រោម\n"
        f"2️⃣ បង់ចំនួន <b>${1.99:.2f}</b>\n"
        "3️⃣ ថតរូបវិក័យបត្រ (Screenshot)\n"
        "4️⃣ ផ្ញើវិក័យបត្រមកទីនេះវិញ\n"
        "5️⃣ រង់ចាំអ្នកគ្រប់គ្រងពិនិត្យ និងបើកសិទ្ធិ\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ <b>ពេលវេលាដំណើរការ:</b> ក្នុងរយៈពេល 1 ម៉ោង\n"
        "♾️ <b>រយៈពេលសុពលភាព:</b> ពេញមួយជីវិត (មិនផុតកំណត់)\n\n"
        f"🆔 <b>User ID របស់អ្នក:</b> <code>{callback.from_user.id}</code>\n"
        "<i>(សូមរក្សាទុក ID នេះ)</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>អត្ថប្រយោជន៍ Lifetime Premium:</b>\n"
        "• ទាញយកមិនកំណត់ (ជារៀងរហូត)\n"
        "• គុណភាព 1080p\n"
        "• ល្បឿនលឿនបំផុត\n"
        "• ជំនួយអាទិភាព 24/7\n"
        "• គ្មានការបង់ប្រាក់ប្រចាំខែ\n"
        "• បង់តែម្តង ប្រើរហូត! 🚀\n\n"
        f"⚠️ <b>ប្រញាប់! កន្លែងបញ្ចុះតម្លៃនៅសល់ {slots_remaining} ប៉ុណ្ណោះ!</b>\n\n"
        "❓ <b>មានសំណួរ?</b> ទាក់ទងអ្នកគ្រប់គ្រងនៅក្នុង Channel"
    )
    
    try:
        await callback.message.delete()
        
        photo = FSInputFile(payment_qr_path)
        await callback.message.answer_photo(
            photo=photo,
            caption=payment_caption,
            parse_mode="HTML"
        )
        
        await send_log(
            f"💰 Premium Interest\n"
            f"User: {callback.from_user.full_name} (`{callback.from_user.id}`)\n"
            f"Slots: {slots_remaining}/15",
            bot=callback.bot
        )
        
    except Exception as e:
        logger.error(f"Error showing QR: {e}")
        await callback.answer("❌ មានបញ្ហា។ សូមព្យាយាមម្តងទៀត។", show_alert=True)


@router.callback_query(F.data == "premium_info")
async def handle_premium_info(callback: CallbackQuery):
    """Show premium benefits."""
    
    stats = await db.count_users()
    premium_sold = stats['premium']
    slots_remaining = max(0, 15 - premium_sold)
    
    info_text = (
        "💎 <b>សមាជិកភាព Premium ពេញមួយជីវិត</b>\n\n"
        f"💰 <b>តម្លៃ:</b> ~~$3.00~~ → <b>${1.99:.2f}</b>\n"
        f"⚡ <b>កន្លែងនៅសល់:</b> {slots_remaining}/15\n"
        f"📊 <b>លក់រួច:</b> {premium_sold}/15\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📥 ការទាញយក:</b>\n"
        "✅ ទាញយកមិនកំណត់ជារៀងរហូត\n"
        "✅ គ្មានការកំណត់ប្រចាំថ្ងៃ/ខែ\n"
        "✅ គាំទ្រគ្រប់វេទិកា\n"
        "✅ គុណភាពខ្ពស់ (រហូតដល់ 1080p)\n\n"
        "<b>⚡ ប្រតិបត្តិការ:</b>\n"
        "🚀 ជួរអាទិភាពក្នុងការទាញយក\n"
        "🚀 ល្បឿនទាញយកលឿនបំផុត\n"
        "🚀 ទាញយកច្រើនក្នុងពេលដំណាលគ្នា\n\n"
        "<b>🎯 ជំនួយ:</b>\n"
        "💬 ជំនួយអតិថិជនអាទិភាព\n"
        "💬 ទាក់ទងផ្ទាល់ជាមួយអ្នកគ្រប់គ្រង\n"
        "💬 ជំនួយ 24/7\n\n"
        "<b>🎨 មុខងារ:</b>\n"
        "✨ គ្មានការផ្សាយពាណិជ្ជកម្ម\n"
        "✨ ចូលប្រើមុខងារថ្មីមុនគេ\n"
        "✨ ការកំណត់ផ្ទាល់ខ្លួន\n"
        "✨ ចូលប្រើពេញមួយជីវិត (មិនផុតកំណត់)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💵 <b>ទូទាត់តែម្តង:</b>\n"
        f"• បង់ <b>${1.99:.2f}</b> តែម្តង\n"
        "• ប្រើរហូត\n"
        "• គ្មានការបង់ប្រចាំខែ\n"
        "• គ្មានការគិតថ្លៃលាក់\n\n"
        f"⚠️ <b>ការផ្តល់ជូនមានកំណត់:</b> នៅសល់ {slots_remaining} កន្លែង!\n\n"
        "<i>បន្ទាប់ពីលក់ 15 ហើយ តម្លៃនឹងត្រឡប់ទៅ $3.00</i>"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 ទិញឥឡូវនេះ - ${1.99:.2f} ({slots_remaining} left)",
            callback_data="buy_premium"
        )],
        [InlineKeyboardButton(
            text="❌ បិទ",
            callback_data="close_info"
        )]
    ])
    
    await callback.message.edit_text(info_text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "close_info")
async def handle_close_info(callback: CallbackQuery):
    """Close info message."""
    await callback.message.delete()


@router.message(F.photo)
async def handle_receipt(message: Message):
    """Handle payment receipt."""
    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_info = f"User: {user_name} (<code>{message.from_user.id}</code>)"
    
    await message.bot.send_photo(
        chat_id=LOG_CHANNEL_ID,
        photo=message.photo[-1].file_id,
        caption=f"🧾 <b>ទទួលបានវិក័យបត្រទូទាត់ប្រាក់</b>\n\n{user_info}\n📝 ចំណាំ: {caption}\n\n👉 ប្រើ <code>/approve {message.from_user.id}</code> ដើម្បីអនុម័ត។",
        parse_mode="HTML"
    )
    
    await message.answer(
        "✅ <b>ទទួលវិក័យបត្ររួចរាល់!</b>\n"
        "យើងនឹងពិនិត្យ និង upgrade គណនីរបស់អ្នកក្នុងពេលឆាប់ៗ។",
        parse_mode="HTML"
    )