import os
import asyncio
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 6011229637

app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بيك!\n\n"
        "ابعت رابط الفيديو وأنا هحاول تحميله وإرساله ليك."
    )
     async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 ID: {update.effective_user.id}\n"
        f"👤 الاسم: {update.effective_user.first_name}\n"
        f"🔹 username: @{update.effective_user.username or 'لا يوجد'}"
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ ابعت رابط صحيح.")
        return

    msg = await update.message.reply_text("⏳ جاري تحميل الفيديو...")

    filename = f"/tmp/video_{update.effective_user.id}.%(ext)s"

    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": filename,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 50 * 1024 * 1024,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        if not os.path.exists(file_path):
            await msg.edit_text("❌ مقدرتش أجيب الفيديو من الرابط ده.")
            return

        await msg.edit_text("📤 جاري إرسال الفيديو...")

        with open(file_path, "rb") as video:
            await update.message.reply_video(
                video=video,
                caption="✅ تم التحميل"
            )

        os.remove(file_path)
        await msg.delete()

    except Exception as e:
        print("ERROR:", e)

        if os.path.exists(file_path):
            os.remove(file_path)

        await msg.edit_text(
            "❌ مقدرتش أحمل الفيديو.\n"
            "ممكن الموقع غير مدعوم أو الفيديو أكبر من الحد المسموح."
        )

async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("id", get_id))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_video)
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
