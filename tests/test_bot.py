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

    def test_direct_file_url_detection_uses_path_not_query(self):
        self.assertTrue(bot.is_likely_direct_file_url("https://example.com/archive.zip?token=abc"))
        self.assertFalse(bot.is_likely_direct_file_url("https://example.com/watch?id=archive.zip"))


if __name__ == "__main__":
    unittest.main()
