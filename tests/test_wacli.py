import tempfile
import unittest
from pathlib import Path

from transcriber import Queue, Wacli
from test_transcriber import START


def wire(msg_id="NOTE1", **changes):
    return {
        "ChatJID": "123@s.whatsapp.net", "MsgID": msg_id, "ChatName": "Alice",
        "SenderJID": "123@s.whatsapp.net", "Timestamp": "2026-11-04T10:01:00Z",
        "FromMe": False, "MediaType": "audio",
    } | changes


class WacliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.queue = Queue(Path(self.folder.name) / "queue.sqlite", START)

    def tearDown(self):
        self.queue.close()
        self.folder.cleanup()

    def client(self, responses):
        def call(arguments):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return Wacli({}, call=call)

    def test_initial_scan_keeps_pairing_window_arrivals(self):
        client = self.client([
            {"store_id": "s", "next_checkpoint": "base"},
            {"messages": [wire(Timestamp=START), wire("OLD", Timestamp="2026-11-04T09:59:59Z")], "page": {"has_more": False}},
        ])
        client.collect(self.queue)
        self.assertEqual(self.queue.checkpoint, "base")
        self.assertEqual(len(self.queue.pending(0)), 1)

    def test_all_journal_pages_are_saved_before_resolving_media(self):
        self.queue.ingest([], "base")
        client = self.client([
            {"store_id": "s", "changes": [{"sequence": 1, "operation": "upsert", "chat_jid": "123@s.whatsapp.net", "msg_id": "A"}], "next_checkpoint": "p1", "has_more": True},
            {"store_id": "s", "changes": [{"sequence": 2, "operation": "upsert", "chat_jid": "123@s.whatsapp.net", "msg_id": "B"}], "next_checkpoint": "p2", "has_more": False},
        ])
        client.collect(self.queue)
        self.assertEqual(self.queue.checkpoint, "p2")
        self.assertEqual(len(self.queue.references(0)), 2)

    def test_expired_checkpoint_rescans_before_advancing(self):
        self.queue.ingest([], "expired")
        client = self.client([
            RuntimeError("expired checkpoint: old"),
            {"store_id": "s", "next_checkpoint": "fresh"},
            {"messages": [wire()], "page": {"has_more": False}},
        ])
        client.collect(self.queue)
        self.assertEqual(self.queue.checkpoint, "fresh")
        self.assertEqual(len(self.queue.pending(0)), 1)

    def test_invalidated_scan_does_not_skip_partial_page(self):
        client = self.client([
            {"store_id": "s", "next_checkpoint": "anchor"},
            {"messages": [wire("A")], "page": {"has_more": True, "next_cursor": "p2"}},
            RuntimeError("archive changed since cursor; restart the read"),
            {"messages": [wire("A"), wire("B")], "page": {"has_more": False}},
        ])
        client.collect(self.queue)
        self.assertEqual(len(self.queue.pending(0)), 2)
        self.assertEqual(self.queue.checkpoint, "anchor")

    def test_missing_message_does_not_block_later_ref(self):
        self.queue.record_changes({"store_id": "s", "next_checkpoint": "c", "changes": [
            {"sequence": 1, "operation": "upsert", "chat_jid": "123@s.whatsapp.net", "msg_id": "MISSING"},
            {"sequence": 2, "operation": "upsert", "chat_jid": "123@s.whatsapp.net", "msg_id": "NOTE1"},
        ]})
        client = self.client([RuntimeError('wacli: {"success":false,"data":null,"error":"sql: no rows in result set"}'), wire()])
        client.resolve(self.queue, 0)
        self.assertEqual(len(self.queue.pending(0)), 1)
        self.assertEqual(self.queue.references(10000), [])

    def test_delete_cancels_queued_audio(self):
        from test_transcriber import audio
        self.queue.ingest([audio()], "base")
        self.queue.record_changes({"store_id": "s", "next_checkpoint": "next", "changes": [
            {"sequence": 1, "operation": "delete", "chat_jid": "123@s.whatsapp.net", "msg_id": "NOTE1"}
        ]})
        self.client([]).resolve(self.queue, 0)
        self.assertEqual(self.queue.pending(1000), [])
