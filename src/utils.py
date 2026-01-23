from aiogram import Bot
from src.config import LOG_CHANNEL_ID

async def send_log(bot: Bot, text: str):
    """
    មុខងារសម្រាប់ផ្ញើ Log ទៅកាន់ Channel Admin។
    
    Parameters:
    - bot: Object របស់ Bot ដើម្បីធ្វើការផ្ញើ
    - text: អត្ថបទដែលចង់ផ្ញើ (អាចប្រើ Markdown បាន)
    """
    # បើមិនបានដាក់ Channel ID ក្នុង .env ទេ គឺមិនធ្វើអ្វីទាំងអស់
    if not LOG_CHANNEL_ID:
        return

    try:
        # ផ្ញើសារទៅ Channel
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID, 
            text=text, 
            parse_mode="Markdown",
            disable_web_page_preview=True # បិទកុំឱ្យលោត Link Preview រញ៉េរញ៉ៃ
        )
    except Exception as e:
        # បើផ្ញើមិនចេញ (ឧទាហរណ៍ Bot មិនមែនជា Admin ក្នុង Channel)
        # យើងគ្រាន់តែ Print Error ក្នុង Terminal តែមិនឱ្យ Bot គាំងទេ
        print(f"⚠️ Failed to send log to channel: {e}")