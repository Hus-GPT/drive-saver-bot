FROM node:22-bookworm-slim

# Python + ffmpeg are required by the bot; Node 22 is required by current yt-dlp EJS.
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip ffmpeg git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Build the BgUtils PO-token HTTP provider locally so yt-dlp can obtain fresh
# YouTube proof-of-origin tokens without storing browser cookies.
RUN git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider && \
    cd /opt/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc

COPY bot.py .
COPY render_probe.py .

# Add a single FIFO queue around downloads/uploads without changing the source file yet.
RUN python3 - <<'PY'
from pathlib import Path
p = Path('/app/bot.py')
s = p.read_text()

s = s.replace(
    'pending_downloads = {}\ncancel_flags = {}\nyoutube_lock = asyncio.Lock()',
    'pending_downloads = {}\ncancel_flags = {}\ndownload_queue = asyncio.Queue()\ndownload_jobs = {}\nqueue_worker_task = None\nyoutube_lock = asyncio.Lock()'
)

old = '''async def handle_link(update, context, url):
    msg = await update.message.reply_text("🔍 جلب معلومات الرابط...")
    info = await fetch_url_info_safe(url)
    if not info.get("is_video"):
        if info.get("rate_limited") and info.get("youtube"):
            await msg.edit_text("⚠️ YouTube يقيّد مؤقتاً طلبات خادم البوت (429).\\n\\nلن نحاول تنزيل الرابط كأنه ملف مباشر، لأن ذلك لن يحل المشكلة. جرّب بعد فترة قصيرة.")
            return
        await msg.edit_text("📥 رابط مباشر — جارٍ التحميل...")
        await do_download(update, context, msg, url, None, None)
        return
    sid = uuid.uuid4().hex[:10]
    pending_downloads[sid] = {"url": url, "info": info, "user_id": update.effective_user.id, "chat_id": update.effective_chat.id, "message_id": msg.message_id, "created_at": time.time()}
    await msg.edit_text(f"🎬 *{info['title'][:80]}*\\n\\n👤 {info.get('uploader') or '?'}\\n⏱️ المدة: {human_time(info['duration']) if info.get('duration') else '?'}\\n\\nاختر الجودة:", parse_mode="Markdown", reply_markup=build_quality_keyboard(sid, info.get("formats", [])))
'''
new = '''async def enqueue_download(update, context, msg, url, quality, info, kind="url", file_obj=None):
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "update": update,
        "context": context,
        "msg": msg,
        "url": url,
        "quality": quality,
        "info": info,
        "kind": kind,
        "file_obj": file_obj,
        "user_id": update.effective_user.id,
        "created_at": time.time(),
        "status": "queued",
        "cancel_event": asyncio.Event(),
    }
    download_jobs[job_id] = job
    position = download_queue.qsize() + 1
    await download_queue.put(job)
    await msg.edit_text(
        f"⏳ *تمت إضافة الطلب إلى الانتظار*\\n\\n"
        f"📋 رقم الطلب: `{job_id}`\\n"
        f"🔢 ترتيبه الحالي: {position}\\n"
        f"سيبدأ تلقائيًا عند وصول دوره.",
        parse_mode="Markdown",
    )
    return job_id


async def handle_link(update, context, url):
    msg = await update.message.reply_text("🔍 جلب معلومات الرابط...")
    info = await fetch_url_info_safe(url)
    if not info.get("is_video"):
        if info.get("rate_limited") and info.get("youtube"):
            await msg.edit_text("⚠️ YouTube يقيّد مؤقتاً طلبات خادم البوت (429).\\n\\nلن نحاول تنزيل الرابط كأنه ملف مباشر، لأن ذلك لن يحل المشكلة. جرّب بعد فترة قصيرة.")
            return
        await enqueue_download(update, context, msg, url, None, None)
        return
    sid = uuid.uuid4().hex[:10]
    pending_downloads[sid] = {"url": url, "info": info, "user_id": update.effective_user.id, "chat_id": update.effective_chat.id, "message_id": msg.message_id, "created_at": time.time()}
    await msg.edit_text(f"🎬 *{info['title'][:80]}*\\n\\n👤 {info.get('uploader') or '?'}\\n⏱️ المدة: {human_time(info['duration']) if info.get('duration') else '?'}\\n\\nاختر الجودة:", parse_mode="Markdown", reply_markup=build_quality_keyboard(sid, info.get("formats", [])))
'''
if old not in s:
    raise SystemExit('handle_link block not found')
s = s.replace(old, new)

old = '''        if p["user_id"] != uid:
            await q.answer("هذا الطلب ليس لك!", show_alert=True)
            return
        await q.edit_message_text("📥 جارٍ التحميل...")
        await do_download(update, context, q.message, p["url"], quality, p["info"])
'''
new = '''        if p["user_id"] != uid:
            await q.answer("هذا الطلب ليس لك!", show_alert=True)
            return
        await enqueue_download(update, context, q.message, p["url"], quality, p["info"])
'''
if old not in s:
    raise SystemExit('callback block not found')
