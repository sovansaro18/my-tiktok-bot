# Technical Handover Report: TikTok Bot
**Project:** Multi-Platform Video Downloader Bot for Telegram  
**Date:** January 23, 2026  
**Language:** Python 3.10  

---

## 1. Project Architecture

### Tech Stack
- **Framework:** aiogram 3.0+ (Telegram Bot API wrapper)
- **Async Runtime:** asyncio with aiohttp
- **Media Processing:** yt-dlp (YouTube Download Library)
- **Database:** MongoDB with Motor (async driver)
- **Deployment:** Docker containerized
- **Web Server:** aiohttp (health check endpoint)

### Folder Structure
```
my-tiktok-bot/
├── main.py              # Entry point, Bot initialization + web server
├── Dockerfile           # Containerization (Python 3.10 + FFmpeg)
├── requirements.txt     # Dependency list
└── src/
    ├── config.py        # Environment variables + validation
    ├── database.py      # MongoDB async operations
    ├── downloader.py    # Media download logic (yt-dlp wrapper)
    ├── handlers.py      # Command & message handlers (aiogram routes)
    ├── utils.py         # Logging utility
    └── __init__.py
```

### Architecture Pattern
- **Async-first design:** All I/O operations use asyncio for non-blocking concurrency
- **Modular separation:** Each module handles one responsibility (config, DB, downloads, handlers)
- **Singleton pattern:** Global instances (`db` in database.py, `downloader` in downloader.py, `router` in handlers.py)
- **Layered approach:** Main → Handlers → Downloader/Database → yt-dlp/MongoDB

---

## 2. Core Logic & Function Interactions

### Request Flow
```
User Message/Callback
    ↓
handlers.py (Route matching: @router.message/@router.callback_query)
    ↓
Validate URL & Check User Limits (database.py: get_user, check status)
    ↓
Display Download Type Selection (Video/Audio callback buttons)
    ↓
process_download() → downloader.py (async download)
    ↓
Send Result to User + Cleanup + Log
    ↓
Update usage count (database.py: increment_download if not premium)
```

### Key Functions

#### **handlers.py**
- `cmd_start()` – Welcome message, display user status & available downloads
- `cmd_plan()` – Show current usage stats
- `cmd_help()` – Display usage instructions
- `cmd_approve(user_id)` – Admin-only: upgrade user to Premium tier
- `handle_receipt()` – Process payment proof (photos), forward to admin channel
- `handle_link()` – Main handler: validate URL and present download options
- `process_download()` – Execute download, send file, update counts, cleanup

#### **downloader.py**
- `_download_sync()` – Synchronous yt-dlp execution (runs in ThreadPoolExecutor)
  - Validates file size (max 49MB for Telegram limit)
  - Returns status dict with file path or error details
- `download()` – Async wrapper that delegates to thread pool

#### **database.py**
- `get_user(user_id)` – Fetch or create user record, returns user dict + is_new flag
- `increment_download(user_id)` – Increment downloads_count +1 for free users
- `set_premium(user_id)` – Upgrade user status to "premium"
- `count_users()` – Return total users and premium count stats

#### **config.py**
- Load & validate environment variables (BOT_TOKEN, MONGO_URI, ADMIN_ID, LOG_CHANNEL_ID)
- Raise ValueError if critical configs missing

#### **utils.py**
- `send_log()` – Send formatted messages to admin log channel (Markdown support)

#### **main.py**
- Initialize Bot and Dispatcher with router
- Start polling (message receiving)
- Start aiohttp web server on configurable port (default 8080)
- Health check endpoint: `GET /` returns "Bot is running smoothly! 🚀"

---

## 3. Database Schema

### MongoDB Database: `downloader_bot`

#### Collection: `users`
```json
{
  "_id": ObjectId,
  "user_id": 123456789,           // Telegram user ID (primary identifier)
  "status": "free" | "premium",   // Tier: "free" (10 downloads) or "premium" (unlimited)
  "downloads_count": 5,            // Integer: incremented after each non-premium download
  "joined_date": null             // Placeholder: can store datetime.now() if needed
}
```

**Indexes:** Implicit on `user_id` for fast lookups  
**CRUD Operations:**
- **Create:** Auto-insert on first /start
- **Read:** find_one() by user_id
- **Update:** update_one() with $inc or $set operators
- **Delete:** Not implemented (preserve user history)

---

## 4. Implemented Features

### User Features
✅ **Download Media**
- Support: TikTok, Facebook, Instagram, YouTube, Twitter/X
- Dual format: Video (MP4) or Audio (M4A)
- File size validation (max 49MB for Telegram)
- Progress feedback ("Downloading...", "Uploading...")

✅ **Freemium Model**
- Free tier: 10 downloads per user, then blocked
- Premium tier: Unlimited downloads
- User status visible on /start and /plan commands

✅ **Payment Flow**
- User sends receipt photo
- Auto-forward to admin channel with approval command
- Admin runs `/approve [user_id]` to grant premium

