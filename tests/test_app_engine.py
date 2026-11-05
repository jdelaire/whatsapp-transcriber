import hashlib
import io
import os
import json
import sqlite3
import tempfile
import unittest
import threading
from contextlib import closing
from pathlib import Path

from app_engine import Paths, Migration, initialize, snapshot, set_paused, verify_model, promote_model, watch_parent, supervise_parent, Runtime
from transcriber import Queue
from test_transcriber import START, audio


class AppEngineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("wacli", "ffmpeg", "ffprobe", "whisper-cli"):
            path = self.tools / name
            path.write_text("fixture")
            path.chmod(0o700)
        self.paths = Paths(self.root / "data", self.tools)

    def tearDown(self):
        self.folder.cleanup()

    def test_status_on_fresh_install_creates_nothing(self):
        state = snapshot(self.paths, auth_probe=lambda _: False)
        self.assertEqual(state["schema_version"], 1)
        self.assertFalse(state["configured"])
        self.assertFalse(self.paths.data.exists())

    def test_initialize_preserves_legacy_session_paths_but_replaces_executables(self):
        self.paths.data.mkdir()
        source = self.root / "legacy.json"
        original = {"data": str(self.paths.data), "store": str(self.paths.data / "store"),
                    "output": str(self.paths.data / "saved-transcripts"), "model": str(self.paths.data / "models/ggml-small.bin"),
                    "start_time": START, "wacli": "/opt/homebrew/bin/wacli"}
        source.write_text(json.dumps(original))
        self.paths.legacy_config = source
        config = initialize(self.paths)
        self.assertEqual(config["start_time"], START)
        self.assertEqual(config["store"], original["store"])
        self.assertEqual(config["output"], original["output"])
        self.assertEqual(config["wacli"], str(self.tools / "wacli"))
        self.assertEqual(json.loads(source.read_text()), original)

    def test_snapshot_reads_recent_transcript_without_modifying_queue(self):
        config = initialize(self.paths)
        queue = Queue(self.paths.data / "jobs.sqlite", START)
        queue.ingest([audio(timestamp=config["start_time"])], "checkpoint")
        job = queue.pending(0)[0]
        transcript = self.paths.data / "transcripts/note.txt"
        transcript.parent.mkdir(exist_ok=True)
        transcript.write_text("Chat: Alice\n\nBonjour John.\n")
        queue.complete(job["key"], str(transcript))
        queue.close()
        before = (self.paths.data / "jobs.sqlite").stat().st_mtime_ns
        files = {path.name: path.stat().st_mtime_ns for path in self.paths.data.iterdir()}
        state = snapshot(self.paths, auth_probe=lambda _: True)
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["recent"][0]["text"], "Bonjour John.")
        self.assertEqual(state["recent"][0]["title"], "Alice")
        self.assertEqual(state["counts"], {"complete": 1})
        self.assertEqual((self.paths.data / "jobs.sqlite").stat().st_mtime_ns, before)
        self.assertEqual({path.name: path.stat().st_mtime_ns for path in self.paths.data.iterdir()}, files)

    def test_snapshot_includes_uncheckpointed_wal_without_source_writes(self):
        config = initialize(self.paths)
        queue = Queue(self.paths.data / "jobs.sqlite", START)
        try:
            queue.ingest([audio(timestamp=config["start_time"])], "checkpoint")
            files = {path.name: path.stat().st_mtime_ns for path in self.paths.data.iterdir()}
            self.assertEqual(snapshot(self.paths, auth_probe=lambda _: True)["counts"], {"pending": 1})
            self.assertEqual({path.name: path.stat().st_mtime_ns for path in self.paths.data.iterdir()}, files)
        finally:
            queue.close()

    def test_rejected_runtime_cannot_replace_live_worker_state(self):
        initialize(self.paths)
        marker = self.paths.data / "runtime.json"
        marker.write_text('{"pid":123,"heartbeat":42}')
        runtime = Runtime(self.paths)
        runtime.finish()
        self.assertEqual(json.loads(marker.read_text()), {"pid": 123, "heartbeat": 42})

    def test_pause_survives_configuration_reload(self):
        initialize(self.paths)
        set_paused(self.paths, True)
        initialize(self.paths)
        self.assertTrue(snapshot(self.paths, auth_probe=lambda _: True)["paused"])
        set_paused(self.paths, False)
        self.assertFalse(snapshot(self.paths, auth_probe=lambda _: True)["paused"])

    def test_invalid_model_never_replaces_good_existing_file(self):
        good = self.root / "model.bin"
        good.write_bytes(b"abc")
        part = self.root / "model.part"
        part.write_bytes(b"bad")
        expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertTrue(verify_model(good, 3, expected))
        with self.assertRaises(ValueError):
            promote_model(part, good, 3, expected)
        self.assertEqual(good.read_bytes(), b"abc")

    def test_parent_eof_requests_shutdown(self):
        stopped = []
        watch_parent(io.BytesIO(b""), lambda: stopped.append(True))
        self.assertEqual(stopped, [True])

    def test_successful_operation_joins_parent_watch_with_stdin_still_open(self):
        read, write = os.pipe()
        stopped = []
        try:
            with os.fdopen(read, "rb") as stream:
                before = set(threading.enumerate())
                with supervise_parent(stream, lambda: stopped.append(True)):
                    pass
                self.assertEqual(set(threading.enumerate()), before)
            self.assertEqual(stopped, [])
        finally:
            os.close(write)

    def test_isolated_paths_cannot_migrate_real_launchd_registration(self):
        initialize(self.paths)
        called = []
        migration = Migration(self.paths, stop=lambda: called.append("stop"), start=lambda: called.append("start"))
        self.assertFalse(migration.prepare()["needed"])
        self.assertEqual(called, [])

    def test_migration_rollback_restores_startup_without_reverting_new_data(self):
        initialize(self.paths)
        plist = self.root / "LaunchAgents/legacy.plist"
        plist.parent.mkdir()
        plist.write_text("original startup")
        self.paths.legacy_plist = plist
        db_path = self.paths.data / "store/session.db"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE content(value TEXT)")
            db.execute("INSERT INTO content VALUES ('old')")
        called = []
        migration = Migration(self.paths, stop=lambda: called.append("stop"), start=lambda: called.append("start"), active=lambda: not called)
        result = migration.prepare()
        self.assertTrue(result["needed"])
        self.assertFalse(plist.exists())
        self.assertTrue((Path(result["backup"]) / "session.db").exists())
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE content SET value='new'")
        migration.rollback()
        self.assertEqual(plist.read_text(), "original startup")
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("SELECT value FROM content").fetchone()[0], "new")
        self.assertEqual(called, ["stop", "start"])

    def test_crash_after_startup_removal_resumes_takeover_and_retains_rollback(self):
        initialize(self.paths)
        plist = self.root / "legacy.plist"
        plist.write_text("original")
        self.paths.legacy_plist = plist
        migration = Migration(self.paths, stop=lambda: None, start=lambda: None, active=lambda: True)
        state = migration.prepare()
        state["phase"] = "prepared"
        migration.save(state)
        restarted = Migration(self.paths, stop=lambda: None, start=lambda: None, active=lambda: False)
        recovered = restarted.prepare()
        self.assertTrue(recovered["needed"])
        self.assertTrue(recovered["was_active"])
        self.assertEqual(recovered["phase"], "disabled")

    def test_crash_before_startup_removal_keeps_original_active_state(self):
        initialize(self.paths)
        plist = self.root / "legacy.plist"
        plist.write_text("original")
        self.paths.legacy_plist = plist
        migration = Migration(self.paths, stop=lambda: None, start=lambda: None, active=lambda: True)
        state = migration.prepare()
        plist.write_text("original")
        state["phase"] = "prepared"
        migration.save(state)
        restarted = Migration(self.paths, stop=lambda: None, start=lambda: None, active=lambda: False)
        recovered = restarted.prepare()
        self.assertTrue(recovered["was_active"])

    def test_parent_loss_during_migration_restores_startup_without_restarting_capture(self):
        initialize(self.paths)
        plist = self.root / "legacy.plist"
        plist.write_text("original")
        self.paths.legacy_plist = plist
        cancelled = []
        started = []
        migration = Migration(self.paths, stop=lambda: cancelled.append(True), start=lambda: started.append(True),
                              active=lambda: True, cancelled=lambda: bool(cancelled))
        with self.assertRaises(InterruptedError):
            migration.prepare()
        self.assertEqual(plist.read_text(), "original")
        self.assertEqual(started, [])
        self.assertEqual(json.loads(migration.journal.read_text())["phase"], "restored")

    def test_quit_after_takeover_restores_startup_without_starting_capture(self):
        initialize(self.paths)
        plist = self.root / "legacy.plist"
        plist.write_text("original")
        self.paths.legacy_plist = plist
        started = []
        migration = Migration(self.paths, stop=lambda: None, start=lambda: started.append(True), active=lambda: True)
        migration.prepare()
        migration.rollback(restart=False)
        self.assertEqual(plist.read_text(), "original")
        self.assertEqual(started, [])
