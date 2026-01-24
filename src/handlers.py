import asyncio
import logging
import os
from html import escape
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime

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
    """Validate URL for security."""
    if not url:
        return False, "URL is empty"
    
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL too long (max {MAX_URL_LENGTH} characters)"
    
    try:
        parsed = urlparse(url)
        
        if parsed.scheme not in ['http', 'https']:
            return False, "Only HTTP/HTTPS URLs are allowed"
        
        netloc_lower = parsed.netloc.lower()
        if any(blocked in netloc_lower for blocked in ['localhost', '127.0.0.1', '0.0.0.0', '::1', '192.168.', '10.', '172.16.']):
            return False, "Internal URLs are not allowed"
        
        if not any(domain in netloc_lower for domain in ALLOWED_DOMAINS):
            return False, "Platform not supported. Supported: YouTube, TikTok, Facebook, Instagram, Twitter/X"
        
        return True, None
        
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False, "Invalid URL format"


def get_usage_notification(downloads_count: int, status: str) -> dict:
    """
    Generate usage notification message with premium promotion.
    
    Returns: dict with 'text' and 'keyboard'
    """
    remaining = max(0, 10 - downloads_count)
    
    # Get premium stats for slot info
    # Note: This is synchronous, we'll need to make it async in actual use
    
    if status == "premium":
        return {
            "text": (
                "✅ <b>ទាញយករួចរាល់!</b>\n\n"
                "💎 <b>Premium Member</b>\n"
                "♾️ ប្រើបានមិនកំណត់\n\n"
                "<i>អរគុណសម្រាប់ការជឿទុកចិត្ត!</i>"
            ),
            "keyboard": None
        }
    
    # Free user
    if remaining > 0:
        # Calculate percentage
        percentage = (remaining / 10) * 100
        
        # Progress bar
        filled = int(remaining / 2)  # 10 downloads = 5 filled blocks
        empty = 5 - filled
        progress_bar = "🟩" * filled + "⬜" * empty
        
        text = (
            f"📢 <b>ស្ថានភាពការប្រើប្រាស់</b>\n\n"
            f"🎞️ <b>បានទាញយក:</b> {downloads_count}/10\n"
            f"📊 <b>នៅសល់:</b> {remaining} ដងទៀត\n"
            f"{progress_bar} {percentage:.0f}%\n\n"
        )
        
        # Add premium promotion if running low
        if remaining <= 3:
            text += (
                "⚠️ <b>ជិតអស់ហើយ!</b>\n\n"
                "🎉 <b>ទិញ Premium ដើម្បីប្រើបានរហូត!</b>\n"
                "💰 បញ្ចុះតម្លៃ 34%! ~~$3.00~~ → <b>$1.99</b> 🔥\n"
                "⚡ សម្រាប់ 15នាក់ដំបូង (1/15 ទិញរួច)\n\n"
                "<i>បង់ម្តង ប្រើរហូត មិនផុតកំណត់!</i>"
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💎 ទិញឥឡូវនេះ $1.99!",
                    callback_data="buy_premium"
                )]
            ])
        else:
            text += (
                "💡 <b>Tip:</b> ចង់ប្រើមិនកំណត់?\n"
                "Upgrade ទៅ Premium ត្រឹមតែ $1.99! 💎"
            )
            keyboard = None
        
        return {"text": text, "keyboard": keyboard}
    
    # No downloads remaining
    return {
        "text": (
            "🚫 <b>អស់ការទាញយករបស់អ្នកហើយ!</b>\n\n"
            "📊 ប្រើអស់: 10/10 ដង\n\n"
            "🎉 <b>ទិញ Premium ដើម្បីប្រើបានរហូត!</b>\n"
            "💰 បញ្ចុះតម្លៃ 34%! ~~$3.00~~ → <b>$1.99</b> 🔥\n"
            "⚡ សម្រាប់ 15នាក់ដំបូង (1/15 ទិញរួច)\n\n"
            "✅ ទាញយកគ្មានដែនកំណត់\n"
            "✅ Support 24/7\n"
            "✅ ល្បឿនរហ័ស\n\n"
            "<i>បង់ម្តង ប្រើរហូត!</i>"
        ),
        "keyboard": InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💎 ទិញឥឡូវនេះ $1.99!",
                callback_data="buy_premium"
            )]
        ])
    }


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

    status = user_data.get("status")
    downloads_count = user_data.get("downloads_count", 0)
    
    if status == "premium":
        status_icon = "💎"
        status_text = "PREMIUM"
        downloads_text = "Unlimited ♾️"
    else:
        status_icon = "🆓"
        status_text = "FREE"
        remaining = max(0, 10 - downloads_count)
        downloads_text = f"{remaining}/10 នៅសល់"
    
    text = (
        f"👋 <b>សួស្តី {message.from_user.full_name}!</b>\n\n"
        f"ខ្ញុំអាចទាញយក videos ពី TikTok, FB, IG, YouTube។\n"
        f"គ្រាន់តែផ្ញើ link មកខ្ញុំ!\n\n"
        f"📊 <b>ស្ថានភាពរបស់អ្នក:</b> {status_text} {status_icon}\n"
        f"⬇️ <b>ការទាញយក:</b> {downloads_text}"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    status = user_data.get("status")
    count = user_data.get("downloads_count", 0)
    
    if status == "premium":
        status_display = "PREMIUM 💎"
        downloads_display = "Unlimited ♾️"
        usage_note = "✨ <i>អ្នកជា Premium member រីករាយជាមួយការទាញយកមិនកំណត់!</i>"
    else:
        status_display = "FREE 🆓"
        remaining = max(0, 10 - count)
        downloads_display = f"{remaining}/10 នៅសល់"
        usage_note = (
            "⚠️ <i>កំណត់: 10 downloads។ ចង់បានមិនកំណត់?</i>\n\n"
            "💎 <b>Upgrade ទៅ Lifetime Premium $1.99!</b>\n"
            "• បង់ម្តង ប្រើរហូត\n"
            "• គ្មានការបង់ប្រចាំខែ\n"
            "• ទាញយកមិនកំណត់\n\n"
            "ចុច /start ហើយជ្រើសរើស Premium!"
        )
    
    text = (
        f"📊 <b>ស្ថិតិការប្រើប្រាស់</b>\n\n"
        f"👤 អ្នកប្រើ: {message.from_user.full_name}\n"
        f"🏷 ស្ថានភាព: <b>{status_display}</b>\n"
        f"📥 ការទាញយក: <b>{downloads_display}</b>\n\n"
        f"{usage_note}"
    )
        
    await message.answer(text, parse_mode="HTML")


# ... (Keep all admin commands: /broadcast, /broadcast_promo, /stats, /approve as before)


@router.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    if user_data.get("status") == "free" and user_data.get("downloads_count") >= 10:
        await message.answer(
            "🚫 <b>អស់ការទាញយករបស់អ្នកហើយ!</b>\n\n"
            "អ្នកបានប្រើអស់ការទាញយក 10 ដងរបស់អ្នក។\n"
            "សូម upgrade ទៅ Premium ដើម្បីបន្ត។\n\n"
            "💎 <b>ទិញ Premium:</b> ផ្ញើរូបវិក័យបត្រមកទីនេះ។",
            parse_mode="HTML"
        )
        return

    url = message.text.strip()
    
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>Invalid URL</b>\n\n{escape(error_msg or 'Unknown error')}",
            parse_mode="HTML"
        )
        return
    
    # ✅ NEW: Store the URL message ID for later deletion
    await state.update_data(url=url, url_message_id=message.message_id)
    await state.set_state(DownloadState.waiting_for_format)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video (MP4)", callback_data="fmt_video"),
            InlineKeyboardButton(text="🎵 Audio (M4A)", callback_data="fmt_audio")
        ]
    ])
    
    format_msg = await message.answer("👇 ជ្រើសរើសប្រភេទទាញយក:", reply_markup=keyboard)
    
    # ✅ NEW: Store format message ID for deletion
    await state.update_data(format_message_id=format_msg.message_id)


