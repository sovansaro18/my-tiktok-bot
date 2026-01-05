import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
import yt_dlp
from aiohttp import web
import pymongo # ហៅ MongoDB មកប្រើ

# --- ការកំណត់ (Configuration) ---
API_TOKEN = os.getenv('BOT_TOKEN', '8122462719:AAEPt-oIfSxCVcLz0SjXGz2cDHrPuVKOkJk')
ADMIN_ID = 8399209514

# --- ផ្នែក MongoDB (កន្លែងកែថ្មី) ---
# Link របស់បងដែលបានផ្ដល់ឱ្យ
MONGO_URI = "mongodb+srv://admin:123@downloader.xur9mwk.mongodb.net/?appName=downloader"

# ភ្ជាប់ទៅ Database
try:
    client = pymongo.MongoClient(MONGO_URI)
    db = client['downloader_bot']  # បង្កើត Database ឈ្មោះ downloader_bot
    users_collection = db['paid_users'] # បង្កើតតារាងឈ្មោះ paid_users
    print("✅ ភ្ជាប់ទៅ MongoDB ជោគជ័យ!")
except Exception as e:
    print(f"❌ បញ្ហាភ្ជាប់ MongoDB: {e}")

# --- កន្លែង Save Video ---
DOWNLOAD_PATH = '/tmp/' if os.getenv('RENDER') else 'downloads/'
if not os.path.exists(DOWNLOAD_PATH) and not os.getenv('RENDER'):
    os.makedirs(DOWNLOAD_PATH)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

ydl_opts = {
    'format': 'best',
    'outtmpl': f'{DOWNLOAD_PATH}%(id)s.%(ext)s',
    'quiet': True,
    'noplaylist': True
}

# --- Function គ្រប់គ្រងអ្នកបង់លុយ (តាមរយៈ MongoDB) ---
def is_user_paid(user_id):
    # ស្វែងរក user_id ក្នុង database
    user = users_collection.find_one({"user_id": user_id})
    if user:
        return True
    return False

def add_paid_user(user_id):
    # បន្ថែម user ថ្មីចូល database
    if not is_user_paid(user_id):
        users_collection.insert_one({"user_id": user_id, "status": "premium"})
        return True
    return False

# --- ផ្នែក Web Server ---
async def handle(request):
    return web.Response(text="Bot is running with MongoDB!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- ផ្នែក Bot Logic ---

# ១. មុខងារ Start
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    user_id = message.from_user.id

    # ឆែកមើលក្នុង MongoDB ឬមើលថាជា Admin
    if is_user_paid(user_id) or user_id == ADMIN_ID:
        await message.reply(
            "✅ **ស្វាគមន៍ការត្រលប់មកវិញ!**\n"
            "ឈ្មោះរបស់អ្នកមានក្នុងបញ្ជីរួចហើយ។\n\n"
            "👇 ផ្ញើ Link Video មកទីនេះដើម្បីទាញយកបានភ្លាមៗ!",
            parse_mode="Markdown"
        )
    else:
        # បើមិនទាន់បង់លុយ
        await message.reply("🔒 **សេវាកម្មនេះតម្រូវឱ្យបង់ប្រាក់ 2$ ដើម្បីប្រើប្រាស់បានរហូត**")
        
        if os.path.exists('qrcode.jpg'):
            with open('qrcode.jpg', 'rb') as photo:
                await message.answer_photo(
                    photo,
                    caption=(
                        "💰 **សូមបង់ប្រាក់ 2$ ដើម្បីប្រើប្រាស់មួយជីវិត!**\n\n"
                        "1. ស្កេន QR Code ខាងលើ។\n"
                        "2. ផ្ញើរូបវិក័យបត្រមកទីនេះ។\n"
                        "3. Admin នឹងបញ្ចូលឈ្មោះអ្នកទៅក្នុងបញ្ជី។"
                    )
                )
        else:
            await message.answer("⚠️ Admin មិនទាន់ដាក់ QR Code។")

# ២. ទទួលរូបវិក័យបត្រ
@dp.message_handler(content_types=['photo'])
async def handle_receipt(message: types.Message):
    user_id = message.from_user.id

    if is_user_paid(user_id):
        return # បើបង់ហើយ មិនបាច់ធ្វើអីទេ

    await message.reply("⏳ បានទទួលរូប! Admin កំពុងត្រួតពិនិត្យ...")
    
    caption_to_admin = (
        f"📩 **វិក័យបត្រថ្មី!**\n"
        f"User: {message.from_user.full_name}\n"
        f"ID: `{user_id}`\n\n"
        f"វាយពាក្យនេះដើម្បីអនុញ្ញាត:\n"
        f"`/approve {user_id}`"
    )
    
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption_to_admin, parse_mode="Markdown")

# ៣. Admin Approve (បញ្ចូលទៅ MongoDB)
@dp.message_handler(commands=['approve'])
async def admin_approve(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        target_user_id = int(message.get_args())
        
        # ហៅ Function បញ្ចូលទៅ MongoDB
        if add_paid_user(target_user_id):
            await message.reply(f"✅ បានរក្សាទុក User {target_user_id} ចូល Database ជោគជ័យ!")
            await bot.send_message(target_user_id, "🎉 **ការបង់ប្រាក់ជោគជ័យ!** អ្នកអាចប្រើប្រាស់បានដោយសេរី។")
        else:
            await message.reply(f"⚠️ User {target_user_id} មានក្នុង Database រួចហើយ។")
            
    except Exception as e:
        await message.reply(f"⚠️ Error: {e}")

# ៤. ទាញយកវីដេអូ
@dp.message_handler()
async def download_video(message: types.Message):
    user_id = message.from_user.id

    # ឆែកសិទ្ធិក្នុង MongoDB
    if not is_user_paid(user_id) and user_id != ADMIN_ID:
        await message.reply("⛔️ អ្នកមិនទាន់បានបង់ប្រាក់ទេ។ សូមចុច /start។")
        return

    url = message.text.strip()
    if "tiktok.com" in url or "facebook.com" in url or "fb.watch" in url:
        status_msg = await message.reply("⏳ កំពុងដំណើរការ...")
        try:
            loop = asyncio.get_event_loop()
            filename = await loop.run_in_executor(None, download_logic, url)
            
            if filename:
                file_size = os.path.getsize(filename) / (1024 * 1024)
                if file_size > 50:
                    await message.reply("❌ វីដេអូធំពេក (>50MB)។")
                else:
                    with open(filename, 'rb') as video:
                        await message.answer_video(video, caption="✅ Downloaded (Premium)")
                if os.path.exists(filename): os.remove(filename)
                await bot.delete_message(message.chat.id, status_msg.message_id)
            else:
                await message.reply("❌ ទាញយកមិនបាន។")
        except Exception as e:
            await message.reply(f"Error: {e}")
    else:
        await message.reply("⚠️ Link មិនត្រឹមត្រូវ។")

def download_logic(url):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except: return None

# --- Start ---
async def on_startup(_):
    await start_web_server()
    print("🤖 MongoDB Bot Started!")

if __name__ == '__main__':

    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
