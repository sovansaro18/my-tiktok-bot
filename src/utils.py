from aiogram import Bot
from src.config import LOG_CHANNEL_ID

async def send_log(bot: Bot, text: str):

    if not LOG_CHANNEL_ID:
        return

    try:
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID, 
            text=text, 
            parse_mode="Markdown",
            disable_web_page_preview=True 
        )
    except Exception as e:

        print(f"⚠️ Failed to send log to channel: {e}")