@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None
    
    if not url:
        await callback.message.edit_text("⚠️ Session ផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត។")
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"
    
    progress_msg = await callback.message.edit_text(
        f"⏳ <b>កំពុងទាញយក {download_type.upper()}...</b>\n"
        f"<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML"
    )
    
    try:
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Download timeout for URL: {url}")
        await progress_msg.edit_text(
            "❌ <b>ការទាញយកយូរពេកហើយ</b>\n\n"
            "សូមព្យាយាមម្តងទៀតជាមួយ video ខ្លីជាងនេះ។",
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
    
    if result["status"] == "error":
        safe_message = escape(result.get('message', 'Unknown error'))
        await progress_msg.edit_text(f"❌ <b>Error:</b> {safe_message}", parse_mode="HTML")
        
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
    
    safe_title = escape(str(result.get('title', 'Unknown')))
    safe_duration = escape(str(result.get('duration', 0)))
    
    caption = (
        f"✅ <b>ទាញយករួចរាល់!</b>\n"
        f"📌 ចំណងជើង: {safe_title}\n"
        f"⏱ រយៈពេល: {safe_duration}s\n"
        f"🤖 via @ravi_downloader_bot"
    )

    try:
        await progress_msg.edit_text("📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")
        
        file_input = FSInputFile(file_path)
        
        if download_type == "audio":
            await callback.message.answer_audio(file_input, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer_video(file_input, caption=caption, parse_mode="HTML")
        
        # ✅ NEW: Delete URL message and format selection message
        try:
            if url_message_id:
                await callback.bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=url_message_id
                )
                logger.info(f"Deleted URL message {url_message_id}")
        except Exception as e:
            logger.warning(f"Could not delete URL message: {e}")
        
        try:
            if format_message_id:
                await callback.bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=format_message_id
                )
                logger.info(f"Deleted format message {format_message_id}")
        except Exception as e:
            logger.warning(f"Could not delete format message: {e}")
        
        # Delete progress message
        await progress_msg.delete()
        
        # Update stats for free users
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        
        if user_data.get("status") == "free":
            await db.increment_download(user_id)
            
            # ✅ NEW: Get updated user data and show usage notification
            updated_user_data, _ = await db.get_user(user_id)
            downloads_count = updated_user_data.get("downloads_count", 0)
            status = updated_user_data.get("status", "free")
            
            notification = get_usage_notification(downloads_count, status)
            
            await callback.message.answer(
                notification["text"],
                parse_mode="HTML",
                reply_markup=notification["keyboard"]
            )
        else:
            # Premium user - simple success message
            notification = get_usage_notification(0, "premium")
            await callback.message.answer(
                notification["text"],
                parse_mode="HTML"
            )
            
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await callback.message.answer(
            "❌ មិនអាចបញ្ជូន file បានទេ។ វាអាចធំពេក។"
        )
        
        await send_log(
            f"❌ Upload Error\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)}",
            bot=callback.bot
        )
    finally:
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()


@router.message(F.photo)
async def handle_receipt(message: Message):
    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_info = f"User: {user_name} (<code>{message.from_user.id}</code>)"
    
    await message.bot.send_photo(
        chat_id=LOG_CHANNEL_ID,
        photo=message.photo[-1].file_id,
        caption=f"🧾 <b>Payment Receipt Received</b>\n\n{user_info}\n📝 Note: {caption}\n\n👉 Use <code>/approve {message.from_user.id}</code> to confirm.",
        parse_mode="HTML"
    )
    
    await message.answer(
        "✅ <b>ទទួលវិក័យបត្ររួចរាល់!</b>\n"
        "យើងនឹងពិនិត្យហើយ upgrade គណនីរបស់អ្នកក្នុងពេលឆាប់ៗ។",
        parse_mode="HTML"
    )


# ✅ Keep all other handlers: /broadcast, /broadcast_promo, /stats, /approve
# (Copy from your original file - they remain unchanged)