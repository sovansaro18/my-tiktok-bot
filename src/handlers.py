import asyncio
import logging
from html import escape
from typing import Optional
from urllib.parse import urlparse

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.config import ADMIN_ID, LOG_CHANNEL_ID
from src.database import db
from src.downloader import downloader
from src.utils import send_log, safe_remove_file

router = Router()
logger = logging.getLogger(__name__)

# ====== Security: URL Validation ======
ALLOWED_DOMAINS = [
    'youtube.com', 'youtu.be', 'www.youtube.com', 'm.youtube.com',
    'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com',
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com',
    'instagram.com', 'www.instagram.com',
    'twitter.com', 'www.twitter.com', 'x.com', 'www.x.com',
]

MAX_URL_LENGTH = 2048
DOWNLOAD_TIMEOUT = 300  # 5 minutes


def validate_url(url: str) -> tuple[bool, Optional[str]]:
    """
    Validate URL for security.
    
    Returns:
        (is_valid, error_message)
    """
    if not url:
        return False, "URL is empty"
    
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL too long (max {MAX_URL_LENGTH} characters)"
    
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if parsed.scheme not in ['http', 'https']:
            return False, "Only HTTP/HTTPS URLs are allowed"
        
        # Check for localhost/internal IPs (SSRF protection)
        netloc_lower = parsed.netloc.lower()
        if any(blocked in netloc_lower for blocked in ['localhost', '127.0.0.1', '0.0.0.0', '::1', '192.168.', '10.', '172.16.']):
            return False, "Internal URLs are not allowed"
        
        # Check domain whitelist
        if not any(domain in netloc_lower for domain in ALLOWED_DOMAINS):
            return False, "Platform not supported. Supported: YouTube, TikTok, Facebook, Instagram, Twitter/X"
        
        return True, None
        
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False, "Invalid URL format"

class DownloadState(StatesGroup):
    waiting_for_format = State()

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_data, is_new = await db.get_user(user_id)
    
    if is_new:
        await send_log(
            f"🆕 New User Joined: {message.from_user.full_name} (`{user_id}`)",
            bot=message.bot
        )

    status_icon = "💎" if user_data.get("status") == "premium" else "🆓"
    text = (
        f"👋 <b>Hello {message.from_user.full_name}!</b>\n\n"
        f"I can download videos from TikTok, FB, IG, YouTube, etc.\n"
        f"Just send me a link!\n\n"
        f"📊 <b>Your Status:</b> {user_data.get('status').upper()} {status_icon}\n"
        f"⬇️ <b>Downloads:</b> {user_data.get('downloads_count')}/10 (Free Tier)"
    )
    
    if user_data.get("status") == "premium":
        text = text.replace("/10 (Free Tier)", " (Unlimited)")

    await message.answer(text, parse_mode="HTML")

@router.message(Command("plan"))
async def cmd_plan(message: Message):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    status = user_data.get("status")
    count = user_data.get("downloads_count")
    
    text = (
        f"📊 <b>Usage Statistics</b>\n\n"
        f"👤 User: {message.from_user.full_name}\n"
        f"🏷 Status: <b>{status.upper()}</b>\n"
        f"🔢 Total Downloads: {count}\n\n"
    )
    
    if status == "free":
        text += "⚠️ <i>Limit: 10 downloads. Upgrade to Premium for unlimited access!</i>"
    else:
        text += "✨ <i>You are a Premium member. Enjoy unlimited downloads!</i>"
        
    await message.answer(text, parse_mode="HTML")

@router.message(Command("approve"))
async def cmd_approve(message: Message):
    # Security: Use integer comparison for admin check
    if message.from_user.id != ADMIN_ID:
        return

    try:
        target_id = int(message.text.split()[1])
        success = await db.set_premium(target_id)
        
        if success:
            await message.answer(f"✅ User {target_id} is now PREMIUM.")
            await message.bot.send_message(
                target_id, 
                "🎉 <b>Congratulations!</b> Your account has been upgraded to PREMIUM! 💎", 
                parse_mode="HTML"
            )
            await send_log(
                f"👮‍♂️ Admin approved Premium for `{target_id}`",
                bot=message.bot
            )
        else:
            await message.answer("❌ Failed to update user. Check ID.")
    except (IndexError, ValueError):
        await message.answer("⚠️ Usage: /approve [user_id]")

