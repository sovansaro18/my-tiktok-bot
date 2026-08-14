import asyncio
import logging
import os
from types import SimpleNamespace
from html import escape
from datetime import datetime, timezone

from src.tts_engine import generate_speech
from src.pdf_converter import pdf_converter

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
    waiting_for_url = State()

class ReportState(StatesGroup):
    waiting_for_report = State()

class ConvertState(StatesGroup):
    waiting_for_video = State()

class TTSState(StatesGroup):
    waiting_for_voice = State()
    waiting_for_text = State()

class PdfState(StatesGroup):
    waiting_for_pdf = State()
    waiting_for_format = State()
    waiting_for_pages = State()


# ─────────────────────────────────────────────
# Helper: Friendly Error Messages
# ─────────────────────────────────────────────

def friendly_download_error(url: str, err: str) -> str:
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
            f"✅ សូមផ្ញើ Link វីដេអូដែលជា <b>Public</b> ពី {plat} មកវិញ。"
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
# Helper: Keyboards
# ─────────────────────────────────────────────

def feature_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 ទាញយកវីដេអូ", callback_data="feat_formats")],
            [InlineKeyboardButton(text="🎵 បំលែង Video ទៅ MP3", callback_data="feat_convert")],
            [InlineKeyboardButton(text="🗣 អានអត្ថបទ", callback_data="feat_tts")],
            [InlineKeyboardButton(text="🖼 បំលែង PDF ទៅរូបភាព", callback_data="feat_pdf")],
            [InlineKeyboardButton(text="ℹ ព័ត៌មានទូទៅ", callback_data="feat_general_info")],
        ]
    )

def general_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 របៀបទាញយក", callback_data="feat_howto"),
                InlineKeyboardButton(text="🌐 វេទិកាគាំទ្រ", callback_data="feat_platforms"),
            ],
            [
                InlineKeyboardButton(text="👤 គណនីរបស់ខ្ញុំ", callback_data="feat_plan"),
                InlineKeyboardButton(text="⚠ កំណត់ប្រើប្រាស់", callback_data="feat_limits"),
            ],
            [
                InlineKeyboardButton(text="❓ សំណួរញឹកញាប់", callback_data="feat_faq"),
                InlineKeyboardButton(text="📩 ជូនដំណឹង", callback_data="feat_report"),
            ],
            [
                InlineKeyboardButton(text="⬅ ត្រឡប់", callback_data="feat_back"),
            ],
        ]
    )

def tts_voice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧑 សំឡេងប្រុស", callback_data="tts_voice_male"),
                InlineKeyboardButton(text="👩 សំឡេងស្រី", callback_data="tts_voice_female"),
            ],
            [
                InlineKeyboardButton(text="⬅ បោះបង់", callback_data="feat_back"),
            ]
        ]
    )

