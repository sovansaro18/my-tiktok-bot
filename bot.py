import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
import yt_dlp
from aiohttp import web
import pymongo

# --- ១. ការកំណត់ (Configuration) ---
API_TOKEN = os.getenv('BOT_TOKEN', '8122462719:AAEPt-oIfSxCVcLz0SjXGz2cDHrPuVKOkJk')
ADMIN_ID = 8399209514
MONGO_URI = "mongodb+srv://admin:123@downloader.xur9mwk.mongodb.net/?appName=downloader"

# --- ២. ភ្ជាប់ MongoDB ---
try:
    client = pymongo.MongoClient(MONGO_URI)
    db = client['downloader_bot']
    users_collection = db['users'] 
    print("✅ ភ្ជាប់ទៅ MongoDB ជោគជ័យ!")
except Exception as e:
    print(f"❌ បញ្ហាភ្ជាប់ MongoDB: {e}")

# --- ៣. កំណត់កន្លែង Save ---
DOWNLOAD_PATH = '/tmp/' if os.getenv('RENDER') else 'downloads/'
if not os.path.exists(DOWNLOAD_PATH) and not os.getenv('RENDER'):
    os.makedirs(DOWNLOAD_PATH)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# --- ៤. Logic គ្រប់គ្រង User ---
def get_user_data(user_id):
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        new_user = {
            "user_id": user_id,
            "status": "free",
            "downloads_count": 0
        }
        users_collection.insert_one(new_user)
        return new_user
    return user

def upgrade_to_premium(user_id):
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"status": "premium"}}
    )

def increment_download(user_id):
    users_collection.update_one(
        {"user_id": user_id},
        {"$inc": {"downloads_count": 1}}
    )

