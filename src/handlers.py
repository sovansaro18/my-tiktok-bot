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
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMemberUpdated

from src.config import (
    ADMIN_ID,
    LOG_CHANNEL_ID,
    MAX_FILE_SIZE,
    DOWNLOAD_TIMEOUT,
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
            "ដូច្នេះ Bot <b>មិនអាចទាញយកបាន</b>。\n\n"
            f"✅ សូមផ្ញើ Link វីដេអូដែលជា <b>Public</b> ពី {plat} មកវិញ។"
        )
    if any(m in e for m in login_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            f"វីដេអូនេះមានការកំណត់ <b>Age-restricted/Login required</b> "
            f"ពី {plat}។ Bot មិនអាចទាញយកវីដេអូប្រភេទនេះបានទេ។\n\n"
            "✅ សូមសាកល្បងវីដេអូ <b>Public</b> ផ្សេង "
            "ឬប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin。"
        )
    if any(m in e for m in geo_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            f"វីដេអូនេះអាចមានការកំណត់ <b>តំបន់/ប្រទេស</b> ពី {plat}។\n\n"
            "✅ សូមសាកល្បង Link ផ្សេង "
            "ឬប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin。"
        )
    if any(m in e for m in copyright_markers):
        return (
            "❌ <b>មិនអាចទាញយកបានទេ</b>\n\n"
            "វីដេអូនេះអាចជាវីដេអូដែលមាន <b>Copyright/Blocked</b> "
            "ហើយស្ថិតក្រៅគោលការណ៍ Bot。\n\n"
            "✅ សូមសាកល្បង Link ផ្សេង。"
        )
    return (
        "❌ <b>មានបញ្ហាក្នុងការទាញយក</b>\n\n"
        "សូមព្យាយាមម្តងទៀត ឬផ្ញើ Link ផ្សេង។ "
        "បើបញ្ហានេះកើតឡើងជាបន្តបន្ទាប់ "
        "សូមប្រើ <b>/report</b> ដើម្បីជូនដំណឹងមក Admin。"
    )


# ─────────────────────────────────────────────
# Helper: Format Selection Keyboard
# ─────────────────────────────────────────────

def format_select_keyboard(platform: str) -> InlineKeyboardMarkup:
    """
    TikTok → 3 buttons (Video / MP3 / Photo)
    Other platforms → 2 buttons (Video / MP3)
    """
    if platform == "tiktok":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎬 វីដេអូ (MP4)", callback_data="fmt_video"
                    ),
                    InlineKeyboardButton(
                        text="🎵 MP3", callback_data="fmt_audio"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🖼️ រូបភាព (Photo)", callback_data="fmt_photo"
                    ),
                ],
            ]
        )
    else:
        return InlineKeyboardMarkup(
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


# ─────────────────────────────────────────────
# Helper: Message Deletion
# ─────────────────────────────────────────────

async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
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

    welcome = (
        f"👋 <b>សួស្តី {escape(message.from_user.full_name)}!</b>\n\n"
        "🤖 <b>អ្វីដែលបតអាចធ្វើបាន:</b>\n"
        "✅ ទាញយកវីដេអូពីវេទិកាល្បីៗ\n"
        "✅ គាំទ្រ: TikTok, Facebook, YouTube, Instagram, Pinterest\n"
        "✅ ទាញយកជា Video, Audio ឬ Photo (TikTok)\n"
        "✅ ប្រើប្រាស់ <b>ឥតគិតថ្លៃ</b> ទាំងស្រុង!\n\n"
        "🚫 <b>កំណត់:</b>\n"
        "❌ មិនគាំទ្រវីដេអូ Private\n"
        "❌ មិនគាំទ្រវីដេអូ Copyright\n"
        "❌ ទំហំអតិបរមា 49MB\n\n"
        "<i>គ្រាន់តែផ្ញើ Link ហើយខ្ញុំទាញយកឱ្យ!</i> 🚀"
    )
    await message.answer(welcome, parse_mode="HTML")


# ─────────────────────────────────────────────
# Commands: /plan
# ─────────────────────────────────────────────

@router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext):
    await state.clear()
    text = (
        f"📊 <b>ព័ត៌មានគណនី</b>\n\n"
        f"👤 {escape(message.from_user.full_name)}\n"
        f"🏷 ស្ថានភាព: <b>ឥតគិតថ្លៃ ✅</b>\n\n"
        "♾️ ទាញយកបានគ្មានកំណត់\n"
        "🎬 Video, Audio, Photo\n"
        "🚀 ប្រើបានភ្លាម គ្មានការចុះឈ្មោះ\n\n"
        "<i>គ្រាន់តែផ្ញើ Link ហើយទាញយកបានជាសំណប!</i>"
    )
    await message.answer(text, parse_mode="HTML")


