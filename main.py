import asyncio
import logging
import sys
import os
from aiogram import Bot, Dispatcher
from aiohttp import web  # <--- ថែម Library នេះ
from src.config import BOT_TOKEN
from src.handlers import router

# កំណត់ការបង្ហាញ Log
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ផ្នែក Web Server (សម្រាប់បន្លំ Render) ---
async def health_check(request):
    return web.Response(text="Bot is running smoothly! 🚀")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render នឹងផ្តល់ PORT មកឱ្យយើងតាមរយៈ Environment Variable
    port = int(os.getenv("PORT", 8080)) 
    
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌍 Web server started on port {port}")

# --- ផ្នែក Bot ---
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("🚀 Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)

    # Run ទាំង Bot និង Web Server ព្រមគ្នា
    await asyncio.gather(
        dp.start_polling(bot),
        start_web_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped!")