import asyncio
import ipaddress
import os
import re
import socket
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
import yt_dlp


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = Path(os.getenv("DATABASE_URL", "sqlite+aiosqlite:////data/bot.db")
# Accept Deployka's SQLAlchemy-style value and turn it into a real SQLite path.
db_text = str(DB_PATH)
if "////" in db_text:
    DB_FILE = Path("/" + db_text.split("////", 1)[1].lstrip("/"))
elif db_text.startswith("sqlite:///"):
    DB_FILE = Path(db_text.replace("sqlite:///", "", 1))
else:
    DB_FILE = Path(db_text)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/data/downloads"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

PLANS = {
    "free": {"daily": 10, "max_mb": 50},
    "premium": {"daily": 100, "max_mb": 500},
    "vip": {"daily": 500, "max_mb": 2000},
}

db_lock = asyncio.Lock()


def now_utc():
    return datetime.now(timezone.utc)


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            plan TEXT NOT NULL DEFAULT 'free',
            daily_downloads INTEGER NOT NULL DEFAULT 0,
            daily_date TEXT NOT NULL,
            total_downloads INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            file_name TEXT,
            file_size INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_downloads_user
        ON downloads(telegram_id);

        CREATE INDEX IF NOT EXISTS idx_downloads_created
        ON downloads(created_at);
        """
    )
    conn.commit()
    conn.close()


def ensure_user(message: Message):
    user = message.from_user
    if not user:
        return
    today = now_utc().date().isoformat()
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (user.id,)
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO users
            (telegram_id, username, display_name, plan, daily_date, created_at, updated_at)
            VALUES (?, ?, ?, 'free', ?, ?, ?)
            """,
            (
                user.id,
                user.username,
                user.full_name[:200],
                today,
                now_utc().isoformat(),
                now_utc().isoformat(),
            ),
        )
    else:
        daily = row["daily_downloads"]
        if row["daily_date"] != today:
            daily = 0
        conn.execute(
            """
            UPDATE users
            SET username=?, display_name=?, daily_downloads=?, updated_at=?
            WHERE telegram_id=?
            """,
            (
                user.username,
                user.full_name[:200],
                daily,
                now_utc().isoformat(),
                user.id,
            ),
        )
    conn.commit()
    conn.close()


def get_user(telegram_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def normalize_user_day(row):
    today = now_utc().date().isoformat()
    if row["daily_date"] != today:
        return 0
    return row["daily_downloads"]


def safe_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname.lower()
        if host in {"localhost", "localhost.localdomain"}:
            return False
        # Reject literal private/link-local/reserved IPs.
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            pass

        # Basic DNS check to reduce SSRF risk.
        infos = socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)
        return bool(infos) and all(ipaddress.ip_address(x[4][0]).is_global for x in infos)
    except Exception:
        return False


def clean_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "download")[:180]


async def reserve_download(user_id: int):
    async with db_lock:
        row = get_user(user_id)
        if not row:
            return False, "الحساب غير موجود."
        plan_name = row["plan"] if row["plan"] in PLANS else "free"
        limit = PLANS[plan_name]["daily"]
        used = normalize_user_day(row)
        if used >= limit:
            return False, f"وصلت للحد اليومي لخطة {plan_name}: {limit} تنزيلات."
        conn = db()
        today = now_utc().date().isoformat()
        conn.execute(
            """
            UPDATE users
            SET daily_downloads=?, daily_date=?, updated_at=?
            WHERE telegram_id=?
            """,
            (used + 1, today, now_utc().isoformat(), user_id),
        )
        conn.commit()
        conn.close()
        return True, plan_name


async def rollback_download(user_id: int):
    async with db_lock:
        row = get_user(user_id)
        if not row:
            return
        used = max(0, normalize_user_day(row) - 1)
        conn = db()
        conn.execute(
            "UPDATE users SET daily_downloads=?, updated_at=? WHERE telegram_id=?",
            (used, now_utc().isoformat(), user_id),
        )
        conn.commit()
        conn.close()