# ─────────────────────────────────────────────
# Commands: /report
# ─────────────────────────────────────────────

@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.set_state(ReportState.waiting_for_report)
    await message.answer(
        "📩 <b>សូមវាយសារជូនដំណឹង!</b>\n\n"
        "សរសេរសាររបស់អ្នកនៅទីនេះ ហើយផ្ញើមកខ្ញុំ。",
        parse_mode="HTML",
    )


@router.message(ReportState.waiting_for_report, F.text)
async def handle_report(message: Message, state: FSMContext):
    report_text = (message.text or "").strip()
    if not report_text:
        await message.answer("⚠️ សូមវាយសារជូនដំណឹង。")
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
        await message.answer("✅ បានផ្ញើ report ទៅ Admin រួចរាល់。")
    except Exception as e:
        logger.error(f"Failed to send report: {e}")
        await message.answer("❌ មិនអាចផ្ញើ report បានទេ។ សូមព្យាយាមម្តងទៀត。")
    finally:
        await state.clear()


@router.message(ReportState.waiting_for_report)
async def handle_report_non_text(message: Message):
    await message.answer(
        "⚠️ សូមផ្ញើជា <b>អត្ថបទ</b> ដើម្បីជូនដំណឹង。",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# URL Handler → Format Selection
# ─────────────────────────────────────────────

@router.message(F.text.regexp(r"(https?://[^\s]+)"))
async def handle_link(message: Message, state: FSMContext):
    """Validate URL and show format selection buttons."""
    user_id = message.from_user.id
    await db.get_user(user_id)  # Register user if new

    raw_url = message.text.strip()
    try:
        url, _platform = validate_and_normalize_url(raw_url)
    except BotError as e:
        await message.answer(
            f"⚠️ <b>URL មិនត្រឹមត្រូវ</b>\n\n{escape(e.user_message)}",
            parse_mode="HTML",
        )
        return

    await state.update_data(url=url, platform=_platform, url_message_id=message.message_id)
    await state.set_state(DownloadState.waiting_for_format)

    keyboard = format_select_keyboard(_platform)

    info_text = "👇 សូមជ្រើសរើសប្រភេទ:\n\n"
    if _platform == "tiktok":
        info_text += "🎵 <b>MP3</b> — ទាញយកជាសំឡេង\n"
        info_text += "🖼️ <b>Photo</b> — សម្រាប់ TikTok រូបភាព/Slideshow\n"

    format_msg = await message.answer(
        info_text, reply_markup=keyboard, parse_mode="HTML"
    )
    await state.update_data(format_message_id=format_msg.message_id)


# ─────────────────────────────────────────────
# Download Callback Handler
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    """Handle Video/Audio/Photo format selection and execute download."""
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None

    if not url:
        await callback.message.edit_text(
            "⚠️ សម័យផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត。"
        )
        return

    # Determine download type from callback data
    if callback.data == "fmt_audio":
        download_type = "audio"
    elif callback.data == "fmt_photo":
        download_type = "photo"
    else:
        download_type = "video"

    # Label for progress message
    type_label = {
        "audio": "MP3",
        "photo": "PHOTO",
        "video": "VIDEO",
    }.get(download_type, "VIDEO")

    progress_msg = await callback.message.edit_text(
        f"⏳ <b>កំពុងទាញយក {type_label}...</b>\n"
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
            "សូមព្យាយាមជាមួយវីដេអូខ្លីជាងនេះ。",
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

    # ── TikTok Slideshow / Photo ─────────────────────────────────
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
                "❌ <b>មិនអាចរកឃើញរូបភាពបានទេ</b>\n\n"
                "Link នេះអាចជាវីដេអូ — សូមសាកល្បង 🎬 <b>Video</b> ជំនួស。",
                parse_mode="HTML",
            )
            await state.clear()
            return

        safe_title = escape(str(result.get("title", "TikTok Photo")))
        caption = (
            f"✅ <b>ទាញយករួចរាល់!</b>\n"
            f"📌 {safe_title}\n"
            f"🖼️ រូបភាព {len(paths)} សន្លឹក\n"
            "🤖 @ravi_downloader_bot"
        )

        # Telegram media groups: max 10 per batch
        for i in range(0, len(paths), 10):
            chunk = paths[i: i + 10]
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

        # Cleanup image files + folder
        for p in paths:
            await safe_remove_file(p)
        try:
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
                "សូមព្យាយាមវីដេអូគុណភាពទាបជាង ឬជ្រើស Audio。",
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

    except TelegramBadRequest as e:
        err_str = str(e).lower()
        if "file is too big" in err_str or "too large" in err_str:
            error_msg = (
                "❌ <b>ឯកសារធំពេក</b>\n\n"
                "⚠️ Telegram កំណត់: 50MB\n"
                "សូមជ្រើស Audio ឬ Link វីដេអូខ្លីជាង。"
            )
        elif "wrong file identifier" in err_str:
            error_msg = "❌ ទម្រង់ឯកសារខុស។ សូមព្យាយាមម្តងទៀត。"
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
            f"❌ មានបញ្ហា upload 🧠\n\n<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        await send_log(
            f"❌ Upload Error (General)\n"
            f"User: <code>{callback.from_user.id}</code>\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot,
        )

    finally:
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()


