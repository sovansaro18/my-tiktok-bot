import asyncio
import logging
import os
from html import escape
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from src.config import ADMIN_ID, LOG_CHANNEL_ID
from src.database import db
from src.downloader import downloader
from src.utils import send_log, safe_remove_file

router = Router()
logger = logging.getLogger(__name__)

# ====== Security: URL Validation ======
ALLOWED_DOMAINS = [
    'youtube.com', 'youtu.be', 'www.youtube.com', 'm.youtube.com',
    'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com',
    'facebook.com', 'www.facebook.com', 'fb.watch', 'm.facebook.com',
    'instagram.com', 'www.instagram.com',
    'twitter.com', 'www.twitter.com', 'x.com', 'www.x.com',
]

MAX_URL_LENGTH = 2048
DOWNLOAD_TIMEOUT = 300  # 5 minutes
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49MB for Telegram


def validate_url(url: str) -> tuple[bool, Optional[str]]:
    """Validate URL for security."""
    if not url:
        return False, "URL is empty"
    
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL too long (max {MAX_URL_LENGTH} characters)"
    
    try:
        parsed = urlparse(url)
        
        if parsed.scheme not in ['http', 'https']:
            return False, "Only HTTP/HTTPS URLs are allowed"
        
        netloc_lower = parsed.netloc.lower()
        if any(blocked in netloc_lower for blocked in ['localhost', '127.0.0.1', '0.0.0.0', '::1', '192.168.', '10.', '172.16.']):
            return False, "Internal URLs are not allowed"
        
        if not any(domain in netloc_lower for domain in ALLOWED_DOMAINS):
            return False, "Platform not supported. Supported: YouTube, TikTok, Facebook, Instagram, Twitter/X"
        
        return True, None
        
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False, "Invalid URL format"


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    """
    Safely delete a message without raising exceptions.
    
    Returns:
        True if deleted successfully or message doesn't exist
        False if deletion failed due to other errors
    """
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"✅ Deleted message {message_id}")
        return True
    except TelegramBadRequest as e:
        if "message to delete not found" in str(e).lower():
            logger.info(f"ℹ️ Message {message_id} already deleted or not found")
            return True  # Consider it success since message is gone
        elif "message can't be deleted" in str(e).lower():
            logger.warning(f"⚠️ Cannot delete message {message_id} (too old or permission issue)")
            return False
        else:
            logger.error(f"❌ Error deleting message {message_id}: {e}")
            return False
    except Exception as e:
        logger.error(f"❌ Unexpected error deleting message {message_id}: {e}")
        return False


