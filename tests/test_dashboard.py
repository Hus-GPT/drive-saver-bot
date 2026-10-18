"""Unit tests for the Telegram task dashboard formatting helpers."""
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:unit-test-token")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DRIVE_FOLDER_ID", "test-drive-folder")

import bot


class DashboardFormattingTests(unittest.TestCase):
    def setUp(self):
        self.active = bot.active_tasks.copy()
        self.queued = bot.queued_tasks.copy()
        self.pending = bot.pending_downloads.copy()
        self.failed = bot.failed_tasks.copy()
        bot.active_tasks.clear()
        bot.queued_tasks.clear()
        bot.pending_downloads.clear()
        bot.failed_tasks.clear()

    def tearDown(self):
        bot.active_tasks.clear()
        bot.active_tasks.update(self.active)
        bot.queued_tasks.clear()
        bot.queued_tasks.update(self.queued)
        bot.pending_downloads.clear()
        bot.pending_downloads.update(self.pending)
        bot.failed_tasks.clear()
        bot.failed_tasks.update(self.failed)

    def test_progress_bar_clamps_to_valid_width(self):
        self.assertEqual(bot.progress_bar(0, 100, width=10), "░" * 10)
        self.assertEqual(bot.progress_bar(50, 100, width=10), "█" * 5 + "░" * 5)
        self.assertEqual(bot.progress_bar(150, 100, width=10), "█" * 10)
        self.assertEqual(bot.progress_bar(1, 0, width=4), "░" * 4)

    def test_task_progress_includes_percentage_speed_and_eta(self):
        line = bot.task_progress_line({
            "status": "تنزيل",
            "download_bytes": 5 * 1024 * 1024,
            "download_total": 10 * 1024 * 1024,
            "download_speed": 1024 * 1024,
        })
        self.assertIn("50%", line)
        self.assertIn("1.0 MB/ث", line)
        self.assertIn("المتبقي: 5 ث", line)

    def test_dashboard_does_not_display_signed_query_parameters(self):
        bot.active_tasks["a1b2c3d4"] = {
            "id": "a1b2c3d4",
            "status": "تنزيل",
            "url": "https://example.com/archive.zip?token=secret-value",
            "download_bytes": 1,
            "download_total": 10,
        }
        snapshot = bot.dashboard_snapshot()
        self.assertIn("archive.zip", snapshot)
        self.assertNotIn("secret-value", snapshot)
        self.assertNotIn("token=", snapshot)


if __name__ == "__main__":
    unittest.main()
