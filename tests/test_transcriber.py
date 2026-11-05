import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from transcriber import Queue, atomic_text, output_path


START = "2026-11-04T10:00:00Z"


def audio(**changes):
    message = {
        "chat_jid": "123@s.whatsapp.net",
        "msg_id": "NOTE1",
        "chat_name": "Alice",
        "sender": "123@s.whatsapp.net",
        "timestamp": "2026-11-04T10:01:00Z",
        "from_me": False,
        "media_type": "audio",
    }
    return message | changes


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.queue = Queue(self.root / "jobs.sqlite", START)

    def tearDown(self):
        self.queue.close()
        self.folder.cleanup()

    def test_only_new_incoming_audio_is_scheduled(self):
        self.queue.ingest([
            audio(),
            audio(msg_id="OUT", from_me=True),
            audio(msg_id="PHOTO", media_type="image"),
            audio(msg_id="OLD", timestamp="2026-11-04T09:59:59Z"),
        ], "cursor1")
        jobs = self.queue.pending(0)
        self.assertEqual([job["message"]["msg_id"] for job in jobs], ["NOTE1"])
        self.assertEqual(self.queue.checkpoint, "cursor1")

    def test_replay_after_restart_does_not_repeat_completed_job(self):
        self.queue.ingest([audio()], "cursor1")
        job = self.queue.pending(0)[0]
        self.queue.complete(job["key"], str(self.root / "note.txt"))
        self.queue.close()
        self.queue = Queue(self.root / "jobs.sqlite", "2027-01-01T00:00:00Z")
        self.queue.ingest([audio()], "cursor2")
        self.assertEqual(self.queue.pending(10000), [])
        self.assertEqual(self.queue.start_time, START)

    def test_checkpoint_and_jobs_roll_back_together(self):
        self.queue.ingest([], "old")
        self.queue.connection.execute(
            "CREATE TRIGGER reject_jobs BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.queue.ingest([audio()], "new")
        self.assertEqual(self.queue.checkpoint, "old")
        self.assertEqual(self.queue.pending(10000), [])

    def test_failed_audio_survives_restart_and_waits_for_backoff(self):
        self.queue.ingest([audio()], "cursor1")
        key = self.queue.pending(0)[0]["key"]
        self.queue.fail(key, "network offline", 100)
        self.assertEqual(self.queue.pending(100), [])
        self.queue.close()
        self.queue = Queue(self.root / "jobs.sqlite", START)
        retried = self.queue.pending(4000)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["attempts"], 1)

    def test_duplicate_message_id_in_different_chats_is_not_lost(self):
        self.queue.ingest([audio(), audio(chat_jid="456@s.whatsapp.net")], "c")
        self.assertEqual(len(self.queue.pending(0)), 2)

    def test_missing_message_identity_does_not_advance_checkpoint(self):
        self.queue.ingest([], "old")
        with self.assertRaises(ValueError):
            self.queue.ingest([audio(msg_id="")], "new")
        self.assertEqual(self.queue.checkpoint, "old")

    def test_unresolved_refs_and_checkpoint_survive_restart(self):
        self.queue.record_changes({"store_id": "s", "next_checkpoint": "c", "changes": [
            {"sequence": 1, "operation": "upsert", "chat_jid": "123@s.whatsapp.net", "msg_id": "NOTE1"}
        ]})
        self.queue.close()
        self.queue = Queue(self.root / "jobs.sqlite", START)
        self.assertEqual(self.queue.checkpoint, "c")
        self.assertEqual(self.queue.references(0)[0]["change"]["msg_id"], "NOTE1")

    def test_identity_move_preserves_completed_state(self):
        self.queue.ingest([audio()], "c")
        key = self.queue.pending(0)[0]["key"]
        self.queue.complete(key, "/tmp/original.txt")
        self.queue.move("123@s.whatsapp.net", "NOTE1", "999@lid", "NOTE1")
        self.queue.ingest([audio(chat_jid="999@lid")], "d")
        self.assertEqual(self.queue.pending(9999), [])
        row = self.queue.connection.execute("SELECT output FROM jobs").fetchall()
        self.assertEqual(row, [("/tmp/original.txt",)])

    def test_deleted_message_never_enters_queue(self):
        self.queue.ingest([audio(deleted=True)], "c")
        self.assertEqual(self.queue.pending(0), [])

    def test_identity_merge_discards_stale_worker_publication(self):
        self.queue.ingest([audio(), audio(chat_jid="999@lid")], "c")
        jobs = self.queue.pending(0)
        stale = jobs[0]
        self.queue.complete(jobs[1]["key"], "/tmp/already-published.txt")
        self.queue.move("123@s.whatsapp.net", "NOTE1", "999@lid", "NOTE1")
        destination = self.root / "duplicate.txt"
        self.assertFalse(self.queue.publish(stale["key"], destination, "duplicate"))
        self.assertFalse(destination.exists())
        self.assertEqual(self.queue.connection.execute("SELECT count(*) FROM jobs WHERE state='complete'").fetchone()[0], 1)

    def test_cancelled_job_cannot_publish_from_stale_snapshot(self):
        self.queue.ingest([audio()], "c")
        stale = self.queue.pending(0)[0]
        self.queue.cancel("123@s.whatsapp.net", "NOTE1")
        destination = self.root / "deleted.txt"
        self.assertFalse(self.queue.publish(stale["key"], destination, "deleted audio"))
        self.assertFalse(destination.exists())


class OutputTests(unittest.TestCase):
    def test_untrusted_labels_cannot_escape_transcript_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = output_path(root, audio(chat_name="../../outside/日本語"))
            self.assertTrue(path.is_relative_to(root))
            self.assertNotIn("..", path.relative_to(root).parts)
            self.assertEqual(path.suffix, ".txt")

    def test_renamed_chat_still_uses_same_output_location(self):
        root = Path("/tmp/transcripts")
        self.assertEqual(output_path(root, audio()), output_path(root, audio(chat_name="New name")))

    def test_atomic_retry_replaces_incomplete_transcript_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat" / "note.txt"
            atomic_text(path, "Bonjour, comment vas-tu ?\n")
            atomic_text(path, "Bonjour, comment vas-tu ?\n")
            self.assertEqual(path.read_text(), "Bonjour, comment vas-tu ?\n")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