FEATURE_PANELS = {
    "feat_howto": (
        "📥 <b>របៀបទាញយក</b>\n\n"
        "1️⃣ ចម្លង Link វីដេអូពីវេទិកាល្បីៗ\n"
        "2️⃣ ផ្ញើ Link មក Bot\n"
        "3️⃣ ជ្រើសរើសប្រភេទ (Video / Audio / Photo)\n"
        "4️⃣ រង់ចាំ Bot ទាញយក និងបញ្ជូនមកវិញ\n\n"
        "<i>ងាយស្រួល គ្រាន់តែផ្ញើ Link!</i> 🚀"
    ),
    "feat_platforms": (
        "🌐 <b>វេទិកាគាំទ្រ</b>\n\n"
        "✅ TikTok\n"
        "✅ Facebook\n"
        "✅ YouTube\n"
        "✅ Instagram\n"
        "✅ Pinterest\n\n"
        "<i>ផ្ញើ Link ពីវេទិកាខាងលើមក Bot បានភ្លាម!</i>"
    ),
    "feat_plan": (
        "📊 <b>គណនីរបស់ខ្ញុំ</b>\n\n"
        f"🏷 ស្ថានភាព: <b>ឥតគិតថ្លៃ ✅</b>\n\n"
        "♾️ ទាញយកបានគ្មានកំណត់\n"
        "🎬 Video, Audio, Photo\n"
        "🚀 ប្រើបានភ្លាម គ្មានការចុះឈ្មោះ"
    ),
    "feat_limits": (
        "🚫 <b>កំណត់ប្រើប្រាស់</b>\n\n"
        "❌ មិនគាំទ្រវីដេអូ Private\n"
        "❌ មិនគាំទ្រវីដេអូ Copyright\n"
        "❌ មិនគាំទ្រវីដេអូ Age-restricted\n"
        "⚠️ ទំហំអតិបរមា 49MB (សម្រាប់ទាញយក)\n"
        "⚠️ ទំហំអតិបរមា 20MB (សម្រាប់បំលែង Video/PDF)\n\n"
        "<i>សូមផ្ញើ Link ដែលជា Public ប៉ុណ្ណោះ!</i>"
    ),
    "feat_faq": (
        "❓ <b>សំណួរញឹកញាប់</b>\n\n"
        "<b>Q: តើ Bot ឥតគិតថ្លៃទេ?</b>\n"
        "A: បាទ/ចាស ឥតគិតថ្លៃ ទាំងស្រុង!\n\n"
        "<b>Q: ធ្វើយ៉ាងណាខ្លា បើទាញយកមិនបាន?</b>\n"
        "A: សូមប្រើ <b>/report</b> ដើម្បីជូនដំណឹង Admin\n\n"
        "<b>Q: ធ្វើយ៉ាងណាបើ Link មិនសម?</b>\n"
        "A: ផ្ញើ Link ត្រឹមត្រូវពីវេទិកាគាំទ្រប៉ុណ្ណោះ"
    ),
}

# ─────────────────────────────────────────────
# Helper: Format Selection Keyboard
# ─────────────────────────────────────────────

def download_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Video (MP4)", callback_data="fmt_video"
                ),
                InlineKeyboardButton(
                    text="🎵 Audio (MP3)", callback_data="fmt_audio"
                ),
            ]
        ]
    )

def format_select_keyboard(platform: str) -> InlineKeyboardMarkup:
    if platform == "tiktok":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 វីដេអូ", callback_data="fmt_video"),
                    InlineKeyboardButton(text="🎵 អូឌីយ៉ូ", callback_data="fmt_audio"),
                ],
                [
                    InlineKeyboardButton(text="🖼 រូបភាព", callback_data="fmt_photo"),
                ],
            ]
        )
    else:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 វីដេអូ", callback_data="fmt_video"),
                    InlineKeyboardButton(text="🎵 អូឌីយ៉ូ", callback_data="fmt_audio"),
                ]
            ]
        )


# ─────────────────────────────────────────────
# Helper: Message Deletion & Editing
# ─────────────────────────────────────────────

async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
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
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error deleting message {message_id}: {e}")
        return False

async def safe_edit_text(message: Message, new_text: str, parse_mode: str = "HTML", **kwargs) -> Message:
    try:
        return await message.edit_text(new_text, parse_mode=parse_mode, **kwargs)
    except TelegramBadRequest as e:
        logger.warning(f"⚠️ safe_edit_text ignored TelegramBadRequest: {e}")
        return message 
    except Exception as e:
        logger.error(f"❌ safe_edit_text encountered unexpected error: {e}")
        return message


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
        "⚙️ <b>សូមជ្រើសរើសមុខងារនៅខាងក្រោម៖</b>"
    )
    await message.answer(
        welcome, parse_mode="HTML", reply_markup=feature_menu_keyboard()
    )


