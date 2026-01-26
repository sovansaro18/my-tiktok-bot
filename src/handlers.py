import asyncio
import logging
import os
from html import escape
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from src.config import (
    ADMIN_ID,
    LOG_CHANNEL_ID,
    MAX_FILE_SIZE,
    DOWNLOAD_TIMEOUT,
    FREE_DAILY_LIMIT,
    FREE_MAX_QUALITY,
    PREMIUM_PRICE,
    REPORT_CHANNEL_ID,
)
from src.database import db
from src.downloader import downloader
from src.utils import send_log, safe_remove_file
from src.security.validators import validate_and_normalize_url
from src.errors import BotError

router = Router()
logger = logging.getLogger(__name__)


def premium_buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 ទិញ Premium ${PREMIUM_PRICE:.2f}",
                    callback_data="buy_premium",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"ℹ️ ព័ត៌មាន Premium (${PREMIUM_PRICE:.2f})",
                    callback_data="premium_info",
                )
            ],
        ]
    )


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


def check_daily_limit(user_data: dict) -> tuple[bool, str, InlineKeyboardMarkup | None]:
    """
    Check if user has exceeded daily download limit.
    
    Returns:
        (can_download, message)
    """
    status = user_data.get("status", "free")
    
    # Premium users: unlimited
    if status == "premium":
        return True, "", None
    
    # After trial: check daily limit
    last_download_date = user_data.get("last_download_date")
    daily_count = user_data.get("daily_download_count", 0)
    
    today = datetime.now(timezone.utc).date()
    
    # Reset counter if new day
    if not last_download_date or last_download_date.date() != today:
        return True, "", None
    
    # Check if exceeded daily limit
    if daily_count >= FREE_DAILY_LIMIT:
        return False, (
            f"🚫 <b>អស់ការទាញយកប្រចាំថ្ងៃរបស់អ្នកហើយ!</b>\n\n"
            f"📊 កំណត់សម្រាប់អ្នកប្រើឥតគិតថ្លៃ: {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
            f"⏰ សូមព្យាយាមម្តងទៀតនៅថ្ងៃស្អែក\n\n"
            f"💎 <b>ចង់ប្រើមិនកំណត់?</b>\n"
            f"Upgrade ទៅ Premium តម្លៃ <b>${PREMIUM_PRICE:.2f}</b> (បង់តែម្តង)"
        ), premium_buy_keyboard()
    
    remaining = FREE_DAILY_LIMIT - daily_count
    return True, f"📊 នៅសល់: {remaining}/{FREE_DAILY_LIMIT} ដងសម្រាប់ថ្ងៃនេះ", None


def get_usage_notification(user_data: dict) -> dict:
    """Generate usage notification with daily limit info."""
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
    
    text += (
        "💎 <b>Premium (បង់តែម្តង)</b>\n"
        "• ទាញយកមិនកំណត់ ♾️\n"
        "• គុណភាព 1080p 🎬\n"
        "• ល្បឿនលឿន 🚀\n"
        f"• តម្លៃ: <b>${PREMIUM_PRICE:.2f}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 ទិញ Premium ${PREMIUM_PRICE:.2f}",
                    callback_data="buy_premium",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"ℹ️ ព័ត៌មាន Premium (${PREMIUM_PRICE:.2f})",
                    callback_data="premium_info",
                )
            ],
        ]
    )
    
    return {"text": text, "keyboard": keyboard}


class DownloadState(StatesGroup):
    waiting_for_format = State()


