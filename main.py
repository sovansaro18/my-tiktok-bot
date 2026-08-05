import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from src.config import BOT_TOKEN, PORT, ADMIN_ID, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW
from src.handlers import router
from src.middleware import RateLimitMiddleware
from src.database import db
from src.downloader import downloader

# ====== Logging Configuration ======
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

# ====== Global State for Cleanup ======
_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_runner: Optional[web.AppRunner] = None
_shutdown_event: Optional[asyncio.Event] = None


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint for container orchestration."""
    return web.Response(text="Bot is running smoothly! 🚀", status=200)


async def start_web_server() -> web.AppRunner:
    """
    Start the health check web server.
    
    Returns:
        The AppRunner instance for cleanup.
    """
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌍 Web server started on port {PORT}")
    return runner


async def cleanup() -> None:
    """
    Cleanup all resources gracefully.
    Called on shutdown signal or exception.
    """
    global _bot, _dp, _runner
    
    logger.info("🧹 Starting cleanup...")
    
    # Stop polling first
    if _dp:
        try:
            await _dp.stop_polling()
            logger.info("✅ Stopped polling")
        except Exception as e:
            logger.error(f"Error stopping polling: {e}")
    
    # Close bot session
    if _bot:
        try:
            await _bot.session.close()
            logger.info("✅ Closed bot session")
        except Exception as e:
            logger.error(f"Error closing bot session: {e}")
    
    # Close database connection
    if db:
        try:
            await db.close()
            logger.info("✅ Closed database connection")
        except Exception as e:
            logger.error(f"Error closing database: {e}")
    
    # Shutdown downloader thread pool
    try:
        downloader.shutdown(wait=True)
        logger.info("✅ Downloader shutdown complete")
    except Exception as e:
        logger.error(f"Error shutting down downloader: {e}")
    
    # Cleanup web server
    if _runner:
        try:
            await _runner.cleanup()
            logger.info("✅ Web server cleanup complete")
        except Exception as e:
            logger.error(f"Error cleaning up web server: {e}")
    
    logger.info("🛑 Cleanup complete.")


def handle_shutdown_signal(sig: signal.Signals) -> None:
    """
    Handle shutdown signals (SIGINT, SIGTERM).
    """
    logger.info(f"📛 Received signal {sig.name}, initiating graceful shutdown...")
    if _shutdown_event:
        _shutdown_event.set()


async def main() -> None:
    """Main entry point for the bot."""
    global _bot, _dp, _runner, _shutdown_event
    
    _shutdown_event = asyncio.Event()
    
    # Setup signal handlers
    loop = asyncio.get_running_loop()
    
    # Handle SIGINT (Ctrl+C) and SIGTERM (Docker stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: handle_shutdown_signal(s)
            )
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            # Fallback to default behavior
            logger.warning(f"Signal handler for {sig.name} not supported on this platform")
    
    # Initialize bot normally since Outbound IPv6 is now enabled on Railway
    _bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # Register command menu (non-fatal if the network is temporarily down).
    try:
        await _bot.set_my_commands(
            [
                BotCommand(command="start", description="ចាប់ផ្តើម"),
                BotCommand(command="plan", description="គម្រោង"),
                BotCommand(command="report", description="ជូនដំណឹង"),
            ]
        )
    except Exception as e:
        logger.warning(f"Could not register command menu: {e}")
        
    # Admin-only commands, visible just in the admin's command menu.
    try:
        await _bot.set_my_commands(
            [
                BotCommand(command="stats", description="ស្ថិតិ bot (Admin)"),
                BotCommand(command="broadcast", description="ផ្សាយសារ (Admin)"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception as e:
        logger.warning(f"Could not register admin command menu: {e}")
    
    # Initialize dispatcher
    _dp = Dispatcher()
    _dp.message.middleware(RateLimitMiddleware(limit=RATE_LIMIT_REQUESTS, window=RATE_LIMIT_WINDOW))
    _dp.include_router(router)
    
    # Start web server
    _runner = await start_web_server()

    try:
        logger.info("🚀 Bot is starting...")
        
        # Start polling in background
        polling_task = asyncio.create_task(
            _dp.start_polling(_bot, handle_signals=False)
        )
        
        # Wait for shutdown signal
        await _shutdown_event.wait()
        
        # Cancel polling task
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
            
    except Exception as e:
        logger.error(f"❌ Critical error: {e}")
    finally:
        await cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, exiting...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
