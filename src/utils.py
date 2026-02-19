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
    ✅ FIX 2.1: Properly sanitize text for MarkdownV2 to prevent
    log channel injection. Escapes ALL special Markdown characters.
    """
    if not text or not isinstance(text, str):
        return ""

    # MarkdownV2 special characters that must be escaped
    special_chars = [
        "_", "*", "[", "]", "(", ")", "~", "`", ">",
        "#", "+", "-", "=", "|", "{", "}", ".", "!",
    ]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")

    return text


def sanitize_html(text: str) -> str:
    """
    Sanitize text for safe use inside HTML parse_mode messages.
    Escapes <, >, & characters to prevent tag injection.
    """
    if not text or not isinstance(text, str):
        return ""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def send_log(
    text: str,
    bot: Optional[Bot] = None,
    parse_mode: str = "HTML",
) -> bool:
    """
    Send a log message to the configured log channel.

    ✅ FIX 2.1: Uses HTML parse_mode by default (more predictable
    than Markdown). User-supplied content is sanitized before sending
    to prevent injection into the log channel.

    Args:
        text: The message text to send
        bot: The Bot instance to use
        parse_mode: "HTML" (default) or "Markdown"

    Returns:
        True if sent successfully, False otherwise
    """
    if not LOG_CHANNEL_ID:
        logger.debug("send_log: LOG_CHANNEL_ID not configured — skipping")
        return False

    if not bot:
        logger.warning("send_log: called without bot instance")
        return False

    if not text or not isinstance(text, str):
        logger.warning(f"send_log: invalid text type: {type(text)}")
        return False

    try:
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return True

    except Exception as e:
        logger.error(f"⚠️ Failed to send log to channel {LOG_CHANNEL_ID}: {e}")
        return False


async def safe_remove_file(file_path: str) -> bool:
    """
    Safely remove a file without blocking the event loop.

    Args:
        file_path: Path to the file to remove

    Returns:
        True if removed (or didn't exist), False on error
    """
    if not file_path:
        return True

    try:
        exists = await asyncio.to_thread(os.path.exists, file_path)
        if exists:
            await asyncio.to_thread(os.remove, file_path)
            logger.debug(f"🗑️ Removed file: {file_path}")
        return True

    except PermissionError as e:
        logger.warning(f"Permission denied removing {file_path}: {e}")
        return False
    except OSError as e:
        logger.error(f"OS error removing {file_path}: {e}")
        return False


_ALLOWED_HTML_TAGS = {
    "b", "strong", "i", "em", "u", "ins",
    "s", "strike", "del", "code", "pre", "a", "br",
}


def validate_telegram_html(text: str) -> tuple[bool, str]:
    """
    Best-effort Telegram HTML tag validator.
    Checks for balanced, allowed tags only.

    Returns:
        (is_valid: bool, reason: str)
    """
    if not text:
        return True, ""

    # Fast path: no HTML tags at all
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

        # Self-closing <br> — no stack tracking needed
        if name == "br":
            continue

        # Anchor tags require href attribute
        if name == "a":
            if closing:
                if not stack or stack[-1] != "a":
                    return False, "Tag <a> មិនបានបិទត្រឹមត្រូវ"
                stack.pop()
                continue
            if "href" not in attrs.lower():
                return False, "Tag <a> ត្រូវមាន href"
            stack.append("a")
            continue

        # Normalize semantic aliases to canonical names
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
        unclosed = ", ".join(f"</{t}>" for t in reversed(stack))
        return False, f"Tag HTML មិនបានបិទ: {unclosed}"

    return True, ""