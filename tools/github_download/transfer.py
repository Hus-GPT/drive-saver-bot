#!/usr/bin/env python3
"""Download a URL on the GitHub runner, safely extract archives, and upload contents to Drive.

Required environment variables: SOURCE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
GOOGLE_REFRESH_TOKEN. OUTPUT_FILENAME is optional.
"""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

CHUNK = 1024 * 1024
MAX_REDIRECTS = 10
MAX_EXTRACTED_BYTES = 12 * 1024**3
MAX_EXTRACTED_FILES = 100_000
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GithubDownloadTransfer/1.0"})


def log(message: str) -> None:
    print(f"::notice::{message}", flush=True)


def validate_public_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("الرابط يجب أن يكون HTTP/HTTPS عامًا، دون بيانات دخول مضمّنة.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f"تعذر حل اسم المضيف: {p.hostname}") from exc
    if not addresses:
        raise ValueError("اسم المضيف لا يملك عنوان IP صالحًا.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ValueError("تم رفض الرابط لأن المضيف ليس عنوانًا عامًا.")
    return url


def safe_filename(name: str) -> str:
    name = unquote(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    return (name[:180] or "download.bin")


def filename_from_response(url: str, headers: dict, override: str = "") -> str:
    if override.strip():
        return safe_filename(override.strip())
    cd = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", cd, re.I)
    if match:
        return safe_filename(match.group(1) or match.group(2))
    return safe_filename(Path(urlparse(url).path).name or "download.bin")


def stream_download(source_url: str, destination: Path, override: str = "") -> tuple[str, int, str]:
    """Download with retries and HTTP Range continuation when the source supports it."""
    current = validate_public_url(source_url)
    redirects = 0
    # Resolve redirect chain explicitly so each destination is validated before request.
    while True:
        validate_public_url(current)
        try:
            response = SESSION.get(current, stream=True, allow_redirects=False, timeout=(25, 90))
        except requests.RequestException as exc:
            raise RuntimeError(f"تعذر الاتصال بمصدر التنزيل: {exc}") from exc
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location or redirects >= MAX_REDIRECTS:
                raise RuntimeError("تجاوز الرابط عدد التحويلات المسموح.")
            current = validate_public_url(urljoin(current, location))
            redirects += 1
            continue
        break

    if response.status_code not in (200, 206):
        code = response.status_code
        response.close()
        raise RuntimeError(f"المصدر رفض التنزيل برمز HTTP {code}. قد يحتاج الرابط إلى تسجيل دخول.")
    name = filename_from_response(current, response.headers, override)
    expected = response.headers.get("Content-Length")
    expected_size = int(expected) if expected and expected.isdigit() else None
    response.close()

    partial = destination.with_suffix(destination.suffix + ".part")
    attempts = 0
    while attempts < 5:
        attempts += 1
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            # Re-open the validated final URL on each retry.
            validate_public_url(current)
            with SESSION.get(current, headers=headers, stream=True, allow_redirects=False, timeout=(25, 90)) as r:
                if r.is_redirect or r.is_permanent_redirect:
                    # Re-enter the redirect chain through the initial URL; safe but restarts if needed.
                    raise RuntimeError("تغير رابط التحويل أثناء إعادة المحاولة؛ أعد إرسال رابط المصدر.")
                if offset and r.status_code == 206:
                    cr = r.headers.get("Content-Range", "")
                    if not cr.startswith(f"bytes {offset}-"):
                        partial.unlink(missing_ok=True)
                        raise RuntimeError("استجابة Range غير متطابقة؛ أُعيد ضبط التنزيل.")
                    mode = "ab"
                elif r.status_code == 200:
                    offset = 0
                    mode = "wb"
                else:
                    raise RuntimeError(f"استجابة التنزيل غير صالحة: HTTP {r.status_code}.")
                total_header = r.headers.get("Content-Length")
                total_this_response = int(total_header) if total_header and total_header.isdigit() else None
                with partial.open(mode) as f:
                    for block in r.iter_content(chunk_size=CHUNK):
                        if block:
                            f.write(block)
                            f.flush()
                    f.flush()
                    os.fsync(f.fileno())
            if expected_size is not None and partial.stat().st_size != expected_size:
                # Content-Length from a range response may describe only the remainder.
                if partial.stat().st_size < expected_size:
                    raise requests.ConnectionError("اكتمل الاتصال قبل اكتمال حجم الملف المتوقع.")
            partial.replace(destination)
            digest = hashlib.sha256()
            size = 0
            with destination.open("rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
            return name, size, digest.hexdigest()
        except (requests.RequestException, OSError) as exc:
            log(f"انقطع التنزيل؛ محاولة {attempts}/5: {exc}")
            if attempts >= 5:
                raise RuntimeError("فشل التنزيل بعد 5 محاولات.") from exc
            time.sleep(min(2 ** attempts, 20))
        except RuntimeError:
            raise
    raise RuntimeError("فشل التنزيل.")


def safe_relative(raw: str) -> Path:
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ValueError(f"مسار غير آمن داخل الأرشيف: {raw}")
    if not path.parts or path.parts[0].endswith(":"):
        raise ValueError(f"مسار غير آمن داخل الأرشيف: {raw}")
    return Path(*path.parts)


def extract_archive(archive: Path, output: Path, original_name: str) -> bool:
    lower = original_name.lower()
    output.mkdir(parents=True, exist_ok=True)
    if lower.endswith(".zip"):
        total = 0
        count = 0
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = safe_relative(info.filename)
                # Reject symlinks and special files in ZIP metadata.
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) not in (0, 0o100000):
                    raise ValueError(f"نوع ملف غير مسموح داخل ZIP: {info.filename}")
                total += info.file_size
                count += 1
                if total > MAX_EXTRACTED_BYTES or count > MAX_EXTRACTED_FILES:
                    raise ValueError("الأرشيف يتجاوز حدود الحجم أو عدد الملفات المسموح.")
                target = output / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
        return True

    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        total = 0
        count = 0
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            for member in members:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"روابط رمزية أو أنواع TAR الخاصة غير مسموحة: {member.name}")
                rel = safe_relative(member.name)
                total += member.size
                count += 1
                if total > MAX_EXTRACTED_BYTES or count > MAX_EXTRACTED_FILES:
                    raise ValueError("الأرشيف يتجاوز حدود الحجم أو عدد الملفات المسموح.")
                src = tf.extractfile(member)
                if src is None:
                    raise ValueError(f"تعذر قراءة الملف داخل الأرشيف: {member.name}")
                target = output / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
        return True

    if lower.endswith((".7z", ".rar")):
        listing = subprocess.run(["7z", "l", "-slt", str(archive)], capture_output=True, text=True, timeout=120)
        if listing.returncode not in (0, 1):
            raise RuntimeError("تعذر قراءة فهرس أرشيف 7z/RAR.")
        entries = re.findall(r"(?m)^Path = (.+)$", listing.stdout)
        total = 0
        count = 0
        for entry in entries[1:]:
            rel = safe_relative(entry)
            if entry.endswith(("/", "\\")):
                continue
            count += 1
            if count > MAX_EXTRACTED_FILES:
                raise ValueError("الأرشيف يحتوي عدد ملفات يتجاوز الحد.")
        if count == 0:
            raise RuntimeError("الأرشيف فارغ أو غير مدعوم.")
        result = subprocess.run(["7z", "x", "-y", f"-o{output}", str(archive)], capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError("فشل فك ضغط 7z/RAR. قد يكون الأرشيف مشفرًا أو غير مدعوم.")
        files = [p for p in output.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total > MAX_EXTRACTED_BYTES or len(files) > MAX_EXTRACTED_FILES:
            raise ValueError("المحتويات المستخرجة تتجاوز حدود الحجم أو عدد الملفات.")
        for p in files:
            if not p.resolve().is_relative_to(output.resolve()):
                raise ValueError("رُصد مسار استخراج خارج المجلد المخصص.")
        return True

    if lower.endswith(".gz"):
        # A plain .gz is one compressed file, not necessarily a tar archive.
        out_name = original_name[:-3] or "extracted"
        target = output / safe_relative(out_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive, "rb") as src, target.open("wb") as dst:
            total = 0
            while True:
                block = src.read(CHUNK)
                if not block:
                    break
                total += len(block)
                if total > MAX_EXTRACTED_BYTES:
                    raise ValueError("الملف بعد فك الضغط يتجاوز الحد المسموح.")
                dst.write(block)
        return True
    return False


def drive_service():
    required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("يلزم إعداد أسرار GitHub التالية: " + ", ".join(missing))
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_children(service, parent_id: str, name: str, mime_type: str | None = None) -> list[dict]:
    query = f"'{parent_id}' in parents and name = '{escape_query(name)}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    out = []
    token = None
    while True:
        page = service.files().list(
            q=query, fields="nextPageToken,files(id,name,mimeType,size,md5Checksum)",
            pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        out.extend(page.get("files", []))
        token = page.get("nextPageToken")
        if not token:
            return out


def create_folder(service, parent_id: str, name: str) -> str:
    item = service.files().create(
        body={"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [parent_id]},
        fields="id,name",
    ).execute()
    log(f"أُنشئ مجلد Drive: {name}")
    return item["id"]


def get_or_create_folder(service, parent_id: str, name: str) -> str:
    found = list_children(service, parent_id, name, DRIVE_FOLDER_MIME)
    return found[0]["id"] if found else create_folder(service, parent_id, name)


def unique_name(service, parent_id: str, name: str, folder: bool = False) -> str:
    mime = DRIVE_FOLDER_MIME if folder else None
    if not list_children(service, parent_id, name, mime):
        return name
    path = Path(name)
    stem, suffix = (path.stem, path.suffix) if not folder else (name, "")
    for i in range(1, 10000):
        candidate = f"{stem} ({i}){suffix}"
        if not list_children(service, parent_id, candidate, mime):
            return candidate
    raise RuntimeError("تعذر اختيار اسم فريد في Drive.")


def upload_file(service, parent_id: str, path: Path) -> dict:
    name = unique_name(service, parent_id, path.name)
    media = MediaFileUpload(str(path), mimetype="application/octet-stream", chunksize=8 * 1024 * 1024, resumable=True)
    request = service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        fields="id,name,size,md5Checksum,mimeType",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            log(f"رفع {name}: {status.progress() * 100:.1f}%")
    if not response:
        raise RuntimeError(f"لم تُرجع Drive تأكيدًا لرفع {name}.")
    expected_md5 = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            expected_md5.update(block)
    if response.get("size") and int(response["size"]) != path.stat().st_size:
        raise RuntimeError(f"حجم الملف بعد الرفع لا يطابق المصدر: {name}")
    if response.get("md5Checksum") and response["md5Checksum"].lower() != expected_md5.hexdigest():
        raise RuntimeError(f"فشل التحقق من سلامة الملف بعد الرفع: {name}")
    log(f"اكتمل رفع الملف والتحقق منه: {name}")
    return response


def upload_tree(service, root_id: str, source_dir: Path) -> int:
    uploaded = 0
    dir_ids = {source_dir: root_id}
    dirs = sorted((p for p in source_dir.rglob("*") if p.is_dir()), key=lambda p: (len(p.relative_to(source_dir).parts), str(p)))
    for directory in dirs:
        parent = dir_ids[directory.parent]
        name = unique_name(service, parent, directory.name, folder=True)
        dir_ids[directory] = create_folder(service, parent, name)
    files = sorted((p for p in source_dir.rglob("*") if p.is_file()), key=lambda p: str(p))
    if not files:
        raise RuntimeError("الأرشيف لا يحتوي ملفات قابلة للرفع.")
    for path in files:
        upload_file(service, dir_ids[path.parent], path)
        uploaded += 1
    return uploaded


def main() -> int:
    url = os.environ.get("SOURCE_URL", "").strip()
    override = os.environ.get("OUTPUT_FILENAME", "").strip()
    if not url:
        raise RuntimeError("لم يُمرر رابط التنزيل.")
    with tempfile.TemporaryDirectory(prefix="github-download-") as temp:
        work = Path(temp)
        # First fetch to a generic name; use the final URL/content-disposition name after response.
        downloaded = work / "download.payload"
        log("الحالة: جارٍ تنزيل الملف على خادم GitHub Actions.")
        name, size, sha256 = stream_download(url, downloaded, override)
        log(f"اكتمل التنزيل: {name} ({size} bytes), SHA-256={sha256}")
        service = drive_service()
        drive_root = get_or_create_folder(service, "root", "Github download")
        lower = name.lower()
        is_archive = lower.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".gz", ".7z", ".rar"))
        if is_archive:
            log("الحالة: جارٍ فحص الأرشيف وفك الضغط بأمان.")
            extracted = work / "extracted"
            if not extract_archive(downloaded, extracted, name):
                raise RuntimeError("امتداد الأرشيف غير مدعوم.")
            archive_stem = name
            for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".zip", ".7z", ".rar", ".tar", ".gz"):
                if archive_stem.lower().endswith(suffix):
                    archive_stem = archive_stem[:-len(suffix)]
                    break
            folder_name = unique_name(service, drive_root, safe_filename(archive_stem or "Extracted files"), folder=True)
            target_id = create_folder(service, drive_root, folder_name)
            log("الحالة: جارٍ رفع الملفات المستخرجة إلى مجلدها.")
            count = upload_tree(service, target_id, extracted)
            log(f"نجاح: تم رفع {count} ملفًا مستخرجًا والتحقق منها. الأرشيف الأصلي لم يُحفظ في Drive.")
        else:
            log("الحالة: جارٍ رفع الملف إلى مجلد Github download.")
            upload_file(service, drive_root, downloaded)
            log("نجاح: اكتمل رفع الملف والتحقق من سلامته.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"::error::{type(exc).__name__}: {exc}", flush=True)
        raise
