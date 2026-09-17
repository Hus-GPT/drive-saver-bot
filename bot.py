import os
import logging
import tempfile
import time
import uuid
import asyncio
import threading
from pathlib import Path
from urllib.parse import urlparse
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
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
PROGRESS_UPDATE_INTERVAL = 3
YOUTUBE_MIN_INTERVAL = 8
YOUTUBE_RETRIES = 2
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024

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
    # Google Colab: use the account authenticated by google.colab.auth.
    # This avoids requiring OAuth client ID/secret/refresh-token values.
    if os.environ.get("COLAB_AUTH", "").lower() == "1":
        import google.auth
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # Non-Colab deployments keep the existing refresh-token authentication.
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
