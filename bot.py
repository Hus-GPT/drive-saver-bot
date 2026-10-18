import os
import logging
import tempfile
import time
import uuid
import asyncio
import threading
import shutil
import json
import hashlib
import re
import zipfile
import posixpath
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, urlunsplit
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
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
import rarfile

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
PROGRESS_UPDATE_INTERVAL = 1
PROGRESS_DASHBOARD_REFRESH_SECONDS = max(3, int(os.environ.get("PROGRESS_DASHBOARD_REFRESH_SECONDS", "5")))
YOUTUBE_MIN_INTERVAL = 8
YOUTUBE_RETRIES = 2
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024
DRIVE_UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024  # 32 MiB, multiple of 256 KiB
TASK_LOG_PATH = os.environ.get('TASK_LOG_PATH', '/content/drive/MyDrive/.drive_saver_bot/tasks.jsonl')
TASK_LOG_LOCK = threading.Lock()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

authenticated_users = set()
pending_downloads = {}
queued_tasks = {}
active_tasks = {}
transfer_semaphore = asyncio.Semaphore(max(1, int(os.environ.get('MAX_CONCURRENT_TRANSFERS', '8'))))
cancel_flags = {}
progress_dashboard_loops = set()
youtube_lock = asyncio.Lock()
youtube_last_request = 0.0