✅ **Help & Info**
- `/start` – Welcome + account status
- `/plan` – Usage statistics
- `/help` – Instructions

### Admin Features
✅ **Premium Upgrade:** `/approve [user_id]` command  
✅ **Logging:** All user actions logged to designated Telegram channel
- New user join
- Premium upgrades
- Download errors
- Payment receipts

✅ **Health Monitoring:** Web endpoint for deployment checks (Render, Railway, etc.)

### Technical Features
✅ **Async Concurrency:** Non-blocking download with 2-worker max (prevents server overload)  
✅ **Error Handling:** Graceful failures, user-friendly error messages  
✅ **Docker Ready:** Containerized with FFmpeg dependency  
✅ **File Cleanup:** Temporary files deleted after upload  

---

## 5. Current Coding Task & Next Steps

### What I'm Working On
The bot has core functionality complete and running. The current state shows:
- ✅ All command handlers implemented
- ✅ Download pipeline working (URL validation → format selection → download → upload)
- ✅ User tier system (free vs premium) functional
- ✅ Admin approval workflow integrated
- ✅ Logging infrastructure in place

### Logical Next Steps

**Phase 1: Stability & Robustness** (Immediate)
1. Add retry logic for failed downloads (network timeouts)
2. Implement request rate limiting per user (prevent abuse)
3. Add database connection pooling config
4. Test error paths (malformed URLs, unavailable videos, network failures)

**Phase 2: Analytics & Monitoring** (Short-term)
1. Expand `/plan` command with detailed stats (total bandwidth used, most common sources)
2. Add `/stats` admin command (daily active users, revenue-pending approvals)
3. Implement download speed metrics logging
4. Create dashboard using MongoDB aggregation pipelines

**Phase 3: Feature Enhancement** (Medium-term)
1. Batch download support (multiple URLs in one message)
2. Video quality/resolution picker for YouTube
3. Subtitle extraction for video downloads
4. Bot command menu with Telegram's setMyCommands() API
5. Expiring premium subscriptions (time-based vs lifetime)

**Phase 4: Deployment & DevOps** (Ongoing)
1. Add environment-specific configs (.env.local, .env.production)
2. Implement structured logging (JSON format for parsing)
3. Add pre-commit hooks (linting, type checking with mypy)
4. Database backup strategy (MongoDB Atlas or self-managed)
5. CI/CD pipeline (GitHub Actions for testing & Docker image push)

---

## 6. Environment Setup Requirements

### .env File (Required)
```
BOT_TOKEN=<your_telegram_bot_token>
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>/<database>?retryWrites=true&w=majority
ADMIN_ID=<your_telegram_user_id>
LOG_CHANNEL_ID=<telegram_channel_id_for_logs>
PORT=8080  # Optional (defaults to 8080)
```

### System Dependencies
- Python 3.10+
- FFmpeg (for yt-dlp media processing)
- MongoDB instance (local or Atlas cloud)

### Deployment Environments Supported
- Docker (local or cloud: Render, Railway, Heroku)
- VPS (Ubuntu/Debian)
- Serverless (requires adaptation for webhook instead of polling)

---

## 7. Critical Implementation Details

### Download Limits
- **File Size:** Max 49MB (Telegram API limit is 50MB)
- **Concurrent Downloads:** Max 2 parallel (ThreadPoolExecutor limit)
- **Timeout:** 30 seconds per download

### Async Pattern
- **Polling-based:** Bot uses `dp.start_polling()` (simple, requires persistent process)
- **Web server:** Runs concurrently via `asyncio.gather()` (no webhook needed)

### Error Handling Philosophy
- Silent failures for non-critical errors (don't crash bot)
- User-facing messages for download failures
- All errors logged to admin channel for visibility
- Temporary files always cleaned up even on exception

---

## 8. Known Limitations & Technical Debt

⚠️ **Current Limitations**
- Single-instance deployment only (concurrent downloads limited to 2)
- No database transactions (atomic multi-step operations)
- Hard-coded 49MB limit (could be configurable)
- Joined_date field initialized as null (unused)
- No user authentication beyond Telegram ID
- yt-dlp updated in Docker CMD each run (inefficient)

🔧 **Recommended Refactoring**
- Extract magic strings (URLs, limits) to config.py constants
- Add type hints to all functions (improve IDE support & catch bugs)
- Implement middleware pattern for rate limiting & logging
- Separate concerns: payment approval flow deserves dedicated module
- Add comprehensive docstrings (current comments are in Khmer)

---

## Quick Reference: Command Map

| Command | Access | Purpose |
|---------|--------|---------|
| `/start` | All | Welcome + status check |
| `/plan` | All | Usage statistics |
| `/help` | All | Instructions |
| `/approve [id]` | Admin only | Upgrade to premium |
| Send URL | All | Trigger download dialog |
| Send photo | All | Payment proof upload |

---

**Report End**
