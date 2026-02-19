import asyncio
import logging
import os
from html import escape
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto,
)
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


# ─────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────

class DownloadState(StatesGroup):
    waiting_for_format = State()


class ReportState(StatesGroup):
    waiting_for_report = State()


# ─────────────────────────────────────────────
# Helper: Friendly Error Messages
# ─────────────────────────────────────────────

def friendly_download_error(url: str, err: str) -> str:
    """Map raw downloader errors to user-friendly Khmer messages."""
    u = (url or "").lower()
    e = (err or "").lower()

    def platform_name() -> str:
        if "tiktok.com" in u:
            return "TikTok"
        if "youtube.com" in u or "youtu.be" in u:
            return "YouTube"
        if "facebook.com" in u or "fb.watch" in u:
            return "Facebook"
        if "instagram.com" in u:
            return "Instagram"
        if "pinterest" in u or "pin.it" in u:
            return "Pinterest"
        return "វេទិកា"

    plat = platform_name()

    privacy_markers = (
        "cannot download this facebook video",
        "private", "friends-only", "members", "group",
        "this content isn't available", "content isn't available",
        "not available", "video unavailable", "unavailable",
        "has been removed", "deleted",
    )
    login_markers = (
        "login", "sign in", "need cookies", "cookies.txt",
        "confirm your age", "age-restricted",
    )
    geo_markers = (
        "not available in your country", "regional",
        "geo", "country", "location",
    )
    copyright_markers = ("copyright", "claimed", "blocked")

    if any(m in e for m in privacy_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            "នេះជាវីដេអូ <b>Private</b> (ឬ Friends-only/Group-private) "
            "ហើយ <b>ខុសគោលការណ៍របស់ Bot</b> "
            "ដូច្នេះ Bot <b>មិនអាចទាញយកបាន</b>។\n\n"
            f"✅ សូមផ្ញើ Link វីដេអូដែលជា <b>Public</b> ពី {plat} មកវិញ។"
        )
    if any(m in e for m in login_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            f"វីដេអូនេះមានការកំណត់ <b>Age-restricted/Login required</b> "
            f"ពី {plat}។ Bot មិនអាចទាញយកវីដេអូប្រភេទនេះបានទេ។\n\n"
            "✅ សូមសាកល្បងវីដេអូ <b>Public</b> ផ្សេង "
            "ឬប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin។"
        )
    if any(m in e for m in geo_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            f"វីដេអូនេះអាចមានការកំណត់ <b>តំបន់/ប្រទេស</b> ពី {plat}។\n\n"
            "✅ សូមសាកល្បង Link ផ្សេង "
            "ឬប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin។"
        )
    if any(m in e for m in copyright_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            "វីដេអូនេះអាចជាវីដេអូដែលមាន <b>Copyright/Blocked</b> "
            "ហើយស្ថិតក្រៅគោលការណ៍ Bot។\n\n"
            "✅ សូមសាកល្បង Link ផ្សេង។"
        )
    return (
        "❌ <b>មានបញ្ហាក្នុងការទាញយក</b>\n\n"
        "សូមព្យាយាមម្តងទៀត ឬផ្ញើ Link ផ្សេង។ "
        "បើបញ្ហានេះកើតឡើងជាបន្តបន្ទាប់ "
        "សូមប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin។"
    )


# ─────────────────────────────────────────────
# Helper: Keyboards
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Helper: Message Deletion
# ─────────────────────────────────────────────