def persist_task(record):
    """Append task history, redacting URLs whose query strings contain credentials."""
    try:
        path = Path(TASK_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        url = payload.get("url")
        if url:
            parsed = urlparse(url)
            query_keys = [key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
            sensitive_terms = ("token", "signature", "secret", "password", "auth", "credential", "session", "jwt", "key")
            if any(term in key for key in query_keys for term in sensitive_terms):
                payload.pop("url", None)
                payload["url_redacted"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                payload["recovery_requires_manual_url"] = True
        payload.setdefault("recorded_at", time.time())
        with TASK_LOG_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Could not persist task history: %s", exc)


def get_drive_service():
    if os.environ.get("COLAB_AUTH", "").lower() == "1":
        import google.auth
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

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


drive_service = None


def sha256_file(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



ARCHIVE_EXTENSIONS = {".zip", ".rar"}
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_EXPANDED_BYTES = 50 * 1024 * 1024 * 1024


def _safe_archive_target(root, member_name):
    """Resolve an archive member without permitting absolute paths or traversal."""
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise RuntimeError(f"رفض مسار مطلق داخل الأرشيف: {member_name[:120]}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise RuntimeError(f"رفض مسار غير آمن داخل الأرشيف: {member_name[:120]}")
    target = (Path(root) / Path(*parts)).resolve()
    base = Path(root).resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"رفض مسار خارج مجلد الاستخراج: {member_name[:120]}")
    return target


def extract_archive_contents(file_path, work_dir):
    """Extract ZIP/RAR safely; return extracted files, or None for non-archives."""
    source = Path(file_path)
    suffix = source.suffix.lower()
    if suffix not in ARCHIVE_EXTENSIONS:
        return None

    extract_root = Path(work_dir) / ("extracted_" + re.sub(r"[^A-Za-z0-9_-]", "_", source.stem)[:80])
    extract_root.mkdir(parents=True, exist_ok=True)
    members = []
    expanded_bytes = 0

    if suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise RuntimeError(f"الأرشيف يحتوي على أكثر من {MAX_ARCHIVE_MEMBERS} عنصر.")
            for info in infos:
                if info.is_dir():
                    continue
                # Reject Unix symlinks in ZIP metadata.
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    continue
                target = _safe_archive_target(extract_root, info.filename)
                expanded_bytes += int(info.file_size or 0)
                if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise RuntimeError("حجم الملفات بعد فك الأرشيف يتجاوز الحد الآمن 50 GiB.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                members.append(str(target))
    else:
        try:
            with rarfile.RarFile(source) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_MEMBERS:
                    raise RuntimeError(f"الأرشيف يحتوي على أكثر من {MAX_ARCHIVE_MEMBERS} عنصر.")
                for info in infos:
                    if info.is_dir():
                        continue
                    target = _safe_archive_target(extract_root, info.filename)
                    expanded_bytes += int(getattr(info, "file_size", 0) or 0)
                    if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise RuntimeError("حجم الملفات بعد فك الأرشيف يتجاوز الحد الآمن 50 GiB.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    members.append(str(target))
        except rarfile.RarCannotExec as exc:
            raise RuntimeError("تعذر فك RAR: أداة 7z غير متاحة. أعد تشغيل خلية إعداد Colab لتثبيتها.") from exc

    if not members:
        raise RuntimeError("الأرشيف لا يحتوي على ملفات قابلة للاستخراج.")
    logger.info("Extracted %s files from archive %s", len(members), source.name)
    return members


def upload_to_drive(file_path, progress_cb=None, cancel_check=None):
    """Avoid duplicate uploads, use resumable chunks, and verify the saved file."""
    expected_size = os.path.getsize(file_path)
    fingerprint = sha256_file(file_path)
    service = drive_service or get_drive_service()
    # Search across the entire Drive, not only the destination root. This
    # prevents identical content from being duplicated in nested folders too.
    query = (
        "trashed = false and "
        f"appProperties has {{ key='sha256' and value='{fingerprint}' }}"
    )
    existing = service.files().list(
        q=query, pageSize=1, fields="files(id, name, size, webViewLink, appProperties)"
    ).execute(num_retries=3).get("files", [])
    for item in existing:
        if int(item.get("size", -1)) == expected_size:
            if progress_cb:
                progress_cb(expected_size, expected_size)
            return {
                "id": item["id"], "link": item.get("webViewLink"),
                "name": item.get("name") or Path(file_path).name,
                "size": expected_size, "already_exists": True,
            }

    metadata = {
        "name": Path(file_path).name,
        "parents": [DRIVE_FOLDER_ID],
        "appProperties": {"sha256": fingerprint},
    }
    media = MediaFileUpload(
        file_path, resumable=True, chunksize=DRIVE_UPLOAD_CHUNK_BYTES
    )
    request = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink, name, size, appProperties"
    )
    response = None
    while response is None:
        if cancel_check and cancel_check():
            raise RuntimeError("__CANCELLED__")
        status, response = request.next_chunk(num_retries=3)
        if status and progress_cb:
            progress_cb(min(expected_size, int(status.progress() * expected_size)), expected_size)
    if not response or not response.get("id"):
        raise RuntimeError("Google Drive لم يُرجع معرّف الملف بعد الرفع.")
    verified = service.files().get(
        fileId=response["id"], fields="id, webViewLink, name, size, appProperties"
    ).execute(num_retries=3)
    actual_size = int(verified.get("size", -1))
    if actual_size != expected_size:
        raise RuntimeError(
            f"فشل التحقق من الملف في Drive: المتوقع {expected_size} بايت، الموجود {actual_size} بايت."
        )
    if (verified.get("appProperties") or {}).get("sha256") != fingerprint:
        raise RuntimeError("فشل التحقق من بصمة المحتوى بعد الرفع.")
    return {
        "id": verified.get("id"),
        "link": verified.get("webViewLink") or response.get("webViewLink"),
        "name": verified.get("name") or Path(file_path).name,
        "size": actual_size,
        "already_exists": False,
    }


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


def is_likely_direct_file_url(url):
    path = urlparse(url).path.lower()
    direct_exts = {
        ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".json", ".xml", ".apk", ".epub",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".mp3", ".m4a", ".wav", ".flac", ".ogg",
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    }
    return any(path.endswith(ext) for ext in direct_exts)


def ytdlp_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if extra:
        opts.update(extra)
    return opts


def fetch_url_info(url):
    if is_likely_direct_file_url(url):
        return {"is_video": False, "url": url, "direct_file": True}
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts({"skip_download": True})) as ydl:
            i = ydl.extract_info(url, download=False)
            formats = i.get("formats", []) or []
            has_video = any(f.get("vcodec") not in (None, "none") for f in formats)
            return {
                "title": i.get("title", "بدون عنوان"),
                "duration": i.get("duration"),
                "uploader": i.get("uploader") or i.get("channel"),
                "thumbnail": i.get("thumbnail"),
                "formats": formats,
                "webpage_url": i.get("webpage_url", url),
                "is_video": has_video,
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
        "concurrent_fragment_downloads": int(os.environ.get("YTDLP_CONCURRENT_FRAGMENTS", "8")),
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
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
    filename = urlparse(url).path.rsplit("/", 1)[-1] or "downloaded_file"
    filename = "".join(c for c in filename if c.isalnum() or c in "._- ") or "downloaded_file"
    fp = os.path.join(out_dir, filename)
    base_headers = {"User-Agent": "Mozilla/5.0"}
    attempts = 3

    for attempt in range(attempts):
        if cancel_check and cancel_check():
            raise RuntimeError("__CANCELLED__")
        existing = os.path.getsize(fp) if os.path.exists(fp) else 0
        headers = dict(base_headers)
        if existing:
            headers["Range"] = f"bytes={existing}-"
        try:
            with requests.get(url, stream=True, timeout=(15, 120), headers=headers) as response:
                if response.status_code == 416 and existing:
                    content_range = response.headers.get("Content-Range", "")
                    match = __import__("re").search(r"/(\d+)$", content_range)
                    if match and int(match.group(1)) == existing:
                        return fp
                response.raise_for_status()
                resume = existing > 0 and response.status_code == 206
                if not resume:
                    existing = 0
                content_range = response.headers.get("Content-Range", "")
                match = __import__("re").search(r"/(\d+)$", content_range)
                content_length = int(response.headers.get("Content-Length", 0) or 0)
                total = int(match.group(1)) if match else (content_length + existing if content_length else 0)
                done = existing
                mode = "ab" if resume else "wb"
                with open(fp, mode) as handle:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if cancel_check and cancel_check():
                            raise RuntimeError("__CANCELLED__")
                        if chunk:
                            handle.write(chunk)
                            done += len(chunk)
                            if progress_cb:
                                progress_cb({"downloaded_bytes": done, "total_bytes": total})
            if total and done != total:
                raise IOError(f"Incomplete download: expected {total} bytes, received {done}.")
            if done == 0:
                raise IOError("The source returned an empty file.")
            return fp
        except Exception as exc:
            if "__CANCELLED__" in str(exc) or attempt >= attempts - 1:
                raise
            delay = attempt + 1
            logger.warning("Direct download attempt %s/%s failed; retrying in %ss: %s", attempt + 1, attempts, delay, exc)
            time.sleep(delay)
    raise RuntimeError("Direct download failed after retries.")


async def youtube_gate():
    global youtube_last_request
    async with youtube_lock:
        wait = YOUTUBE_MIN_INTERVAL - (time.monotonic() - youtube_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        youtube_last_request = time.monotonic()


async def fetch_url_info_safe(url):
    if is_likely_direct_file_url(url):
        return await asyncio.to_thread(fetch_url_info, url)
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
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "👋 أهلاً بك في *DriveSaverBot*!\n\n"
        "تم تقييد البوت على حساب المالك المصرّح به. أرسل رابطًا أو ملفًا للبدء، أو استخدم /help.",
        parse_mode="Markdown",
    )


async def cmd_help(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "📖 *المساعدة*\n\n"
        "🔗 أرسل رابطاً → اختر الجودة عند الحاجة → يُحفظ في Drive\n"
        "📎 أرسل ملفاً → يُحفظ في Drive\n"
        "📋 /queue — العمليات الجارية والمنتظرة\n"
        "📈 /progress — لوحة تقدم قابلة للتحديث لكل مهمة\n"
        "📊 /status — حالة البوت ومساحة Colab\n"
        "🛑 /cancel [رقم المهمة] — إلغاء مهمة محددة أو كل المهام الجارية\n"
        "🔁 /retry [رقم المهمة] — إعادة محاولة مهمة فاشلة\\n"
        "📒 /colab — فتح دفتر Google Colab\n"
        "🆔 /id — معرف Telegram",
        parse_mode="Markdown",
    )


async def cmd_queue(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    running = list(active_tasks.values())
    queued = list(queued_tasks.values())
    waiting = [item for item in pending_downloads.values() if item.get("user_id") == OWNER_ID]
    lines = [
        "📋 حالة العمليات",
        f"🔄 نشطة: {len(running)}",
        f"⏳ في طابور التنفيذ: {len(queued)}",
        f"🎬 تنتظر اختيار الجودة: {len(waiting)}",
    ]
    for item in running[:10]:
        label = item.get("name") or item.get("url", "ملف")
        lines.append(f"• {item['id']} — {item.get('status', 'قيد التنفيذ')} — {label[:60]}")
    for item in queued[:10]:
        lines.append(f"• {item['id']} — انتظار — {item.get('url', 'ملف')[:70]}")
    for item in waiting[:10]:
        lines.append(f"• بانتظار اختيار الجودة — {item.get('url', 'رابط')[:70]}")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)



def format_task_progress(task):
    """Render one task's latest in-memory transfer progress."""
    task_id = task.get("id") or task.get("task_id") or "?"
    status = task.get("status", "قيد التنفيذ")
    name = task.get("name") or task.get("url") or task.get("file_name") or "ملف"
    phase = "تنزيل"
    done = total = speed = 0
    if "upload_bytes" in task or status == "رفع إلى Drive":
        phase = "رفع إلى Drive"
        done = int(task.get("upload_bytes") or 0)
        total = int(task.get("upload_total") or 0)
        speed = float(task.get("upload_speed") or 0)
    elif "download_bytes" in task:
        done = int(task.get("download_bytes") or 0)
        total = int(task.get("download_total") or 0)
        speed = float(task.get("download_speed") or 0)
    elif "telegram_download_started" in status.lower():
        phase = "تنزيل من Telegram"
    if total > 0:
        pct = max(0.0, min(100.0, done / total * 100))
        filled = int(pct // 5)
        bar = "█" * filled + "░" * (20 - filled)
        amount = f"{human_size(done)} / {human_size(total)} ({pct:.1f}%)"
    else:
        bar = "░" * 20
        amount = human_size(done) if done else "جارٍ تحديد الحجم"
    started_at = float(task.get("started_at") or time.time())
    elapsed = human_time(max(0, time.time() - started_at))
    return (
        f"🆔 المهمة: {task_id}\n"
        f"📄 الملف: {str(name)[:100]}\n"
        f"⚙️ المرحلة: {phase} — {status}\n"
        f"[{bar}]\n"
        f"💾 {amount}\n"
        f"⚡ السرعة: {human_size(speed)}/ث\\n"
        f"⏱️ الوقت المنقضي: {elapsed}"
    )


def build_progress_keyboard(tasks):
    rows = []
    for task in tasks[:12]:
        task_id = task.get("id") or task.get("task_id")
        if task_id:
            name = str(task.get("name") or task.get("file_name") or task.get("url") or "ملف")
            rows.append([InlineKeyboardButton(f"📊 {task_id} · {name[:32]}", callback_data=f"progress|{task_id}")])
    rows.append([InlineKeyboardButton("🔄 تحديث التقدم", callback_data="progress_refresh")])
    return InlineKeyboardMarkup(rows)


def render_progress_dashboard():
    tasks = list(active_tasks.values()) + list(queued_tasks.values())
    tasks.sort(key=lambda task: task.get("started_at", 0))
    if not tasks:
        return "📊 لا توجد مهام نشطة الآن. أرسل رابطًا أو ملفًا لبدء مهمة جديدة.", None
    lines = [
        "📊 لوحة تقدم المهام — تحديث تلقائي",
        f"🔄 نشطة: {len(active_tasks)} | ⏳ منتظرة: {len(queued_tasks)}",
        f"🔃 التحديث كل {PROGRESS_DASHBOARD_REFRESH_SECONDS} ثوانٍ",
        "",
    ]
    lines.extend(format_task_progress(task) for task in tasks[:8])
    if len(tasks) > 8:
        lines.append(f"… وهناك {len(tasks) - 8} مهام أخرى؛ استخدم /queue للقائمة الكاملة.")
    return "\n\n".join(lines), build_progress_keyboard(tasks)


async def progress_dashboard_loop(application, chat_id, message_id):
    """Keep one Telegram dashboard message updated while transfers are active."""
    try:
        while True:
            await asyncio.sleep(PROGRESS_DASHBOARD_REFRESH_SECONDS)
            text, keyboard = render_progress_dashboard()
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.debug("Progress dashboard update skipped: %s", exc)
            if keyboard is None:
                return
    finally:
        progress_dashboard_loops.discard(chat_id)


async def cmd_progress(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    text, keyboard = render_progress_dashboard()
    message = await update.message.reply_text(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    chat_id = update.effective_chat.id
    if keyboard is not None and chat_id not in progress_dashboard_loops:
        progress_dashboard_loops.add(chat_id)
        context.application.create_task(
            progress_dashboard_loop(context.application, chat_id, message.message_id),
            name=f"progress-dashboard-{chat_id}",
        )

async def cmd_status(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    try:
        usage = shutil.disk_usage("/content")
        disk = f"{human_size(usage.free)} متاحة من {human_size(usage.total)}"
    except OSError:
        disk = "غير متاحة"
    await update.message.reply_text(
        "🧠 *حالة DriveSaverBot*\n\n"
        f"🔄 عمليات نشطة: {len(active_tasks)}\n"
        f"💽 مساحة Colab: {disk}\n"
        f"📦 حجم جزء رفع Drive: {human_size(DRIVE_UPLOAD_CHUNK_BYTES)}\n"
        "☁️ بيئة التشغيل: Google Colab",
        parse_mode="Markdown",
    )


async def cmd_colab(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    url = "https://colab.research.google.com/github/Hus-GPT/drive-saver-bot/blob/main/colab/DriveSaverBot_Colab.ipynb"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📒 فتح دفتر Google Colab", url=url)]])
    await update.message.reply_text(
        "افتح الدفتر لتشغيل Colab. إذا كانت الجلسة متوقفة، شغّل خلية التشغيل يدويًا.",
        reply_markup=keyboard,
    )

async def cmd_id(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(f"🆔 `{update.effective_chat.id}`", parse_mode="Markdown")


async def cmd_stats(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    uid = update.effective_user.id
    pending = sum(1 for p in pending_downloads.values() if p["user_id"] == uid)
    await update.message.reply_text(f"📊 *إحصائياتك*\n\n👤 معرّفك: `{uid}`\n🔄 تحميلات معلقة: {pending}\n✅ المصادقة: {'نعم' if uid in authenticated_users else 'لا'}", parse_mode="Markdown")


async def cmd_retry(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    latest = latest_task_records()
    target = context.args[0] if context.args else None
    if target:
        record = latest.get(target)
        if not record:
            await update.message.reply_text("لم أجد سجلًا لهذه المهمة.")
            return
    else:
        failed = [item for item in latest.values() if item.get("status") == "failed"]
        failed.sort(key=lambda item: item.get("recorded_at", 0), reverse=True)
        record = failed[0] if failed else None
        if not record:
            await update.message.reply_text("لا توجد مهام فاشلة محفوظة لإعادة المحاولة.")
            return
        target = record.get("task_id")
    if record.get("status") != "failed":
        await update.message.reply_text("يمكن إعادة محاولة المهام الفاشلة فقط. تحقق من /queue ومن سجل المهمة.")
        return
    if record.get("recovery_requires_manual_url") or (not record.get("url") and not record.get("file_id")):
        await update.message.reply_text(
            "لا يمكن إعادة هذه المهمة تلقائيًا لأن رابطها يحتوي بيانات وصول حساسة أو لم يُحفظ مرجع قابل للاستعادة. "
            "أرسل الرابط مجددًا لأسباب أمنية."
        )
        return

    if record.get("url"):
        url = record["url"]
        duplicate = find_duplicate_url_task(url)
        if duplicate:
            await update.message.reply_text(
                f"هذه المهمة موجودة بالفعل في قائمة العمل: {duplicate['task_id']}."
            )
            return
        persist_task({
            "task_id": target, "user_id": OWNER_ID, "url": url,
            "quality": record.get("quality"), "is_video": record.get("is_video", False),
            "status": "retry_scheduled", "retry_scheduled_at": time.time(),
        })
        message = await update.message.reply_text(f"🔁 إعادة محاولة المهمة {target}...")
        info = {"is_video": True} if record.get("is_video") else None
        fake_update = SimpleNamespace(effective_user=update.effective_user)
        context.application.create_task(
            do_download(fake_update, context.application, message, url, record.get("quality"), info),
            name=f"retry-download-{target}",
        )
        return

    file_unique_id = record.get("file_unique_id")
    duplicate_id = find_duplicate_telegram_task(file_unique_id)
    if duplicate_id:
        await update.message.reply_text(f"هذا الملف قيد المعالجة بالفعل ضمن المهمة {duplicate_id}.")
        return
    persist_task({
        "task_id": target, "user_id": OWNER_ID, "file_id": record.get("file_id"),
        "file_name": record.get("file_name"), "file_unique_id": file_unique_id,
        "status": "retry_scheduled", "retry_scheduled_at": time.time(),
    })
    fake_message = SimpleNamespace(reply_text=update.message.reply_text)
    fake_update = SimpleNamespace(
        effective_user=update.effective_user,
        effective_chat=update.effective_chat,
        message=fake_message,
    )
    fake_file = SimpleNamespace(
        file_id=record["file_id"], file_size=record.get("size", 0),
        file_name=record.get("file_name"), file_unique_id=file_unique_id or target,
    )
    context.application.create_task(
        handle_telegram_file(fake_update, context.application, fake_file),
        name=f"retry-telegram-{target}",
    )
    await update.message.reply_text(f"🔁 أُعيدت جدولة ملف Telegram من المهمة {target}.")




async def cmd_cancel(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    target = context.args[0] if context.args else None
    pool = {**queued_tasks, **active_tasks}
    matches = [
        task_id for task_id, task in pool.items()
        if task.get("user_id") == OWNER_ID and (target is None or task_id == target)
    ]
    if target and not matches:
        await update.message.reply_text("لم أجد مهمة بهذا الرقم. استخدم /queue لعرض أرقام المهام.")
        return
    if not matches:
        await update.message.reply_text("لا توجد عمليات لإلغائها.")
        return
    for task_id in matches:
        cancel_flags[task_id] = True
    await update.message.reply_text("🛑 طُلب إلغاء: " + ", ".join(matches))


async def handle_message(update, context):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    uid = update.effective_user.id
    authenticated_users.add(uid)
    message = update.message
    file_obj = (
        message.document or message.video or message.audio or message.animation
        or message.voice or message.video_note or (message.photo[-1] if message.photo else None)
    )
    if file_obj:
        await handle_telegram_file(update, context, file_obj)
        return
    text = (update.message.text or "").strip()
    if text.startswith(("http://", "https://")):
        await handle_link(update, context, text)
        return
    await update.message.reply_text("❓ أرسل رابطاً أو ملفاً، أو /help.")


def find_duplicate_url_task(url):
    """Find an identical URL already waiting for quality selection or transfer."""
    for task_id, task in {**active_tasks, **queued_tasks}.items():
        if task.get("user_id") == OWNER_ID and task.get("url") == url:
            return {"kind": "transfer", "task_id": task_id, "task": task}
    for short_id, task in pending_downloads.items():
        if task.get("user_id") == OWNER_ID and task.get("url") == url:
            return {"kind": "quality", "task_id": short_id, "task": task}
    return None


async def handle_link(update, context, url):
    duplicate = find_duplicate_url_task(url)
    if duplicate:
        if duplicate["kind"] == "quality":
            await update.message.reply_text(
                "♻️ هذا الرابط موجود بالفعل وينتظر اختيار الجودة. استخدم الرسالة السابقة لإكماله."
            )
        else:
            await update.message.reply_text(
                f"♻️ هذا الرابط قيد الانتظار أو التنزيل بالفعل.\n🆔 المهمة: {duplicate['task_id']}\n"
                "لم أضف مهمة مكررة."
            )
        return
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
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await q.answer("غير مصرح لك باستخدام هذا البوت.", show_alert=True)
        return
    await q.answer()
    data = q.data
    uid = update.effective_user.id
    if data == "progress_refresh":
        text, keyboard = render_progress_dashboard()
        await q.edit_message_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return
    if data.startswith("progress|"):
        task_id = data.split("|", 1)[1]
        task = active_tasks.get(task_id) or queued_tasks.get(task_id)
        if not task:
            await q.edit_message_text("انتهت هذه المهمة أو لم تعد نشطة. أرسل /progress لعرض المهام الحالية.")
            return
        await q.message.reply_text(
            format_task_progress(task),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 تحديث", callback_data=f"progress|{task_id}"),
                InlineKeyboardButton("📋 لوحة المهام", callback_data="progress_refresh"),
            ]]),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("cancel|"):
        sid = data.split("|", 1)[1]
        p = pending_downloads.pop(sid, None)
        if p and p.get("user_id") == uid:
            await q.edit_message_text("❌ تم إلغاء طلب التنزيل.")
        else:
            await q.edit_message_text("❌ انتهى الطلب أو لا تملك صلاحية إلغائه.")
        return
    if data.startswith("large_cancel|"):
        await q.edit_message_text("تم إلغاء الإجراء. لم تُنشأ مهمة نقل.")
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
    task_id = uuid.uuid4().hex[:8]
    started = time.time()
    loop = asyncio.get_running_loop()
    sample = {"time": started, "bytes": 0, "last_edit": 0.0}
    queued_tasks[task_id] = {
        "id": task_id, "user_id": uid, "url": url, "status": "في الانتظار",
        "started_at": started,
    }
    cancel_flags[task_id] = False
    is_video = bool(info and info.get("is_video"))
    persist_task({
        "task_id": task_id, "user_id": uid, "url": url, "quality": quality,
        "is_video": is_video, "status": "queued", "started_at": started,
    })
    await transfer_semaphore.acquire()
    queued_tasks.pop(task_id, None)
    if cancel_flags.get(task_id, False):
        persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "cancelled"})
        cancel_flags.pop(task_id, None)
        transfer_semaphore.release()
        await msg.edit_text(f"🛑 أُلغيت المهمة {task_id} قبل بدء النقل.")
        return
    active_tasks[task_id] = {
        "id": task_id, "user_id": uid, "url": url,
        "status": "تنزيل", "started_at": started,
    }
    persist_task({
        "task_id": task_id, "user_id": uid, "url": url, "quality": quality,
        "is_video": is_video, "status": "started", "started_at": started,
    })

    def cancelled():
        return cancel_flags.get(task_id, False)

    def download_progress(data):
        now = time.time()
        done = int(data.get("downloaded_bytes") or 0)
        total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
        elapsed = max(now - sample["time"], 0.001)
        speed = data.get("speed") or max(0, done - sample["bytes"]) / elapsed
        sample.update({"time": now, "bytes": done})
        active_tasks[task_id].update({
            "status": "تنزيل", "download_bytes": done,
            "download_total": total, "download_speed": speed,
        })
        if now - sample["last_edit"] < PROGRESS_UPDATE_INTERVAL:
            return
        sample["last_edit"] = now
        if total:
            pct = min(100, done / total * 100)
            filled = min(20, int(pct / 5))
            bar = "█" * filled + "░" * (20 - filled)
            text = f"⬇️ *تنزيل الملف*\n\nالمهمة: {task_id}\n[{bar}] {pct:.1f}%\n💾 {human_size(done)} / {human_size(total)}\n⚡ {human_size(speed)}/s"
        else:
            text = f"⬇️ *تنزيل الملف*\n\nالمهمة: {task_id}\n💾 {human_size(done)}\n⚡ {human_size(speed)}/s"
        loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_edit(msg, text)))

    try:
        with tempfile.TemporaryDirectory(prefix="drive-saver-") as tmp:
            try:
                if is_youtube_url(url):
                    await youtube_gate()
                if info and info.get("is_video"):
                    file_path = await asyncio.to_thread(
                        download_video, url, tmp, quality or "720", download_progress, cancelled
                    )
                else:
                    file_path = await asyncio.to_thread(
                        download_direct, url, tmp, download_progress, cancelled
                    )
            except Exception as exc:
                if "__CANCELLED__" in str(exc):
                    await msg.edit_text(f"🛑 أُلغيت المهمة {task_id}.")
                    persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "cancelled"})
                    return
                if info and info.get("is_video") and is_rate_limit_error(exc) and is_youtube_url(url):
                    await msg.edit_text("⚠️ YouTube قيّد الطلبات من خادم البوت (429). جرّب لاحقًا.")
                    persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "failed", "error": str(exc)[:400]})
                    return
                raise

            if cancelled():
                await msg.edit_text(f"🛑 أُلغيت المهمة {task_id}.")
                persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "cancelled"})
                return
            if not file_path or not os.path.isfile(file_path):
                raise RuntimeError("لم يتم إنشاء ملف قابل للرفع.")
            extracted_paths = await asyncio.to_thread(extract_archive_contents, file_path, tmp)
            if extracted_paths is not None:
                results = []
                for extracted_path in extracted_paths:
                    if cancelled():
                        raise RuntimeError("__CANCELLED__")
                    extracted_size = os.path.getsize(extracted_path)
                    active_tasks[task_id].update({"status": "رفع محتويات الأرشيف", "name": Path(extracted_path).name, "size": extracted_size})
                    result = await asyncio.to_thread(upload_to_drive, extracted_path, None, cancelled)
                    results.append(result)
                    persist_task({
                        "task_id": task_id, "user_id": uid, "url": url,
                        "name": result["name"], "size": extracted_size,
                        "drive_file_id": result["id"], "drive_link": result["link"],
                        "status": "archive_member_completed",
                        "already_exists": result.get("already_exists", False),
                    })
                links = "\n".join(
                    f"• {item['name']} — " + ("موجود مسبقًا" if item.get("already_exists") else "تم الرفع")
                    for item in results
                )
                persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "completed", "archive": True, "extracted_count": len(results)})
                await msg.edit_text(
                    f"✅ تم استخراج الأرشيف دون رفعه، ومعالجة {len(results)} ملفًا.\n\n{links}",
                    disable_web_page_preview=True,
                )
                return
            size = os.path.getsize(file_path)
            active_tasks[task_id].update({"status": "رفع إلى Drive", "name": Path(file_path).name, "size": size})
            persist_task({"task_id": task_id, "user_id": uid, "url": url, "quality": quality, "is_video": is_video, "name": Path(file_path).name, "size": size, "status": "uploading"})
            upload_started = time.time()
            upload_sample = {"time": upload_started, "bytes": 0, "last_edit": 0.0}

            def upload_progress(done, total):
                now = time.time()
                speed = max(0, done - upload_sample["bytes"]) / max(now - upload_sample["time"], 0.001)
                upload_sample.update({"time": now, "bytes": done})
                active_tasks[task_id].update({
                    "status": "رفع إلى Drive", "upload_bytes": done,
                    "upload_total": total, "upload_speed": speed,
                })
                if now - upload_sample["last_edit"] < PROGRESS_UPDATE_INTERVAL and done < total:
                    return
                upload_sample["last_edit"] = now
                pct = done / total * 100 if total else 0
                text = f"📤 *رفع إلى Google Drive*\n\nالمهمة: {task_id}\nالتقدم: {pct:.1f}% — {human_size(done)} / {human_size(total)}\n⚡ {human_size(speed)}/s\n⏱️ {human_time(now - upload_started)}"
                loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_edit(msg, text)))

            await msg.edit_text(f"📤 جارٍ الرفع إلى Drive...\n💾 {human_size(size)}\nالمهمة: {task_id}")
            result = await asyncio.to_thread(upload_to_drive, file_path, upload_progress, cancelled)
            elapsed = time.time() - started
            persist_task({
                "task_id": task_id, "user_id": uid, "url": url,
                "name": result["name"], "size": size, "status": "completed",
                "drive_file_id": result["id"], "drive_link": result["link"],
                "elapsed_seconds": round(elapsed, 2),
                "average_bytes_per_second": round(size / elapsed, 2) if elapsed else 0,
            })
            if result.get("already_exists"):
                success_prefix = "♻️ *الملف موجود مسبقًا في Drive؛ لم أرفع نسخة مكررة.*"
            else:
                success_prefix = "✅ *تم الحفظ والتحقق في Drive!*"
            await msg.edit_text(
                f"{success_prefix}\n\n📁 [{result['name']}]({result['link']})"
                f"\n💾 الحجم: {human_size(size)}\n⏱️ الوقت الكلي: {human_time(elapsed)}"
                f"\n⚡ متوسط السرعة الكلية: {human_size(size / elapsed if elapsed else 0)}/s"
                f"\n🆔 المهمة: {task_id}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
    except Exception as exc:
        if "__CANCELLED__" in str(exc) or cancelled():
            persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "cancelled"})
            try:
                await msg.edit_text(f"🛑 أُلغيت المهمة {task_id}.")
            except Exception:
                pass
        else:
            logger.error("Task %s failed: %s", task_id, exc, exc_info=True)
            persist_task({"task_id": task_id, "user_id": uid, "url": url, "status": "failed", "error": str(exc)[:500]})
            try:
                await msg.edit_text(f"❌ فشلت المهمة {task_id}:\n{str(exc)[:300]}")
            except Exception:
                pass
    finally:
        cancel_flags.pop(task_id, None)
        queued_tasks.pop(task_id, None)
        active_tasks.pop(task_id, None)
        transfer_semaphore.release()


