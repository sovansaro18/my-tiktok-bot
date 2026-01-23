# src/handlers.py
import os
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from src.database import db
from src.downloader import downloader
from src.config import ADMIN_ID, LOG_CHANNEL_ID
from src.utils import send_log

router = Router()

# ===================== COMMAND HANDLERS =====================

@router.message(CommandStart())
async def cmd_start(message: types.Message, bot: Bot):
    # ទាញយក User និងពិនិត្យថាថ្មីឬចាស់
    user, is_new = await db.get_user(message.from_user.id)
    
    # បើជា User ថ្មី -> ជូនដំណឹងទៅ Channel
    if is_new:
        log_msg = (
            f"🆕 **NEW USER JOINED!**\n"
            f"👤 Name: {message.from_user.full_name}\n"
            f"🆔 ID: `{message.from_user.id}`\n"
            f"🔗 Username: @{message.from_user.username}"
        )
        await send_log(bot, log_msg)

    # សារស្វាគមន៍
    msg = (
        f"👋 **សួស្ដី {message.from_user.first_name}!**\n\n"
        "**សូមស្វាគមន៍! មកកាន់ Video Downloader Bot។**\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
    )
    
    if user['status'] == 'premium' or message.from_user.id == ADMIN_ID:
        msg += "🌟 ស្ថានភាព: **Premium** (ប្រើបានឥតដែនកំណត់) ✅"
    else:
        left = 10 - user['downloads_count']
        if left > 0:
            msg += f"👤 ស្ថានភាព: **Free Trial**\n📉 អ្នកនៅសល់: **{left}/10** ដង។"
        else:
            msg += "⛔️ ស្ថានភាព: **អស់ចំនួនកំណត់**\nសូមបង់ប្រាក់ដើម្បីបន្ត។"
            
    msg += "\n\n👇 **ផ្ញើ Link (TikTok, FB, IG) មកទីនេះដើម្បីទាញយក!**"
    await message.answer(msg, parse_mode="Markdown")

@router.message(Command("plan"))
async def cmd_plan(message: types.Message):
    user, _ = await db.get_user(message.from_user.id)
    count = user['downloads_count']
    
    msg = f"📊 **ព័ត៌មានគណនី:** `{message.from_user.id}`\n\n"
    if user['status'] == 'premium':
        msg += "🌟 **Premium User** (Lifetime) ✅"
    else:
        msg += f"👤 **Free User**\n📉 បានប្រើ: {count}/10"
        if count >= 10:
            msg += "\n⛔️ **អស់ចំនួនកំណត់!** សូមផ្ញើរូបវិក័យបត្រមកទីនេះដើម្បីទិញ។"
        
    await message.answer(msg, parse_mode="Markdown")

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    msg = (
        "❓ **ជំនួយការប្រើប្រាស់:**\n\n"
        "1. Copy Link វីដេអូ (TikTok, FB, IG, YouTube)\n"
        "2. Paste ចូលក្នុង Bot នេះ\n"
        "3. ចុចប៊ូតុង Video ឬ Audio\n\n"
        "💎 **ចង់ទិញ Premium?**\n"
        "សូមបង់ប្រាក់តាម QR Code (ទាក់ទង Admin) រួចផ្ញើរូបវិក័យបត្រមកទីនេះ។"
    )
    await message.answer(msg)

# ===================== ADMIN COMMANDS =====================

@router.message(Command("approve"))
async def cmd_approve(message: types.Message, bot: Bot):
    # ពិនិត្យសិទ្ធិ Admin
    if message.from_user.id != ADMIN_ID:
        return

    try:
        # ទម្រង់: /approve 123456789
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("⚠️ សូមសរសេរ: `/approve [user_id]`")
            return
        
        target_id = int(parts[1])
        
        # Update Database
        await db.set_premium(target_id)
        
        # 1. ប្រាប់ Admin
        await message.reply(f"✅ User `{target_id}` ត្រូវបានដំឡើងជា Premium!", parse_mode="Markdown")
        
        # 2. ជូនដំណឹងទៅ User ផ្ទាល់
        try:
            await bot.send_message(target_id, "🎉 **អបអរសាទរ!**\nគណនីរបស់អ្នកត្រូវបានដំឡើងជា **Premium** ហើយ។\nអ្នកអាចទាញយកបានដោយសេរី! 🚀")
        except:
            await message.reply("⚠️ មិនអាចផ្ញើសារទៅ User បានទេ (គេអាចនឹង Block Bot) ប៉ុន្តែសិទ្ធិបានដំឡើងរួចរាល់។")
            
        # 3. Log ចូល Channel
        await send_log(bot, f"💎 **PREMIUM UPGRADED**\n👮‍♂️ By Admin: {message.from_user.first_name}\n👤 User ID: `{target_id}`")
        
    except ValueError:
        await message.reply("⚠️ ID ត្រូវតែជាលេខ!")

