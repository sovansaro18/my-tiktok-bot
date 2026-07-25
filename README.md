# Telegram Downloader Bot

A server-side Telegram bot that downloads videos from TikTok, Facebook, YouTube, Instagram, and Pinterest and sends them back to users. Built for cloud hosting (Docker / Railway / Linux) — there are no PC-runner scripts; the bot runs exclusively inside a container via `Dockerfile` + `main.py` + environment variables.

## Supported platforms

| Platform | Video | Audio (MP3) | Photo / Slideshow |
| --- | --- | --- | --- |
| TikTok | ✅ | ✅ | ✅ |
| YouTube | ✅ | ✅ | — |
| Facebook | ✅ | — | — |
| Instagram | ✅ | ✅ | — |
| Pinterest | ✅ | — | — |

Public videos only — private, age-restricted (without cookies), and region/copyright-blocked content is rejected.

## Tech stack

- **Python 3.10** + [aiogram 3](https://aiogram.dev) (async Telegram Bot API)
- **yt-dlp** for extraction, **ffmpeg** for transcoding
- **MongoDB** (via Motor) for user state — falls back to in-memory if `MONGO_URI` is unset
- **aiohttp** for a small health-check web server used by container orchestrators
- Docker image based on `python:3.10-slim` with `ffmpeg` and `git` installed

## Project layout

```
.
├── Dockerfile           # Container image definition
├── main.py              # Entry point: starts polling + health server + graceful shutdown
├── requirements.txt     # Pinned Python dependencies
├── .env.example         # Template for required environment variables
└── src/
    ├── config.py            # Loads + validates env vars
    ├── handlers.py          # Telegram command / message / callback handlers
    ├── middleware.py        # Per-user rate limiting
    ├── downloader.py        # yt-dlp orchestrator + platform routing
    ├── cobalt_api.py        # Cobalt API v7 + TikWM fallback for TikTok
    ├── facebook_api.py      # Multi-API fallback for Facebook
    ├── database.py          # MongoDB / in-memory data layer
    ├── errors.py            # Domain exceptions
    ├── utils.py             # Logging + file helpers + HTML sanitization
    └── security/
        └── validators.py    # URL validation + SSRF protection
```

## Environment variables

Copy `.env.example` to `.env` and fill in the values (or set them in your hosting provider's dashboard).

| Variable | Required | Description |
| --- | --- | --- |
| `BOT_TOKEN` | ✅ | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_ID` | ✅ | Your Telegram user ID (numeric); enables `/stats` and `/broadcast` |
| `REPORT_CHANNEL_ID` | ✅ | Telegram channel ID that receives `/report` messages |
| `MONGO_URI` | Optional | MongoDB connection string. If omitted, runs with an in-memory store (resets on restart) |
| `LOG_CHANNEL_ID` | Optional | Telegram channel ID for join/leave/error logging |
| `COOKIES_FILE` | Optional | Path to a `cookies.txt` file for age-restricted / login-required YouTube & Facebook |
| `PORT` | Optional | Health-check server port (default `10000`) |
| `LOG_LEVEL` | Optional | Python log level (default `INFO`) |

## Running in production

### Docker

```bash
docker build -t telegram-downloader-bot .
docker run --env-file .env -p 10000:10000 telegram-downloader-bot
```

The container starts long-polling Telegram and an HTTP health endpoint on `GET /` (returns `Bot is running smoothly!`). The bot handles `SIGINT` / `SIGTERM` for graceful shutdown, so orchestrators can stop it cleanly.

### Railway / other PaaS

1. Connect this repository to your hosting provider.
2. Set the environment variables from the table above in the provider's dashboard.
3. Let the provider build from the `Dockerfile` ( Railway auto-detects it). The `PORT` the health server listens on should match the provider's exposed port — if Railway assigns a dynamic port, set `PORT` accordingly.
4. If you need cookies for YouTube/Facebook, mount the `cookies.txt` file as a secret and set `COOKIES_FILE` to its mounted path (e.g. `/secrets/cookies.txt`).

### Linux (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit .env
python main.py
```

`ffmpeg` must be installed and on `PATH` for audio extraction and video merging.

## Bot commands

- `/start` — welcome message
- `/plan` — account/plan info
- `/report` — send a message to the admin channel
- `/stats` *(admin)* — bot usage statistics
- `/broadcast <message>` *(admin)* — announce to all users

To download, just send a supported video link to the bot and pick a format from the inline buttons.

## Notes

- Telegram limits uploaded files to 50 MB; the bot enforces a 49 MB ceiling and will reject larger media with a friendly message.
- Cookies are staged to a writable `/tmp/yt_cookies.txt` at startup because yt-dlp updates cookie files in place and container secret mounts are typically read-only.
- `downloads/` is a working directory for in-flight files; they are deleted after being sent to the user.