def record_download(user_id, url, status, file_name=None, file_size=None, error=None):
    conn = db()
    conn.execute(
        """
        INSERT INTO downloads
        (telegram_id, url, status, file_name, file_size, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, url, status, file_name, file_size, error,
            now_utc().isoformat(),
        ),
    )
    if status == "success":
        conn.execute(
            """
            UPDATE users
            SET total_downloads=total_downloads+1, updated_at=?
            WHERE telegram_id=?
            """,
            (now_utc().isoformat(), user_id),
        )
    conn.commit()
    conn.close()


async def direct_download(url: str, target_dir: Path, max_size: int):
    timeout = aiohttp.ClientTimeout(total=180, connect=20)
    headers = {"User-Agent": "TelegramDownloaderBot/2.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as r:
            r.raise_for_status()
            final_url = str(r.url)
            if not safe_url(final_url):
                raise ValueError("الرابط النهائي غير مسموح به لأسباب أمنية.")
            content_length = r.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise ValueError(f"الملف أكبر من الحد المسموح ({max_size // 1024 // 1024}MB).")

            ctype = (r.headers.get("Content-Type") or "").split(";")[0].lower()
            if not (
                ctype.startswith("image/")
                or ctype.startswith("audio/")
                or ctype.startswith("video/")
                or ctype == "application/octet-stream"
            ):
                raise ValueError("الرابط لا يبدو كملف قابل للتنزيل مباشرة.")

            name = Path(urlparse(final_url).path).name or "download"
            name = clean_filename(name)
            path = target_dir / f"{int(time.time())}_{name}"

            size = 0
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 128):
                    size += len(chunk)
                    if size > max_size:
                        f.close()
                        path.unlink(missing_ok=True)
                        raise ValueError(f"الملف أكبر من الحد المسموح ({max_size // 1024 // 1024}MB).")
                    f.write(chunk)
            return path


def yt_download(url: str, target_dir: Path, max_size: int):
    before = set(target_dir.iterdir())
    opts = {
        "outtmpl": str(target_dir / "%(title).150s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": max_size,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    after = set(target_dir.iterdir())
    files = [p for p in (after - before) if p.is_file()]
    if not files:
        raise RuntimeError("لم يتم العثور على ملف بعد التنزيل.")
    path = max(files, key=lambda p: p.stat().st_mtime)
    if path.stat().st_size > max_size:
        path.unlink(missing_ok=True)
        raise ValueError("الملف تجاوز الحد المسموح.")
    return path


async def download_url(url: str, max_size: int):
    job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=DOWNLOAD_DIR))
    try:
        try:
            return await direct_download(url, job_dir, max_size)
        except Exception:
            return await asyncio.to_thread(yt_download, url, job_dir, max_size)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def display_plan(plan):
    p = PLANS.get(plan, PLANS["free"])
    return f"{plan}: {p['daily']}/يوم، حتى {p['max_mb']}MB"


def parse_url(text: str):
    parts = text.strip().split()
    for part in parts:
        if part.startswith(("http://", "https://")):
            return part
    return None


async def send_download(message: Message, path: Path):
    file = FSInputFile(path)
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        await message.answer_photo(file)
    elif ext in {".mp4", ".mkv", ".webm", ".mov"}:
        await message.answer_video(file, supports_streaming=True)
    elif ext in {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}:
        await message.answer_audio(file)
    else:
        await message.answer_document(file)


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    ensure_user(message)
    await message.answer(
        "أهلاً بك 👋\n"
        "أرسل رابطًا مباشرًا أو رابط صفحة مدعومة، وسأحاول تنزيل المحتوى.\n\n"
        "الخطة المجانية: 10 تنزيلات يوميًا.\n"
        "استخدم /plan لمعرفة خطتك."
    )


@dp.message(Command("plan"))
async def plan_cmd(message: Message):
    ensure_user(message)
    row = get_user(message.from_user.id)
    plan = row["plan"] if row else "free"
    used = normalize_user_day(row) if row else 0
    await message.answer(
        f"خطتك: {plan}\n"
        f"الاستخدام اليوم: {used}/{PLANS.get(plan, PLANS['free'])['daily']}\n"
        f"الحجم الأقصى: {PLANS.get(plan, PLANS['free'])['max_mb']}MB"
    )


@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = db()
    users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    downloads = conn.execute(
        "SELECT COUNT(*) c FROM downloads WHERE status='success'"
    ).fetchone()["c"]
    conn.close()
    await message.answer(f"المستخدمون: {users}\nالتنزيلات الناجحة: {downloads}")


@dp.message(Command("users"))
async def users_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    conn = db()
    rows = conn.execute(
        "SELECT telegram_id, username, plan, total_downloads FROM users "
        "ORDER BY total_downloads DESC LIMIT 30"
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("لا يوجد مستخدمون بعد.")
        return
    lines = ["آخر/أكثر المستخدمين:"]
    for r in rows:
        lines.append(
            f"{r['telegram_id']} | @{r['username'] or '-'} | {r['plan']} | {r['total_downloads']}"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("setplan"))
async def setplan_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in PLANS:
        await message.answer("الاستخدام: /setplan USER_ID free|premium|vip")
        return
    user_id, plan = int(parts[1]), parts[2]
    conn = db()
    cur = conn.execute(
        "UPDATE users SET plan=?, updated_at=? WHERE telegram_id=?",
        (plan, now_utc().isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    await message.answer("تم تحديث الخطة." if cur.rowcount else "المستخدم غير موجود.")


@dp.message(F.text)
async def url_handler(message: Message):
    ensure_user(message)
    url = parse_url(message.text or "")
    if not url:
        await message.answer("أرسل رابطًا يبدأ بـ http:// أو https://")
        return

    if not safe_url(url):
        await message.answer("الرابط غير مسموح به لأسباب أمنية.")
        return

    row = get_user(message.from_user.id)
    plan = row["plan"] if row and row["plan"] in PLANS else "free"
    plan_max = min(PLANS[plan]["max_mb"], MAX_FILE_SIZE_MB) * 1024 * 1024

    ok, result = await reserve_download(message.from_user.id)
    if not ok:
        await message.answer(result)
        return

    status = await message.answer("⏳ جاري التنزيل...")
    path = None
    try:
        path = await download_url(url, plan_max)
        size = path.stat().st_size
        record_download(
            message.from_user.id, url, "success",
            path.name, size, None
        )
        await status.edit_text("✅ تم التنزيل، جارٍ الإرسال...")
        await send_download(message, path)
        await status.delete()
    except Exception as e:
        await rollback_download(message.from_user.id)
        err = str(e).replace(BOT_TOKEN, "[hidden]")[:500]
        record_download(message.from_user.id, url, "failed", error=err)
        await status.edit_text(
            "❌ تعذر تنزيل الرابط.\n"
            "جرّب رابطًا آخر أو رابطًا مباشرًا للملف."
        )
    finally:
        if path:
            try:
                shutil.rmtree(path.parent, ignore_errors=True)
            except Exception:
                pass


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables.")
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