def get_usage_notification(downloads_count: int, status: str) -> dict:
    """
    Generate usage notification message with premium promotion.
    
    Returns: dict with 'text' and 'keyboard'
    """
    remaining = max(0, 10 - downloads_count)
    
    if status == "premium":
        return {
            "text": (
                "✅ <b>ទាញយករួចរាល់!</b>\n\n"
                "💎 <b>Premium Member</b>\n"
                "♾️ ប្រើបានមិនកំណត់\n\n"
                "<i>អរគុណសម្រាប់ការប្រើប្រាស់❤️!</i>"
            ),
            "keyboard": None
        }
    
    # Free user
    if remaining > 0:
        # Calculate percentage
        percentage = (remaining / 10) * 100
        
        # Progress bar
        filled = int(remaining / 2)  # 10 downloads = 5 filled blocks
        empty = 5 - filled
        progress_bar = "🟩" * filled + "⬜" * empty
        
        text = (
            f"📢 <b>ស្ថានភាពការប្រើប្រាស់</b>\n\n"
            f"🎞️ <b>បានទាញយក:</b> {downloads_count}/10\n"
            f"📊 <b>នៅសល់:</b> {remaining} ដងទៀត\n"
            f"{progress_bar} {percentage:.0f}%\n\n"
        )
        
        # Add premium promotion if running low
        if remaining <= 3:
            text += (
                "⚠️ <b>ជិតអស់ហើយ!</b>\n\n"
                "🎉 <b>ទិញ Premium ដើម្បីប្រើបានរហូត!</b>\n"
                "💰 បញ្ចុះតម្លៃ 34%! ~~$3.00~~ → <b>$1.99</b> 🔥\n"
                "⚡ សម្រាប់ 15នាក់ដំបូង (1/15 ទិញរួច)\n\n"
                "<i>បង់ម្តង ប្រើរហូត មិនផុតកំណត់!</i>"
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💎 ទិញឥឡូវនេះ $1.99!",
                    callback_data="buy_premium"
                )]
            ])
        else:
            text += (
                "💡 <b>Tip:</b> ចង់ប្រើមិនកំណត់?\n"
                "Upgrade ទៅ Premium ត្រឹមតែ $1.99! 💎"
            )
            keyboard = None
        
        return {"text": text, "keyboard": keyboard}
    
    # No downloads remaining
    return {
        "text": (
            "🚫 <b>អស់ការទាញយករបស់អ្នកហើយ!</b>\n\n"
            "📊 ប្រើអស់: 10/10 ដង\n\n"
            "🎉 <b>ទិញ Premium ដើម្បីប្រើបានរហូត!</b>\n"
            "💰 បញ្ចុះតម្លៃ 34%! ~~$3.00~~ → <b>$1.99</b> 🔥\n"
            "⚡ សម្រាប់ 15នាក់ដំបូង (1/15 ទិញរួច)\n\n"
            "✅ ទាញយកគ្មានដែនកំណត់\n"
            "✅ Support 24/7\n"
            "✅ ល្បឿនរហ័ស\n\n"
            "<i>បង់ម្តង ប្រើរហូត!</i>"
        ),
        "keyboard": InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💎 ទិញឥឡូវនេះ $1.99!",
                callback_data="buy_premium"
            )]
        ])
    }


