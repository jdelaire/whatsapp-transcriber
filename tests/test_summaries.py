import hashlib
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_summaries import SummaryStore, read_transcript_body, project_summary
from transcriber import Queue, identity
from test_transcriber import START, audio


HEADER = "Chat: Example\nSender: Example\nReceived: 2026-01-01T00:00:00Z\nMessage: sample\n\n"


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.output = self.root / "transcripts"
        self.output.mkdir()
        self.database = self.root / "jobs.sqlite"
        self.queue = Queue(self.database, START)
        self.store = SummaryStore(self.database, self.output)

    def tearDown(self):
        self.store.close()
        self.queue.close()
        self.folder.cleanup()

    def add_note(self, name="one", body="Bonjour.\n"):
        message = audio(msg_id=name, chat_jid="chat@g.us")
        self.queue.ingest([message], None)
        key = identity(message)
        path = self.output / f"{name}.txt"
        path.write_text(HEADER + body, encoding="utf-8")
        self.queue.complete(key, str(path))
        return key, path

    def test_migration_preserves_jobs_aliases_and_originals(self):
        key, path = self.add_note()
        before = path.read_bytes()
        self.queue.move("chat@g.us", "one", "new@g.us", "new")
        self.queue.close()
        self.queue = Queue(self.database, START)
        self.assertEqual(self.queue.connection.execute("SELECT state FROM jobs WHERE key=?", (key,)).fetchone(), ("complete",))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.queue.get("summary_schema_version"), "1")
        self.assertTrue(self.queue.connection.execute("SELECT completed_at FROM jobs WHERE key=?", (key,)).fetchone()[0])

    def test_full_body_preserves_whitespace_and_headerless_paragraphs(self):
        body = "  Bonjour 👋 e\u0301.\n\n" + "Fin de la note.\n" * 80000
        _, path = self.add_note(body=body)
        before = path.read_bytes()
        actual, digest, fingerprint = read_transcript_body(str(path), self.output)
        self.assertEqual(actual, body)
        self.assertEqual(digest, hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(fingerprint["size"], len(before))
        self.assertEqual(path.read_bytes(), before)
        path.write_text("Chat: handwritten\n\nKeep this paragraph.\n\nAnd this one.\n")
        self.assertEqual(read_transcript_body(str(path), self.output)[0], path.read_text())

    def test_replacement_and_symlink_escape_hide_cached_summary(self):
        key, path = self.add_note(body="Original.\n")
        _, digest, fingerprint = read_transcript_body(str(path), self.output)
        self.queue.connection.execute("INSERT INTO summaries(job_key,source_hash,file_fingerprint,prompt_version,state,bullets,priority,queue_order,updated_at) VALUES(?,?,?,1,'ready','[\"A summary\"]',1,1,0)", (key, digest, json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))))
        self.queue.connection.commit()
        self.assertEqual(project_summary(self.queue.connection, key, str(path), self.output)["bullets"], ["A summary"])
        old = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_text(HEADER + "Different\n")
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(path)
        self.assertEqual(project_summary(self.queue.connection, key, str(path), self.output)["bullets"], [])
        outside = self.root / "outside.txt"
        outside.write_text("Outside")
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(project_summary(self.queue.connection, key, str(path), self.output)["bullets"], [])
        with self.assertRaises(ValueError):
            read_transcript_body(str(path), self.output)

    def test_projection_never_hashes_or_writes(self):
        key, path = self.add_note()
        before = self.queue.connection.total_changes
        with patch("app_summaries.read_transcript_body", side_effect=AssertionError("unexpected body read")):
            result = project_summary(self.queue.connection, key, str(path), self.output)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.queue.connection.total_changes, before)

    def test_missing_original_returns_safe_detail(self):
        key, path = self.add_note()
        path.unlink()
        result = self.store.handle("transcript", {"id": key})
        self.assertIsNone(result["text"])
        self.assertEqual(result["summary"]["error_code"], "original_missing")

    def test_merge_discards_losing_summary_attempt(self):
        first, _ = self.add_note("one")
        second, _ = self.add_note("two")
        for number, key in enumerate((first, second)):
            self.queue.connection.execute("INSERT INTO summaries(job_key,prompt_version,state,priority,queue_order,updated_at) VALUES(?,1,'running',1,?,0)", (key, number))
        self.queue.connection.commit()
        self.queue.move("chat@g.us", "one", "chat@g.us", "two")
        winner = self.queue.target(first)
        keys = [row[0] for row in self.queue.connection.execute("SELECT job_key FROM summaries")]
        self.assertEqual(keys, [winner])

    def own(self, owner="worker"):
        self.store.handle("summary-owner", {"owner": owner})
        return owner

    def reconcile(self, owner="worker", hint=None):
        return self.store.handle("summary-reconcile", {"owner": owner, "id": hint})

    def claim(self, owner="worker"):
        return self.store.handle("summary-claim", {"owner": owner})["claim"]

    def finish(self, claim, **changes):
        request = {"identity": claim["identity"], "outcome": "ready", "bullets": ["Résumé."], "error_code": None}
        return self.store.handle("summary-finish", request | changes)

    def test_all_history_survives_paginated_restarts_and_wrap(self):
        keys = {self.add_note(str(number))[0] for number in range(61)}
        self.queue.connection.execute("UPDATE jobs SET completed_at=NULL")
        self.queue.connection.commit()
        self.own()
        for _ in range(8):
            result = self.reconcile()
            self.store.close()
            self.store = SummaryStore(self.database, self.output)
            if result["wrapped"]:
                break
        actual = {row[0] for row in self.queue.connection.execute("SELECT job_key FROM summaries")}
        self.assertEqual(actual, keys)
        pending_message = audio(chat_jid="chat@g.us", msg_id="late")
        self.queue.ingest([pending_message], None)
        late = identity(pending_message)
        path = self.output / "late.txt"
        path.write_text(HEADER + "Late completion.")
        self.queue.complete(late, str(path))
        self.reconcile()
        self.assertEqual(self.claim()["identity"]["id"], late)

    def test_reversed_hints_preserve_new_completion_order(self):
        historical, _ = self.add_note("history")
        first, _ = self.add_note("first")
        second, _ = self.add_note("second")
        self.queue.connection.execute("UPDATE jobs SET completed_at=NULL WHERE key=?", (historical,))
        self.queue.connection.execute("UPDATE jobs SET completed_at=1e12 WHERE key=?", (first,))
        self.queue.connection.execute("UPDATE jobs SET completed_at=1e12+1 WHERE key=?", (second,))
        self.queue.connection.commit()
        self.own()
        self.reconcile(hint=second)
        claims = []
        for _ in range(3):
            claim = self.claim()
            claims.append(claim["identity"]["id"])
            self.assertTrue(self.finish(claim)["accepted"])
        self.assertEqual(claims, [first, second, historical])

    def test_off_on_rejects_late_result_without_losing_cached_summary(self):
        first, _ = self.add_note("first")
        self.own()
        self.reconcile()
        claim = self.claim()
        settings = self.store.handle("summary-settings", {})
        off = self.store.handle("summary-settings", {"enabled": False})
        self.assertEqual(off["revision"], settings["revision"] + 1)
        self.assertIsNone(self.claim())
        self.store.handle("summary-settings", {"enabled": True})
        replacement = self.claim()
        self.assertFalse(self.finish(claim)["accepted"])
        self.assertTrue(self.finish(replacement)["accepted"])
        self.store.handle("summary-settings", {"enabled": False})
        self.assertEqual(self.store.handle("summary-status", {"id": first})["bullets"], ["Résumé."])

    def test_source_changes_and_owner_recovery_reject_stale_claims(self):
        key, path = self.add_note()
        self.own("old")
        self.reconcile("old")
        old = self.claim("old")
        self.own("new")
        self.assertFalse(self.finish(old)["accepted"])
        current = self.claim("new")
        path.write_text(HEADER + "Changed original.")
        self.assertFalse(self.finish(current)["accepted"])
        next_claim = self.claim("new")
        self.assertEqual(next_claim["text"], "Changed original.")
        self.assertNotEqual(next_claim["identity"]["source_hash"], current["identity"]["source_hash"])
        self.assertEqual(self.store.handle("summary-check", {"identity": old["identity"]}), {"valid": False})

    def test_failed_note_waits_for_manual_retry(self):
        key, _ = self.add_note()
        self.own()
        self.reconcile()
        claim = self.claim()
        self.assertTrue(self.finish(claim, outcome="failed", bullets=[], error_code="refused")["accepted"])
        self.reconcile()
        self.assertIsNone(self.claim())
        self.assertTrue(self.store.handle("summary-retry", {"id": key})["accepted"])
        self.assertIsNotNone(self.claim())

    def test_short_note_rejects_multiple_bullets(self):
        key, _ = self.add_note()
        self.own()
        self.reconcile()
        claim = self.claim()
        self.finish(claim, bullets=["One", "Two"])
        state = self.store.handle("summary-status", {"id": key})
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error_code"], "invalid_output")

    def test_claims_are_exclusive_and_interruptions_keep_work(self):
        self.add_note()
        self.own()
        self.reconcile()
        claim = self.claim()
        self.assertIsNone(self.claim())
        self.assertTrue(self.finish(claim, outcome="interrupted", bullets=[])["accepted"])
        self.assertIsNotNone(self.claim())

    def test_read_only_detail_sees_wal_without_mutating_database(self):
        key, _ = self.add_note()
        clone = self.root / "copy.sqlite"
        shutil.copy2(self.database, clone)
        wal = Path(str(self.database) + "-wal")
        shutil.copy2(wal, Path(str(clone) + "-wal"))
        before = [clone.read_bytes(), Path(str(clone) + "-wal").read_bytes()]
        reader = SummaryStore(clone, self.output)
        self.assertEqual(reader.handle("transcript", {"id": key})["text"], "Bonjour.\n")
        self.assertEqual([clone.read_bytes(), Path(str(clone) + "-wal").read_bytes()], before)

    def test_cli_consumes_json_from_stdin_and_emits_full_original(self):
        body = "Bonjour 👋\n" * 100000
        key, _ = self.add_note(body=body)
        (self.root / "app-config.json").write_text(json.dumps({"output": str(self.output)}))
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "app_engine.py"), "transcript", "--tools", str(self.root), "--data-root", str(self.root)], input=json.dumps({"id": key}), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["data"]["text"], body)

    def test_legacy_pending_completion_outranks_migrated_history(self):
        historical = audio(chat_jid="legacy@g.us", msg_id="history")
        pending = audio(chat_jid="legacy@g.us", msg_id="pending")
        path = self.output / "legacy.txt"
        path.write_text(HEADER + "A saved original.")
        legacy = self.root / "legacy.sqlite"
        with sqlite3.connect(legacy) as db:
            db.executescript("CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE jobs(key TEXT PRIMARY KEY,message TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,error TEXT,output TEXT); CREATE TABLE aliases(alias TEXT PRIMARY KEY,target TEXT NOT NULL);")
            db.execute("INSERT INTO metadata VALUES('start_time',?)", (START,))
            db.execute("INSERT INTO jobs(key,message,state,output) VALUES(?,?,'complete',?)", (identity(historical), json.dumps(historical), str(path)))
            db.execute("INSERT INTO jobs(key,message) VALUES(?,?)", (identity(pending), json.dumps(pending)))
            db.execute("INSERT INTO aliases VALUES('legacy-alias',?)", (identity(historical),))
        before = path.read_bytes()
        queue = Queue(legacy, START)
        store = SummaryStore(legacy, self.output)
        try:
            self.assertEqual(queue.target("legacy-alias"), identity(historical))
            store.handle("summary-owner", {"owner": "legacy-worker"})
            store.handle("summary-reconcile", {"owner": "legacy-worker"})
            new_path = self.output / "pending.txt"
            new_path.write_text(HEADER + "New completion.")
            queue.complete(identity(pending), str(new_path))
            claim = store.handle("summary-claim", {"owner": "legacy-worker"})["claim"]
            self.assertEqual(claim["identity"]["id"], identity(pending))
            self.assertEqual(path.read_bytes(), before)
        finally:
            store.close()
            queue.close()

    def test_prompt_change_invalidates_cached_result_and_queues_replacement(self):
        key, _ = self.add_note()
        self.own()
        self.reconcile()
        self.finish(self.claim())
        self.queue.connection.execute("UPDATE summaries SET prompt_version=0 WHERE job_key=?", (key,))
        self.queue.connection.commit()
        self.assertEqual(self.store.handle("summary-status", {"id": key})["bullets"], [])
        self.reconcile()
        self.assertEqual(self.claim()["identity"]["prompt_version"], 1)

    def test_metadata_only_edits_keep_ready_and_failed_results(self):
        self.own()
        for outcome in ("ready", "failed"):
            with self.subTest(outcome=outcome):
                key, path = self.add_note(outcome)
                self.reconcile()
                claim = self.claim()
                self.finish(claim, outcome=outcome, error_code="refused" if outcome == "failed" else None)
                before = self.queue.connection.execute("SELECT source_hash,state,bullets,error_code,queue_order FROM summaries WHERE job_key=?", (key,)).fetchone()
                current = path.stat()
                os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1000000000))
                self.reconcile()
                after = self.queue.connection.execute("SELECT source_hash,state,bullets,error_code,queue_order FROM summaries WHERE job_key=?", (key,)).fetchone()
                self.assertEqual(after, before)
                self.assertIsNone(self.claim())

    def test_empty_cached_claim_work_does_not_scale_with_library(self):
        _, path = self.add_note("shared")
        body, digest, fingerprint = read_transcript_body(str(path), self.output)
        counts = []
        for size in (100, 1000):
            database = self.root / f"performance-{size}.sqlite"
            queue = Queue(database, START)
            first = float(queue.get("summary_first_enabled_at"))
            with queue.connection:
                queue.connection.executemany("INSERT INTO jobs(key,message,state,output,completed_at) VALUES(?,?,'complete',?,?)", [(f"key-{i:05}", json.dumps(audio()), str(path), first + i + 1) for i in range(size)])
                queue.connection.executemany("INSERT INTO summaries(job_key,source_hash,file_fingerprint,prompt_version,state,bullets,priority,queue_order,updated_at) VALUES(?,?,?,1,'ready','[\"Saved\"]',0,?,0)", [(f"key-{i:05}", digest, json.dumps(fingerprint, sort_keys=True, separators=(",", ":")), i) for i in range(size)])
            store = SummaryStore(database, self.output)
            store.handle("summary-owner", {"owner": "performance"})
            store.handle("summary-reconcile", {"owner": "performance"})
            steps = [0]
            connect = sqlite3.connect
            def tracked_connect(*args, **kwargs):
                db = connect(*args, **kwargs)
                def progress():
                    steps[0] += 1
                    return 0
                db.set_progress_handler(progress, 1)
                return db
            with patch("app_summaries.sqlite3.connect", tracked_connect):
                self.assertIsNone(store.handle("summary-claim", {"owner": "performance"})["claim"])
            counts.append(steps[0])
            store.close()
            queue.close()
        self.assertLess(counts[1], counts[0] * 4)

    def test_late_completion_behind_time_cursor_is_repaired_by_wrap(self):
        first, _ = self.add_note("before-cursor")
        enabled = float(self.queue.get("summary_first_enabled_at"))
        self.queue.connection.execute("UPDATE jobs SET completed_at=? WHERE key=?", (enabled + 100, first))
        self.queue.connection.commit()
        self.own()
        self.reconcile()
        self.finish(self.claim())
        late, _ = self.add_note("after-cursor")
        self.queue.connection.execute("UPDATE jobs SET completed_at=? WHERE key=?", (enabled + 1, late))
        self.queue.connection.commit()
        for _ in range(4):
            self.reconcile()
        self.assertEqual(self.claim()["identity"]["id"], late)
