import hashlib
import argparse
import contextlib
import fcntl
import json
import logging
import logging.handlers
import math
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def identity(message):
    if not message.get("chat_jid") or not message.get("msg_id"):
        raise ValueError("Message has no chat or message identity")
    return hashlib.sha256(
        (message["chat_jid"] + "\0" + message["msg_id"]).encode()
    ).hexdigest()


def output_path(root, message):
    original = message | {"chat_jid": message.get("output_chat_jid", message["chat_jid"]), "msg_id": message.get("output_msg_id", message["msg_id"])}
    key = identity(original)
    chat = original["chat_jid"]
    label = re.sub(r"[^a-zA-Z0-9_-]", "_", chat.split("@")[0])[:40] or "chat"
    chat_hash = hashlib.sha256(chat.encode()).hexdigest()[:12]
    date = datetime.fromtimestamp(timestamp(message["timestamp"]), timezone.utc)
    return Path(root) / f"{label}_{chat_hash}" / f"{date:%Y-%m-%d_%H-%M-%S}_{key[:20]}.txt"


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Queue:
    def __init__(self, path, start_time):
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY,
                message TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT,
                output TEXT
            );
            CREATE INDEX IF NOT EXISTS due_jobs ON jobs(state, next_attempt);
            CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, target TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                store_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(store_id, sequence)
            );
        """)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('start_time', ?)", (start_time,)
            )

    def close(self):
        self.connection.close()

    def get(self, name):
        row = self.connection.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    @property
    def start_time(self):
        return self.get("start_time")

    @property
    def checkpoint(self):
        return self.get("checkpoint")

    def ingest(self, messages, checkpoint):
        with self.connection:
            for message in messages:
                if message.get("from_me") is not False or message.get("media_type") != "audio" or message.get("deleted"):
                    continue
                if timestamp(message["timestamp"]) < timestamp(self.start_time):
                    continue
                key = self.target(identity(message))
                self.connection.execute(
                    "INSERT OR IGNORE INTO jobs(key, message) VALUES (?, ?)",
                    (key, json.dumps(message, ensure_ascii=False)),
                )
            if checkpoint is not None:
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('checkpoint', ?)", (checkpoint,)
                )

    def target(self, key):
        row = self.connection.execute("SELECT target FROM aliases WHERE alias=?", (key,)).fetchone()
        return row[0] if row else key

    def record_changes(self, data):
        store = data["store_id"]
        if self.get("store_id") not in (None, store):
            raise ValueError("WhatsApp store identity changed; refusing to mix accounts")
        with self.connection:
            for change in data.get("changes") or []:
                self.connection.execute(
                    "INSERT OR IGNORE INTO inbox(store_id, sequence, payload) VALUES (?, ?, ?)",
                    (store, change["sequence"], json.dumps(change)),
                )
            self.connection.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", [
                ("store_id", store), ("checkpoint", data["next_checkpoint"])
            ])

    def references(self, now, limit=100):
        return [{"store_id": row[0], "sequence": row[1], "change": json.loads(row[2])}
                for row in self.connection.execute(
                    "SELECT store_id, sequence, payload FROM inbox WHERE next_attempt<=? ORDER BY sequence LIMIT ?",
                    (now, limit))]

    def finish_reference(self, ref):
        with self.connection:
            self.connection.execute("DELETE FROM inbox WHERE store_id=? AND sequence=?", (ref["store_id"], ref["sequence"]))

    def retry_reference(self, ref, now):
        with self.connection:
            self.connection.execute(
                "UPDATE inbox SET next_attempt=? + MIN(3600, 30 * (1 << MIN(attempts, 7))), attempts=attempts+1 "
                "WHERE store_id=? AND sequence=?", (now, ref["store_id"], ref["sequence"]))

    def move(self, old_chat, old_msg, new_chat, new_msg):
        old = identity({"chat_jid": old_chat, "msg_id": old_msg})
        new = identity({"chat_jid": new_chat, "msg_id": new_msg})
        old_target, new_target = self.target(old), self.target(new)
        rows = self.connection.execute(
            "SELECT key, state, message FROM jobs WHERE key IN (?, ?) ORDER BY state='complete' DESC, key=? DESC",
            (old_target, new_target, old_target)).fetchall()
        target = rows[0][0] if rows else old_target
        with self.connection:
            if rows:
                message = json.loads(rows[0][2])
                message.setdefault("output_chat_jid", message["chat_jid"])
                message.setdefault("output_msg_id", message["msg_id"])
                message.update(chat_jid=new_chat, msg_id=new_msg)
                self.connection.execute("UPDATE jobs SET message=? WHERE key=?", (json.dumps(message), target))
                for duplicate in rows[1:]:
                    self.connection.execute("DELETE FROM jobs WHERE key=?", (duplicate[0],))
            self.connection.execute("UPDATE aliases SET target=? WHERE target IN (?, ?)", (target, old_target, new_target))
            self.connection.executemany("INSERT OR REPLACE INTO aliases VALUES (?, ?)", [(old, target), (new, target)])

    def pending(self, now, limit=10):
        rows = self.connection.execute(
            "SELECT key, message, attempts FROM jobs WHERE state='pending' AND next_attempt<=? "
            "ORDER BY next_attempt, rowid LIMIT ?", (now, limit)
        ).fetchall()
        return [{"key": row[0], "message": json.loads(row[1]), "attempts": row[2]} for row in rows]

    def fail(self, key, error, now):
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET next_attempt=? + MIN(3600, 30 * (1 << MIN(attempts, 7))), "
                "attempts=attempts+1, error=? WHERE key=?", (now, str(error)[-2000:], key)
            )

    def complete(self, key, path):
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET state='complete', output=?, error=NULL WHERE key=?", (path, key)
            )

    def cancel(self, chat_jid, msg_id):
        key = self.target(identity({"chat_jid": chat_jid, "msg_id": msg_id}))
        with self.connection:
            self.connection.execute("UPDATE jobs SET state='cancelled' WHERE key=? AND state='pending'", (key,))

    def publish(self, key, destination, text):
        with self.connection:
            row = self.connection.execute(
                "UPDATE jobs SET state=state WHERE key=? AND state='pending'", (key,)
            )
            if row.rowcount == 0:
                return False
            if text is not None:
                atomic_text(destination, text)
            self.connection.execute(
                "UPDATE jobs SET state='complete', output=?, error=NULL WHERE key=?", (str(destination), key)
            )
        return True


def normalize(message):
    return {
        "chat_jid": message["ChatJID"], "msg_id": message["MsgID"],
        "chat_name": message.get("ChatName", ""), "sender": message.get("SenderJID", ""),
        "timestamp": message["Timestamp"], "from_me": message.get("FromMe"),
        "media_type": message.get("MediaType"),
        "deleted": bool(message.get("deleted_at") or message.get("payload_purged_at")),
    }


class Wacli:
    def __init__(self, config, call=None, before_recovery=None, mutation_lock=None):
        self.config = config
        self.call = call or self.read
        self.before_recovery = before_recovery or (lambda: None)
        self.mutation_lock = mutation_lock

    def command(self, arguments):
        return [self.config["wacli"], "--store", self.config["store"], *arguments]

    def read(self, arguments):
        result = execute(self.command(["--read-only", "--json", *arguments]), 60)
        envelope = json.loads(result)
        if not envelope.get("success"):
            raise RuntimeError(envelope.get("error") or "wacli read failed")
        return envelope["data"]

    def collect(self, queue):
        if queue.checkpoint is None:
            return self.recover(queue)
        while True:
            try:
                data = self.call(["messages", "changes", "--since", queue.checkpoint, "--limit", "500"])
            except RuntimeError as error:
                if "expired checkpoint:" not in str(error):
                    raise
                return self.recover(queue)
            queue.record_changes(data)
            if not data.get("has_more"):
                return

    def recover(self, queue):
        self.before_recovery()
        if self.mutation_lock:
            self.mutation_lock.acquire()
        try:
            anchor = self.call(["messages", "changes", "--checkpoint"])
            after = datetime.fromtimestamp(timestamp(queue.start_time) - 1, timezone.utc).isoformat()
            for attempt in range(5):
                staged, cursor = [], None
                try:
                    while True:
                        args = ["messages", "list", "--from-them", "--asc", "--after", after, "--limit", "500"]
                        if cursor:
                            args += ["--cursor", cursor]
                        data = self.call(args)
                        staged.extend(normalize(msg) for msg in data.get("messages") or [] if msg.get("MediaType") == "audio")
                        page = data.get("page") or {}
                        if not page.get("has_more"):
                            break
                        cursor = page.get("next_cursor")
                        if not cursor:
                            raise RuntimeError("wacli omitted continuation cursor")
                    queue.ingest(staged, anchor["next_checkpoint"])
                    return
                except RuntimeError as error:
                    if "archive changed since cursor" not in str(error) or attempt == 4:
                        raise
        finally:
            if self.mutation_lock:
                self.mutation_lock.release()

    def resolve(self, queue, now):
        for ref in queue.references(now):
            change = ref["change"]
            try:
                operation = change["operation"]
                if operation == "identity_move":
                    queue.move(change["chat_jid"], change["msg_id"], change["new_chat_jid"], change.get("new_msg_id") or change["msg_id"])
                elif operation == "delete":
                    queue.cancel(change["chat_jid"], change["msg_id"])
                elif operation == "upsert":
                    if change.get("message_time") and timestamp(change["message_time"]) < timestamp(queue.start_time):
                        queue.finish_reference(ref)
                        continue
                    data = self.call(["messages", "show", "--chat", change["chat_jid"], "--id", change["msg_id"]])
                    message = normalize(data)
                    if message["deleted"]:
                        queue.cancel(message["chat_jid"], message["msg_id"])
                    else:
                        queue.ingest([message], None)
                queue.finish_reference(ref)
            except RuntimeError as error:
                if "not found" in str(error).lower() or "sql: no rows in result set" in str(error):
                    queue.finish_reference(ref)
                else:
                    queue.retry_reference(ref, now)


STOP = threading.Event()
PROCESS_EVENT = None
PROCESS_LOCK = threading.Lock()
OWNED_PROCESSES = {}


def start_process(*args, **kwargs):
    process = subprocess.Popen(*args, **kwargs)
    with PROCESS_LOCK:
        OWNED_PROCESSES[process.pid] = process
    if PROCESS_EVENT:
        PROCESS_EVENT({"event": "child_started", "group": process.pid})
    return process


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_process(process):
    process.poll()
    if group_alive(process.pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while group_alive(process.pid) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.02)
        if group_alive(process.pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    with PROCESS_LOCK:
        OWNED_PROCESSES.pop(process.pid, None)
    if PROCESS_EVENT:
        PROCESS_EVENT({"event": "child_stopped", "group": process.pid})


def execute(arguments, timeout):
    process = start_process(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if STOP.is_set():
                raise InterruptedError("Shutting down")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{Path(arguments[0]).name} exceeded {timeout} seconds")
            try:
                stdout, stderr = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"{Path(arguments[0]).name}: {stderr.strip()[-1500:]}")
        return stdout
    finally:
        stop_process(process)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


class Processor:
    def __init__(self, config, command=execute, mutation_lock=None):
        self.config = config
        self.command = command
        self.mutation_lock = mutation_lock or threading.Lock()

    def transcribe(self, source, destination, header="", publish=None):
        duration = float(self.command([
            self.config["ffprobe"], "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source)
        ], 30).strip())
        if not math.isfinite(duration) or duration <= 0 or duration > 3600:
            raise ValueError("Audio must be shorter than 60 minutes with a known duration")
        with tempfile.TemporaryDirectory(prefix="decode-", dir=self.config["data"]) as folder:
            base = Path(folder) / "transcript"
            wav = Path(folder) / "audio.wav"
            self.command([
                self.config["ffmpeg"], "-nostdin", "-v", "error", "-y", "-i", str(source),
                "-t", "3600", "-fs", "120000000", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)
            ], 120)
            if wav.stat().st_size > 120000000:
                raise ValueError("Decoded audio exceeds 120 MB")
            self.command([
                self.config["whisper"], "-m", self.config["model"], "-f", str(wav),
                "-l", "auto", "-t", "4", "-otxt", "-of", str(base), "-np"
            ], 1800)
            text = base.with_suffix(".txt").read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError("Whisper produced no text")
            if publish:
                if not publish(destination, header + text + "\n"):
                    return None
            else:
                atomic_text(destination, header + text + "\n")
        return Path(destination)

    def process(self, job, queue=None):
        message = job["message"]
        destination = output_path(self.config["output"], message)
        if destination.exists() and destination.stat().st_size:
            if queue and not queue.publish(job["key"], destination, None):
                return None
            return destination
        audio_dir = Path(self.config["data"]) / "audio"
        audio_dir.mkdir(mode=0o700, exist_ok=True)
        source = audio_dir / (job["key"] + ".audio")
        with self.mutation_lock:
            self.command([
                self.config["wacli"], "--store", self.config["store"], "--timeout", "120s", "--json",
                "media", "download", "--chat", message["chat_jid"], "--id", message["msg_id"],
                "--output", str(source), "--retry-on-phone"
            ], 130)
        header = (
            f"Chat: {message.get('chat_name') or message['chat_jid']}\n"
            f"Sender: {message.get('sender', '')}\nReceived: {message['timestamp']}\n"
            f"Message: {message['msg_id']}\n\n"
        )
        publish = (lambda path, text: queue.publish(job["key"], path, text)) if queue else None
        return self.transcribe(source, destination, header, publish)


class Sync:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.reader = None
        self.last_start = 0
        self.pairing_needed = Path(config["data"]) / "NEEDS_PAIRING"

    def start(self):
        if self.pairing_needed.exists() or (self.process and self.process.poll() is None):
            return
        if time.monotonic() - self.last_start < 60:
            return
        self.stop()
        self.last_start = time.monotonic()
        self.process = start_process([
            self.config["wacli"], "--store", self.config["store"], "--events",
            "sync", "--follow", "--presence-mode", "quiet", "--max-reconnect", "0"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.reader = threading.Thread(target=self.read_events, args=(self.process,), daemon=True)
        self.reader.start()

    def read_events(self, process):
        for line in process.stderr:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("event", "")
            if name in ("connected", "disconnected", "logged_out", "reconnecting"):
                notify(self.config, {"event": "connection", "state": name})
            if name == "logged_out":
                atomic_text(self.pairing_needed, "WhatsApp unlinked this device. Run pair again.\n")
            if name in ("connected", "disconnected", "logged_out", "reconnecting"):
                logging.info("WhatsApp %s", name)

    def stop(self):
        if self.process:
            stop_process(self.process)
            if self.reader:
                self.reader.join(timeout=5)
            self.process.stderr.close()
            self.process = None


def worker(config, lock):
    queue = Queue(Path(config["data"]) / "jobs.sqlite", config["start_time"])
    processor = Processor(config, mutation_lock=lock)
    try:
        while not STOP.is_set():
            jobs = queue.pending(time.time(), limit=1)
            if not jobs:
                STOP.wait(2)
                continue
            job = jobs[0]
            try:
                notify(config, {"event": "processing", "active": True})
                path = processor.process(job, queue)
                if path:
                    logging.info("Transcript saved: %s", path.name)
                    notify(config, {"event": "transcript", "id": job["key"]})
            except InterruptedError:
                break
            except ValueError as error:
                with queue.connection:
                    queue.connection.execute("UPDATE jobs SET state='failed', error=? WHERE key=?", (str(error), job["key"]))
                logging.warning("Audio rejected: %s", error)
            except Exception as error:
                queue.fail(job["key"], error, time.time())
                logging.warning("Audio queued for retry: %s", str(error)[-300:])
            finally:
                notify(config, {"event": "processing", "active": False})
    finally:
        queue.close()


def notify(config, event):
    callback = config.get("_event")
    if callback:
        callback(event)


def run(config):
    os.umask(0o077)
    data = Path(config["data"])
    with (data / "worker.lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Transcriber already running")
        notify(config, {"event": "ownership"})
        handler = logging.handlers.RotatingFileHandler(data / "transcriber.log", maxBytes=2_000_000, backupCount=3)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler])
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        mutation_lock = threading.Lock()
        sync = Sync(config)
        client = Wacli(config, before_recovery=sync.stop, mutation_lock=mutation_lock)
        queue = Queue(data / "jobs.sqlite", config["start_time"])
        thread = threading.Thread(target=worker, args=(config, mutation_lock))
        thread.start()
        notify(config, {"event": "ready", "pid": os.getpid()})
        try:
            while not STOP.is_set():
                if not thread.is_alive():
                    raise RuntimeError("The transcription worker stopped unexpectedly.")
                if sync.pairing_needed.exists():
                    sync.stop()
                    STOP.wait(30)
                    continue
                try:
                    client.collect(queue)
                    sync.start()
                    client.resolve(queue, time.time())
                except Exception as error:
                    logging.warning("Capture will retry: %s", str(error)[-500:])
                    sync.start()
                STOP.wait(10)
        finally:
            STOP.set()
            sync.stop()
            thread.join()
            queue.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "transcribe", "status"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--file")
    parser.add_argument("--output")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.command == "run":
        run(config)
    elif args.command == "transcribe":
        if not args.file or not args.output:
            parser.error("transcribe requires --file and --output")
        os.umask(0o077)
        print(Processor(config).transcribe(Path(args.file), Path(args.output)))
    else:
        queue = Queue(Path(config["data"]) / "jobs.sqlite", config["start_time"])
        try:
            counts = dict(queue.connection.execute("SELECT state, count(*) FROM jobs GROUP BY state"))
            print(json.dumps({"jobs": counts, "transcripts": config["output"], "pairing_needed": (Path(config["data"]) / "NEEDS_PAIRING").exists()}, indent=2))
        finally:
            queue.close()


if __name__ == "__main__":
    main()