# ===================== RECEIPT / PHOTO HANDLER =====================

@router.message(F.photo)
async def handle_receipt(message: types.Message, bot: Bot):
    # ពេល User ផ្ញើរូបមក យើងសន្មតថាជាវិក័យបត្រ
    user_id = message.from_user.id
    
    await message.reply("⏳ **បានទទួលរូបភាព!**\nAdmin នឹងត្រួតពិនិត្យវិក័យបត្ររបស់អ្នកក្នុងពេលឆាប់ៗ។")
    
    # Forward ទៅ Channel Admin
    caption = (
        f"💸 **PAYMENT RECEIPT**\n"
        f"👤 User: {message.from_user.full_name}\n"
        f"🆔 ID: `{user_id}`\n\n"
        f"👇 **ចុចដើម្បី Approve:**\n"
        f"`/approve {user_id}`"
    )
    
    # ផ្ញើរូបទៅ Channel
    if LOG_CHANNEL_ID:
        await bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=message.photo[-1].file_id, caption=caption, parse_mode="Markdown")

# ===================== LINK HANDLER =====================

ALLOWED_DOMAINS = [
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", 
    "facebook.com", "fb.watch", "instagram.com", 
    "youtube.com", "youtu.be", "twitter.com", "x.com"
]

@router.message(F.text)
async def handle_link(message: types.Message):
    url = message.text.strip()
    
    if not any(domain in url for domain in ALLOWED_DOMAINS):
        return # មិនមែន Link ដែលយើងស្គាល់

    user, _ = await db.get_user(message.from_user.id)
    
    # Check Limit
    if message.from_user.id != ADMIN_ID and user['status'] != 'premium' and user['downloads_count'] >= 10:
        await message.reply("⛔️ **អស់ចំនួនកំណត់ហើយ!**\nសូមផ្ញើរូបវិក័យបត្រមកទីនេះ ដើម្បីបន្តប្រើប្រាស់។")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video", callback_data="dl_video"),
            InlineKeyboardButton(text="🎵 Audio", callback_data="dl_audio")
        ]
    ])
    
    await message.reply("👇 **សូមជ្រើសរើសប្រភេទ៖**", reply_markup=keyboard)

# ===================== CALLBACK HANDLER =====================

@router.callback_query(F.data.in_({"dl_video", "dl_audio"}))
async def process_download(callback: types.CallbackQuery, bot: Bot):
    if not callback.message.reply_to_message or not callback.message.reply_to_message.text:
        await callback.answer("រក Link មិនឃើញ!", show_alert=True)
        return

    url = callback.message.reply_to_message.text.strip()
    is_audio = (callback.data == "dl_audio")
    
    await callback.message.edit_text("⏳ **កំពុងដំណើរការ...**", parse_mode="Markdown")
    
    result = await downloader.download(url, is_audio)
    
    if result['status'] == 'success':
        file_path = result['path']
        try:
            await callback.message.edit_text("⬆️ **កំពុង Upload ជូន...**", parse_mode="Markdown")
            
            file_input = FSInputFile(file_path)
            if is_audio:
                await bot.send_audio(callback.message.chat.id, file_input, caption="✅ **Download ជោគជ័យ!**")
            else:
                await bot.send_video(callback.message.chat.id, file_input, caption="✅ **Download ជោគជ័យ!**")
            
            # Increment Count
            user_id = callback.from_user.id
            user, _ = await db.get_user(user_id)
            if user_id != ADMIN_ID and user['status'] != 'premium':
                await db.increment_download(user_id)
                
        except Exception as e:
            await callback.message.edit_text(f"❌ **Upload បរាជ័យ:** {str(e)}")
            # Log Error
            await send_log(bot, f"⚠️ **UPLOAD ERROR**\nUser: `{callback.from_user.id}`\nError: `{e}`")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
            await callback.message.delete()
            
    elif result['message'] == 'file_too_large':
        size_mb = round(result['size'] / 1024 / 1024, 2)
        await callback.message.edit_text(f"❌ **ឯកសារធំពេក!** ({size_mb}MB)\nTelegram អនុញ្ញាតត្រឹម 50MB ប៉ុណ្ណោះ។")
    else:
        error_msg = result['message']
        await callback.message.edit_text(f"❌ **ទាញយកមិនបាន!**\nAdmin ត្រូវបានជូនដំណឹងហើយ។")
        
        # Log Error to Channel
        log_msg = (
            f"⚠️ **DOWNLOAD ERROR**\n"
            f"👤 User: `{callback.from_user.id}`\n"
            f"🔗 Link: {url}\n"
            f"🛑 Error: `{error_msg}`"
        )
        await send_log(bot, log_msg)