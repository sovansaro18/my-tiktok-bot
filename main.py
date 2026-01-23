import asyncio
import logging
import sys
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import BOT_TOKEN, PORT
from src.handlers import router
from src.middleware import RateLimitMiddleware
from src.database import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

async def health_check(request):
    return web.Response(text="Bot is running smoothly! 🚀", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌍 Web server started on port {PORT}")

async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    dp = Dispatcher()
    
    dp.message.middleware(RateLimitMiddleware(limit=3, window=10))
    
    dp.include_router(router)

    await start_web_server()

    try:
        logger.info("🚀 Bot is starting...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Critical error: {e}")
    finally:
        await bot.session.close()
        if db:
            await db.close()
        logger.info("🛑 Bot stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass