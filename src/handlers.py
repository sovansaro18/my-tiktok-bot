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


def validate_url(url: str) -> tuple[bool, Optional[str]]:
    """
    Validate URL for security.
    
    Returns:
        (is_valid, error_message)
    """
    if not url:
        return False, "URL is empty"
    
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL too long (max {MAX_URL_LENGTH} characters)"
    
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if parsed.scheme not in ['http', 'https']:
            return False, "Only HTTP/HTTPS URLs are allowed"
        
        # Check for localhost/internal IPs (SSRF protection)
        netloc_lower = parsed.netloc.lower()
        if any(blocked in netloc_lower for blocked in ['localhost', '127.0.0.1', '0.0.0.0', '::1', '192.168.', '10.', '172.16.']):
            return False, "Internal URLs are not allowed"
        
        # Check domain whitelist
        if not any(domain in netloc_lower for domain in ALLOWED_DOMAINS):
            return False, "Platform not supported. Supported: YouTube, TikTok, Facebook, Instagram, Twitter/X"
        
        return True, None
        
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        return False, "Invalid URL format"

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

    status_icon = "💎" if user_data.get("status") == "premium" else "🆓"
    text = (
        f"👋 <b>Hello {message.from_user.full_name}!</b>\n\n"
        f"I can download videos from TikTok, FB, IG, YouTube, etc.\n"
        f"Just send me a link!\n\n"
        f"📊 <b>Your Status:</b> {user_data.get('status').upper()} {status_icon}\n"
        f"⬇️ <b>Downloads:</b> {user_data.get('downloads_count')}/10 (Free Tier)"
    )
    
    if user_data.get("status") == "premium":
        text = text.replace("/10 (Free Tier)", " (Unlimited)")

    await message.answer(text, parse_mode="HTML")

@router.message(Command("plan"))
async def cmd_plan(message: Message):
    user_id = message.from_user.id
    user_data, _ = await db.get_user(user_id)
    
    status = user_data.get("status")
    count = user_data.get("downloads_count")
    
    text = (
        f"📊 <b>Usage Statistics</b>\n\n"
        f"👤 User: {message.from_user.full_name}\n"
        f"🏷 Status: <b>{status.upper()}</b>\n"
        f"🔢 Total Downloads: {count}\n\n"
    )
    
    if status == "free":
        text += "⚠️ <i>Limit: 10 downloads. Upgrade to Premium for unlimited access!</i>"
    else:
        text += "✨ <i>You are a Premium member. Enjoy unlimited downloads!</i>"
        
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
            "/broadcast 🔧 Bot will be under maintenance for 30 minutes.",
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
                    f"<i>This is an official message from the bot administrator.</i>.<b>RAVI</b>"
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
        slots_remaining = max(0, 15 - premium_sold)  # 15 slots total
        
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
            "⏰ <b>Hurry! Only {slots_remaining} lifetime slots left!</b>\n\n"
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
        "• Pay <b>${1.99:.2f}</b> once\n"
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
            "🚫 <b>Free Limit Reached!</b>\n\n"
            "You have used your 10 free downloads.\n"
            "Please upgrade to Premium to continue.\n\n"
            "💸 <b>To Upgrade:</b> Send a photo of your payment receipt here.",
            parse_mode="HTML"
        )
        return

    url = message.text.strip()
    
    # Security: Validate URL before processing
    is_valid, error_msg = validate_url(url)
    if not is_valid:
        await message.answer(
            f"⚠️ <b>Invalid URL</b>\n\n{escape(error_msg or 'Unknown error')}",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(url=url)
    await state.set_state(DownloadState.waiting_for_format)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video (MP4)", callback_data="fmt_video"),
            InlineKeyboardButton(text="🎵 Audio (M4A)", callback_data="fmt_audio")
        ]
    ])
    
    await message.answer("👇 Choose download format:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("fmt_"))
async def process_download_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")
    file_path = None
    
    if not url:
        await callback.message.edit_text("⚠️ Session expired. Please send the link again.")
        return

    download_type = "audio" if callback.data == "fmt_audio" else "video"
    
    await callback.message.edit_text(
        f"⏳ <b>Downloading {download_type.upper()}...</b>\n"
        f"<i>Please wait, this may take a moment.</i>",
        parse_mode="HTML"
    )
    
    try:
        # Security: Add timeout for downloads
        result = await asyncio.wait_for(
            downloader.download(url, type=download_type),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Download timeout for URL: {url}")
        await callback.message.edit_text(
            "❌ <b>Download Timeout</b>\n\n"
            "The download took too long. Please try again with a shorter video.",
            parse_mode="HTML"
        )
        
        # ✅ FIX: Send error notification to admin
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
        # Security: Escape error message to prevent XSS
        safe_message = escape(result.get('message', 'Unknown error'))
        await callback.message.edit_text(f"❌ <b>Error:</b> {safe_message}", parse_mode="HTML")
        
        # ✅ FIX: Send error notification to admin with detailed info
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
    
    # Security: Escape title and other user-controllable data (XSS prevention)
    safe_title = escape(str(result.get('title', 'Unknown')))
    safe_duration = escape(str(result.get('duration', 0)))
    
    caption = (
        f"✅ <b>Downloaded Successfully!</b>\n"
        f"📌 Title: {safe_title}\n"
        f"⏱ Duration: {safe_duration}s\n"
        f"🤖 via @ravi_downloader_bot"
    )

    try:
        await callback.message.edit_text("📤 <b>Uploading...</b>", parse_mode="HTML")
        
        file_input = FSInputFile(file_path)
        
        if download_type == "audio":
            await callback.message.answer_audio(file_input, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer_video(file_input, caption=caption, parse_mode="HTML")
            
        # Update stats for free users
        user_id = callback.from_user.id
        user_data, _ = await db.get_user(user_id)
        if user_data.get("status") == "free":
            await db.increment_download(user_id)
        
        # ✅ Don't spam admin with success messages
        # Only log errors, not successes
            
        await callback.message.delete()
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await callback.message.edit_text("❌ Failed to upload file. It might be too large for Telegram.")
        
        # ✅ FIX: Send upload error to admin
        await send_log(
            f"❌ Upload Error\n"
            f"User: `{callback.from_user.id}`\n"
            f"Error: {str(e)}",
            bot=callback.bot
        )
    finally:
        # Security: Use async file removal to avoid blocking event loop
        if file_path:
            await safe_remove_file(file_path)
        await state.clear()

@router.message(F.photo)
async def handle_receipt(message: Message):
    # Security: Escape user-provided caption to prevent XSS
    caption = escape(message.caption or "No caption")
    user_name = escape(message.from_user.full_name)
    user_info = f"User: {user_name} (<code>{message.from_user.id}</code>)"
    
    await message.bot.send_photo(
        chat_id=LOG_CHANNEL_ID,
        photo=message.photo[-1].file_id,
        caption=f"🧾 <b>Payment Receipt Received</b>\n\n{user_info}\n📝 Note: {caption}\n\n👉 Use <code>/approve {message.from_user.id}</code> to confirm.",
        parse_mode="HTML"
    )
    
    await message.answer("✅ <b>Receipt Received!</b>\nWe will review it and upgrade your account shortly.", parse_mode="HTML")