import os
import json
import logging
import tempfile
import time
import uuid
import asyncio
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import yt_dlp
import requests

# ================== الإعدادات ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
PROGRESS_UPDATE_INTERVAL = 3  # ثوانٍ بين تحديثات التقدم

# ================== السجلات ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== الحالة في الذاكرة ==================
authenticated_users = set()
pending_downloads = {}   # {short_id: {"url":..., "info":..., "user_id":..., "chat_id":..., "message_id":...}}
cancel_flags = {}        # {user_id: True/False}


# ================== Google Drive ==================
def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


drive_service = get_drive_service()


def upload_to_drive(file_path: str) -> dict:
    metadata = {"name": Path(file_path).name, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True)
    file = drive_service.files().create(
        body=metadata, media_body=media,
        fields="id, webViewLink, name, size"
    ).execute()
    return {
        "id": file.get("id"),
        "link": file.get("webViewLink"),
        "name": file.get("name"),
        "size": file.get("size")
    }


# ================== أدوات مساعدة ==================
def human_size(bytes_size):
    if not bytes_size:
        return "?"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def human_time(seconds):
    if seconds < 60:
        return f"{int(seconds)} ث"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}:{s:02d} د"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d} س"


# ================== جلب معلومات الرابط ==================
def fetch_url_info(url: str) -> dict:
    """يجلب معلومات الرابط بدون تحميل"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "بدون عنوان"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel"),
                "thumbnail": info.get("thumbnail"),
                "formats": info.get("formats", []),
                "webpage_url": info.get("webpage_url", url),
                "is_video": True,
                "extractor": info.get("extractor_key", "")
            }
    except Exception as e:
        logger.warning(f"fetch_url_info failed: {e}")
        return {"is_video": False, "url": url}


def build_quality_keyboard(short_id: str, formats: list) -> InlineKeyboardMarkup:
    """يبني أزرار الجودات المتاحة"""
    available = set()
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = f.get("height")
        if h:
            if h >= 2160: available.add("2160")
            elif h >= 1440: available.add("1440")
            elif h >= 1080: available.add("1080")
            elif h >= 720: available.add("720")
            elif h >= 480: available.add("480")
            elif h >= 360: available.add("360")
            elif h >= 144: available.add("144")

    order = ["144", "360", "480", "720", "1080", "1440", "2160"]
    labels = {"144": "144p", "360": "360p", "480": "480p",
              "720": "720p", "1080": "1080p", "1440": "2K", "2160": "4K"}

    buttons = []
    row = []
    for q in order:
        if q in available:
            row.append(InlineKeyboardButton(
                labels[q],
                callback_data=f"dl|{short_id}|{q}"
            ))
            if len(row) == 4:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)

    # زر MP3
    buttons.append([InlineKeyboardButton(
        "🎵 تحميل صوت MP3",
        callback_data=f"dl|{short_id}|mp3"
    )])
    # زر إلغاء
    buttons.append([InlineKeyboardButton(
        "❌ إلغاء",
        callback_data=f"cancel|{short_id}"
    )])

    return InlineKeyboardMarkup(buttons)


# ================== التحميل بـ yt-dlp ==================
def download_video(url: str, out_dir: str, quality: str, progress_cb=None, cancel_check=None) -> str:
    """تحميل فيديو/صوت بجودة محددة"""
    if quality == "mp3":
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        ext_override = "mp3"
    else:
        h = quality
        fmt = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        postprocessors = []
        ext_override = None

    def hook(d):
        if cancel_check and cancel_check():
            raise Exception("__CANCELLED__")
        if progress_cb:
            progress_cb(d)

    opts = {
        "outtmpl": f"{out_dir}/%(title).150s.%(ext)s",
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "postprocessors": postprocessors,
        "merge_output_format": "mp4",
    }
    if ext_override:
        opts["final_ext"] = ext_override

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if ext_override:
            base, _ = os.path.splitext(path)
            path = base + "." + ext_override
        return path


# ================== تحميل رابط مباشر ==================
def download_direct(url: str, out_dir: str, progress_cb=None, cancel_check=None) -> str:
    filename = url.split("/")[-1].split("?")[0] or "downloaded_file"
    filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
    if not filename:
        filename = "downloaded_file"
    filepath = os.path.join(out_dir, filename)

    headers = {"User-Agent": "Mozilla/5.0"}
    with requests.get(url, stream=True, timeout=120, headers=headers) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if cancel_check and cancel_check():
                    raise Exception("__CANCELLED__")
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb({"downloaded_bytes": downloaded, "total_bytes": total})
    return filepath


# ================== أوامر البوت ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك في *DriveSaverBot*!\n\n🔐 أرسل كلمة السر للمتابعة.",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *المساعدة*\n\n"
        "🔗 أرسل رابطاً → اختر الجودة → يُحفظ في Drive\n"
        "📎 أرسل ملفاً → يُحفظ في Drive\n"
        "🛑 /cancel — إلغاء التحميل الحالي\n"
        "🆔 /id — معرف المحادثة",
        parse_mode="Markdown"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 `{update.effective_chat.id}`", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending = sum(1 for p in pending_downloads.values() if p["user_id"] == user_id)
    await update.message.reply_text(
        f"📊 *إحصائياتك*\n\n"
        f"👤 معرّفك: `{user_id}`\n"
        f"🔄 تحميلات معلقة: {pending}\n"
        f"✅ المصادقة: {'نعم' if user_id in authenticated_users else 'لا'}",
        parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cancel_flags[user_id] = True
    await update.message.reply_text("🛑 جارٍ الإلغاء...")


# ================== معالج الرسائل ==================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # التحقق
    if user_id not in authenticated_users:
        text = (update.message.text or "").strip()
        if text == BOT_PASSWORD:
            authenticated_users.add(user_id)
            logger.info(f"User {user_id} authenticated")
            await update.message.reply_text(
                "✅ تم التحقق!\n\nأرسل أي رابط أو ملف.\nاستخدم /help للمساعدة."
            )
        else:
            await update.message.reply_text("❌ كلمة سر خاطئة.")
        return

    # ملف من تلجرام
    if update.message.document or update.message.video or update.message.audio:
        file_obj = update.message.document or update.message.video or update.message.audio
        await handle_telegram_file(update, context, file_obj)
        return

    # رابط
    text = (update.message.text or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        await handle_link(update, context, text)
        return

    await update.message.reply_text("❓ أرسل رابطاً أو ملفاً، أو /help.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("🔍 جلب معلومات الرابط...")
    info = fetch_url_info(url)

    if not info.get("is_video"):
        # رابط غير معروف لـ yt-dlp → حمّل مباشرة
        await msg.edit_text("📥 رابط مباشر — جارٍ التحميل...")
        await do_download(
            update, context, msg, url, quality=None, info=None
        )
        return

    # فيديو → اعرض المعلومات والأزرار
    title = info["title"][:80]
    duration = human_time(info["duration"]) if info.get("duration") else "?"
    uploader = info.get("uploader") or "?"

    # نُنشئ ID قصير لهذا التحميل
    short_id = uuid.uuid4().hex[:10]
    pending_downloads[short_id] = {
        "url": url,
        "info": info,
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
        "created_at": time.time()
    }

    keyboard = build_quality_keyboard(short_id, info.get("formats", []))

    await msg.edit_text(
        f"🎬 *{title}*\n\n"
        f"👤 {uploader}\n"
        f"⏱️ المدة: {duration}\n\n"
        f"اختر الجودة:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ================== معالج الأزرار ==================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    if data.startswith("cancel|"):
        short_id = data.split("|", 1)[1]
        pending_downloads.pop(short_id, None)
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    if data.startswith("dl|"):
        _, short_id, quality = data.split("|", 2)
        pending = pending_downloads.pop(short_id, None)
        if not pending:
            await query.edit_message_text("❌ انتهت صلاحية هذا الطلب.")
            return

        if pending["user_id"] != user_id:
            await query.answer("هذا الطلب ليس لك!", show_alert=True)
            return

        await query.edit_message_text("📥 جارٍ التحميل...")
        await do_download(
            update, context,
            query.message,  # نستخدم نفس الرسالة
            pending["url"],
            quality=quality,
            info=pending["info"],
            edit_via_query=True
        )


# ================== التحميل الرئيسي ==================
async def do_download(update, context, msg, url, quality, info, edit_via_query=False):
    user_id = update.effective_user.id
    cancel_flags[user_id] = False
    start_time = time.time()

    def is_cancelled():
        return cancel_flags.get(user_id, False)

    last_update = {"t": 0.0}

    def make_progress_cb(prefix: str):
        def cb(d):
            now = time.time()
            if now - last_update["t"] < PROGRESS_UPDATE_INTERVAL:
                return
            last_update["t"] = now
            try:
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                if total > 0:
                    pct = downloaded / total * 100
                    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                    text = (
                        f"{prefix}\n\n"
                        f"`[{bar}]` {pct:.1f}%\n"
                        f"💾 {human_size(downloaded)} / {human_size(total)}\n"
                        f"⚡ {human_size(speed)}/s"
                    )
                else:
                    text = f"{prefix}\n\n💾 {human_size(downloaded)}"
                # جدولة التعديل (لا ننتظر)
                asyncio.create_task(safe_edit(msg, text))
            except Exception as e:
                logger.warning(f"progress edit failed: {e}")
        return cb

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = None

            if info and info.get("is_video") and quality:
                # yt-dlp مع الجودة المختارة
                try:
                    file_path = download_video(
                        url, tmp_dir, quality,
                        progress_cb=make_progress_cb("⬇️ التحميل"),
                        cancel_check=is_cancelled
                    )
                except Exception as e:
                    if "__CANCELLED__" in str(e):
                        await msg.edit_text("🛑 تم الإلغاء.")
                        return
                    raise
            else:
                # fallback: جرب yt-dlp best ثم direct
                try:
                    file_path = download_video(
                        url, tmp_dir, "720",
                        progress_cb=make_progress_cb("⬇️ التحميل"),
                        cancel_check=is_cancelled
                    )
                except Exception as e:
                    logger.warning(f"yt-dlp failed: {e}, trying direct")
                    file_path = download_direct(
                        url, tmp_dir,
                        progress_cb=make_progress_cb("⬇️ التحميل"),
                        cancel_check=is_cancelled
                    )

            if not file_path or not os.path.exists(file_path):
                raise Exception("لم يتم إنشاء أي ملف")

            size = os.path.getsize(file_path)
            elapsed = time.time() - start_time
            speed_avg = size / elapsed if elapsed > 0 else 0

            await msg.edit_text(
                f"📤 جارٍ الرفع إلى Drive...\n💾 {human_size(size)}"
            )

            result = upload_to_drive(file_path)

            total_time = time.time() - start_time
            await msg.edit_text(
                f"✅ *تم الحفظ في Drive!*\n\n"
                f"📁 [{result['name']}]({result['link']})\n"
                f"💾 الحجم: {human_size(size)}\n"
                f"⏱️ الوقت: {human_time(total_time)}\n"
                f"⚡ متوسط السرعة: {human_size(speed_avg)}/s",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

    except Exception as e:
        if "__CANCELLED__" in str(e):
            await msg.edit_text("🛑 تم الإلغاء.")
        else:
            logger.error(f"Error: {e}", exc_info=True)
            await msg.edit_text(f"❌ فشل:\n`{str(e)[:300]}`", parse_mode="Markdown")


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception:
        pass


# ================== ملفات تلجرام ==================
async def handle_telegram_file(update, context, file_obj):
    msg = await update.message.reply_text("📥 جارٍ التنزيل من تلجرام...")
    start_time = time.time()

    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        file_name = file_obj.file_name or f"tg_{file_obj.file_unique_id}"

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = os.path.join(tmp_dir, file_name)
            await tg_file.download_to_drive(local_path)

            size = os.path.getsize(local_path)
            await msg.edit_text(f"📤 جارٍ الرفع... ({human_size(size)})")

            result = upload_to_drive(local_path)
            total_time = time.time() - start_time

            await msg.edit_text(
                f"✅ *تم الحفظ في Drive!*\n\n"
                f"📁 [{result['name']}]({result['link']})\n"
                f"💾 {human_size(size)}\n"
                f"⏱️ {human_time(total_time)}",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ فشل:\n`{str(e)[:300]}`", parse_mode="Markdown")


# ================== نقطة البداية ==================
def main():
    logger.info("🚀 Starting DriveSaverBot...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.Document.ALL | filters.VIDEO | filters.AUDIO)
        & ~filters.COMMAND,
        handle_message
    ))

    logger.info("✅ Bot running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