s = s.replace(old, new)

s = s.replace(
    'async def do_download(update, context, msg, url, quality, info):\n    uid = update.effective_user.id\n    cancel_flags[uid] = False',
    'async def do_download(update, context, msg, url, quality, info, cancel_event=None):\n    uid = update.effective_user.id\n    cancel_flags[uid] = False'
)
s = s.replace(
    '    def cancelled():\n        return cancel_flags.get(uid, False)',
    '    def cancelled():\n        return cancel_flags.get(uid, False) or (cancel_event is not None and cancel_event.is_set())'
)

old = '''    msg = await update.message.reply_text("📥 جارٍ التنزيل من تلجرام...")
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
            await msg.edit_text(f"✅ *تم الحفظ في Drive!*\\n\\n📁 [{result['name']}]({result['link']})\\n💾 {human_size(size)}\\n⏱️ {human_time(time.time() - start)}", parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ فشل:\\n`{str(e)[:300]}`", parse_mode="Markdown")
'''
new = '''    msg = await update.message.reply_text("⏳ تم استلام الملف ووضعه في الانتظار...")
    await enqueue_download(update, context, msg, None, None, None, kind="telegram_file", file_obj=file_obj)
'''
if old not in s:
    raise SystemExit('telegram block not found')
s = s.replace(old, new)

marker = '\n\napp_flask = Flask(__name__)'
worker = '''

async def process_telegram_file_job(job):
    context = job["context"]
    msg = job["msg"]
    file_obj = job["file_obj"]
    cancel_event = job["cancel_event"]
    start = time.time()
    try:
        await msg.edit_text("📥 *بدأ تنزيل الملف...*", parse_mode="Markdown")
        tg = await context.bot.get_file(file_obj.file_id)
        name = file_obj.file_name or f"tg_{file_obj.file_unique_id}"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            await tg.download_to_drive(path)
            if cancel_event.is_set():
                await msg.edit_text("🛑 تم إلغاء الطلب.")
                return
            size = os.path.getsize(path)
            await msg.edit_text(f"📤 جارٍ الرفع إلى Drive...\\n💾 {human_size(size)}")
            result = await asyncio.to_thread(upload_to_drive, path)
            await msg.edit_text(
                f"✅ *تم الحفظ في Drive!*\\n\\n📁 [{result['name']}]({result['link']})\\n💾 {human_size(size)}\\n⏱️ {human_time(time.time() - start)}",
                parse_mode="Markdown", disable_web_page_preview=True
            )
    except Exception as e:
        logger.error(f"Queue Telegram file error: {e}", exc_info=True)
        await msg.edit_text(f"❌ فشل:\\n`{str(e)[:300]}`", parse_mode="Markdown")


async def queue_worker(application):
    while True:
        job = await download_queue.get()
        job_id = job["id"]
        job["status"] = "running"
        try:
            if job["cancel_event"].is_set():
                await job["msg"].edit_text("🛑 تم إلغاء الطلب قبل بدء التنفيذ.")
                continue
            if job["kind"] == "telegram_file":
                await process_telegram_file_job(job)
            else:
                await job["msg"].edit_text("📥 *جارٍ بدء التنزيل...*", parse_mode="Markdown")
                await do_download(
                    job["update"], job["context"], job["msg"],
                    job["url"], job["quality"], job["info"],
                    cancel_event=job["cancel_event"]
                )
            job["status"] = "done"
        except Exception as e:
            job["status"] = "failed"
            logger.error(f"Queue job {job_id} failed: {e}", exc_info=True)
            try:
                await job["msg"].edit_text(f"❌ فشل الطلب:\\n`{str(e)[:300]}`", parse_mode="Markdown")
            except Exception:
                pass
        finally:
            download_jobs.pop(job_id, None)
            download_queue.task_done()


async def post_init(application):
    global queue_worker_task
    queue_worker_task = asyncio.create_task(queue_worker(application))
    logger.info("📦 Download queue worker started")
'''
if marker not in s:
    raise SystemExit('app marker not found')
s = s.replace(marker, worker + marker)
s = s.replace(
    'app = Application.builder().token(BOT_TOKEN).concurrent_updates(4).build()',
    'app = Application.builder().token(BOT_TOKEN).concurrent_updates(4).post_init(post_init).build()'
)

compile(s, '/app/bot.py', 'exec')
p.write_text(s)
print('Queue patch applied and syntax validated.')
PY

# Run the temporary Render-side probe once (when RENDER_PROBE_URL is set),
# then start the PO-token provider and the Telegram bot.
CMD ["sh", "-c", "python3 render_probe.py; node /opt/bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 & exec python3 -u bot.py"]