@router.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    if user_data.get("status") == "free" and user_data.get("downloads_count") >= 10:
        await message.answer(
            "🚫 <b>Free Limit Reached!</b>\n\n"
            "You have used your 10 free downloads.\n"
            "Please upgrade to Premium to continue.\n\n"
            "💸 <b>To Upgrade:</b> Send a photo of your payment receipt here.",
            parse_mode="HTML"
        )
        return

    url = message.text.strip()
    
    # Security: Validate URL before processing
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>Invalid URL</b>\n\n{escape(error_msg or 'Unknown error')}",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(url=url)
    await state.set_state(DownloadState.waiting_for_format)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video (MP4)", callback_data="fmt_video"),
            InlineKeyboardButton(text="🎵 Audio (M4A)", callback_data="fmt_audio")
        ]
    ])
    
    await message.answer("👇 Choose download format:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")
    file_path = None
    
    if not url:
        await callback.message.edit_text("⚠️ Session expired. Please send the link again.")
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"
    
    await callback.message.edit_text(
        f"⏳ <b>Downloading {download_type.upper()}...</b>\n"
        f"<i>Please wait, this may take a moment.</i>",
        parse_mode="HTML"
    )
    
    try:
        # Security: Add timeout for downloads
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Download timeout for URL: {url}")
        await callback.message.edit_text(
            "❌ <b>Download Timeout</b>\n\n"
            "The download took too long. Please try again with a shorter video.",
            parse_mode="HTML"
        )
        
        # ✅ FIX: Send error notification to admin
        await send_log(
            f"⏱ Download Timeout\n"
            f"User: `{callback.from_user.id}`\n"
            f"URL: {url}\n"
            f"Type: {download_type}",
            bot=callback.bot
        )
        
        await state.clear()
        return
    
    if result["status"] == "error":
        # Security: Escape error message to prevent XSS
        safe_message = escape(result.get('message', 'Unknown error'))
        await callback.message.edit_text(f"❌ <b>Error:</b> {safe_message}", parse_mode="HTML")
        
        # ✅ FIX: Send error notification to admin with detailed info
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
    
    # Security: Escape title and other user-controllable data (XSS prevention)
    safe_title = escape(str(result.get('title', 'Unknown')))
    safe_duration = escape(str(result.get('duration', 0)))
    
    caption = (
        f"✅ <b>Downloaded Successfully!</b>\n"
        f"📌 Title: {safe_title}\n"
        f"⏱ Duration: {safe_duration}s\n"
        f"🤖 via @ravi_downloader_bot"
    )

    try:
        await callback.message.edit_text("📤 <b>Uploading...</b>", parse_mode="HTML")
        
        file_input = FSInputFile(file_path)
        
        if download_type == "audio":
            await callback.message.answer_audio(file_input, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer_video(file_input, caption=caption, parse_mode="HTML")
            
        # Update stats for free users
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        if user_data.get("status") == "free":
            await db.increment_download(user_id)
        
        # ✅ FIX: Send success notification to admin
        await send_log(
            f"✅ Download Success\n"
            f"User: {callback.from_user.full_name} (`{user_id}`)\n"
            f"Title: {safe_title}\n"
            f"Type: {download_type}",
            bot=callback.bot
        )
            
        await callback.message.delete()
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await callback.message.edit_text("❌ Failed to upload file. It might be too large for Telegram.")
        
        # ✅ FIX: Send upload error to admin
        await send_log(
            f"❌ Upload Error\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)}",
            bot=callback.bot
        )
    finally:
        # Security: Use async file removal to avoid blocking event loop
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()

@router.message(F.photo)
async def handle_receipt(message: Message):
    # Security: Escape user-provided caption to prevent XSS
    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_info = f"User: {user_name} (<code>{message.from_user.id}</code>)"
    
    await message.bot.send_photo(
        chat_id=LOG_CHANNEL_ID,
        photo=message.photo[-1].file_id,
        caption=f"🧾 <b>Payment Receipt Received</b>\n\n{user_info}\n📝 Note: {caption}\n\n👉 Use <code>/approve {message.from_user.id}</code> to confirm.",
        parse_mode="HTML"
    )
    
    await message.answer("✅ <b>Receipt Received!</b>\nWe will review it and upgrade your account shortly.", parse_mode="HTML")