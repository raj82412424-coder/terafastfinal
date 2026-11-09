import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="terabox-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["BOT_DB_PATH"] = os.path.join(_TMP, "test.sqlite3")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test-hash")
os.environ.setdefault("BOT_TOKEN", "test-token")

import db  # noqa: E402


class DatabaseTest(unittest.TestCase):
    def test_target_lifecycle(self):
        target_id = db.add_target(
            101, -1001234567890, "SSC CGL Group", "ssc_cgl", "supergroup", 42966,
            "https://t.me/ssc_cgl/42966",
        )
        target = db.get_target(target_id)
        self.assertIsNotNone(target)
        self.assertEqual(target["chat_id"], -1001234567890)
        self.assertEqual(target["start_message_id"], 42966)
        self.assertEqual(target["last_seen_id"], 42965)
        self.assertEqual(target["auto_mode"], 0)

        db.set_auto_mode(target_id, True)
        self.assertEqual(db.get_target(target_id)["auto_mode"], 1)

        db.set_last_seen(target_id, 43000)
        self.assertEqual(db.get_target(target_id)["last_seen_id"], 43000)

        db.set_target_total(target_id, 500)
        db.bump_target(target_id, uploaded=2, failed=1)
        target = db.get_target(target_id)
        self.assertEqual(target["total_messages"], 500)
        self.assertEqual(target["uploaded_count"], 2)
        self.assertEqual(target["failed_count"], 1)

        self.assertEqual(len(db.list_targets(101)), 1)
        self.assertTrue(db.delete_target(target_id))
        self.assertIsNone(db.get_target(target_id))

    def test_duplicate_upload_is_rejected(self):
        target_id = db.add_target(
            102, -1009999999999, "Test Channel", None, "channel", 10, None
        )
        first = db.save_upload(
            102, target_id, -1009999999999, 10, "a.mp4", 100,
            "https://1024terabox.com/s/abc", "https://t.me/c/9999999999/10",
        )
        second = db.save_upload(
            102, target_id, -1009999999999, 10, "a.mp4", 100,
            "https://1024terabox.com/s/xyz", "https://t.me/c/9999999999/10",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            db.upload_exists(102, -1009999999999, 10),
            "https://1024terabox.com/s/abc",
        )
        self.assertEqual(db.count_uploads(target_id), 1)
        rows = db.list_uploads(target_id, 0, 10)
        self.assertEqual(rows[0]["file_name"], "a.mp4")

    def test_batch_job_cursor(self):
        target_id = db.add_target(
            103, -1001111111111, "Batch Channel", None, "channel", 100, None
        )
        job_id = db.create_batch_job(103, target_id, 100, 199, 100)
        job = db.get_batch_job(job_id)
        self.assertEqual(job["cursor_id"], 99)
        self.assertEqual(job["status"], "running")

        db.update_batch_job(job_id, cursor_id=150, processed=51, uploaded=50)
        job = db.get_batch_job(job_id)
        self.assertEqual(job["cursor_id"], 150)
        self.assertEqual(job["uploaded"], 50)

        db.update_batch_job(job_id, status="paused")
        resumable = [j for j in db.list_resumable_jobs() if j["id"] == job_id]
        self.assertEqual(len(resumable), 1)

    def test_session_roundtrip(self):
        db.save_session(104, "session-string-value", "+919876543210")
        self.assertEqual(db.get_session(104), "session-string-value")
        self.assertEqual(db.get_session_phone(104), "+919876543210")
        self.assertTrue(db.delete_session(104))
        self.assertIsNone(db.get_session(104))

    def test_token_roundtrip(self):
        db.save_token(105, "ndus=abc; browserid=xyz")
        self.assertEqual(db.get_token(105), "ndus=abc; browserid=xyz")
        self.assertTrue(db.delete_token(105))
        self.assertIsNone(db.get_token(105))


if __name__ == "__main__":
    unittest.main()
