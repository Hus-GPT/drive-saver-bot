import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DRIVE_FOLDER_ID", "folder-test")
os.environ.pop("COLAB_AUTH", None)

import bot


class FakeStatus:
    def progress(self):
        return 1.0


class FakeRequest:
    def __init__(self, file_id, size, fingerprint="abc"):
        self.file_id = file_id
        self.size = size
        self.fingerprint = fingerprint
        self.called = False

    def next_chunk(self, num_retries=0):
        if not self.called:
            self.called = True
            return FakeStatus(), {
                "id": self.file_id,
                "name": "sample.bin",
                "webViewLink": "https://drive.google.com/file/d/" + self.file_id,
                "size": str(self.size),
                "appProperties": {"sha256": self.fingerprint},
            }
        raise AssertionError("next_chunk should finish after the first response")


class FakeFiles:
    def __init__(self, size, existing=None):
        self.size = size
        self.existing = existing or []
        self.request = None

    def list(self, q, pageSize, fields):
        self.last_query = q
        return FakeListRequest(self.existing)

    def create(self, body, media_body, fields):
        fingerprint = body.get("appProperties", {}).get("sha256", "abc")
        self.request = FakeRequest("drive-id-1", self.size, fingerprint)
        return self.request

    def get(self, fileId, fields):
        return FakeGetRequest(fileId, self.size, "abc")


class FakeListRequest:
    def __init__(self, files):
        self.files = files

    def execute(self, num_retries=0):
        return {"files": self.files}


class FakeGetRequest:
    def __init__(self, file_id, size, fingerprint="abc"):
        self.file_id = file_id
        self.size = size
        self.fingerprint = fingerprint

    def execute(self, num_retries=0):
        return {
            "id": self.file_id,
            "name": "sample.bin",
            "webViewLink": "https://drive.google.com/file/d/" + self.file_id,
            "size": str(self.size),
            "appProperties": {"sha256": self.fingerprint},
        }


class FakeService:
    def __init__(self, size, existing=None):
        self.fake_files = FakeFiles(size, existing)

    def files(self):
        return self.fake_files