# ─────────────────────────────────────────────
# Callback: Feature Menu
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("feat_"))
async def feature_menu_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "feat_general_info":
        await safe_edit_text(callback.message,
            "ℹ️ <b>ព័ត៌មានទូទៅ</b>\n\n"
            "សូមជ្រើសរើសព័ត៌មានដែលអ្នកចង់ស្វែងយល់ខាងក្រោម៖",
            parse_mode="HTML",
            reply_markup=general_info_keyboard()
        )
        return

    if callback.data == "feat_report":
        await state.set_state(ReportState.waiting_for_report)
        await safe_edit_text(callback.message,
            "📩 <b>សូមវាយសារជូនដំណឹង!</b>\n\n"
            "សរសេរសាររបស់អ្នកនៅទីនេះ ហើយផ្ញើមកខ្ញុំ។",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅ ត្រឡប់", callback_data="feat_general_info")]]
            )
        )
        return

    if callback.data == "feat_convert":
        await state.set_state(ConvertState.waiting_for_video)
        await safe_edit_text(callback.message,
            "🔄 <b>បំលែង Video ទៅជា MP3</b>\n\n"
            "សូមផ្ញើ <b>ឯកសារវីដេអូ</b> របស់អ្នកចូលមកក្នុង Chat នេះ (ទំហំអតិបរមា 20MB)។\n\n"
            "<i>ចំណាំ៖ សូមផ្ញើជាវីដេអូផ្ទាល់ មិនមែនជា Link ទេ។</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅ បោះបង់", callback_data="feat_back")]]
            )
        )
        return

    if callback.data == "feat_tts":
        await state.set_state(TTSState.waiting_for_voice)
        await safe_edit_text(callback.message,
            "🗣️ <b>អានអត្ថបទ (Text-to-Speech)</b>\n\n"
            "សូមជ្រើសរើសប្រភេទសំឡេងដែលអ្នកចង់បាន:",
            parse_mode="HTML",
            reply_markup=tts_voice_keyboard()
        )
        return

    if callback.data == "feat_pdf":
        await state.set_state(PdfState.waiting_for_pdf)
        await safe_edit_text(callback.message,
            "📄 <b>បំលែង PDF ទៅជារូបភាព</b>\n\n"
            "សូមផ្ញើ <b>ឯកសារ PDF</b> របស់អ្នកចូលមកក្នុង Chat នេះ (ទំហំអតិបរមា 20MB)។\n\n"
            "<i>ចំណាំ៖ សូមផ្ញើជាឯកសារ PDF ផ្ទាល់ មិនមែនជា Link ទេ។</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅ បោះបង់", callback_data="feat_back")]]
            )
        )
        return

    if callback.data == "feat_back":
        await state.clear()
        welcome = (
            f"👋 <b>សួស្តី {escape(callback.from_user.full_name)}!</b>\n\n"
            "⚙️ <b>សូមជ្រើសរើសមុខងារនៅខាងក្រោម៖</b>"
        )
        try:
            await callback.message.edit_text(
                welcome, parse_mode="HTML", reply_markup=feature_menu_keyboard()
            )
        except Exception:
            pass
        return

    if callback.data == "feat_formats":
        await state.set_state(DownloadState.waiting_for_url)
        try:
            await callback.message.edit_text(
                "🎬 <b>ទាញយកវីដេអូ</b>\n\n"
                "សូមផ្ញើ Link Video ដើម្បីធ្វើការទាញយក:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅ បោះបង់", callback_data="feat_back")]]
                )
            )
        except Exception:
            pass
        return

    panel_text = FEATURE_PANELS.get(callback.data)
    if panel_text is None:
        return

    try:
        await callback.message.edit_text(
            panel_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅ ត្រឡប់", callback_data="feat_general_info")]
                ]
            ),
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
# Local Video -> MP3 Converter Handler
# ─────────────────────────────────────────────