async def safe_delete_message(
    bot: Bot, chat_id: int, message_id: int
) -> bool:
    """Delete a Telegram message without raising exceptions."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message to delete not found" in err:
            return True
        if "message can't be deleted" in err:
            logger.warning(f"⚠️ Cannot delete message {message_id}")
            return False
        logger.error(f"❌ Error deleting message {message_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error deleting message {message_id}: {e}")
        return False


# ─────────────────────────────────────────────
# Helper: Daily Limit Check
# ─────────────────────────────────────────────

def check_daily_limit(
    user_data: dict,
) -> tuple[bool, str, InlineKeyboardMarkup | None]:
    """
    Check if a free user has exceeded their daily download quota.

    Returns:
        (can_download, info_message, keyboard_or_None)
    """
    if user_data.get("status") == "premium":
        return True, "", None

    last_download_date = user_data.get("last_download_date")
    daily_count = user_data.get("daily_download_count", 0)
    today = datetime.now(timezone.utc).date()

    # Reset counter if it's a new day
    if not last_download_date or last_download_date.date() != today:
        return True, "", None

    if daily_count >= FREE_DAILY_LIMIT:
        return (
            False,
            (
                f"🚫 <b>អស់ការទាញយកប្រចាំថ្ងៃរបស់អ្នកហើយ!</b>\n\n"
                f"📊 កំណត់: {FREE_DAILY_LIMIT} ដង/ថ្ងៃ\n"
                f"⏰ សូមព្យាយាមម្តងទៀតនៅថ្ងៃស្អែក\n\n"
                f"💎 <b>ចង់ប្រើមិនកំណត់?</b>\n"
                f"Upgrade ទៅ Premium តម្លៃ <b>${PREMIUM_PRICE:.2f}</b> "
                f"(បង់តែម្តង)"
            ),
            premium_buy_keyboard(),
        )

    remaining = FREE_DAILY_LIMIT - daily_count
    return True, f"📊 នៅសល់: {remaining}/{FREE_DAILY_LIMIT} ដងសម្រាប់ថ្ងៃនេះ", None


# ─────────────────────────────────────────────
# Helper: Usage Notification
# ─────────────────────────────────────────────

def get_usage_notification(user_data: dict) -> dict:
    """Build post-download usage summary message."""
    if user_data.get("status") == "premium":
        return {
            "text": (
                "✅ <b>ទាញយករួចរាល់!</b>\n\n"
                "💎 <b>សមាជិកពិសេស Premium</b>\n"
                "♾️ ទាញយកបានមិនកំណត់\n"
                "🚀 ល្បឿនលឿនបំផុត\n"
                "🎬 គុណភាព 1080p\n\n"
                "<i>អរគុណសម្រាប់ការជឿទុកចិត្ត!</i>"
            ),
            "keyboard": None,
        }

    daily_count = user_data.get("daily_download_count", 0)
    remaining = max(0, FREE_DAILY_LIMIT - daily_count)
    filled = int((daily_count / FREE_DAILY_LIMIT) * 5)
    progress_bar = "🟩" * filled + "⬜" * (5 - filled)

    text = (
        f"📢 <b>ស្ថានភាពការទាញយក</b>\n\n"
        f"🎞️ <b>ទាញយកថ្ងៃនេះ:</b> {daily_count}/{FREE_DAILY_LIMIT}\n"
        f"📊 <b>នៅសល់:</b> {remaining} ដងទៀត\n"
        f"{progress_bar}\n"
        f"🎬 គុណភាព: {FREE_MAX_QUALITY}\n\n"
        "💎 <b>Premium (បង់តែម្តង)</b>\n"
        "• ទាញយកមិនកំណត់ ♾️\n"
        "• គុណភាព 1080p 🎬\n"
        "• ល្បឿនលឿន 🚀\n"
        f"• តម្លៃ: <b>${PREMIUM_PRICE:.2f}</b>"
    )
    return {"text": text, "keyboard": premium_buy_keyboard()}


# ─────────────────────────────────────────────
# Commands: /start
# ─────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_data, is_new = await db.get_user(user_id)

    if is_new:
        await send_log(
            f"🆕 New User: {escape(message.from_user.full_name)} "
            f"(<code>{user_id}</code>)",
            bot=message.bot,
        )

    status = user_data.get("status", "free")
    welcome = f"👋 <b>សួស្តី {escape(message.from_user.full_name)}!</b>\n\n"
    welcome += (
        "🤖 <b>អ្វីដែលបតអាចធ្វើបាន:</b>\n"
        "✅ ទាញយកវីដេអូពីវេទិកាល្បីៗ\n"
        "✅ គាំទ្រ: TikTok, Facebook, YouTube, Instagram, Pinterest\n"
        "✅ ទាញយកជា Video ឬ Audio\n\n"
        "🚫 <b>កំណត់:</b>\n"
        "❌ មិនគាំទ្រវីដេអូ Private\n"
        "❌ មិនគាំទ្រវីដេអូ Copyright\n"
        "❌ ទំហំតូចជាង 49MB\n\n"
    )

    if status == "premium":
        welcome += (
            "💎 <b>ស្ថានភាព: PREMIUM</b>\n\n"
            "♾️ ទាញយកបានមិនកំណត់\n"
            "🎬 គុណភាព 1080p\n"
            "🚀 ល្បឿនលឿនបំផុត\n\n"
            "<i>គ្រាន់តែផ្ញើ link ហើយខ្ញុំទាញយកឱ្យ!</i>"
        )
        await message.answer(welcome, parse_mode="HTML")
    else:
        daily_count = user_data.get("daily_download_count", 0)
        remaining = max(0, FREE_DAILY_LIMIT - daily_count)
        welcome += (
            "🆓 <b>ស្ថានភាព: ឥតគិតថ្លៃ</b>\n\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (នៅសល់: {remaining})\n"
            f"• គុណភាព: {FREE_MAX_QUALITY}\n\n"
            f"💎 Premium: <b>${PREMIUM_PRICE:.2f}</b> (បង់តែម្តង)\n"
            "<i>ផ្ញើ link ហើយជ្រើស Video/Audio!</i>"
        )
        await message.answer(
            welcome, parse_mode="HTML", reply_markup=premium_buy_keyboard()
        )


# ─────────────────────────────────────────────
# Commands: /plan
# ─────────────────────────────────────────────

@router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    status = user_data.get("status", "free")

    if status == "premium":
        text = (
            f"📊 <b>ព័ត៌មានគណនី</b>\n\n"
            f"👤 {escape(message.from_user.full_name)}\n"
            f"🏷 ស្ថានភាព: <b>PREMIUM 💎</b>\n\n"
            "♾️ ទាញយកមិនកំណត់\n"
            "🎬 គុណភាព 1080p\n"
            "🚀 ល្បឿនលឿន\n\n"
            "<i>អរគុណ! ❤️</i>"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        daily_count = user_data.get("daily_download_count", 0)
        remaining = max(0, FREE_DAILY_LIMIT - daily_count)
        text = (
            f"📊 <b>ព័ត៌មានគណនី</b>\n\n"
            f"👤 {escape(message.from_user.full_name)}\n"
            f"🏷 ស្ថានភាព: <b>ឥតគិតថ្លៃ 🆓</b>\n\n"
            f"• {FREE_DAILY_LIMIT} ដង/ថ្ងៃ (នៅសល់: {remaining})\n"
            f"• គុណភាព: {FREE_MAX_QUALITY}\n\n"
            f"💎 Premium: <b>${PREMIUM_PRICE:.2f}</b> (បង់តែម្តង)\n"
            "• ♾️ មិនកំណត់ | 🎬 1080p | 🚀 លឿន"
        )
        await message.answer(
            text, parse_mode="HTML", reply_markup=premium_buy_keyboard()
        )


# ─────────────────────────────────────────────
# Commands: /report
# ─────────────────────────────────────────────

@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.set_state(ReportState.waiting_for_report)
    await message.answer(
        "📩 <b>សូមវាយសារជូនដំណឹង!</b>\n\n"
        "សរសេរសាររបស់អ្នកនៅទីនេះ ហើយផ្ញើមកខ្ញុំ។",
        parse_mode="HTML",
    )


@router.message(ReportState.waiting_for_report, F.text)
async def handle_report(message: Message, state: FSMContext):
    report_text = (message.text or "").strip()
    if not report_text:
        await message.answer("⚠️ សូមវាយសារជូនដំណឹង។")
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
        f"📝 <b>Message:</b>\n{escape(report_text)}"
    )

    try:
        await message.bot.send_message(
            chat_id=REPORT_CHANNEL_ID,
            text=payload,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.answer("✅ បានផ្ញើ report ទៅ Admin រួចរាល់។")
    except Exception as e:
        logger.error(f"Failed to send report: {e}")
        await message.answer("❌ មិនអាចផ្ញើ report បានទេ។ សូមព្យាយាមម្តងទៀត។")
    finally:
        await state.clear()


@router.message(ReportState.waiting_for_report)
async def handle_report_non_text(message: Message):
    await message.answer(
        "⚠️ សូមផ្ញើជា <b>អត្ថបទ</b> ដើម្បីជូនដំណឹង។",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# URL Handler → Format Selection
# ─────────────────────────────────────────────

@router.message(F.text.regexp(r"(https?://[^\s]+)"))
async def handle_link(message: Message, state: FSMContext):
    """Validate URL and ask user to choose Video or Audio format."""
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)

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

    await state.update_data(url=url, url_message_id=message.message_id)
    await state.set_state(DownloadState.waiting_for_format)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 វីដេអូ (MP4)", callback_data="fmt_video"
                ),
                InlineKeyboardButton(
                    text="🎵 អូឌីយ៉ូ (MP3)", callback_data="fmt_audio"
                ),
            ]
        ]
    )

    info_text = "👇 សូមជ្រើសរើសប្រភេទ:\n\n"
    if limit_msg:
        info_text += f"<i>{limit_msg}</i>"

    format_msg = await message.answer(
        info_text, reply_markup=keyboard, parse_mode="HTML"
    )
    await state.update_data(format_message_id=format_msg.message_id)


# ─────────────────────────────────────────────
# Download Callback Handler
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    """Handle Video/Audio format selection and execute download."""
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None

    if not url:
        await callback.message.edit_text(
            "⚠️ សម័យផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត។"
        )
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"

    progress_msg = await callback.message.edit_text(
        f"⏳ <b>កំពុងទាញយក {download_type.upper()}...</b>\n"
        "<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML",
    )

    # ── Execute download with timeout ────────────────────────────
    try:
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(f"⏱ Download timeout: {url}")
        await progress_msg.edit_text(
            "❌ <b>ការទាញយកយូរពេកហើយ</b>\n\n"
            "សូមព្យាយាមជាមួយវីដេអូខ្លីជាងនេះ។",
            parse_mode="HTML",
        )
        await send_log(
            f"⏱ Timeout\nUser: <code>{callback.from_user.id}</code>\n"
            f"URL: {url}\nType: {download_type}",
            bot=callback.bot,
        )
        await state.clear()
        return

    # ── Handle download errors ───────────────────────────────────
    if result["status"] == "error":
        raw_error = str(result.get("message", "Unknown error"))
        await progress_msg.edit_text(
            friendly_download_error(url, raw_error), parse_mode="HTML"
        )
        await send_log(
            f"❌ Download Error\n"
            f"User: {escape(callback.from_user.full_name)} "
            f"(<code>{callback.from_user.id}</code>)\n"
            f"URL: {url}\nType: {download_type}\nError: {raw_error[:300]}",
            bot=callback.bot,
        )
        await state.clear()
        return

    # ── TikTok Slideshow ─────────────────────────────────────────
    if (
        result.get("media_kind") == "slideshow"
        and isinstance(result.get("file_paths"), list)
    ):
        await progress_msg.edit_text("📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")

        paths = [
            p
            for p in result.get("file_paths", [])
            if isinstance(p, str) and os.path.exists(p)
        ]

        if not paths:
            await progress_msg.edit_text(
                "❌ <b>មិនអាចរកឃើញរូបភាពបានទេ</b>", parse_mode="HTML"
            )
            await state.clear()
            return

        safe_title = escape(str(result.get("title", "TikTok Photo")))
        caption = (
            f"✅ <b>ទាញយករួចរាល់!</b>\n"
            f"📌 {safe_title}\n"
            "🤖 @ravi_downloader_bot"
        )

        # Telegram media groups: max 10 per batch
        for i in range(0, len(paths), 10):
            chunk = paths[i : i + 10]
            media = [
                InputMediaPhoto(
                    media=FSInputFile(p),
                    caption=(caption if i == 0 and j == 0 else None),
                    parse_mode=("HTML" if i == 0 and j == 0 else None),
                )
                for j, p in enumerate(chunk)
            ]
            await callback.message.answer_media_group(media)

        # Cleanup UI messages
        chat_id = callback.message.chat.id
        for mid in [url_message_id, format_message_id]:
            if mid:
                await safe_delete_message(callback.bot, chat_id, mid)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        # ✅ FIX 4.1: Record download AFTER successful send
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        if user_data.get("status") != "premium":
            updated = await db.record_download(user_id)
            notification = get_usage_notification(updated)
        else:
            notification = get_usage_notification(user_data)

        await callback.message.answer(
            notification["text"],
            parse_mode="HTML",
            reply_markup=notification["keyboard"],
        )

        # Remove image files + empty folder
        for p in paths:
            await safe_remove_file(p)
        try:
            # ✅ FIX 1.3: Guard against empty paths list
            if paths:
                folder = os.path.dirname(paths[0])
                if folder and os.path.isdir(folder) and not os.listdir(folder):
                    os.rmdir(folder)
        except Exception:
            pass

        await state.clear()
        return

    # ── Regular Video / Audio File ───────────────────────────────
    file_path = result["file_path"]

    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            await progress_msg.edit_text(
                f"❌ <b>ឯកសារធំពេកសម្រាប់ Telegram</b>\n\n"
                f"📊 ទំហំ: {file_size / 1024 / 1024:.1f}MB\n"
                f"⚠️ កំណត់: {MAX_FILE_SIZE / 1024 / 1024:.0f}MB\n\n"
                "សូមព្យាយាមវីដេអូគុណភាពទាបជាង ឬជ្រើស Audio។",
                parse_mode="HTML",
            )
            await safe_remove_file(file_path)
            await state.clear()
            return

    safe_title = escape(str(result.get("title", "Unknown")))
    safe_duration = escape(str(result.get("duration", 0)))
    caption = (
        f"✅ <b>ទាញយករួចរាល់!</b>\n"
        f"📌 {safe_title}\n"
        f"⏱ {safe_duration}វិ\n"
        "🤖 @ravi_downloader_bot"
    )

    try:
        await progress_msg.edit_text("📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")

        file_input = FSInputFile(file_path)
        if download_type == "audio":
            await callback.message.answer_audio(
                file_input, caption=caption, parse_mode="HTML"
            )
        else:
            await callback.message.answer_video(
                file_input, caption=caption, parse_mode="HTML"
            )

        # Cleanup UI messages
        chat_id = callback.message.chat.id
        for mid in [url_message_id, format_message_id]:
            if mid:
                await safe_delete_message(callback.bot, chat_id, mid)
        try:
            await progress_msg.delete()
        except Exception:
            pass

        # ✅ FIX 4.1: Record download AFTER successful Telegram send only
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        if user_data.get("status") != "premium":
            updated = await db.record_download(user_id)
            notification = get_usage_notification(updated)
        else:
            notification = get_usage_notification(user_data)

        await callback.message.answer(
            notification["text"],
            parse_mode="HTML",
            reply_markup=notification["keyboard"],
        )

    except TelegramBadRequest as e:
        err_str = str(e).lower()
        if "file is too big" in err_str or "too large" in err_str:
            error_msg = (
                "❌ <b>ឯកសារធំពេក</b>\n\n"
                "⚠️ Telegram កំណត់: 50MB\n"
                "សូមជ្រើស Audio ឬ Link វីដេអូខ្លីជាង។"
            )
        elif "wrong file identifier" in err_str:
            error_msg = "❌ ទម្រង់ឯកសារខុស។ សូមព្យាយាមម្តងទៀត។"
        else:
            error_msg = (
                f"❌ មិនអាចបញ្ជូនបានទេ។\n\n"
                f"<code>{escape(str(e)[:200])}</code>"
            )
        await callback.message.answer(error_msg, parse_mode="HTML")
        await send_log(
            f"❌ Upload Error (Telegram)\n"
            f"User: <code>{callback.from_user.id}</code>\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot,
        )

    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        await callback.message.answer(
            f"❌ មានបញ្ហា upload ។\n\n<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        await send_log(
            f"❌ Upload Error (General)\n"
            f"User: <code>{callback.from_user.id}</code>\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot,
        )

    finally:
        # Always cleanup downloaded file regardless of outcome
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()


# ─────────────────────────────────────────────
# Admin Commands
# ─────────────────────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Admin: Broadcast a message to all users."""
    # ✅ FIX 2.2: Respond with unknown command instead of silent return
    if message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ រកមិនឃើញពាក្យបញ្ជានេះទេ។")
        return

    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await message.answer(
            "⚠️ <b>របៀបប្រើ:</b> /broadcast [សារ]\n\n"
            "<b>ឧទាហរណ៍:</b>\n"
            "/broadcast 🔧 Bot កំពុងថែទាំ 30 នាទី។",
            parse_mode="HTML",
        )
        return

    # Validate HTML syntax with a preview send to admin
    preview_text = (
        "📢 <b>សេចក្តីជូនដំណឹង</b>\n\n"
        f"{text}\n\n"
        "<i>សារផ្លូវការពី Admin Bot</i>"
    )
    try:
        preview = await message.bot.send_message(
            chat_id=ADMIN_ID,
            text=preview_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        try:
            await message.bot.delete_message(
                chat_id=ADMIN_ID, message_id=preview.message_id
            )
        except Exception:
            pass
    except TelegramBadRequest as te:
        if "can't parse entities" in str(te).lower():
            await message.answer(
                "❌ <b>Tag HTML មិនត្រឹមត្រូវ</b>\n\n"
                "ពិនិត្យ <b>&lt;b&gt;</b>, <b>&lt;i&gt;</b> "
                "ឲ្យបិទ tag ត្រឹមត្រូវ។",
                parse_mode="HTML",
            )
            return
        raise

    all_users = await db.list_users()
    total = len(all_users)
    success = failed = 0

    progress_msg = await message.answer(
        f"📢 <b>កំពុងផ្សាយ...</b>\nសរុប: {total}",
        parse_mode="HTML",
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
                    f"✅ {success} | ❌ {failed} | {idx}/{total}",
                    parse_mode="HTML",
                )
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast failed for {user_id}: {e}")

    await progress_msg.edit_text(
        f"✅ <b>ផ្សាយរួចរាល់!</b>\n\n"
        f"📊 សរុប: {total}\n✅ {success} | ❌ {failed}",
        parse_mode="HTML",
    )
    await send_log(
        f"📢 Broadcast done: {success}/{total}", bot=message.bot
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Admin: View bot statistics."""
    # ✅ FIX 2.2: Respond instead of silent return
    if message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ រកមិនឃើញពាក្យបញ្ជានេះទេ។")
        return

    try:
        stats = await db.count_users()
        total_downloads = await db.total_downloads()
        revenue = stats["premium"] * PREMIUM_PRICE

        text = (
            f"📊 <b>ស្ថិតិបត</b>\n\n"
            f"👥 សរុប: <b>{stats['total']}</b>\n"
            f"💎 Premium: <b>{stats['premium']}</b>\n"
            f"🆓 Free: <b>{stats['free']}</b>\n\n"
            f"⬇️ Downloads: <b>{total_downloads}</b>\n\n"
            f"💰 Revenue: <b>${revenue:.2f}</b>\n\n"
            f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Stats error: {e}")
        await message.answer(f"❌ Error: {escape(str(e))}", parse_mode="HTML")


@router.message(Command("approve"))
async def cmd_approve(message: Message):
    """Admin: Grant premium status to a user."""
    # ✅ FIX 2.2: Respond instead of silent return
    if message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ រកមិនឃើញពាក្យបញ្ជានេះទេ។")
        return

    try:
        target_id = int(message.text.split()[1])
        success = await db.set_premium(target_id)

        if success:
            await message.answer(f"✅ User {target_id} → PREMIUM ហើយ។")
            await message.bot.send_message(
                target_id,
                "🎉 <b>អបអរសាទរ!</b> គណនីរបស់អ្នក Upgrade ទៅ PREMIUM ហើយ! 💎",
                parse_mode="HTML",
            )
            await send_log(
                f"👮 Admin approved Premium: <code>{target_id}</code>",
                bot=message.bot,
            )
        else:
            await message.answer("❌ Update បរាជ័យ។ សូមពិនិត្យ ID។")

    except (IndexError, ValueError):
        await message.answer("⚠️ ប្រើ: /approve [user_id]")


# ─────────────────────────────────────────────
# Payment Handlers
# ─────────────────────────────────────────────

@router.callback_query(F.data == "buy_premium")
async def handle_buy_premium(callback: CallbackQuery):
    """Show QR payment image."""
    payment_qr_path = "payment.jpg"

    if not os.path.exists(payment_qr_path):
        await callback.message.edit_text(
            "❌ <b>រកមិនឃើញ QR ទូទាត់!</b>\n\nទាក់ទង Admin។",
            parse_mode="HTML",
        )
        logger.error("payment.jpg not found!")
        return

    caption = (
        f"💳 <b>Premium (បង់តែម្តង)</b>\n\n"
        f"💎 តម្លៃ: <b>${PREMIUM_PRICE:.2f}</b>\n"
        "♾️ ទាញយកមិនកំណត់ + 1080p\n\n"
        "📱 <b>របៀបបង់:</b>\n"
        "1️⃣ ស្កេន QR Code\n"
        f"2️⃣ បង់ <b>${PREMIUM_PRICE:.2f}</b>\n"
        "3️⃣ ថតរូប Screenshot\n"
        "4️⃣ ផ្ញើ Screenshot មកខ្ញុំ\n"
        "5️⃣ រង់ Admin អនុញ្ញាត\n\n"
        f"🆔 User ID: <code>{callback.from_user.id}</code>"
    )

    try:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=FSInputFile(payment_qr_path),
            caption=caption,
            parse_mode="HTML",
        )
        await send_log(
            f"💰 Premium Interest\n"
            f"User: {escape(callback.from_user.full_name)} "
            f"(<code>{callback.from_user.id}</code>)",
            bot=callback.bot,
        )
    except Exception as e:
        logger.error(f"QR show error: {e}")
        await callback.answer("❌ មានបញ្ហា។ ព្យាយាមម្តងទៀត។", show_alert=True)


@router.callback_query(F.data == "premium_info")
async def handle_premium_info(callback: CallbackQuery):
    """Show premium benefits."""
    text = (
        f"💎 <b>Premium ពេញមួយជីវិត</b>\n\n"
        f"💰 <b>តម្លៃ: ${PREMIUM_PRICE:.2f}</b> (បង់តែម្តង)\n\n"
        "✅ ទាញយកមិនកំណត់ ♾️\n"
        "✅ គុណភាព 1080p 🎬\n"
        "✅ ល្បឿនលឿន 🚀\n"
        "✅ គ្រប់វេទិកា\n"
        "✅ ជំនួយអាទិភាព 💬\n\n"
        "<b>បង់តែម្តង — ប្រើរហូត!</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 ទិញ ${PREMIUM_PRICE:.2f}",
                    callback_data="buy_premium",
                )
            ],
            [InlineKeyboardButton(text="❌ បិទ", callback_data="close_info")],
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "close_info")
async def handle_close_info(callback: CallbackQuery):
    await callback.message.delete()


# ─────────────────────────────────────────────
# Receipt Handler (Photo Upload)
# ─────────────────────────────────────────────

@router.message(F.photo)
async def handle_receipt(message: Message):
    """Forward payment receipt photo to log channel."""
    # ✅ FIX 2.3: Guard against LOG_CHANNEL_ID being None
    if not LOG_CHANNEL_ID:
        logger.warning("handle_receipt: LOG_CHANNEL_ID not configured")
        await message.answer(
            "✅ <b>ទទួលបានរូបភាព!</b>\n"
            "សូមទាក់ទង Admin ដោយផ្ទាល់ ព្រោះ channel មិនទាន់ configured។",
            parse_mode="HTML",
        )
        return

    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_id = message.from_user.id

    try:
        await message.bot.send_photo(
            chat_id=LOG_CHANNEL_ID,
            photo=message.photo[-1].file_id,
            caption=(
                "🧾 <b>វិក័យបត្រទូទាត់</b>\n\n"
                f"👤 {user_name}\n"
                f"🆔 <code>{user_id}</code>\n"
                f"📝 {caption}\n\n"
                f"👉 <code>/approve {user_id}</code>"
            ),
            parse_mode="HTML",
        )
        await message.answer(
            "✅ <b>ទទួលវិក័យបត្ររួចរាល់!</b>\n"
            "Admin នឹង Upgrade គណនីអ្នកឆាប់ៗ។",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Receipt forward error: {e}")
        await message.answer(
            "⚠️ មានបញ្ហា។ សូមទាក់ទង Admin ដោយផ្ទាល់។",
            parse_mode="HTML",
        )