def find_duplicate_telegram_task(file_unique_id):
    if not file_unique_id:
        return None
    for task_id, task in {**active_tasks, **queued_tasks}.items():
        if task.get("user_id") == OWNER_ID and task.get("file_unique_id") == file_unique_id:
            return task_id
    return None


async def handle_telegram_file(update, context, file_obj):
    file_size = getattr(file_obj, "file_size", None) or 0
    if file_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
        sid = uuid.uuid4().hex[:8]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 إرسال / فتح بوت التحويل", url="https://t.me/File2url_rbot"),
            InlineKeyboardButton("❌ إلغاء", callback_data=f"large_cancel|{sid}"),
        ]])
        await update.message.reply_text(
            "📦 *الملف يتجاوز حد التنزيل المباشر للبوت*\n\n"
            f"💾 الحجم: {human_size(file_size)}\n"
            "للمتابعة، افتح بوت تحويل الملف إلى رابط وأرسل الملف إليه. بعد حصولك على الرابط، أرسله هنا ليُرفع إلى Google Drive.\n\n"
            "هل تريد المتابعة؟",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    uid = update.effective_user.id
    file_unique_id = getattr(file_obj, "file_unique_id", None)
    duplicate_id = find_duplicate_telegram_task(file_unique_id)
    if duplicate_id:
        await update.message.reply_text(
            f"♻️ هذا الملف موجود بالفعل في قائمة العمل.\\n🆔 المهمة: {duplicate_id}\\nلم أضف مهمة مكررة."
        )
        return
    task_id = uuid.uuid4().hex[:8]
    started = time.time()
    queued_tasks[task_id] = {
        "id": task_id, "user_id": uid, "status": "في الانتظار",
        "name": getattr(file_obj, "file_name", None),
        "file_unique_id": file_unique_id,
        "started_at": started,
    }
    cancel_flags[task_id] = False
    persist_task({
        "task_id": task_id, "user_id": uid, "file_id": file_obj.file_id,
        "file_name": getattr(file_obj, "file_name", None),
        "file_unique_id": getattr(file_obj, "file_unique_id", None),
        "status": "queued", "started_at": started,
    })
    await transfer_semaphore.acquire()
    queued_tasks.pop(task_id, None)
    if cancel_flags.get(task_id, False):
        persist_task({"task_id": task_id, "user_id": uid, "file_id": file_obj.file_id, "status": "cancelled"})
        cancel_flags.pop(task_id, None)
        transfer_semaphore.release()
        await update.message.reply_text("🛑 أُلغيت المهمة قبل بدء النقل.")
        return
    active_tasks[task_id] = {
        "id": task_id, "user_id": uid, "status": "تنزيل من Telegram",
        "name": getattr(file_obj, "file_name", None),
        "file_unique_id": file_unique_id,
        "started_at": started,
    }
    persist_task({
        "task_id": task_id, "user_id": uid, "file_id": file_obj.file_id,
        "file_name": getattr(file_obj, "file_name", None),
        "file_unique_id": getattr(file_obj, "file_unique_id", None),
        "status": "telegram_download_started", "started_at": started,
    })
    msg = None
    try:
        msg = await update.message.reply_text(f"📥 جارٍ التنزيل من Telegram...\nالمهمة: {task_id}")
        tg = await context.bot.get_file(file_obj.file_id)
        raw_name = getattr(file_obj, "file_name", None) or f"telegram_{getattr(file_obj, 'file_unique_id', task_id)}"
        name = Path(raw_name).name
        name = "".join(ch for ch in name if ch.isalnum() or ch in "._- ()")[:180] or f"telegram_{task_id}"
        with tempfile.TemporaryDirectory(prefix="drive-saver-telegram-") as tmp:
            path = os.path.join(tmp, name)
            await tg.download_to_drive(path)
            extracted_paths = await asyncio.to_thread(extract_archive_contents, path, tmp)
            if extracted_paths is not None:
                results = []
                for extracted_path in extracted_paths:
                    if cancel_flags.get(task_id, False):
                        raise RuntimeError("__CANCELLED__")
                    extracted_size = os.path.getsize(extracted_path)
                    result = await asyncio.to_thread(upload_to_drive, extracted_path, None, lambda: cancel_flags.get(task_id, False))
                    results.append(result)
                    persist_task({
                        "task_id": task_id, "user_id": uid, "file_name": Path(extracted_path).name,
                        "size": extracted_size, "drive_file_id": result["id"],
                        "drive_link": result["link"], "status": "archive_member_completed",
                        "already_exists": result.get("already_exists", False),
                    })
                links = "\n".join(
                    f"• {item['name']} — " + ("موجود مسبقًا" if item.get("already_exists") else "تم الرفع")
                    for item in results
                )
                persist_task({"task_id": task_id, "user_id": uid, "status": "completed", "archive": True, "extracted_count": len(results)})
                await msg.edit_text(
                    f"✅ تم استخراج الأرشيف دون رفعه، ومعالجة {len(results)} ملفًا.\n\n{links}",
                    disable_web_page_preview=True,
                )
                return
            size = os.path.getsize(path)
            active_tasks[task_id].update({"status": "رفع إلى Drive", "name": name, "size": size})
            persist_task({
                "task_id": task_id, "user_id": uid, "file_id": file_obj.file_id,
                "file_name": name, "file_unique_id": getattr(file_obj, "file_unique_id", None),
                "size": size, "status": "uploading_telegram",
            })
            upload_started = time.time()
            sample = {"time": upload_started, "bytes": 0, "last_edit": 0.0}
            loop = asyncio.get_running_loop()

            def upload_progress(done, total):
                now = time.time()
                speed = max(0, done - sample["bytes"]) / max(now - sample["time"], 0.001)
                sample.update({"time": now, "bytes": done})
                pct = done / total * 100 if total else 0
                active_tasks[task_id].update({"upload_bytes": done, "upload_total": total, "upload_speed": speed})
                if now - sample["last_edit"] < PROGRESS_UPDATE_INTERVAL and done < total:
                    return
                sample["last_edit"] = now
                text = f"📤 رفع إلى Drive: {pct:.1f}%\n💾 {human_size(done)} / {human_size(total)}\n⚡ {human_size(speed)}/s"
                loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_edit(msg, text)))

            result = await asyncio.to_thread(upload_to_drive, path, upload_progress, lambda: cancel_flags.get(task_id, False))
            elapsed = time.time() - started
            persist_task({
                "task_id": task_id, "user_id": uid, "name": result["name"], "size": size,
                "status": "completed", "drive_file_id": result["id"], "drive_link": result["link"],
                "elapsed_seconds": round(elapsed, 2),
            })
            await msg.edit_text(
                f"✅ *تم الحفظ والتحقق في Drive!*\n\n📁 [{result['name']}]({result['link']})"
                f"\n💾 {human_size(size)}\n⏱️ {human_time(elapsed)}\n🆔 المهمة: {task_id}",
                parse_mode="Markdown", disable_web_page_preview=True,
            )
    except Exception as exc:
        logger.error("Telegram file task %s failed: %s", task_id, exc, exc_info=True)
        persist_task({"task_id": task_id, "user_id": uid, "status": "failed", "error": str(exc)[:500]})
        try:
            if msg is not None:
                await msg.edit_text(f"❌ فشلت المهمة {task_id}:\n{str(exc)[:300]}")
            else:
                await update.message.reply_text(f"❌ فشلت المهمة {task_id}: {str(exc)[:250]}")
        except Exception:
            pass
    finally:
        cancel_flags.pop(task_id, None)
        queued_tasks.pop(task_id, None)
        active_tasks.pop(task_id, None)
        transfer_semaphore.release()