@router.message(ConvertState.waiting_for_video, F.video | F.document)
async def handle_local_video(message: Message, state: FSMContext):
    video = message.video or message.document
    
    if message.document and not message.document.mime_type.startswith('video/'):
        await message.answer("⚠️ សូមផ្ញើជាប្រភេទឯកសារ <b>វីដេអូ (Video)</b>។", parse_mode="HTML")
        return

    if video.file_size > 20 * 1024 * 1024:
        await message.answer("❌ <b>វីដេអូធំពេកហើយ!</b>\n\nទំហំអតិបរមាដែលអនុញ្ញាតគឺ <b>20MB</b>។ សូមផ្ញើវីដេអូខ្លីជាងនេះ។", parse_mode="HTML")
        return

    prog_msg = await message.answer("⏳ <b>កំពុងទាញយកវីដេអូរបស់អ្នក...</b>", parse_mode="HTML")

    file_id = video.file_id
    file = await message.bot.get_file(file_id)
    file_ext = file.file_path.split('.')[-1]
    input_path = f"temp_in_{file_id}.{file_ext}"
    output_path = f"temp_out_{file_id}.mp3"

    try:
        await message.bot.download_file(file.file_path, input_path)
        await safe_edit_text(prog_msg, "⏳ <b>កំពុងបំលែងទៅជា MP3...</b>\n<i>សូមរង់ចាំបន្តិច...</i>", parse_mode="HTML")

        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', input_path, '-q:a', '0', '-map', 'a', output_path, '-y',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"FFmpeg conversion error: {stderr.decode()}")
            await safe_edit_text(prog_msg, "❌ <b>សុំទោស មានបញ្ហាក្នុងការបំលែងវីដេអូនេះ។</b>\nសូមព្យាយាមវីដេអូផ្សេង។", parse_mode="HTML")
            return

        await safe_edit_text(prog_msg, "📤 <b>កំពុងបញ្ជូនសំឡេងទៅកាន់អ្នក...</b>", parse_mode="HTML")

        audio_file = FSInputFile(output_path)
        await message.answer_audio(audio=audio_file, title="Converted Audio", performer="@v_videodownloader_bot")

    except Exception as e:
        logger.error(f"Error in video conversion: {e}", exc_info=True)
        await safe_edit_text(prog_msg, "❌ <b>មានបញ្ហាប្រព័ន្ធ។</b> សូមសាកល្បងម្ដងទៀតនៅពេលក្រោយ។", parse_mode="HTML")
    finally:
        try:
            await prog_msg.delete()
        except:
            pass
        for p in [input_path, output_path]:
            if os.path.exists(p):
                os.remove(p)
        await state.clear()

@router.message(ConvertState.waiting_for_video)
async def handle_convert_invalid_input(message: Message):
    await message.answer("⚠️ សូមផ្ញើជា <b>ឯកសារវីដេអូ (Video File)</b> ដែលមានក្នុងទូរស័ព្ទរបស់អ្នក មិនមែនជាអត្ថបទ ឬ Link ទេ។", parse_mode="HTML")


# ─────────────────────────────────────────────
# PDF → Image Converter Handlers
# ─────────────────────────────────────────────

def pdf_format_keyboard(total_pages: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🖼 JPG", callback_data="pdf_fmt_jpg"),
                InlineKeyboardButton(text="🖼 PNG", callback_data="pdf_fmt_png"),
            ],
            [
                InlineKeyboardButton(
                    text=f"📄 ទំព័រទាំងអស់ ({total_pages})",
                    callback_data="pdf_fmt_all",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅ បោះបង់", callback_data="feat_back"),
            ],
        ]
    )


@router.message(PdfState.waiting_for_pdf, F.document)
async def handle_pdf_document(message: Message, state: FSMContext):
    document = message.document

    mime = (document.mime_type or "").lower()
    file_name = (document.file_name or "").lower()
    if not (mime == "application/pdf" or file_name.endswith(".pdf")):
        await message.answer(
            "⚠️ សូមផ្ញើជាឯកសារ <b>PDF</b> ប៉ុណ្ណោះ។",
            parse_mode="HTML",
        )
        return

    if document.file_size > 20 * 1024 * 1024:
        await message.answer(
            "❌ <b>PDF ធំពេកហើយ!</b>\n\nទំហំអតិបរមាដែលអនុញ្ញាតគឺ <b>20MB</b>។",
            parse_mode="HTML",
        )
        return

    prog_msg = await message.answer(
        "⏳ <b>កំពុងទាញយក PDF របស់អ្នក...</b>",
        parse_mode="HTML",
    )

    file_id = document.file_id
    file = await message.bot.get_file(file_id)
    input_path = f"temp_pdf_{file_id}.pdf"

    try:
        await message.bot.download_file(file.file_path, input_path)

        import fitz
        doc = fitz.open(input_path)
        total_pages = doc.page_count
        doc.close()

        if total_pages == 0:
            await safe_edit_text(
                prog_msg,
                "❌ <b>PDF នេះទទេ ឬមិនអាចអានបាន។</b>",
                parse_mode="HTML",
            )
            await state.clear()
            return

        await state.update_data(pdf_path=input_path, pdf_total_pages=total_pages)
        await state.set_state(PdfState.waiting_for_format)

        await safe_edit_text(
            prog_msg,
            f"📄 <b>បានទាញយក PDF រួច!</b>\n\n"
            f"📚 ចំនួនទំព័រ: <b>{total_pages}</b>\n\n"
            "សូមជ្រើសរើសទម្រង់រូបភាព ឬបំប្លែងទាំងអស់៖",
            parse_mode="HTML",
            reply_markup=pdf_format_keyboard(total_pages),
        )
    except Exception as e:
        logger.error(f"PDF download error: {e}", exc_info=True)
        await safe_edit_text(
            prog_msg,
            "❌ <b>មានបញ្ហាក្នុងការទាញយក PDF ។</b> សូមព្យាយាមម្ដងទៀត។",
            parse_mode="HTML",
        )
        if os.path.exists(input_path):
            os.remove(input_path)
        await state.clear()