class DownloadState(StatesGroup):
    waiting_for_format = State()


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_data, is_new = await db.get_user(user_id)
    
    if is_new:
        await send_log(
            f"🆕 New User Joined: {message.from_user.full_name} (`{user_id}`)",
            bot=message.bot
        )

    status = user_data.get("status")
    downloads_count = user_data.get("downloads_count", 0)
    
    if status == "premium":
        status_icon = "💎"
        status_text = "PREMIUM"
        downloads_text = "Unlimited ♾️"
    else:
        status_icon = "🆓"
        status_text = "FREE"
        remaining = max(0, 10 - downloads_count)
        downloads_text = f"{remaining}/10 នៅសល់"
    
    text = (
        f"👋 <b>សួស្តី {message.from_user.full_name}!</b>\n\n"
        f"ខ្ញុំអាចទាញយក videos ពី TikTok, FB, IG, YouTube។\n"
        f"គ្រាន់តែផ្ញើ link មកខ្ញុំ!\n\n"
        f"📊 <b>ស្ថានភាពរបស់អ្នក:</b> {status_text} {status_icon}\n"
        f"⬇️ <b>ការទាញយក:</b> {downloads_text}"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    status = user_data.get("status")
    count = user_data.get("downloads_count", 0)
    
    if status == "premium":
        status_display = "PREMIUM 💎"
        downloads_display = "Unlimited ♾️"
        usage_note = "✨ <i>អ្នកជា Premium member រីករាយជាមួយការទាញយកមិនកំណត់!</i>"
    else:
        status_display = "FREE 🆓"
        remaining = max(0, 10 - count)
        downloads_display = f"{remaining}/10 នៅសល់"
        usage_note = (
            "⚠️ <i>កំណត់: 10 downloads។ ចង់បានមិនកំណត់?</i>\n\n"
            "💎 <b>Upgrade ទៅ Lifetime Premium $1.99!</b>\n"
            "• បង់ម្តង ប្រើរហូត\n"
            "• គ្មានការបង់ប្រចាំខែ\n"
            "• ទាញយកមិនកំណត់\n\n"
            "ចុច /start ហើយជ្រើសរើស Premium!"
        )
    
    text = (
        f"📊 <b>ស្ថិតិការប្រើប្រាស់</b>\n\n"
        f"👤 អ្នកប្រើ: {message.from_user.full_name}\n"
        f"🏷 ស្ថានភាព: <b>{status_display}</b>\n"
        f"📥 ការទាញយក: <b>{downloads_display}</b>\n\n"
        f"{usage_note}"
    )
        
    await message.answer(text, parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Admin command to broadcast message to all users."""
    if message.from_user.id != ADMIN_ID:
        return
    
    # Get message text after /broadcast
    text = message.text.replace("/broadcast", "", 1).strip()
    
    if not text:
        await message.answer(
            "⚠️ <b>Usage:</b> /broadcast [your message]\n\n"
            "<b>Example:</b>\n"
            "/broadcast 🔧 Bot will be under maintenance for 30 minutes.\n\n"
            "<b>Special Commands:</b>\n"
            "/broadcast_promo - Send premium promotion with buy button",
            parse_mode="HTML"
        )
        return
    
    # Get all users from database
    try:
        # Get all users
        all_users = await db.users.find({}).to_list(length=None)
        
        total = len(all_users)
        success = 0
        failed = 0
        
        # Show progress
        progress_msg = await message.answer(
            f"📢 <b>Broadcasting...</b>\n"
            f"Total users: {total}\n"
            f"Sent: 0\n"
            f"Failed: 0",
            parse_mode="HTML"
        )
        
        # Send to each user
        for idx, user in enumerate(all_users, 1):
            user_id = user.get("user_id")
            
            try:
                # Send message with admin badge
                broadcast_text = (
                    f"📢 <b>Announcement from Admin</b>\n\n"
                    f"{text}\n\n"
                    f"<i>This is an official message from the bot administrator.</i>"
                )
                
                await message.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    parse_mode="HTML"
                )
                success += 1
                
                # Avoid Telegram rate limits (30 messages/second)
                if idx % 20 == 0:
                    await asyncio.sleep(1)
                
                # Update progress every 10 users
                if idx % 10 == 0 or idx == total:
                    await progress_msg.edit_text(
                        f"📢 <b>Broadcasting...</b>\n"
                        f"Total users: {total}\n"
                        f"✅ Sent: {success}\n"
                        f"❌ Failed: {failed}\n"
                        f"Progress: {idx}/{total} ({idx*100//total}%)",
                        parse_mode="HTML"
                    )
                
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to send to {user_id}: {e}")
        
        # Final report
        await progress_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"📊 Total users: {total}\n"
            f"✅ Successfully sent: {success}\n"
            f"❌ Failed: {failed}\n\n"
            f"<i>Failed users may have blocked the bot.</i>",
            parse_mode="HTML"
        )
        # Log to channel
        await send_log(
            f"📢 Broadcast Sent\n"
            f"By: Admin (`{ADMIN_ID}`)\n"
            f"Success: {success}/{total}\n"
            f"Message: {text[:100]}...",
            bot=message.bot
        )
        
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await message.answer(
            f"❌ <b>Broadcast Failed</b>\n\n"
            f"Error: {escape(str(e))}",
            parse_mode="HTML"
        )

@router.message(Command("broadcast_promo"))
async def cmd_broadcast_promo(message: Message):
    """Admin command to broadcast premium promotion with buy button."""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        # Get premium users count to calculate remaining slots
        stats = await db.count_users()
        premium_sold = stats['premium']
        slots_remaining = max(0, 15 - premium_sold)
        
        # Don't send if all slots are sold
        if slots_remaining == 0:
            await message.answer(
                "⚠️ <b>All discount slots are sold out!</b>\n\n"
                "All 15 lifetime discount slots have been claimed.\n"
                "Update promotion or pricing before sending.",
                parse_mode="HTML"
            )
            return
        
        # Get all FREE users only
        all_users = await db.users.find({"status": "free"}).to_list(length=None)
        
        total = len(all_users)
        success = 0
        failed = 0
        
        # Create buy button
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💎 Buy Lifetime Premium - ${1.99:.2f}!",
                    callback_data="buy_premium"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 See Premium Benefits",
                    callback_data="premium_info"
                )
            ]
        ])
        
        # Promotion message with lifetime and slots info
        promo_text = (
            "🎉 <b>LIMITED LIFETIME OFFER!</b> 🎉\n\n"
            "💎 <b>Lifetime Premium Access</b>\n"
            f"~~$3.00~~ → <b>${1.99:.2f}</b> (34% OFF!) 🔥\n\n"
            f"⚡ <b>Only {slots_remaining} slots remaining!</b>\n"
            f"📊 {premium_sold}/15 already claimed\n\n"
            "<b>🎁 What You Get (FOREVER):</b>\n"
            "✅ Unlimited downloads\n"
            "✅ No daily limits\n"
            "✅ Priority support 24/7\n"
            "✅ Faster download speeds\n"
            "✅ Ad-free experience\n"
            "✅ Early access to new features\n\n"
            "💰 <b>Pay once, use forever!</b>\n"
            f"⏰ <b>Hurry! Only {slots_remaining} lifetime slots left!</b>\n\n"
            "<i>This is a one-time payment. No recurring fees! 🚀</i>"
        )
        
        # Show progress
        progress_msg = await message.answer(
            f"💎 <b>Sending Lifetime Promo...</b>\n"
            f"Target: Free users\n"
            f"Total: {total}\n"
            f"Slots remaining: {slots_remaining}/15\n"
            f"Sent: 0",
            parse_mode="HTML"
        )
        
        # Send to each free user
        for idx, user in enumerate(all_users, 1):
            user_id = user.get("user_id")
            
            try:
                await message.bot.send_message(
                    chat_id=user_id,
                    text=promo_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                success += 1
                
                # Rate limiting
                if idx % 20 == 0:
                    await asyncio.sleep(1)
                
                # Update progress
                if idx % 10 == 0 or idx == total:
                    await progress_msg.edit_text(
                        f"💎 <b>Sending Lifetime Promo...</b>\n"
                        f"Target: Free users\n"
                        f"Total: {total}\n"
                        f"Slots remaining: {slots_remaining}/15\n"
                        f"✅ Sent: {success}\n"
                        f"❌ Failed: {failed}\n"
                        f"Progress: {idx}/{total} ({idx*100//total}%)",
                        parse_mode="HTML"
                    )
                
            except Exception as e:
                failed += 1
                logger.warning(f"Failed promo to {user_id}: {e}")
        
        # Calculate potential revenue
        potential_revenue = slots_remaining * 1.99
        
        # Final report
        await progress_msg.edit_text(
            f"✅ <b>Promotion Campaign Complete!</b>\n\n"
            f"🎯 Targeted: Free users\n"
            f"📊 Total sent: {success}\n"
            f"❌ Failed: {failed}\n\n"
            f"💎 <b>Lifetime Slots:</b>\n"
            f"• Sold: {premium_sold}/15\n"
            f"• Remaining: {slots_remaining}/15\n"
            f"• Potential revenue: ${potential_revenue:.2f}\n\n"
            f"<i>Track conversions in /stats</i>",
            parse_mode="HTML"
        )
        
        # Log
        await send_log(
            f"💎 Lifetime Promo Sent\n"
            f"Targeted: {total} free users\n"
            f"Success: {success}\n"
            f"Slots: {slots_remaining}/15 left\n"
            f"Potential: ${potential_revenue:.2f}",
            bot=message.bot
        )
        
    except Exception as e:
        logger.error(f"Promo broadcast error: {e}")
        await message.answer(f"❌ Error: {escape(str(e))}", parse_mode="HTML")

@router.callback_query(F.data == "buy_premium")
async def handle_buy_premium(callback: CallbackQuery):
    """Handle buy premium button click - Show QR Code payment."""
    
    # Check remaining slots
    stats = await db.count_users()
    premium_sold = stats['premium']
    slots_remaining = max(0, 15 - premium_sold)
    
    # Check if sold out
    if slots_remaining == 0:
        await callback.message.edit_text(
            "😢 <b>Sorry, All Slots Sold Out!</b>\n\n"
            "All 15 lifetime discount slots have been claimed.\n\n"
            "💬 Contact admin for regular pricing or future offers.",
            parse_mode="HTML"
        )
        return
    
    # Check if payment.jpg exists
    payment_qr_path = "payment.jpg"
    
    if not os.path.exists(payment_qr_path):
        await callback.message.edit_text(
            "❌ <b>Payment QR Code not found!</b>\n\n"
            "Please contact admin to set up payment method.",
            parse_mode="HTML"
        )
        logger.error(f"payment.jpg not found in project root!")
        return
    
    payment_caption = (
        "💳 <b>Lifetime Premium Payment</b>\n\n"
        f"💎 <b>Lifetime Access:</b> ${1.99:.2f} (One-time payment)\n"
        f"⚡ <b>Slots Remaining:</b> {slots_remaining}/15\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱 <b>របៀបបង់ប្រាក់:</b>\n\n"
        "1️⃣ ស្កេន QR Code ខាងក្រោម\n"
        f"2️⃣ បង់ចំនួន <b>${1.99:.2f}</b>\n"
        "3️⃣ ថតរូបវិក័យបត្រ (Screenshot)\n"
        "4️⃣ ផ្ញើវិក័យបត្រមកទីនេះវិញ\n"
        "5️⃣ រង់ចាំ Admin ពិនិត្យ និងបើកសិទ្ធ\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ <b>ពេលវេលាដំណើរការ:</b> ក្នុងរយៈពេល 1 ម៉ោង\n"
        "♾️ <b>រយៈពេលសុពលភាព:</b> LIFETIME (មិនផុតកំណត់)\n\n"
        f"🆔 <b>User ID របស់អ្នក:</b> <code>{callback.from_user.id}</code>\n"
        "<i>(សូមរក្សាទុក ID នេះ)</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>អត្ថប្រយោជន៍ Lifetime Premium:</b>\n"
        "• ទាញយកគ្មានដែនកំណត់ (ជារៀងរហូត)\n"
        "• មិនមានការរឹតបន្តឹងប្រចាំថ្ងៃ\n"
        "• ល្បឿនទាញយករហ័ស\n"
        "• គាំទ្រអាទិភាព 24/7\n"
        "• គ្មានការបង់ប្រាក់ប្រចាំខែ\n"
        "• បង់តែម្តង ប្រើរហូត! 🚀\n\n"
        f"⚠️ <b>Hurry! Only {slots_remaining} discount slots left!</b>\n\n"
        "❓ <b>មានសំណួរ?</b> ផ្ញើសារមក Admin នៅក្នុង Channel"
    )
    
    try:
        # Delete previous message
        await callback.message.delete()
        
        # Send QR Code image
        photo = FSInputFile(payment_qr_path)
        await callback.message.answer_photo(
            photo=photo,
            caption=payment_caption,
            parse_mode="HTML"
        )
        
        # Log interest with slots info
        await send_log(
            f"💰 Premium Interest\n"
            f"User: {callback.from_user.full_name} (`{callback.from_user.id}`)\n"
            f"Action: Opened payment QR Code\n"
            f"Slots remaining: {slots_remaining}/15",
            bot=callback.bot
        )
        
    except Exception as e:
        logger.error(f"Error showing QR code: {e}")
        await callback.answer(
            "❌ មានបញ្ហាក្នុងការបង្ហាញ QR Code។ សូមព្យាយាមម្តងទៀត។",
            show_alert=True
        )

@router.callback_query(F.data == "premium_info")
async def handle_premium_info(callback: CallbackQuery):
    """Show detailed premium benefits."""
    
    # Get slots info
    stats = await db.count_users()
    premium_sold = stats['premium']
    slots_remaining = max(0, 15 - premium_sold)
    
    info_text = (
        "💎 <b>Lifetime Premium Membership</b>\n\n"
        f"💰 <b>Price:</b> ~~$3.00~~ → <b>${1.99:.2f}</b>\n"
        f"⚡ <b>Slots Left:</b> {slots_remaining}/15\n"
        f"📊 <b>Already Sold:</b> {premium_sold}/15\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📥 Downloads:</b>\n"
        "✅ Unlimited downloads forever\n"
        "✅ No daily/monthly limits\n"
        "✅ All platforms supported\n"
        "✅ High-quality (up to 1080p)\n\n"
        "<b>⚡ Performance:</b>\n"
        "🚀 Priority download queue\n"
        "🚀 Faster download speeds\n"
        "🚀 Multiple concurrent downloads\n\n"
        "<b>🎯 Support:</b>\n"
        "💬 Priority customer support\n"
        "💬 Direct contact with admin\n"
        "💬 24/7 assistance\n\n"
        "<b>🎨 Features:</b>\n"
        "✨ Ad-free experience\n"
        "✨ Early access to new features\n"
        "✨ Custom preferences\n"
        "✨ Lifetime access (no expiration)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💵 <b>One-Time Payment:</b>\n"
        f"• Pay <b>${1.99:.2f}</b> once\n"
        "• Use forever\n"
        "• No monthly fees\n"
        "• No hidden charges\n\n"
        f"⚠️ <b>Limited Offer:</b> Only {slots_remaining} slots left!\n\n"
        "<i>After 15 sales, price returns to $3.00</i>"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"💳 Buy Now - ${1.99:.2f} ({slots_remaining} left)",
                callback_data="buy_premium"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Close",
                callback_data="close_info"
            )
        ]
    ])
    
    await callback.message.edit_text(
        info_text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "close_info")
async def handle_close_info(callback: CallbackQuery):
    """Close premium info message."""
    await callback.message.delete()

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Admin command to view bot statistics."""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        stats = await db.count_users()
        
        # Calculate total downloads
        pipeline = [
            {"$group": {
                "_id": None,
                "total_downloads": {"$sum": "$downloads_count"}
            }}
        ]
        
        result = await db.users.aggregate(pipeline).to_list(length=1)
        total_downloads = result[0]["total_downloads"] if result else 0
        
        # Lifetime slots info
        premium_sold = stats['premium']
        slots_remaining = max(0, 15 - premium_sold)
        lifetime_revenue = premium_sold * 1.99
        potential_revenue = slots_remaining * 1.99
        
        text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: <b>{stats['total']}</b>\n"
            f"💎 Premium Users: <b>{stats['premium']}</b>\n"
            f"🆓 Free Users: <b>{stats['free']}</b>\n\n"
            f"⬇️ Total Downloads: <b>{total_downloads}</b>\n"
            f"📈 Avg per user: <b>{total_downloads // stats['total'] if stats['total'] > 0 else 0}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>Lifetime Discount Campaign:</b>\n"
            f"• Price: ${1.99:.2f} (Lifetime)\n"
            f"• Sold: <b>{premium_sold}/15</b>\n"
            f"• Remaining: <b>{slots_remaining}/15</b>\n"
            f"• Revenue: <b>${lifetime_revenue:.2f}</b>\n"
            f"• Potential: <b>${potential_revenue:.2f}</b>\n\n"
            f"{'⚠️ <b>All slots sold out!</b>' if slots_remaining == 0 else f'✅ <b>{slots_remaining} slots available</b>'}\n\n"
            f"<i>Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await message.answer(f"❌ Error: {escape(str(e))}", parse_mode="HTML")

@router.message(Command("approve"))
async def cmd_approve(message: Message):
    # Security: Use integer comparison for admin check
    if message.from_user.id != ADMIN_ID:
        return

    try:
        target_id = int(message.text.split()[1])
        success = await db.set_premium(target_id)
        
        if success:
            await message.answer(f"✅ User {target_id} is now PREMIUM.")
            await message.bot.send_message(
                target_id, 
                "🎉 <b>Congratulations!</b> Your account has been upgraded to PREMIUM! 💎", 
                parse_mode="HTML"
            )
            await send_log(
                f"👮‍♂️ Admin approved Premium for `{target_id}`",
                bot=message.bot
            )
        else:
            await message.answer("❌ Failed to update user. Check ID.")
    except (IndexError, ValueError):
        await message.answer("⚠️ Usage: /approve [user_id]")



@router.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    if user_data.get("status") == "free" and user_data.get("downloads_count") >= 10:
        await message.answer(
            "🚫 <b>អស់ការទាញយករបស់អ្នកហើយ!</b>\n\n"
            "អ្នកបានប្រើអស់ការទាញយក 10 ដងរបស់អ្នក។\n"
            "សូម upgrade ទៅ Premium ដើម្បីបន្ត។\n\n"
            "💎 <b>ទិញ Premium:</b> ផ្ញើរូបវិក័យបត្រមកទីនេះ។",
            parse_mode="HTML"
        )
        return

    url = message.text.strip()
    
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>Invalid URL</b>\n\n{escape(error_msg or 'Unknown error')}",
            parse_mode="HTML"
        )
        return
    
    # Store URL and message IDs for cleanup
    await state.update_data(url=url, url_message_id=message.message_id)
    await state.set_state(DownloadState.waiting_for_format)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video (MP4)", callback_data="fmt_video"),
            InlineKeyboardButton(text="🎵 Audio (M4A)", callback_data="fmt_audio")
        ]
    ])
    
    format_msg = await message.answer("👇 ជ្រើសរើសប្រភេទទាញយក:", reply_markup=keyboard)
    await state.update_data(format_message_id=format_msg.message_id)


