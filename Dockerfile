FROM python:3.11-slim

# تثبيت ffmpeg (ضروري لدمج الفيديو واستخراج MP3)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# مجلد العمل
WORKDIR /app

# تثبيت المكتبات (طبقة منفصلة للاستفادة من الكاش)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود البوت
COPY bot.py .

# تشغيل البوت
CMD ["python", "-u", "bot.py"]
