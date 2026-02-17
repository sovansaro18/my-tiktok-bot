import asyncio
import logging
import os
import re
from typing import Optional

from aiogram import Bot
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)

def sanitize_markdown(text: str) -> str:
    """
    Sanitize text for Markdown to prevent injection.
    Escapes special Markdown characters.
    """
    if not text or not isinstance(text, str):
        return ""
    
    # Escape Markdown special characters
    special_chars = ['_', '*', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    
    return text


async def send_log(text: str, bot: Optional[Bot] = None) -> bool:
    """
    Send log message to the configured log channel.
    
    Args:
        text: The message text to send
        bot: The Bot instance to use for sending
        
    Returns:
        True if successful, False otherwise
    """
    if not LOG_CHANNEL_ID:
        logger.warning("send_log called without LOG_CHANNEL_ID configured")
        return False
        
    if not bot:
        logger.warning("send_log called without bot instance")
        return False
    
    if not text or not isinstance(text, str):
        logger.warning(f"send_log called with invalid text: {type(text)}")
        return False

    try:
        # Sanitize text before sending
        safe_text = sanitize_markdown(text) if text else "Empty log message"
        
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID, 
            text=safe_text, 
            parse_mode="Markdown",
            disable_web_page_preview=True 
        )
        return True
        
    except Exception as e:
        # Use logger instead of print
        logger.error(f"⚠️ Failed to send log to channel: {e}")
        return False


async def safe_remove_file(file_path: str) -> bool:
    """
    Safely remove a file asynchronously without blocking the event loop.
    
    Args:
        file_path: Path to the file to remove
        
    Returns:
        True if file was removed or didn't exist, False on error
    """
    if not file_path:
        return True
        
    try:
        # Run blocking os operations in a thread pool
        exists = await asyncio.to_thread(os.path.exists, file_path)
        if exists:
            await asyncio.to_thread(os.remove, file_path)
            logger.debug(f"Removed file: {file_path}")
        return True
        
    except PermissionError as e:
        logger.warning(f"Permission denied when removing file {file_path}: {e}")
        return False
    except OSError as e:
        logger.error(f"Error removing file {file_path}: {e}")
        return False


_ALLOWED_HTML_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "a",
    "br",
}


def validate_telegram_html(text: str) -> tuple[bool, str]:
    """Best-effort Telegram HTML validation.

    Telegram rejects messages with malformed/unbalanced tags. This validator focuses on
    common tags used by admins in broadcasts.

    Returns:
        (ok, reason)
    """

    if not text:
        return True, ""

    # Fast path: no tags
    if "<" not in text and ">" not in text:
        return True, ""

    tag_re = re.compile(r"<\s*(/)?\s*([a-zA-Z0-9]+)([^>]*)>")
    stack: list[str] = []

    for m in tag_re.finditer(text):
        closing = bool(m.group(1))
        name = (m.group(2) or "").lower()
        attrs = m.group(3) or ""

        if name not in _ALLOWED_HTML_TAGS:
            return False, f"Tag មិនអនុញ្ញាត: <{name}>"

        if name == "br":
            continue

        if name == "a":
            if closing:
                if not stack or stack[-1] != "a":
                    return False, "Tag <a> មិនបានបិទត្រឹមត្រូវ"
                stack.pop()
                continue
            # opening <a>
            if "href" not in attrs.lower():
                return False, "Tag <a> ត្រូវមាន href"
            stack.append("a")
            continue

        # Normalize alias tags
        aliases = {
            "strong": "b",
            "em": "i",
            "ins": "u",
            "strike": "s",
            "del": "s",
        }
        name = aliases.get(name, name)

        if closing:
            if not stack or stack[-1] != name:
                return False, f"Tag </{name}> មិនត្រូវជាមួយ tag បើក"
            stack.pop()
        else:
            stack.append(name)

    if stack:
        # Most common: missing closing tag
        return False, f"Tag HTML មិនបានបិទ: {', '.join(f'</{t}>' for t in reversed(stack))}"

    return True, ""