@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")
    url_message_id = data.get("url_message_id")
    format_message_id = data.get("format_message_id")
    file_path = None
    
    if not url:
        await callback.message.edit_text("⚠️ Session ផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត។")
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"
    
    progress_msg = await callback.message.edit_text(
        f"⏳ <b>កំពុងទាញយក {download_type.upper()}...</b>\n"
        f"<i>សូមរង់ចាំបន្តិច...</i>",
        parse_mode="HTML"
    )
    
    try:
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Download timeout for URL: {url}")
        await progress_msg.edit_text(
            "❌ <b>ការទាញយកយូរពេកហើយ</b>\n\n"
            "សូមព្យាយាមម្តងទៀតជាមួយ video ខ្លីជាងនេះ។",
            parse_mode="HTML"
        )
        await send_log(
            f"⏱ Download Timeout\n"
            f"User: `{callback.from_user.id}`\n"
            f"URL: {url}\n"
            f"Type: {download_type}",
            bot=callback.bot
        )
        await state.clear()
        return
    
    if result["status"] == "error":
        safe_message = escape(result.get('message', 'Unknown error'))
        await progress_msg.edit_text(f"❌ <b>Error:</b> {safe_message}", parse_mode="HTML")
        
        await send_log(
            f"❌ Download Error\n"
            f"User: {callback.from_user.full_name} (`{callback.from_user.id}`)\n"
            f"URL: {url}\n"
            f"Type: {download_type}\n"
            f"Error: {result.get('message', 'Unknown')}",
            bot=callback.bot
        )
        await state.clear()
        return

    file_path = result["file_path"]
    
    # ✅ Check file size before uploading
    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            await progress_msg.edit_text(
                f"❌ <b>File ធំពេកសម្រាប់ Telegram</b>\n\n"
                f"📊 ទំហំ: {file_size / 1024 / 1024:.1f}MB\n"
                f"⚠️ កំណត់: {MAX_FILE_SIZE / 1024 / 1024:.0f}MB\n\n"
                f"សូមព្យាយាម video គុណភាពទាបជាង ឬ audio only។",
                parse_mode="HTML"
            )
            await safe_remove_file(file_path)
            await state.clear()
            return
    
    safe_title = escape(str(result.get('title', 'Unknown')))
    safe_duration = escape(str(result.get('duration', 0)))
    
    caption = (
        f"✅ <b>ទាញយករួចរាល់!</b>\n"
        f"📌 ចំណងជើង: {safe_title}\n"
        f"⏱ រយៈពេល: {safe_duration}s\n"
        f"🤖 via @ravi_downloader_bot"
    )

    try:
        await progress_msg.edit_text("📤 <b>កំពុងបញ្ជូន...</b>", parse_mode="HTML")
        
        file_input = FSInputFile(file_path)
        
        if download_type == "audio":
            await callback.message.answer_audio(file_input, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer_video(file_input, caption=caption, parse_mode="HTML")
        
        # ✅ Safe cleanup of messages
        chat_id = callback.message.chat.id
        
        # Delete URL message
        if url_message_id:
            await safe_delete_message(callback.bot, chat_id, url_message_id)
        
        # Delete format selection message
        if format_message_id:
            await safe_delete_message(callback.bot, chat_id, format_message_id)
        
        # Delete progress message
        try:
            await progress_msg.delete()
        except Exception as e:
            logger.warning(f"Could not delete progress message: {e}")
        
        # Update stats and show notification
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        
        if user_data.get("status") == "free":
            await db.increment_download(user_id)
            
            # Get updated user data and show usage notification
            updated_user_data, _ = await db.get_user(user_id)
            downloads_count = updated_user_data.get("downloads_count", 0)
            status = updated_user_data.get("status", "free")
            
            notification = get_usage_notification(downloads_count, status)
            
            await callback.message.answer(
                notification["text"],
                parse_mode="HTML",
                reply_markup=notification["keyboard"]
            )
        else:
            # Premium user - simple success message
            notification = get_usage_notification(0, "premium")
            await callback.message.answer(
                notification["text"],
                parse_mode="HTML"
            )
            
    except TelegramBadRequest as e:
        logger.error(f"Telegram API error during upload: {e}")
        
        # Check specific error types
        error_str = str(e).lower()
        if "file is too big" in error_str or "too large" in error_str:
            error_msg = (
                "❌ <b>File ធំពេកសម្រាប់ Telegram</b>\n\n"
                "⚠️ Telegram កំណត់: 50MB\n"
                "សូមព្យាយាម video គុណភាពទាបជាង ឬ audio only។"
            )
        elif "wrong file identifier" in error_str:
            error_msg = "❌ មានបញ្ហាជាមួយ file format។ សូមព្យាយាមម្តងទៀត។"
        else:
            error_msg = f"❌ មិនអាចបញ្ជូន file បានទេ។\n\n<code>{escape(str(e)[:200])}</code>"
        
        await callback.message.answer(error_msg, parse_mode="HTML")
        
        await send_log(
            f"❌ Upload Error (Telegram)\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot
        )
        
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        await callback.message.answer(
            f"❌ មានបញ្ហាក្នុងការបញ្ជូន file។\n\n"
            f"<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML"
        )
        
        await send_log(
            f"❌ Upload Error (General)\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)[:200]}",
            bot=callback.bot
        )
    finally:
        # Always cleanup file
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()


@router.message(F.photo)
async def handle_receipt(message: Message):
    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_info = f"User: {user_name} (<code>{message.from_user.id}</code>)"
    
    await message.bot.send_photo(
        chat_id=LOG_CHANNEL_ID,
        photo=message.photo[-1].file_id,
        caption=f"🧾 <b>Payment Receipt Received</b>\n\n{user_info}\n📝 Note: {caption}\n\n👉 Use <code>/approve {message.from_user.id}</code> to confirm.",
        parse_mode="HTML"
    )
    
    await message.answer(
        "✅ <b>ទទួលវិក័យបត្ររួចរាល់!</b>\n"
        "យើងនឹងពិនិត្យហើយ upgrade គណនីរបស់អ្នកក្នុងពេលឆាប់ៗ។",
        parse_mode="HTML"
    )