@router.message(PdfState.waiting_for_pdf)
async def handle_pdf_invalid_input(message: Message):
    await message.answer(
        "⚠️ សូមផ្ញើជា <b>ឯកសារ PDF</b> មិនមែនជាអត្ថបទ ឬរូបភាពទេ។",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("pdf_fmt_"), PdfState.waiting_for_format)
async def handle_pdf_format_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    total_pages = data.get("pdf_total_pages", 0)

    if callback.data == "pdf_fmt_all":
        image_format = data.get("pdf_image_format", "jpg")
        pages = list(range(1, total_pages + 1))
        await state.update_data(pdf_pages=pages)
        await _run_pdf_conversion(callback, state, image_format, pages)
        return

    image_format = "jpg" if callback.data == "pdf_fmt_jpg" else "png"
    await state.update_data(pdf_image_format=image_format)
    await state.set_state(PdfState.waiting_for_pages)

    await safe_edit_text(
        callback.message,
        f"✅ បានជ្រើសរើស: <b>{image_format.upper()}</b>\n\n"
        f"📚 PDF នេះមាន <b>{total_pages}</b> ទំព័រ។\n\n"
        "វាយលេខទំព័រដែលអ្នកចង់បំលែង៖\n"
        "• ទំព័រតែមួយ៖ <code>3</code>\n"
        "• ច្រើនទំព័រ៖ <code>1,3,5</code>\n"
        "• ជួរទំព័រ៖ <code>1-5</code>\n"
        "• លាយល្បូម៖ <code>1,3-5,8</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"📄 ទំព័រទាំងអស់ ({total_pages})",
                        callback_data="pdf_all_from_pages",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ បោះបង់", callback_data="feat_back")],
            ]
        ),
    )


