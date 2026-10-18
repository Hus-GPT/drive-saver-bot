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
    def __init__(self, file_id, size):
        self.file_id = file_id
        self.size = size
        self.called = False

    def next_chunk(self, num_retries=0):
        if not self.called:
            self.called = True
            return FakeStatus(), {
                "id": self.file_id,
                "name": "sample.bin",
                "webViewLink": "https://drive.google.com/file/d/" + self.file_id,
                "size": str(self.size),
            }
        raise AssertionError("next_chunk should finish after the first response")


class FakeFiles:
    def __init__(self, size):
        self.size = size
        self.request = None

    def create(self, body, media_body, fields):
        self.request = FakeRequest("drive-id-1", self.size)
        return self.request

    def get(self, fileId, fields):
        return FakeGetRequest(fileId, self.size)


class FakeGetRequest:
    def __init__(self, file_id, size):
        self.file_id = file_id
        self.size = size

    def execute(self, num_retries=0):
        return {
            "id": self.file_id,
            "name": "sample.bin",
            "webViewLink": "https://drive.google.com/file/d/" + self.file_id,
            "size": str(self.size),
        }


class FakeService:
    def __init__(self, size):
        self.fake_files = FakeFiles(size)

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
                bot, "MediaFileUpload", return_value=object()
            ) as media_factory:
                result = bot.upload_to_drive(handle.name, lambda done, total: progress.append((done, total)))
            self.assertEqual(result["id"], "drive-id-1")
            self.assertEqual(result["size"], 10)
            self.assertEqual(progress, [(10, 10)])
            self.assertEqual(media_factory.call_args.kwargs["chunksize"], 8 * 1024 * 1024)
            self.assertTrue(media_factory.call_args.kwargs["resumable"])

    def test_upload_fails_if_drive_size_does_not_match(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"0123456789")
            handle.flush()
            service = FakeService(9)
            with patch.object(bot, "drive_service", service), patch.object(
                bot, "MediaFileUpload", return_value=object()
            ):
                with self.assertRaisesRegex(RuntimeError, "فشل التحقق"):
                    bot.upload_to_drive(handle.name)

    def test_direct_file_url_detection_uses_path_not_query(self):
        self.assertTrue(bot.is_likely_direct_file_url("https://example.com/archive.zip?token=abc"))
        self.assertFalse(bot.is_likely_direct_file_url("https://example.com/watch?id=archive.zip"))


if __name__ == "__main__":
    unittest.main()