class TransferTests(unittest.TestCase):
    def test_upload_uses_resumable_chunks_and_verifies_size(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"0123456789")
            handle.flush()
            service = FakeService(10)
            progress = []
            with patch.object(bot, "drive_service", service), patch.object(
                bot, "sha256_file", return_value="abc"
            ), patch.object(bot, "MediaFileUpload", return_value=object()) as media_factory:
                result = bot.upload_to_drive(handle.name, lambda done, total: progress.append((done, total)))
            self.assertEqual(result["id"], "drive-id-1")
            self.assertEqual(result["size"], 10)
            self.assertEqual(progress, [(10, 10)])
            self.assertEqual(media_factory.call_args.kwargs["chunksize"], 32 * 1024 * 1024)
            self.assertTrue(media_factory.call_args.kwargs["resumable"])

    def test_existing_identical_file_is_not_uploaded_twice(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"0123456789")
            handle.flush()
            existing = [{
                "id": "existing-id",
                "name": "sample.bin",
                "size": "10",
                "webViewLink": "https://drive.google.com/file/d/existing-id",
            }]
            service = FakeService(10, existing)
            with patch.object(bot, "drive_service", service), patch.object(
                bot, "sha256_file", return_value="abc"
            ), patch.object(bot, "MediaFileUpload", return_value=object()) as media_factory:
                result = bot.upload_to_drive(handle.name)
            self.assertTrue(result["already_exists"])
            self.assertEqual(result["id"], "existing-id")
            media_factory.assert_not_called()

    def test_duplicate_query_is_global_not_limited_to_destination_folder(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"0123456789")
            handle.flush()
            service = FakeService(10, [{
                "id": "nested-existing-id",
                "name": "sample.bin",
                "size": "10",
                "webViewLink": "https://drive.google.com/file/d/nested-existing-id",
            }])
            with patch.object(bot, "drive_service", service), patch.object(
                bot, "sha256_file", return_value="abc"
            ), patch.object(bot, "MediaFileUpload", return_value=object()) as media_factory:
                result = bot.upload_to_drive(handle.name)
            self.assertTrue(result["already_exists"])
            self.assertNotIn("in parents", service.fake_files.last_query)
            self.assertIn("appProperties", service.fake_files.last_query)
            media_factory.assert_not_called()

    def test_upload_fails_if_drive_size_does_not_match(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"0123456789")
            handle.flush()
            service = FakeService(9)
            with patch.object(bot, "drive_service", service), patch.object(
                bot, "sha256_file", return_value="abc"
            ), patch.object(bot, "MediaFileUpload", return_value=object()):
                with self.assertRaisesRegex(RuntimeError, "فشل التحقق"):
                    bot.upload_to_drive(handle.name)

    def test_index_existing_drive_files_hashes_nested_files_and_skips_native_docs(self):
        contents = {"root-bin": b"root bytes", "nested-bin": b"nested bytes"}

        class FakeDownloader:
            def __init__(self, sink, request, chunksize):
                self.sink = sink
                self.request = request
                self.done = False

            def next_chunk(self, num_retries=0):
                if not self.done:
                    self.sink.write(contents[self.request])
                    self.done = True
                return FakeStatus(), self.done

        class UpdateRequest:
            def __init__(self, owner, file_id, body):
                self.owner = owner
                self.file_id = file_id
                self.body = body

            def execute(self, num_retries=0):
                self.owner.updates[self.file_id] = self.body
                return {"id": self.file_id, **self.body}

        class IndexFiles:
            def __init__(self):
                self.updates = {}
                self.by_parent = {
                    "root-folder": [
                        {"id": "subfolder", "name": "nested", "mimeType": "application/vnd.google-apps.folder"},
                        {"id": "root-bin", "name": "root.bin", "mimeType": "application/octet-stream", "appProperties": {}},
                        {"id": "native-doc", "name": "notes", "mimeType": "application/vnd.google-apps.document", "appProperties": {}},
                        {"id": "already", "name": "indexed.bin", "mimeType": "application/octet-stream", "appProperties": {"sha256": "known"}},
                    ],
                    "subfolder": [
                        {"id": "nested-bin", "name": "nested.bin", "mimeType": "application/octet-stream", "appProperties": {}},
                    ],
                }

            def list(self, q, pageSize, fields, **kwargs):
                parent = __import__("re").search(r"'([^']+)' in parents", q).group(1)
                return FakeListRequest(self.by_parent.get(parent, []))

            def get_media(self, fileId):
                return fileId

            def update(self, fileId, body, fields):
                return UpdateRequest(self, fileId, body)

        class IndexService:
            def __init__(self):
                self.resource = IndexFiles()

            def files(self):
                return self.resource

        service = IndexService()
        with patch.object(bot, "drive_service", service), patch.object(
            bot, "DRIVE_FOLDER_ID", "root-folder"
        ), patch.object(bot, "MediaIoBaseDownload", FakeDownloader):
            stats = bot.index_existing_drive_files()

        self.assertEqual(stats["seen"], 4)
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["already_indexed"], 1)
        self.assertEqual(stats["skipped_native"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(
            service.resource.updates["root-bin"]["appProperties"]["sha256"],
            __import__("hashlib").sha256(contents["root-bin"]).hexdigest(),
        )
        self.assertEqual(
            service.resource.updates["nested-bin"]["appProperties"]["sha256"],
            __import__("hashlib").sha256(contents["nested-bin"]).hexdigest(),
        )

    def test_archive_nested_folders_are_created_and_reused_in_drive(self):
        class FolderCreateRequest:
            def __init__(self, owner, body):
                self.owner = owner
                self.body = body

            def execute(self, num_retries=0):
                folder = {
                    "id": f"folder-{len(self.owner.folders) + 1}",
                    "name": self.body["name"],
                    "parent": self.body["parents"][0],
                }
                self.owner.folders.append(folder)
                return {"id": folder["id"], "name": folder["name"]}

        class FolderFiles:
            def __init__(self):
                self.folders = []

            def list(self, q, pageSize, fields):
                re = __import__("re")
                name_match = re.search(r"name = '([^']+)'", q)
                parent_match = re.search(r"'([^']+)' in parents", q)
                name = name_match.group(1) if name_match else None
                parent = parent_match.group(1) if parent_match else None
                matches = [
                    {"id": item["id"], "name": item["name"]}
                    for item in self.folders
                    if item["name"] == name and item["parent"] == parent
                ]
                return FakeListRequest(matches)

            def create(self, body, fields):
                return FolderCreateRequest(self, body)

        class FolderService:
            def __init__(self):
                self.resource = FolderFiles()

            def files(self):
                return self.resource

        service = FolderService()
        with patch.object(bot, "DRIVE_FOLDER_ID", "root-folder"):
            nested_id = bot.ensure_drive_folder_path("docs/images", service=service)
            self.assertEqual(nested_id, "folder-2")
            self.assertEqual(
                [(item["name"], item["parent"]) for item in service.resource.folders],
                [("docs", "root-folder"), ("images", "folder-1")],
            )
            self.assertEqual(
                bot.ensure_drive_folder_path("docs/images", service=service),
                "folder-2",
            )
            self.assertEqual(len(service.resource.folders), 2)

    def test_archive_member_parent_id_preserves_nested_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = os.path.join(tmp, "bundle.zip")
            extraction_root = os.path.join(tmp, "extracted_bundle")
            nested_file = os.path.join(extraction_root, "docs", "images", "photo.jpg")
            os.makedirs(os.path.dirname(nested_file))
            with open(nested_file, "wb") as handle:
                handle.write(b"image")
            relative_parent = bot.archive_member_parent_id(nested_file, archive_path, tmp)
            self.assertEqual(str(relative_parent), os.path.join("docs", "images"))

    def test_zip_archive_extracts_files_and_does_not_return_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = os.path.join(tmp, "bundle.zip")
            with __import__("zipfile").ZipFile(archive_path, "w") as archive:
                archive.writestr("docs/readme.txt", "hello")
                archive.writestr("images/picture.txt", "image")
            extracted = bot.extract_archive_contents(archive_path, tmp)
            self.assertEqual(len(extracted), 2)
            self.assertTrue(all(os.path.isfile(path) for path in extracted))
            self.assertNotIn(archive_path, extracted)
            self.assertEqual(open(extracted[0], encoding="utf-8").read(), "hello")

    def test_7z_archive_extracts_nested_files_without_returning_archive(self):
        import py7zr

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = os.path.join(tmp, "source")
            os.makedirs(os.path.join(source_dir, "docs"))
            source_file = os.path.join(source_dir, "docs", "readme.txt")
            with open(source_file, "w", encoding="utf-8") as handle:
                handle.write("hello from 7z")
            archive_path = os.path.join(tmp, "bundle.7z")
            with py7zr.SevenZipFile(archive_path, mode="w") as archive:
                archive.write(source_file, arcname="docs/readme.txt")

            extracted = bot.extract_archive_contents(archive_path, tmp)
            self.assertEqual(len(extracted), 1)
            self.assertNotIn(archive_path, extracted)
            self.assertTrue(os.path.isfile(extracted[0]))
            with open(extracted[0], encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "hello from 7z")
            self.assertEqual(
                os.path.relpath(extracted[0], os.path.join(tmp, "extracted_bundle")),
                os.path.join("docs", "readme.txt"),
            )

    def test_social_video_url_is_sent_to_ytdlp_even_if_path_looks_like_media(self):
        class FakeYDL:
            def __init__(self, options):
                self.options = options
                self.requested_url = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.requested_url = url
                return {
                    "title": "Test post",
                    "formats": [{"vcodec": "h264"}],
                    "extractor_key": "Instagram",
                }

        url = "https://www.instagram.com/p/example.mp4"
        fake = FakeYDL({})
        with patch.object(bot.yt_dlp, "YoutubeDL", return_value=fake) as factory:
            info = bot.fetch_url_info(url)
        factory.assert_called_once()
        self.assertTrue(info["is_video"])
        self.assertEqual(fake.requested_url, url)

    def test_zip_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = os.path.join(tmp, "unsafe.zip")
            with __import__("zipfile").ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")
            with self.assertRaisesRegex(RuntimeError, "غير آمن"):
                bot.extract_archive_contents(archive_path, tmp)

    def test_progress_format_shows_percent_and_speed(self):
        text = bot.format_task_progress({
            "id": "abc123", "name": "video.mp4", "status": "تنزيل",
            "download_bytes": 50, "download_total": 100, "download_speed": 10,
        })
        self.assertIn("abc123", text)
        self.assertIn("50.0%", text)
        self.assertIn("السرعة", text)
        self.assertIn("الوقت المنقضي", text)

    def test_progress_dashboard_renders_active_tasks_and_refresh_interval(self):
        task = {
            "id": "live123", "user_id": 123456, "name": "large-file.bin",
            "status": "تنزيل", "started_at": __import__("time").time(),
            "download_bytes": 25, "download_total": 100, "download_speed": 5,
        }
        with patch.dict(bot.active_tasks, {"live123": task}, clear=True), patch.dict(
            bot.queued_tasks, {}, clear=True
        ):
            dashboard, keyboard = bot.render_progress_dashboard()
        self.assertIn("تحديث تلقائي", dashboard)
        self.assertIn("نشطة: 1", dashboard)
        self.assertIn("live123", dashboard)
        self.assertIsNotNone(keyboard)

    def test_progress_dashboard_handles_empty_queue(self):
        with patch.dict(bot.active_tasks, {}, clear=True), patch.dict(
            bot.queued_tasks, {}, clear=True
        ):
            dashboard, keyboard = bot.render_progress_dashboard()
        self.assertIn("لا توجد مهام نشطة", dashboard)
        self.assertIsNone(keyboard)

    def test_duplicate_url_is_detected_in_active_and_queued_tasks(self):
        task = {"id": "task123", "user_id": 123456, "url": "https://example.com/file.zip"}
        with patch.dict(bot.active_tasks, {"task123": task}, clear=True), patch.dict(
            bot.queued_tasks, {}, clear=True
        ), patch.dict(bot.pending_downloads, {}, clear=True):
            duplicate = bot.find_duplicate_url_task("https://example.com/file.zip")
        self.assertEqual(duplicate["task_id"], "task123")
        self.assertEqual(duplicate["kind"], "transfer")

    def test_duplicate_url_waiting_for_quality_is_detected(self):
        pending = {"url": "https://example.com/watch", "user_id": 123456}
        with patch.dict(bot.active_tasks, {}, clear=True), patch.dict(
            bot.queued_tasks, {}, clear=True
        ), patch.dict(bot.pending_downloads, {"short1": pending}, clear=True):
            duplicate = bot.find_duplicate_url_task("https://example.com/watch")
        self.assertEqual(duplicate["task_id"], "short1")
        self.assertEqual(duplicate["kind"], "quality")

    def test_duplicate_telegram_file_is_detected(self):
        task = {"id": "tg123", "user_id": 123456, "file_unique_id": "unique-file"}
        with patch.dict(bot.active_tasks, {"tg123": task}, clear=True), patch.dict(
            bot.queued_tasks, {}, clear=True
        ):
            self.assertEqual(bot.find_duplicate_telegram_task("unique-file"), "tg123")
        self.assertIsNone(bot.find_duplicate_telegram_task(None))

    def test_retry_parent_remains_recoverable_until_child_is_journaled(self):
        parent = {
            "task_id": "parent1", "status": "retry_scheduled",
            "url": "https://example.com/file.bin",
        }
        self.assertEqual(
            [item["task_id"] for item in bot.get_recoverable_task_records({"parent1": parent})],
            ["parent1"],
        )

    def test_recovery_parent_is_not_replayed_after_child_is_journaled(self):
        parent = {
            "task_id": "parent1", "status": "recovery_scheduled",
            "url": "https://example.com/file.bin",
        }
        child = {
            "task_id": "child1", "status": "started",
            "url": "https://example.com/file.bin", "recovery_parent_id": "parent1",
        }
        recoverable = bot.get_recoverable_task_records({"parent1": parent, "child1": child})
        self.assertEqual([item["task_id"] for item in recoverable], ["child1"])

    def test_task_journal_redacts_signed_url_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = os.path.join(tmp, "tasks.jsonl")
            with patch.object(bot, "TASK_LOG_PATH", journal):
                bot.persist_task({
                    "task_id": "private-url",
                    "status": "started",
                    "url": "https://example.com/file.zip?X-Amz-Signature=secretvalue&download=1",
                })
            with open(journal, encoding="utf-8") as handle:
                record = __import__("json").loads(handle.readline())
            self.assertNotIn("url", record)
            with open(journal, encoding="utf-8") as handle:
                logged_text = handle.read()
            self.assertNotIn("secretvalue", logged_text)
            self.assertTrue(record["recovery_requires_manual_url"])
            self.assertEqual(record["url_redacted"], "https://example.com/file.zip")

    def test_parallel_range_download_assembles_exact_file(self):
        data = b"abcdefghijklmnopqrstuvwxyz"

        class FakeResponse:
            def __init__(self, status_code, headers, content):
                self.status_code = status_code
                self.headers = headers
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def iter_content(self, chunk_size=1):
                for offset in range(0, len(self.content), max(1, chunk_size)):
                    yield self.content[offset:offset + chunk_size]

        def fake_get(url, stream, timeout, headers):
            value = headers.get("Range", "bytes=0-")
            start, end = value.removeprefix("bytes=").split("-", 1)
            start = int(start)
            end = int(end) if end else len(data) - 1
            if start < 0 or end >= len(data) or start > end:
                return FakeResponse(416, {"Content-Range": f"bytes */{len(data)}"}, b"")
            content = data[start:end + 1]
            return FakeResponse(
                206,
                {
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    "Content-Length": str(len(content)),
                },
                content,
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DIRECT_DOWNLOAD_WORKERS": "3",
                "DIRECT_DOWNLOAD_MIN_PARALLEL_BYTES": "1",
                "DIRECT_DOWNLOAD_CHUNK_BYTES": "10",
            },
        ), patch.object(bot.requests, "get", side_effect=fake_get):
            output = os.path.join(tmp, "sample.bin")
            progress = []
            result = bot.download_direct_parallel(
                "https://example.com/sample.bin", tmp, output,
                lambda current: progress.append(current["downloaded_bytes"]),
            )
            self.assertEqual(result, output)
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), data)
            self.assertEqual(progress[-1], len(data))
            self.assertTrue(all(value <= len(data) for value in progress))

    def test_parallel_download_falls_back_when_server_ignores_ranges(self):
        data = b"server-does-not-support-ranges"
        calls = []

        class FakeResponse:
            def __init__(self, status_code, headers, content):
                self.status_code = status_code
                self.headers = headers
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def iter_content(self, chunk_size=1):
                for offset in range(0, len(self.content), max(1, chunk_size)):
                    yield self.content[offset:offset + chunk_size]

        def fake_get(url, stream, timeout, headers):
            calls.append(headers.get("Range"))
            return FakeResponse(200, {"Content-Length": str(len(data))}, data)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DIRECT_DOWNLOAD_WORKERS": "3",
                "DIRECT_DOWNLOAD_MIN_PARALLEL_BYTES": "1",
                "DIRECT_DOWNLOAD_CHUNK_BYTES": "10",
            },
        ), patch.object(bot.requests, "get", side_effect=fake_get):
            output = bot.download_direct("https://example.com/sample.bin", tmp)
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), data)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], "bytes=0-0")
        self.assertIsNone(calls[1])

    def test_direct_file_url_detection_uses_path_not_query(self):
        self.assertTrue(bot.is_likely_direct_file_url("https://example.com/archive.zip?token=abc"))
        self.assertFalse(bot.is_likely_direct_file_url("https://example.com/watch?id=archive.zip"))