# --- ៥. Web Server ---
async def handle(request):
    return web.Response(text="Bot is running smoothly!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- ៦. Bot Handlers (តម្រៀបលំដាប់ថ្មី) ---

# ៦.១ Start Command
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    user = get_user_data(message.from_user.id)
    status = user.get("status", "free")
    count = user.get("downloads_count", 0)
    
    msg = (
        "សួស្ដី! 👋\n"
        "ខ្ញុំជា Bot ទាញយកវីដេអូ TikTok & Facebook។\n\n"
    )
    
    if status == 'premium' or message.from_user.id == ADMIN_ID:
        msg += "🌟 ស្ថានភាព: **Premium** (ប្រើបានឥតដែនកំណត់)"
    else:
        left = 3 - count
        if left > 0:
            msg += f"👤 ស្ថានភាព: **Free Trial**\n📉 អ្នកនៅសល់: **{left}/3** ដង។"
        else:
            msg += "⛔️ ស្ថានភាព: **អស់ចំនួនកំណត់**\nសូមបង់ប្រាក់ដើម្បីបន្ត។"
            
    msg += "\n\n👇 ផ្ញើ Link របស់អ្នកមកទីនេះ!"
    await message.reply(msg, parse_mode="Markdown")

# ៦.២ Admin Approve Command (លើកមកដាក់ខាងលើគេ មុន Text Handler) ⭐️
@dp.message_handler(commands=['approve'])
async def admin_approve(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(message.get_args())
        upgrade_to_premium(target_id)
        await message.reply(f"✅ User {target_id} ឥឡូវជា Premium!")
        await bot.send_message(target_id, "🎉 **ជោគជ័យ!** ការបង់ប្រាក់ត្រូវបានទទួលស្គាល់។ អ្នកអាចប្រើប្រាស់បានឥតដែនកំណត់។")
    except: pass

# ៦.៣ ទទួលវិក័យបត្រ
@dp.message_handler(content_types=['photo'])
async def handle_receipt(message: types.Message):
    user_id = message.from_user.id
    user = get_user_data(user_id)

    if user.get("status") == "premium":
        return

    await message.reply("⏳ បានទទួលរូប! Admin កំពុងត្រួតពិនិត្យ...")
    caption = f"📩 **វិក័យបត្រថ្មី!**\nUser: {message.from_user.full_name}\nID: `{user_id}`\n\nApprove: `/approve {user_id}`"
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, parse_mode="Markdown")

# ៦.៤ Function បង្ហាញ QR Code
async def send_payment_prompt(message: types.Message):
    msg_text = (
        "🔒 **អស់ចំនួនសាកល្បងហើយ!** (3/3)\n\n"
        "💰 **សូមបង់ប្រាក់ 2$ ដើម្បីប្រើប្រាស់មួយជីវិត!**\n"
        "1. ស្កេន QR Code ខាងលើ។\n"
        "2. ផ្ញើរូបវិក័យបត្រមកទីនេះ។\n"
        "3. Admin នឹងបើកសិទ្ធិជូនភ្លាមៗ។"
    )
    
    if os.path.exists('qrcode.jpg'):
        with open('qrcode.jpg', 'rb') as photo:
            await message.answer_photo(photo, caption=msg_text, parse_mode="Markdown")
    else:
        await message.answer(msg_text + "\n(QR Code កំពុងរៀបចំ សូមទាក់ទង Admin)")

# ៦.៥ ទទួលការចុចប៊ូតុង (Callback Handler)
@dp.callback_query_handler(lambda c: c.data in ['dl_video', 'dl_audio'])
async def process_callback_button(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    if not message.reply_to_message or not message.reply_to_message.text:
        await bot.answer_callback_query(callback_query.id, "រក Link មិនឃើញ!")
        return
        
    url = message.reply_to_message.text.strip()
    original_msg_id = message.reply_to_message.message_id
    download_type = callback_query.data
    
    user = get_user_data(user_id)
    if user_id != ADMIN_ID and user.get("status") != "premium" and user.get("downloads_count", 0) >= 3:
        await bot.answer_callback_query(callback_query.id, "អស់ចំនួនកំណត់ហើយ!", show_alert=True)
        await send_payment_prompt(message)
        return

    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text="⏳ កំពុងដំណើរការ... សូមរង់ចាំ!"
    )
    
    try:
        loop = asyncio.get_event_loop()
        is_audio = (download_type == 'dl_audio')
        filename = await loop.run_in_executor(None, download_logic, url, is_audio)
        
        if filename:
            with open(filename, 'rb') as file:
                if is_audio:
                    await bot.send_audio(message.chat.id, file, caption="✅ Audio Downloaded")
                else:
                    await bot.send_video(message.chat.id, file, caption="✅ Video Downloaded")
            
            if user_id != ADMIN_ID and user.get("status") != "premium":
                increment_download(user_id)
            
            if os.path.exists(filename): os.remove(filename)
            await bot.delete_message(message.chat.id, message.message_id)
            try:
                await bot.delete_message(message.chat.id, original_msg_id)
            except Exception: pass
        else:
             await bot.edit_message_text("❌ ទាញយកមិនបាន។ Link អាចខូច។", chat_id=message.chat.id, message_id=message.message_id)
             
    except Exception as e:
        await bot.edit_message_text(f"Error: {str(e)}", chat_id=message.chat.id, message_id=message.message_id)

# ៦.៦ Logic ទាញយក (yt-dlp)
def download_logic(url, audio_only=False):
    opts = {
        'format': 'best',
        'outtmpl': f'{DOWNLOAD_PATH}%(id)s.%(ext)s',
        'quiet': True,
        'noplaylist': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }
    if audio_only:
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best' 
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except: return None

# ៦.៧ ទទួល Link (Text Handler) - ដាក់នៅក្រោមគេបង្អស់! ⚠️
@dp.message_handler()
async def check_link_and_limit(message: types.Message):
    url = message.text.strip()
    
    # ពិនិត្យមើលថាជា Link ដែរឬទេ
    if not any(domain in url for domain in ["tiktok.com", "facebook.com", "fb.watch"]):
        if message.content_type == 'text':
             await message.reply("⚠️ Link មិនត្រឹមត្រូវ។ សូមផ្ញើ Link TikTok ឬ Facebook។")
        return

    user_id = message.from_user.id
    user = get_user_data(user_id)
    
    if user_id != ADMIN_ID and user.get("status") != "premium" and user.get("downloads_count", 0) >= 3:
        await send_payment_prompt(message)
        return

    keyboard = InlineKeyboardMarkup()
    btn_video = InlineKeyboardButton("🎬 Video", callback_data="dl_video")
    btn_audio = InlineKeyboardButton("🎵 Audio", callback_data="dl_audio")
    keyboard.add(btn_video, btn_audio)
    
    await message.reply("👇 សូមជ្រើសរើសប្រភេទ៖", reply_markup=keyboard)

# --- ៧. ចាប់ផ្តើមដំណើរការ ---
async def on_startup(_):
    await start_web_server()
    print("🤖 Bot Started!")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)