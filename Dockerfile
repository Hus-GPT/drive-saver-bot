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

# Start the PO-token provider on localhost, then start the Telegram bot.
CMD ["sh", "-c", "node /opt/bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 & exec python3 -u bot.py"]