class UnifiedIntakeTests(unittest.TestCase):
    def test_extract_urls_returns_all_unique_urls_in_order(self):
        text = "First https://example.com/a, then https://drive.google.com/file/d/abc/view and https://example.com/a!"
        self.assertEqual(
            bot.extract_urls(text),
            ["https://example.com/a", "https://drive.google.com/file/d/abc/view"],
        )

    def test_extract_urls_uses_caption_text_too_at_handler_level(self):
        self.assertEqual(
            bot.extract_urls("Caption: https://example.com/file.pdf"),
            ["https://example.com/file.pdf"],
        )

    def test_google_drive_file_id_variants(self):
        cases = {
            "https://drive.google.com/file/d/FILE_123/view?usp=sharing": "FILE_123",
            "https://drive.google.com/drive/folders/FOLDER-123?usp=sharing": "FOLDER-123",
            "https://docs.google.com/spreadsheets/d/SHEET_123/edit": "SHEET_123",
            "https://drive.google.com/open?id=QUERY_123": "QUERY_123",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(bot.google_drive_file_id(url), expected)

    def test_google_drive_host_detection(self):
        self.assertTrue(bot.is_google_drive_url("https://drive.google.com/file/d/abc/view"))
        self.assertTrue(bot.is_google_drive_url("https://docs.google.com/document/d/abc/edit"))
        self.assertFalse(bot.is_google_drive_url("https://example.com/drive.google.com/file"))

    def test_ytdlp_cookie_file_is_optional_and_not_added_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = os.path.join(tmp, "cookies.txt")
            with patch.dict(os.environ, {"YTDLP_COOKIES_FILE": cookie_path}):
                self.assertNotIn("cookiefile", bot.ytdlp_opts())
                with open(cookie_path, "w", encoding="utf-8") as handle:
                    handle.write("# Netscape HTTP Cookie File\\n")
                self.assertEqual(bot.ytdlp_opts()["cookiefile"], cookie_path)

    def test_quality_keyboard_offers_best_available_and_8k_when_present(self):
        formats = [
            {"height": 4320, "vcodec": "av01", "acodec": "none"},
            {"height": 1080, "vcodec": "avc1", "acodec": "none"},
            {"height": 720, "vcodec": "none", "acodec": "mp4a"},
        ]
        class FakeButton:
            def __init__(self, text, callback_data):
                self.text, self.callback_data = text, callback_data
        class FakeMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard
        with patch.object(bot, "InlineKeyboardButton", FakeButton), patch.object(
            bot, "InlineKeyboardMarkup", FakeMarkup
        ):
            markup = bot.build_quality_keyboard("abc123", formats)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("dl|abc123|best", callbacks)
        self.assertIn("dl|abc123|4320", callbacks)
        self.assertIn("dl|abc123|1080", callbacks)
        self.assertIn("dl|abc123|720", callbacks)

    def test_best_quality_download_does_not_apply_height_cap(self):
        captured = {}
        class FakeYDL:
            def __init__(self, options):
                captured.update(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=True):
                return {"title": "sample", "ext": "mp4"}
            def prepare_filename(self, info):
                return "/tmp/sample.mp4"
        with patch.object(bot.yt_dlp, "YoutubeDL", FakeYDL):
            bot.download_video("https://example.com/video", "/tmp", "best")
        self.assertEqual(captured["format"], "bestvideo+bestaudio/best")

    def test_numeric_quality_download_keeps_requested_height_cap(self):
        captured = {}
        class FakeYDL:
            def __init__(self, options):
                captured.update(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=True):
                return {"title": "sample", "ext": "mp4"}
            def prepare_filename(self, info):
                return "/tmp/sample.mp4"
        with patch.object(bot.yt_dlp, "YoutubeDL", FakeYDL):
            bot.download_video("https://example.com/video", "/tmp", "720")
        self.assertEqual(captured["format"], "bestvideo[height<=720]+bestaudio/best[height<=720]/best")

    def test_youtube_bot_challenge_is_recognized(self):
        self.assertTrue(bot.is_youtube_bot_challenge(
            "Sign in to confirm you're not a bot. Use --cookies-from-browser"
        ))
        self.assertFalse(bot.is_youtube_bot_challenge("HTTP Error 404"))


if __name__ == "__main__":
    unittest.main()