@router.callback_query(F.data == "pdf_all_from_pages", PdfState.waiting_for_pages)
async def handle_pdf_all_from_pages(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    total_pages = data.get("pdf_total_pages", 0)
    image_format = data.get("pdf_image_format", "jpg")
    pages = list(range(1, total_pages + 1))
    await _run_pdf_conversion(callback, state, image_format, pages)


@router.message(PdfState.waiting_for_pages, F.text)
async def handle_pdf_page_selection(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("pdf_total_pages", 0)
    image_format = data.get("pdf_image_format", "jpg")

    pages, err = pdf_converter.parse_page_selection(message.text, total_pages)
    if err:
        await message.answer(f"⚠️ {err}", parse_mode="HTML")
        return

    prog_msg = await message.answer(
        f"⏳ <b>កំពុងបំលែង {len(pages)} ទំព័រទៅ {image_format.upper()}...</b>\n"
        "<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML",
    )
    await _run_pdf_conversion_with_msg(message, prog_msg, state, image_format, pages)


@router.message(PdfState.waiting_for_pages)
async def handle_pdf_pages_invalid(message: Message):
    await message.answer(
        "⚠️ សូមវាយបញ្ចូល <b>លេខទំព័រ</b> (ឧ. 1, 3-5)។",
        parse_mode="HTML",
    )


async def _run_pdf_conversion(callback: CallbackQuery, state: FSMContext, image_format: str, pages: list):
    prog_msg = await safe_edit_text(
        callback.message,
        f"⏳ <b>កំពុងបំលែង {len(pages)} ទំព័រទៅ {image_format.upper()}...</b>\n"
        "<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML",
    )
    await _run_pdf_conversion_with_msg(callback, prog_msg, state, image_format, pages)


async def _run_pdf_conversion_with_msg(ctx, prog_msg, state: FSMContext, image_format: str, pages: list):
    bot = ctx.bot
    data = await state.get_data()
    pdf_path = data.get("pdf_path")
    chat_id = ctx.message.chat.id if hasattr(ctx, "message") else ctx.chat.id

    if not pdf_path or not os.path.exists(pdf_path):
        await safe_edit_text(prog_msg, "❌ សម័យផុតកំណត់។ សូមផ្ញើ PDF ម្តងទៀត។", parse_mode="HTML")
        await state.clear()
        return

    try:
        result = await asyncio.wait_for(
            pdf_converter.convert(pdf_path, image_format, pages),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await safe_edit_text(
            prog_msg,
            "❌ <b>ការបំលែងយូរពេកហើយ។</b>\n\nសូមសាកល្បងជាមួយចំនួនទំព័រតិចជាងនេះ។",
            parse_mode="HTML",
        )
        await state.clear()
        return

    if result["status"] == "error":
        await safe_edit_text(
            prog_msg,
            f"❌ <b>មិនអាចបំលែងបានទេ</b>\n\n{escape(result.get('message', ''))}",
            parse_mode="HTML",
        )
        await state.clear()
        return

    image_paths = [
        p for p in result.get("file_paths", [])
        if isinstance(p, str) and os.path.exists(p)
    ]
    if not image_paths:
        await safe_edit_text(
            prog_msg,
            "❌ <b>មិនអាចបំលែងទំព័រណាមួយបានទេ។</b>",
            parse_mode="HTML",
        )
        await state.clear()
        return

    await safe_edit_text(prog_msg, "📤 <b>កំពុងបញ្ជូនរូបភាព...</b>", parse_mode="HTML")

    try:
        for i in range(0, len(image_paths), 10):
            chunk = image_paths[i:i + 10]
            media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
            await bot.send_media_group(chat_id=chat_id, media=media)

        await send_log(
            f"📄 PDF → {image_format.upper()}\n"
            f"User: <code>{ctx.from_user.id}</code>\n"
            f"Pages: {len(image_paths)}",
            bot=bot,
        )
    except TelegramBadRequest as e:
        logger.error(f"PDF upload error: {e}", exc_info=True)
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>មិនអាចបញ្ជូនរូបភាពបានទេ។</b>\n\n<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"PDF send error: {e}", exc_info=True)
        await bot.send_message(
            chat_id=chat_id,
            text="❌ <b>មានបញ្ហាក្នុងការបញ្ជូនរូបភាព។</b> សូមព្យាយាមម្ដងទៀត។",
            parse_mode="HTML",
        )
    finally:
        try:
            await prog_msg.delete()
        except Exception:
            pass
        for p in image_paths:
            await safe_remove_file(p)
        try:
            if image_paths:
                folder = os.path.dirname(image_paths[0])
                if folder and os.path.isdir(folder) and not os.listdir(folder):
                    os.rmdir(folder)
        except Exception:
            pass
        if pdf_path and os.path.exists(pdf_path):
            os.remove(pdf_path)
        await state.clear()


# ─────────────────────────────────────────────
# Text-to-Speech (TTS) Handler
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("tts_voice_"), TTSState.waiting_for_voice)
async def handle_tts_voice_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    voice_gender = "male" if callback.data == "tts_voice_male" else "female"
    await state.update_data(voice_gender=voice_gender)
    await state.set_state(TTSState.waiting_for_text)
    
    voice_label = "👨 ប្រុស" if voice_gender == "male" else "👩 ស្រី"
    await safe_edit_text(callback.message,
        f"✅ បានជ្រើសរើសសំឡេង: <b>{voice_label}</b>\n\n"
        "🗣️ សូមវាយ ឬ Copy អត្ថបទជា <b>ភាសាខ្មែរ ឬអង់គ្លេស</b> បញ្ចូលមកទីនេះ។\n\n"
        "<i>ចំណាំ៖ សូមបញ្ចូលអត្ថបទក្រោម 3000 តួអក្សរ។</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="⬅️ បោះបង់", callback_data="feat_back"
            )]]
        )
    )