# ─────────────────────────────────────────────
# Admin Commands
# ─────────────────────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    """Admin: Broadcast a message to all active users."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ រកមិនឃើញពាក្យបញ្ជានេះទេ។")
        return

    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "⚠️ <b>របៀបប្រើ:</b> /broadcast [សារ]\n\n"
            "<b>ឧទាហរណ៍:</b>\n"
            "/broadcast 🔧 Bot កំពុងថែទាំ 30 នាទី។",
            parse_mode="HTML",
        )
        return

    broadcast_body = (
        "📢 <b>សេចក្តីជូនដំណឹង</b>\n\n"
        f"{text}\n\n"
        "<i>សារផ្លូវការពី Admin Bot</i>"
    )

    active_users = await db.list_active_users()
    total = len(active_users)
    if total == 0:
        await message.answer("⚠️ មិនមាន user សកម្មសម្រាប់ផ្សាយទេ។")
        return

    # Validate HTML entities by sending a preview to the admin first.
    try:
        await message.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📣 <b>កំពុងផ្សាយទៅ {total} user សកម្ម</b>\n\n{broadcast_body}",
            parse_mode="HTML",
            disable_notification=True,
        )
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

    progress_msg = await message.answer(
        f"📢 <b>កំពុងផ្សាយ...</b>\nសរុប: {total}",
        parse_mode="HTML",
    )

    success = failed = blocked = 0
    BATCH = 25

    for i in range(0, total, BATCH):
        batch = active_users[i:i + BATCH]
        tasks = [
            message.bot.send_message(
                chat_id=u.get("user_id"),
                text=broadcast_body,
                parse_mode="HTML",
            )
            for u in batch
            if u.get("user_id") is not None
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                failed += 1
                err = str(res).lower()
                if ("blocked by the user" in err
                        or "bot was blocked" in err
                        or "user is deactivated" in err):
                    blocked += 1
                else:
                    logger.warning(f"Broadcast send failed: {res}")
            else:
                success += 1

        done = min(i + BATCH, total)
        await progress_msg.edit_text(
            f"📢 <b>កំពុងផ្សាយ...</b>\n"
            f"✅ {success} | ❌ {failed} | {done}/{total}",
            parse_mode="HTML",
        )
        if done < total:
            await asyncio.sleep(1)

    # Mark blocked recipients inactive so future broadcasts skip them.
    if blocked:
        blocked_ids = [
            u.get("user_id")
            for u in active_users
            if u.get("user_id") is not None
        ]
        for bid in blocked_ids:
            await db.set_user_active(bid, False)

    summary = (
        f"✅ <b>ផ្សាយរួចរាល់!</b>\n\n"
        f"📊 សរុប: {total}\n"
        f"✅ ជោគជ័យ: {success}\n"
        f"❌ បរាជ័យ: {failed}"
    )
    if blocked:
        summary += f"\n🚫 បាន block: {blocked} (បានដកចេញពីបញ្ជីសកម្ម)"
    await progress_msg.edit_text(summary, parse_mode="HTML")
    await send_log(
        f"📢 Broadcast done: {success}/{total} (blocked: {blocked})",
        bot=message.bot,
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Admin: View bot statistics."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ រកមិនឃើញពាក្យបញ្ជានេះទេ។")
        return

    try:
        stats = await db.count_users()
        active = await db.count_active_users()
        total_downloads = await db.total_downloads()
        active_downloads = await db.total_active_downloads()

        text = (
            f"📊 <b>ស្ថិតិ Bot ផ្លូវការ</b>\n\n"
            f"👥 <b>ទិន្នន័យអ្នកប្រើប្រាស់:</b>\n"
            f"• អ្នកប្រើប្រាស់សរុប (Lifetime): <b>{stats['total']}</b> នាក់\n"
            f"• អ្នកប្រើប្រាស់សកម្ម (Active): <b>{active}</b> នាក់\n"
            f"• គណនី Premium: <b>{stats['premium']}</b> | Free: <b>{stats['free']}</b>\n\n"
            f"📥 <b>ទិន្នន័យនៃការទាញយក (Downloads):</b>\n"
            f"• ការទាញយកសរុបទាំងអស់: <b>{total_downloads}</b> ដង\n"
            f"• ការទាញយកពី Active Users: <b>{active_downloads}</b> ដង\n\n"
            f"🕒 <i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        )
        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Stats error: {e}")
        await message.answer(f"❌ Error: {escape(str(e))}", parse_mode="HTML")


# ─────────────────────────────────────────────
# User Block / Leave Detection
# ─────────────────────────────────────────────

@router.my_chat_member()
async def handle_bot_blocked(event: ChatMemberUpdated):
    """Fires when user blocks, unblocks, or kicks the bot."""
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    user = event.from_user

    user_id = user.id
    full_name = escape(user.full_name or "")
    username = f"@{escape(user.username)}" if user.username else "(no username)"

    # User blocked or kicked the bot
    if new_status in ("kicked", "left") and old_status == "member":
        logger.info(f"🚫 User blocked bot: {user_id}")
        await db.set_user_active(user_id, False)
        await send_log(
            f"🚫 <b>User បានចាកចេញ / Block Bot</b>\n\n"
            f"👤 {full_name}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🔗 {username}",
            bot=event.bot,
        )
        return

    # User unblocked the bot
    if new_status == "member" and old_status in ("kicked", "left"):
        logger.info(f"✅ User unblocked bot: {user_id}")
        await db.set_user_active(user_id, True)
        await send_log(
            f"✅ <b>User បានត្រឡប់មកវិញ / Unblock Bot</b>\n\n"
            f"👤 {full_name}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🔗 {username}",
            bot=event.bot,
        )