def latest_task_records():
    latest = {}
    path = Path(TASK_LOG_PATH)
    if not path.exists():
        return latest
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    task_id = item.get("task_id")
                    if task_id:
                        latest[task_id] = item
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("Could not read task journal: %s", exc)
    return latest


async def configure_bot_menu(application):
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("help", "المساعدة"),
        BotCommand("queue", "العمليات الجارية والمنتظرة"),
        BotCommand("progress", "لوحة تقدم المهام"),
        BotCommand("status", "حالة البوت ومساحة Colab"),
        BotCommand("colab", "فتح دفتر Google Colab"),
        BotCommand("cancel", "إلغاء مهمة"),
        BotCommand("retry", "إعادة محاولة مهمة فاشلة"),
    ]
    await application.bot.set_my_commands(commands)
    # Telegram's native menu can show commands; a direct URL requires a Mini App.
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    latest = latest_task_records()
    recoverable = [
        item for item in latest.values()
        if item.get("status") in {"queued", "started", "uploading", "telegram_download_started", "uploading_telegram", "retry_scheduled"}
        and (item.get("url") or item.get("file_id"))
    ]
    for item in recoverable:
        old_id = item.get("task_id")
        try:
            if item.get("file_id"):
                async def reply_text(text, **kwargs):
                    return await application.bot.send_message(chat_id=OWNER_ID, text=text, **kwargs)
                fake_message = SimpleNamespace(reply_text=reply_text)
                fake_update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=OWNER_ID), message=fake_message
                )
                fake_file = SimpleNamespace(
                    file_id=item["file_id"], file_size=0,
                    file_name=item.get("file_name"),
                    file_unique_id=item.get("file_unique_id", old_id),
                )
                asyncio.create_task(handle_telegram_file(fake_update, application, fake_file))
            else:
                message = await application.bot.send_message(
                    chat_id=OWNER_ID,
                    text="♻️ استعادة مهمة انقطعت سابقًا.",
                    disable_web_page_preview=True,
                )
                info = {"is_video": True} if item.get("is_video") else None
                fake_update = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER_ID))
                asyncio.create_task(
                    do_download(fake_update, application, message, item["url"], item.get("quality"), info)
                )
            persist_task({
                "task_id": old_id, "user_id": OWNER_ID, "url": item.get("url"),
                "file_id": item.get("file_id"), "status": "recovery_scheduled",
                "recovered_as_new_task": True,
            })
        except Exception as exc:
            logger.warning("Could not recover task %s: %s", old_id, exc)



def main():
    logger.info("🚀 Starting DriveSaverBot inside Google Colab...")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(16).post_init(configure_bot_menu).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("colab", cmd_colab))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & filters.User(user_id=OWNER_ID) & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