class ReportState(StatesGroup):
    waiting_for_report = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Handle /start command with detailed welcome message."""
    await state.clear()
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
        daily_count = user_data.get("daily_download_count", 0)
        remaining = max(0, FREE_DAILY_LIMIT - daily_count)

        welcome += (
            "🆓 <b>ស្ថានភាពរបស់អ្នក: ឥតគិតថ្លៃ</b>\n\n"
            "🎁 <b>អត្ថប្រយោជន៍បច្ចុប្បន្ន:</b>\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (ថ្ងៃនេះនៅសល់: {remaining})\n"
            f"• គុណភាពអតិបរមា: {FREE_MAX_QUALITY}\n"
            "• ល្បឿនមធ្យម\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 <b>Premium (បង់តែម្តង)</b>\n"
            "• ទាញយកមិនកំណត់ ♾️\n"
            "• គុណភាព 1080p 🎬\n"
            "• ល្បឿនលឿន 🚀\n"
            f"• តម្លៃ: <b>${PREMIUM_PRICE:.2f}</b>\n\n"
            "<i>ផ្ញើ link មកខ្ញុំ ហើយជ្រើស Video/Audio ដើម្បីទាញយក</i>"
        )

    if status == "premium":
        await message.answer(welcome, parse_mode="HTML")
    else:
        await message.answer(welcome, parse_mode="HTML", reply_markup=premium_buy_keyboard())


@router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext):
    """Show user plan details."""
    await state.clear()
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
        daily_count = user_data.get("daily_download_count", 0)
        remaining = max(0, FREE_DAILY_LIMIT - daily_count)

        text = (
            f"📊 <b>ព័ត៌មានគណនីរបស់អ្នក</b>\n\n"
            f"👤 ឈ្មោះ: {escape(message.from_user.full_name)}\n"
            f"🏷 ស្ថានភាព: <b>ឥតគិតថ្លៃ 🆓</b>\n\n"
            f"🎁 <b>អត្ថប្រយោជន៍:</b>\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (ថ្ងៃនេះនៅសល់: {remaining})\n"
            f"• គុណភាពអតិបរមា: {FREE_MAX_QUALITY}\n"
            f"• ល្បឿនមធ្យម\n\n"
            f"💎 <b>Premium (បង់តែម្តង):</b>\n"
            f"• ទាញយកមិនកំណត់ ♾️\n"
            f"• គុណភាព 1080p 🎬\n"
            f"• ល្បឿនលឿន 🚀\n"
            f"• តម្លៃ: <b>${PREMIUM_PRICE:.2f}</b>"
        )
        
    if status == "premium":
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=premium_buy_keyboard())


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.set_state(ReportState.waiting_for_report)
    await message.answer(
        "📩 <b>សូមវាយសារជូនដំណឹង!</b>\n\nសរសេរសាររបស់អ្នកនៅទីនេះ ហើយផ្ញើមកខ្ញុំ។",
        parse_mode="HTML",
    )


@router.message(ReportState.waiting_for_report, F.text)
async def handle_report(message: Message, state: FSMContext):
    report_text = (message.text or "").strip()
    if not report_text:
        await message.answer("⚠️ សូមវាយសារជូនដំណឹងមកខ្ញុំ។")
        return

    user_id = message.from_user.id
    full_name = escape(message.from_user.full_name or "")
    username = message.from_user.username
    username_line = f"@{escape(username)}" if username else "(no username)"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    payload = (
        "🆘 <b>Report from User</b>\n\n"
        f"👤 {full_name}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🔗 {username_line}\n"
        f"🕒 {now_str}\n\n"
        "📝 <b>Message:</b>\n"
        f"{escape(report_text)}"
    )

    try:
        await message.bot.send_message(
            chat_id=REPORT_CHANNEL_ID,
            text=payload,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.answer("✅ បានផ្ញើសារជូនដំណឹងទៅ Admin រួចរាល់។")
    except Exception as e:
        logger.error(f"Failed to send report: {e}")
        await message.answer("❌ មិនអាចផ្ញើ report បានទេ។ សូមព្យាយាមម្តងទៀត។")
    finally:
        await state.clear()


@router.message(ReportState.waiting_for_report)
async def handle_report_non_text(message: Message):
    await message.answer("⚠️ សូមផ្ញើជា <b>អត្ថបទ</b> ដើម្បីជូនដំណឹង។", parse_mode="HTML")


@router.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: Message, state: FSMContext):
    """Handle video URL messages."""
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    # Check daily limit for free users
    can_download, limit_msg, limit_kb = check_daily_limit(user_data)
    
    if not can_download:
        await message.answer(limit_msg, parse_mode="HTML", reply_markup=limit_kb)
        return

    raw_url = message.text.strip()
    try:
        url, _platform = validate_and_normalize_url(raw_url)
    except BotError as e:
        await message.answer(
            f"⚠️ <b>URL មិនត្រឹមត្រូវ</b>\n\n{escape(e.user_message)}",
            parse_mode="HTML",
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
        
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        if user_data.get("status") != "premium":
            updated_user_data = await db.record_download(user_id)
            notification = get_usage_notification(updated_user_data)
        else:
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

    # Validate HTML before broadcasting.
    # If HTML is invalid, do not send a fallback with raw tags to users.
    preview_text = (
        f"📢 <b>សេចក្តីជូនដំណឹងពីអ្នកគ្រប់គ្រង</b>\n\n"
        f"{text}\n\n"
        f"<i>នេះជាសារផ្លូវការពីអ្នកគ្រប់គ្រងបត។</i>"
    )
    try:
        preview = await message.bot.send_message(
            chat_id=ADMIN_ID,
            text=preview_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        # Clean up preview to avoid cluttering admin chat
        try:
            await message.bot.delete_message(chat_id=ADMIN_ID, message_id=preview.message_id)
        except Exception:
            pass
    except TelegramBadRequest as te:
        if "can't parse entities" in str(te).lower():
            await message.answer(
                "❌ <b>Tag HTML មិនត្រឹមត្រូវ</b>\n\n"
                "សូមពិនិត្យ <b>&lt;b&gt;...&lt;/b&gt;</b>, <b>&lt;i&gt;...&lt;/i&gt;</b> ឲ្យបិទ tag ត្រឹមត្រូវ។",
                parse_mode="HTML",
            )
            return
        raise
    
    try:
        all_users = await db.list_users()
        
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
                await message.bot.send_message(
                    chat_id=user_id,
                    text=preview_text,
                    parse_mode="HTML",
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
        
        total_downloads = await db.total_downloads()
        
        premium_sold = stats["premium"]
        revenue = premium_sold * PREMIUM_PRICE
        
        text = (
            f"📊 <b>ស្ថិតិបត</b>\n\n"
            f"👥 អ្នកប្រើសរុប: <b>{stats['total']}</b>\n"
            f"💎 Premium: <b>{stats['premium']}</b>\n"
            f"🆓 ឥតគិតថ្លៃ: <b>{stats['free']}</b>\n\n"
            f"⬇️ ការទាញយកសរុប: <b>{total_downloads}</b>\n"
            f"📈 មធ្យមក្នុងមួយអ្នក: <b>{total_downloads // stats['total'] if stats['total'] > 0 else 0}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>ការលក់ Premium:</b>\n"
            f"• តម្លៃ: ${PREMIUM_PRICE:.2f}\n"
            f"• លក់រួច: <b>{premium_sold}</b>\n"
            f"• ប្រាក់ចំណូល: <b>${revenue:.2f}</b>\n\n"
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
        "💳 <b>ទូទាត់ប្រាក់ Premium (បង់តែម្តង)</b>\n\n"
        f"💎 <b>តម្លៃ:</b> <b>${PREMIUM_PRICE:.2f}</b>\n"
        "♾️ <b>ទទួលបាន:</b> ទាញយកមិនកំណត់ + គុណភាព 1080p\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱 <b>របៀបបង់ប្រាក់:</b>\n\n"
        "1️⃣ ស្កេន QR Code ខាងក្រោម\n"
        f"2️⃣ បង់ចំនួន <b>${PREMIUM_PRICE:.2f}</b>\n"
        "3️⃣ ថតរូបវិក័យបត្រ (Screenshot)\n"
        "4️⃣ ផ្ញើវិក័យបត្រមកទីនេះវិញ (ជារូបភាព)\n"
        "5️⃣ រង់ចាំ Admin អនុញ្ញាត (/approve)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "♾️ <b>រយៈពេលសុពលភាព:</b> ពេញមួយជីវិត (មិនផុតកំណត់)\n\n"
        f"🆔 <b>User ID របស់អ្នក:</b> <code>{callback.from_user.id}</code>\n"
        "<i>(សូមរក្សាទុក ID នេះ)</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>អត្ថប្រយោជន៍ Premium:</b>\n"
        "• ទាញយកមិនកំណត់ ♾️\n"
        "• គុណភាព 1080p 🎬\n"
        "• ល្បឿនលឿន 🚀\n"
        "• ជំនួយអាទិភាព 💬"
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
            f"User: {callback.from_user.full_name} (`{callback.from_user.id}`)",
            bot=callback.bot
        )
        
    except Exception as e:
        logger.error(f"Error showing QR: {e}")
        await callback.answer("❌ មានបញ្ហា។ សូមព្យាយាមម្តងទៀត។", show_alert=True)


@router.callback_query(F.data == "premium_info")
async def handle_premium_info(callback: CallbackQuery):
    """Show premium benefits."""

    info_text = (
        "💎 <b>សមាជិកភាព Premium ពេញមួយជីវិត</b>\n\n"
        f"💰 <b>តម្លៃ:</b> <b>${PREMIUM_PRICE:.2f}</b> (បង់តែម្តង)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📥 ការទាញយក:</b>\n"
        "✅ ទាញយកមិនកំណត់ជារៀងរហូត\n"
        "✅ គ្មានការកំណត់ប្រចាំថ្ងៃ/ខែ\n"
        "✅ គាំទ្រគ្រប់វេទិកា\n"
        "✅ គុណភាពខ្ពស់ (រហូតដល់ 1080p)\n\n"
        "<b>⚡ ប្រតិបត្តិការ:</b>\n"
        "🚀 ជួរអាទិភាពក្នុងការទាញយក\n"
        "🚀 ល្បឿនទាញយកលឿន\n\n"
        "<b>🎯 ជំនួយ:</b>\n"
        "💬 ជំនួយអាទិភាព\n\n"
        "<b>💵 ទូទាត់តែម្តង:</b>\n"
        f"• បង់ <b>${PREMIUM_PRICE:.2f}</b> តែម្តង\n"
        "• ប្រើរហូត (មិនផុតកំណត់)"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 ទិញ Premium ${PREMIUM_PRICE:.2f}",
            callback_data="buy_premium",
        )],
        [InlineKeyboardButton(
            text="❌ បិទ",
            callback_data="close_info",
        )],
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