@router.message(TTSState.waiting_for_text, F.text)
async def handle_tts_text(message: Message, state: FSMContext):
    text = message.text.strip()
    
    data = await state.get_data()
    voice_gender = data.get("voice_gender", "female")  # fallback safety
    
    if len(text) > 3000:
        await message.answer("❌ <b>អត្ថបទវែងពេក!</b>\n\nសូមផ្ញើអត្ថបទដែលមានប្រវែងតិចជាង ៣០០០ តួអក្សរ។", parse_mode="HTML")
        return

    prog_msg = await message.answer("⏳ <b>កំពុងអានអត្ថបទរបស់អ្នក...</b>\n<i>សូមរង់ចាំបន្តិច...</i>", parse_mode="HTML")

    file_path = f"tts_{message.from_user.id}_{int(datetime.now().timestamp())}.mp3"

    try:
        # Native async call ទៅកាន់ edge-tts engine របស់យើង
        success = await generate_speech(text, voice_gender, file_path)
        
        if not success:
            await safe_edit_text(prog_msg,
                "❌ <b>មានបញ្ហាក្នុងការបំប្លែងអត្ថបទទៅជាសំឡេង។</b>\n\n"
                "សូមព្យាយាមម្តងទៀត ឬសាកល្បងអត្ថបទខ្លីជាងនេះ。",
                parse_mode="HTML",
            )
            return

        await safe_edit_text(prog_msg, "📤 <b>កំពុងបញ្ជូនសំឡេងទៅកាន់អ្នក...</b>", parse_mode="HTML")

        audio_file = FSInputFile(file_path)
        await message.answer_voice(
            voice=audio_file, 
            caption="🗣️ <b>អានរួចរាល់!</b>",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error in TTS conversion: {e}", exc_info=True)
        await safe_edit_text(prog_msg, "❌ <b>មានបញ្ហាក្នុងការបំប្លែងអត្ថបទ។</b> សូមសាកល្បងម្ដងទៀតនៅពេលក្រោយ។", parse_mode="HTML")
    finally:
        try:
            await prog_msg.delete()
        except:
            pass
        if os.path.exists(file_path):
            os.remove(file_path)
        await state.clear()

@router.message(TTSState.waiting_for_text)
async def handle_tts_invalid_input(message: Message):
    await message.answer("⚠️ សូមផ្ញើជា <b>អត្ថបទ (Text)</b> មិនមែនជារូបភាព ឬវីដេអូទេ។", parse_mode="HTML")


# ─────────────────────────────────────────────
# Commands: /plan & /report
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
    user_id = message.from_user.id
    await db.get_user(user_id)  

    raw_url = message.text.strip()
    try:
        url, _platform = validate_and_normalize_url(raw_url)
    except BotError as e:
        await message.answer(
            f"⚠️ <b>URL មិនត្រឹមត្រូវ</b>\n\n{escape(e.user_message)}",
            parse_mode="HTML",
        )
        return

    current_state = await state.get_state()
    stored_data = await state.get_data()
    selected_type = stored_data.get("download_type")

    if (
        current_state == DownloadState.waiting_for_url.state
        and selected_type in ("audio", "video")
    ):
        await state.update_data(
            url=url,
            platform=_platform,
            url_message_id=message.message_id,
        )
        await state.set_state(DownloadState.waiting_for_format)
        
        progress_msg = await message.answer(
            f"⏳ <b>កំពុងដំណើរការ...</b>\n",
            parse_mode="HTML",
        )
        
        download_context = SimpleNamespace(
            message=progress_msg,
            data=f"fmt_{selected_type}",
            from_user=message.from_user,
            bot=message.bot,
        )
        await process_download_callback(download_context, state)
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
async def process_download_callback_from_query(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    download_context = SimpleNamespace(
        message=callback.message,
        data=callback.data,
        from_user=callback.from_user,
        bot=callback.bot,
    )
    await process_download_callback(download_context, state)


async def process_download_callback(callback: SimpleNamespace, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None

    if await state.get_state() == DownloadState.waiting_for_url.state:
        if callback.data == "fmt_audio":
            selected_type = "audio"
        elif callback.data == "fmt_video":
            selected_type = "video"
        else:
            return
        await state.update_data(
            download_type=selected_type,
            format_message_id=callback.message.message_id,
        )
        await safe_edit_text(callback.message, "សូមបញ្ជូល Link Video ដើម្បីទាញយក")
        return

    if not url:
        await safe_edit_text(callback.message, "⚠️ សម័យផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត。")
        return

    if callback.data == "fmt_audio":
        download_type = "audio"
    elif callback.data == "fmt_photo":
        download_type = "photo"
    else:
        download_type = "video"

    type_label = {
        "audio": "MP3",
        "photo": "PHOTO",
        "video": "VIDEO",
    }.get(download_type, "VIDEO")

    progress_msg = await safe_edit_text(callback.message,
        f"⏳ <b>កំពុងទាញយក {type_label}...</b>\n"
        "<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML",
    )

    try:
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(f"⏱ Download timeout: {url}")
        await safe_edit_text(progress_msg,
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

    if result["status"] == "error":
        raw_error = str(result.get("message", "Unknown error"))
        if result.get("user_message"):
            error_text = result["user_message"]
        else:
            error_text = friendly_download_error(url, raw_error)
        await safe_edit_text(progress_msg, error_text, parse_mode="HTML")
        await send_log(
            f"❌ Download Error\n"
            f"User: {escape(callback.from_user.full_name)} "
            f"(<code>{callback.from_user.id}</code>)\n"
            f"URL: {url}\nType: {download_type}\nError: {raw_error[:300]}",
            bot=callback.bot,
        )
        await state.clear()
        return

    if (
        result.get("media_kind") == "slideshow"
        and isinstance(result.get("file_paths"), list)
    ):
        await safe_edit_text(progress_msg, "📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")

        paths = [
            p
            for p in result.get("file_paths", [])
            if isinstance(p, str) and os.path.exists(p)
        ]

        if not paths:
            await safe_edit_text(progress_msg,
                "❌ <b>មិនអាចរកឃើញរូបភាពបានទេ</b>\n\n"
                "Link នេះអាចជាវីដេអូ — សូមសាកល្បង 🎬 <b>Video</b> ជំនួស。",
                parse_mode="HTML",
            )
            await state.clear()
            return

        for i in range(0, len(paths), 10):
            chunk = paths[i: i + 10]
            media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
            await callback.message.answer_media_group(media)

        chat_id = callback.message.chat.id
        for mid in [url_message_id, format_message_id]:
            if mid:
                await safe_delete_message(callback.bot, chat_id, mid)
        try:
            await progress_msg.delete()
        except Exception:
            pass

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

    file_path = result["file_path"]

    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            await safe_edit_text(progress_msg,
                f"❌ <b>ឯកសារធំពេកសម្រាប់ Telegram</b>\n\n"
                f"📊 ទំហំ: {file_size / 1024 / 1024:.1f}MB\n"
                f"⚠️ កំណត់: {MAX_FILE_SIZE / 1024 / 1024:.0f}MB\n\n"
                "សូមព្យាយាមវីដេអូគុណភាពទាបជាង ឬជ្រើស Audio。",
                parse_mode="HTML",
            )
            await safe_remove_file(file_path)
            await state.clear()
            return

    try:
        await safe_edit_text(progress_msg, "📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")

        file_input = FSInputFile(file_path)
        if download_type == "audio":
            await callback.message.answer_audio(file_input)
        else:
            await callback.message.answer_video(file_input)

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
        await safe_edit_text(progress_msg,
            f"📢 <b>កំពុងផ្សាយ...</b>\n"
            f"✅ {success} | ❌ {failed} | {done}/{total}",
            parse_mode="HTML",
        )
        if done < total:
            await asyncio.sleep(1)

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
    await safe_edit_text(progress_msg, summary, parse_mode="HTML")
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
