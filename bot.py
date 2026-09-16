import os
import logging
import tempfile
import time
import uuid
import asyncio
import threading
from pathlib import Path
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import yt_dlp
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
PROGRESS_UPDATE_INTERVAL = 3
YOUTUBE_MIN_INTERVAL = 8
YOUTUBE_RETRIES = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

authenticated_users = set()
pending_downloads = {}
cancel_flags = {}
youtube_lock = asyncio.Lock()
youtube_last_request = 0.0


def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


drive_service = get_drive_service()


def upload_to_drive(file_path):
    metadata = {"name": Path(file_path).name, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    file = (
        drive_service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink, name, size")
        .execute()
    )
    return {"id": file.get("id"), "link": file.get("webViewLink"), "name": file.get("name"), "size": file.get("size")}


def human_size(n):
    if not n:
        return "?"
    try:
        n = float(n)
    except Exception:
        return "?"
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(s):
    if not s:
        return "?"
    if s < 60:
        return f"{int(s)} ث"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}:{sec:02d} د"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d} س"


def is_youtube_url(url):
    u = url.lower()
    return "youtube.com/" in u or "youtu.be/" in u or "youtube-nocookie.com/" in u


def is_rate_limit_error(exc):
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "google.com/sorry" in text


def ytdlp_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if extra:
        opts.update(extra)
    return opts


def fetch_url_info(url):
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts({"skip_download": True})) as ydl:
            i = ydl.extract_info(url, download=False)
            return {
                "title": i.get("title", "بدون عنوان"),
                "duration": i.get("duration"),
                "uploader": i.get("uploader") or i.get("channel"),
                "thumbnail": i.get("thumbnail"),
                "formats": i.get("formats", []),
                "webpage_url": i.get("webpage_url", url),
                "is_video": True,
                "extractor": i.get("extractor_key", ""),
            }
    except Exception as e:
        logger.warning(f"fetch_url_info failed: {e}")
        return {"is_video": False, "url": url, "error": str(e), "rate_limited": is_rate_limit_error(e), "youtube": is_youtube_url(url)}


