import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from src.config import BOT_TOKEN
from src.handlers import router

# កំណត់ការបង្ហាញ Log (ដើម្បីដឹងថា Bot កំពុងធ្វើអ្វីខ្លះ)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

async def main():
    # បង្កើត Bot និង Dispatcher
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # ដាក់បញ្ចូល Router ដែលយើងបានសរសេរក្នុង handlers.py
    dp.include_router(router)

    print("🚀 Bot is starting...")
    
    # លុប Webhook ចាស់ចោល (ការពារកុំឱ្យ Bot ឆ្លើយសារចាស់ៗដែលគាំង)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # ចាប់ផ្តើមដំណើរការ (Polling)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped!")