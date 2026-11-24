import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app_summaries import read_transcript_body
from app_telegram import DeliveryQueue, TelegramError, update_settings, validate_chat
from transcriber import Queue, normalize


class TelegramDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        self.database = self.data / "jobs.sqlite"
        Queue(self.database, "2000-01-01T00:00:00Z").close()
        self.sent = []
        self.token = "123456:example_token"

    def settings(self, chat_id="123456", **kwargs):
        return update_settings(self.data, {"token": self.token, "chat_id": chat_id}, validate=lambda token, _: int(token.split(":")[0]), **kwargs)

    def transcript(self, key, body, completed, message=None):
        path = self.data / f"{key}.txt"
        path.write_text(body, encoding="utf-8")
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO jobs(key,message,state,output,completed_at) VALUES (?,?,'complete',?,?)",
                       (key, json.dumps(message or {}), str(path), completed))
        return path

    def summary(self, key, path, bullets):
        _, digest, _ = read_transcript_body(path, self.data)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO summaries(job_key,source_hash,prompt_version,state,bullets,priority,queue_order,updated_at) "
                       "VALUES (?,?,1,'ready',?,0,0,0)", (key, digest, json.dumps(bullets)))

    def send(self, token, chat_id, text, reply_to):
        self.sent.append((token, chat_id, text, reply_to))
        return len(self.sent)

    def delivery(self, send=None):
        queue = DeliveryQueue(self.data, self.data, send or self.send)
        self.addCleanup(queue.close)
        return queue

    def test_sends_full_before_late_summary_without_forwarding_history(self):
        old = self.transcript("old", "Historical text", 1)
        self.summary("old", old, ["Historical summary"])
        self.settings(now=lambda: 2)
        message = normalize({"ChatJID": "123@g.us", "ChatName": "Example group", "MsgID": "1",
                             "SenderJID": "123@lid", "SenderName": "Marie \n Dupont", "Timestamp": "2026-11-25T00:00:00Z"})
        path = self.transcript("new", "Chat: Example group\nSender: 123@lid\nReceived: today\nMessage: 1\n\nFull text", 3, message)
        queue = self.delivery()
        self.assertTrue(queue.send_next())
        self.assertFalse(queue.send_next())
        self.assertEqual(self.sent[0][2], "WhatsApp transcript\nFrom: Marie Dupont\n\nFull text")
        self.summary("new", path, ["Summary text"])
        self.assertTrue(self.delivery().send_next())
        self.assertFalse(queue.send_next())
        self.assertEqual(self.sent[1][2], "WhatsApp summary\nFrom: Marie Dupont\n\n• Summary text")
        self.assertEqual(self.sent[1][3], 1)

    def test_older_jobs_use_direct_chat_name_or_sender_identifier(self):
        self.settings(now=lambda: 2)
        personal = "12345@s.whatsapp.net"
        self.transcript("direct", "Direct text", 3, {"chat_jid": personal, "chat_name": "Marie", "sender": personal})
        self.transcript("group", "Group text", 4, {"chat_jid": "123@g.us", "chat_name": "Example group", "sender": "12345:2@s.whatsapp.net"})
        self.transcript("unnamed", "Unnamed direct text", 5, {"chat_jid": personal, "chat_name": personal})
        queue = self.delivery()
        self.assertTrue(queue.send_next())
        self.assertTrue(queue.send_next())
        self.assertTrue(queue.send_next())
        self.assertIn("\nFrom: Marie\n", self.sent[0][2])
        self.assertIn("\nFrom: +12345\n", self.sent[1][2])
        self.assertIn("\nFrom: +12345\n", self.sent[2][2])

    def test_failed_chunk_restarts_at_first_unsent_chunk_using_saved_text(self):
        self.settings(now=lambda: 2)
        original = "🙂 " * 2300 + "ending"
        original_path = self.transcript("long", original, 3)
        queue = self.delivery()
        self.assertTrue(queue.send_next())
        original_path.write_text("Replacement text", encoding="utf-8")

        def unavailable(*_):
            raise TelegramError("Telegram is temporarily unavailable.")

        with self.assertRaises(TelegramError):
            self.delivery(unavailable).send_next()
        resumed = self.delivery()
        while resumed.send_next():
            pass
        self.assertGreater(len(self.sent), 1)
        self.assertEqual("".join(text.partition("\n\n")[2] for _, _, text, _ in self.sent), original)
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= 4096 for _, _, text, _ in self.sent))

    def test_destination_changes_do_not_forward_previous_destination_backlog(self):
        self.settings(now=lambda: 2)
        self.transcript("waiting", "Old destination only", 3)
        self.settings(now=lambda: 4)
        self.assertTrue(self.delivery().send_next())
        self.summary("waiting", self.data / "waiting.txt", ["Old destination summary"])
        self.settings(chat_id="-10098765", now=lambda: 5)
        self.assertFalse(self.delivery().send_next())
        self.transcript("later", "New destination only", 6)
        self.assertTrue(self.delivery().send_next())
        self.assertEqual([chat_id for _, chat_id, _, _ in self.sent], ["123456", "-10098765"])

    def test_token_rotation_keeps_backlog_but_switching_bots_drops_it(self):
        self.settings(now=lambda: 2)
        self.transcript("waiting", "Keep after token rotation", 3)
        self.token = "123456:rotated_token"
        self.settings(now=lambda: 4)
        self.assertTrue(self.delivery().send_next())
        self.transcript("old-bot", "Send only through the old bot", 5)
        self.token = "7890:another_bot"
        self.settings(now=lambda: 6)
        self.assertFalse(self.delivery().send_next())
        self.assertEqual(self.sent[0][0], "123456:rotated_token")

    def test_unavailable_original_does_not_block_later_notes_and_retries(self):
        self.settings(now=lambda: 2)
        missing = self.transcript("missing", "Retry this", 3)
        missing.unlink()
        self.transcript("ready", "Send this while the original is unavailable", 4)
        queue = self.delivery()
        with self.assertRaisesRegex(TelegramError, "could not be read"):
            queue.send_next(now=lambda: 10)
        self.assertTrue(queue.send_next(now=lambda: 11))
        self.assertFalse(queue.send_next(now=lambda: 12))
        missing.write_text("Recovered transcript", encoding="utf-8")
        self.assertTrue(queue.send_next(now=lambda: 360))
        self.assertIn("Recovered transcript", self.sent[1][2])

    def test_rejects_channel_configuration_when_bot_lacks_post_permission(self):
        replies = {"getMe": {"id": 123456}, "getChat": {"id": -100123456, "type": "channel"},
                   "getChatMember": {"status": "administrator", "can_post_messages": False}}

        def request(_, method, __):
            return replies[method]

        with self.assertRaisesRegex(TelegramError, "permission to post"):
            validate_chat(self.token, "-100123456", request=request)
        replies["getChatMember"]["can_post_messages"] = True
        self.assertEqual(validate_chat(self.token, "-100123456", request=request), 123456)


if __name__ == "__main__":
    unittest.main()