def build_quality_keyboard(short_id, formats):
    available = set()
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = f.get("height")
        if h:
            for x in [2160, 1440, 1080, 720, 480, 360, 144]:
                if h >= x:
                    available.add(str(x))
                    break
    order = ["144", "360", "480", "720", "1080", "1440", "2160"]
    labels = {"144": "144p", "360": "360p", "480": "480p", "720": "720p", "1080": "1080p", "1440": "2K", "2160": "4K"}
    buttons = []
    row = []
    for q in order:
        if q in available:
            row.append(InlineKeyboardButton(labels[q], callback_data=f"dl|{short_id}|{q}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🎵 تحميل صوت MP3", callback_data=f"dl|{short_id}|mp3")])
    buttons.append([InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel|{short_id}")])
    return InlineKeyboardMarkup(buttons)


def download_video(url, out_dir, quality, progress_cb=None, cancel_check=None):
    if quality == "mp3":
        fmt = "bestaudio/best"
        pps = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        ext = "mp3"
    else:
        fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        pps = []
        ext = None

    def hook(d):
        if cancel_check and cancel_check():
            raise Exception("__CANCELLED__")
        if progress_cb:
            progress_cb(d)

    opts = ytdlp_opts({
        "outtmpl": f"{out_dir}/%(title).150s.%(ext)s",
        "format": fmt,
        "progress_hooks": [hook],
        "postprocessors": pps,
        "merge_output_format": "mp4",
    })
    if ext:
        opts["final_ext"] = ext

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if ext:
            path = os.path.splitext(path)[0] + "." + ext
        return path


def download_direct(url, out_dir, progress_cb=None, cancel_check=None):
    filename = url.split("/")[-1].split("?")[0] or "downloaded_file"
    filename = "".join(c for c in filename if c.isalnum() or c in "._- ") or "downloaded_file"
    fp = os.path.join(out_dir, filename)
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(fp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if cancel_check and cancel_check():
                    raise Exception("__CANCELLED__")
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb({"downloaded_bytes": done, "total_bytes": total})
    return fp


async def youtube_gate():
    global youtube_last_request
    async with youtube_lock:
        wait = YOUTUBE_MIN_INTERVAL - (time.monotonic() - youtube_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        youtube_last_request = time.monotonic()


async def fetch_url_info_safe(url):
    attempts = YOUTUBE_RETRIES + 1 if is_youtube_url(url) else 1
    last_error = None
    for attempt in range(attempts):
        if is_youtube_url(url):
            await youtube_gate()
        info = await asyncio.to_thread(fetch_url_info, url)
        if info.get("is_video"):
            return info
        last_error = info.get("error")
        if not info.get("rate_limited"):
            return info
        if attempt < attempts - 1:
            delay = 10 * (attempt + 1)
            logger.warning(f"YouTube rate limit detected; retrying after {delay}s")
            await asyncio.sleep(delay)
    return {"is_video": False, "url": url, "error": last_error or "YouTube rate limit", "rate_limited": True, "youtube": True}


async def cmd_start(update, context):
    await update.message.reply_text("👋 أهلاً بك في *DriveSaverBot*!\n\n🔐 أرسل كلمة السر للمتابعة.", parse_mode="Markdown")


async def cmd_help(update, context):
    await update.message.reply_text("📖 *المساعدة*\n\n🔗 أرسل رابطاً → اختر الجودة → يُحفظ في Drive\n📎 أرسل ملفاً → يُحفظ في Drive\n🛑 /cancel — إلغاء التحميل الحالي\n🆔 /id — معرف المحادثة", parse_mode="Markdown")


async def cmd_id(update, context):
    await update.message.reply_text(f"🆔 `{update.effective_chat.id}`", parse_mode="Markdown")


async def cmd_stats(update, context):
    uid = update.effective_user.id
    pending = sum(1 for p in pending_downloads.values() if p["user_id"] == uid)
    await update.message.reply_text(f"📊 *إحصائياتك*\n\n👤 معرّفك: `{uid}`\n🔄 تحميلات معلقة: {pending}\n✅ المصادقة: {'نعم' if uid in authenticated_users else 'لا'}", parse_mode="Markdown")


async def cmd_cancel(update, context):
    cancel_flags[update.effective_user.id] = True
    await update.message.reply_text("🛑 جارٍ الإلغاء...")


async def handle_message(update, context):
    uid = update.effective_user.id
    if uid not in authenticated_users:
        text = (update.message.text or "").strip()
        if text == BOT_PASSWORD:
            authenticated_users.add(uid)
            await update.message.reply_text("✅ تم التحقق!\n\nأرسل أي رابط أو ملف.\nاستخدم /help للمساعدة.")
        else:
            await update.message.reply_text("❌ كلمة سر خاطئة.")
        return
    if update.message.document or update.message.video or update.message.audio:
        await handle_telegram_file(update, context, update.message.document or update.message.video or update.message.audio)
        return
    text = (update.message.text or "").strip()
    if text.startswith(("http://", "https://")):
        await handle_link(update, context, text)
        return
    await update.message.reply_text("❓ أرسل رابطاً أو ملفاً، أو /help.")


async def handle_link(update, context, url):
    msg = await update.message.reply_text("🔍 جلب معلومات الرابط...")
    info = await fetch_url_info_safe(url)
    if not info.get("is_video"):
        if info.get("rate_limited") and info.get("youtube"):
            await msg.edit_text("⚠️ YouTube يقيّد مؤقتاً طلبات خادم البوت (429).\n\nلن نحاول تنزيل الرابط كأنه ملف مباشر، لأن ذلك لن يحل المشكلة. جرّب بعد فترة قصيرة.")
            return
        await msg.edit_text("📥 رابط مباشر — جارٍ التحميل...")
        await do_download(update, context, msg, url, None, None)
        return
    sid = uuid.uuid4().hex[:10]
    pending_downloads[sid] = {"url": url, "info": info, "user_id": update.effective_user.id, "chat_id": update.effective_chat.id, "message_id": msg.message_id, "created_at": time.time()}
    await msg.edit_text(f"🎬 *{info['title'][:80]}*\n\n👤 {info.get('uploader') or '?'}\n⏱️ المدة: {human_time(info['duration']) if info.get('duration') else '?'}\n\nاختر الجودة:", parse_mode="Markdown", reply_markup=build_quality_keyboard(sid, info.get("formats", [])))


async def handle_callback(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id
    if data.startswith("cancel|"):
        sid = data.split("|", 1)[1]
        p = pending_downloads.pop(sid, None)
        if p:
            cancel_flags[p["user_id"]] = True
        await q.edit_message_text("❌ تم الإلغاء.")
        return
    if data.startswith("dl|"):
        _, sid, quality = data.split("|", 2)
        p = pending_downloads.pop(sid, None)
        if not p:
            await q.edit_message_text("❌ انتهت صلاحية هذا الطلب.")
            return
        if p["user_id"] != uid:
            await q.answer("هذا الطلب ليس لك!", show_alert=True)
            return
        await q.edit_message_text("📥 جارٍ التحميل...")
        await do_download(update, context, q.message, p["url"], quality, p["info"])


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception:
        pass


async def do_download(update, context, msg, url, quality, info):
    uid = update.effective_user.id
    cancel_flags[uid] = False
    start = time.time()
    last = {"t": 0}
    loop = asyncio.get_running_loop()

    def cancelled():
        return cancel_flags.get(uid, False)

    def cb(prefix):
        def f(d):
            now = time.time()
            if now - last["t"] < PROGRESS_UPDATE_INTERVAL:
                return
            last["t"] = now
            done = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            if total:
                pct = done / total * 100
                filled = min(20, int(pct / 5))
                bar = "█" * filled + "░" * (20 - filled)
                text = f"{prefix}\n\n`[{bar}]` {pct:.1f}%\n💾 {human_size(done)} / {human_size(total)}\n⚡ {human_size(speed)}/s"
            else:
                text = f"{prefix}\n\n💾 {human_size(done)}"
            loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_edit(msg, text)))
        return f

    try:
        with tempfile.TemporaryDirectory() as tmp:
            progress = cb("⬇️ التحميل")
            try:
                if is_youtube_url(url):
                    await youtube_gate()
                file_path = await asyncio.to_thread(download_video, url, tmp, quality or "720", progress, cancelled)
            except Exception as e:
                if "__CANCELLED__" in str(e):
                    await msg.edit_text("🛑 تم الإلغاء.")
                    return
                if info and info.get("is_video"):
                    if is_rate_limit_error(e) and is_youtube_url(url):
                        await msg.edit_text("⚠️ YouTube قيّد الطلبات من خادم البوت (429).\n\nلن نكرر الطلبات بسرعة. جرّب لاحقاً.")
                        return
                    raise
                logger.warning(f"yt-dlp failed: {e}, trying direct")
                file_path = await asyncio.to_thread(download_direct, url, tmp, progress, cancelled)
            if not file_path or not os.path.exists(file_path):
                raise Exception("لم يتم إنشاء أي ملف")
            size = os.path.getsize(file_path)
            await msg.edit_text(f"📤 جارٍ الرفع إلى Drive...\n💾 {human_size(size)}")
            result = await asyncio.to_thread(upload_to_drive, file_path)
            elapsed = time.time() - start
            await msg.edit_text(f"✅ *تم الحفظ في Drive!*\n\n📁 [{result['name']}]({result['link']})\n💾 الحجم: {human_size(size)}\n⏱️ الوقت: {human_time(elapsed)}\n⚡ متوسط السرعة: {human_size(size / elapsed if elapsed else 0)}/s", parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ فشل:\n`{str(e)[:300]}`", parse_mode="Markdown")
    finally:
        cancel_flags.pop(uid, None)


async def handle_telegram_file(update, context, file_obj):
    msg = await update.message.reply_text("📥 جارٍ التنزيل من تلجرام...")
    start = time.time()
    try:
        tg = await context.bot.get_file(file_obj.file_id)
        name = file_obj.file_name or f"tg_{file_obj.file_unique_id}"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            await tg.download_to_drive(path)
            size = os.path.getsize(path)
            await msg.edit_text(f"📤 جارٍ الرفع... ({human_size(size)})")
            result = await asyncio.to_thread(upload_to_drive, path)
            await msg.edit_text(f"✅ *تم الحفظ في Drive!*\n\n📁 [{result['name']}]({result['link']})\n💾 {human_size(size)}\n⏱️ {human_time(time.time() - start)}", parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ فشل:\n`{str(e)[:300]}`", parse_mode="Markdown")


app_flask = Flask(__name__)

@app_flask.get("/")
def health():
    return "OK", 200


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    app_flask.run(host="0.0.0.0", port=port)


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("🌐 Health server started")
    logger.info("🚀 Starting DriveSaverBot...")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(4).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    logger.info("✅ Bot running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
