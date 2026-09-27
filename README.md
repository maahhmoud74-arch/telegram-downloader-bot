# Telegram Downloader Bot

Deployka-ready Telegram downloader bot.

## Environment variables
- `BOT_TOKEN`: BotFather token (never commit it to GitHub)
- `ADMIN_IDS`: comma-separated Telegram numeric IDs
- `DATABASE_URL`: `sqlite+aiosqlite:////data/bot.db`
- `DOWNLOAD_DIR`: `/data/downloads`
- `MAX_FILE_SIZE_MB`: global maximum for the deployment

Free users have 10 downloads/day. Premium/VIP limits are configured in `main.py` and can later be connected to a payment/subscription system.

The bot is best-effort: yt-dlp support varies by site and can change over time. Do not use it to bypass DRM/access controls or download content you are not authorized to download.
