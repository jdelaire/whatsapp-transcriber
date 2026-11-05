import fcntl
import hashlib
import json
import os
import shutil
import selectors
import sqlite3
import subprocess
import time
import threading
import tempfile
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from transcriber import Queue, atomic_text, execute, start_process, stop_process


MODEL_BYTES = 487601967
MODEL_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
LEGACY_LABEL = "com.jdelaire.whatsapp-transcriber"


@dataclass
class Paths:
    data: Path
    tools: Path
    legacy_config: Path | None = None
    legacy_plist: Path | None = None
    isolated: bool = True

    @property
    def config(self):
        return self.data / "app-config.json"

    @classmethod
    def current(cls, tools, data=None):
        if data is not None:
            return cls(Path(data).expanduser().resolve(), Path(tools).resolve())
        return cls(Path.home() / "Library/Application Support/WhatsAppTranscriber", Path(tools).resolve(),
                   Path.home() / ".local/share/whatsapp-transcriber/config.json",
                   Path.home() / "Library/LaunchAgents" / (LEGACY_LABEL + ".plist"), False)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def verify_model(path, size=MODEL_BYTES, digest=MODEL_SHA256):
    try:
        if Path(path).stat().st_size != size:
            return False
        with Path(path).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == digest
    except OSError:
        return False


def promote_model(part, destination, size=MODEL_BYTES, digest=MODEL_SHA256):
    if not verify_model(part, size, digest):
        raise ValueError("The downloaded speech model failed verification. Please retry.")
    Path(destination).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(part, destination)


def model_ready(paths, config):
    path = Path(config["model"])
    record = read_json(paths.data / "model-verified.json", {})
    try:
        stat = path.stat()
        return stat.st_size == MODEL_BYTES and record.get("sha256") == MODEL_SHA256 and record.get("mtime_ns") == stat.st_mtime_ns
    except OSError:
        return False


def initialize(paths):
    os.umask(0o077)
    paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = read_json(paths.config) or (read_json(paths.legacy_config) if paths.legacy_config else {}) or {}
    output = paths.data / "transcripts" if paths.isolated else Path.home() / "WhatsApp Transcripts"
    defaults = {"data": str(paths.data), "store": str(paths.data / "store"), "output": str(output),
                "model": str(paths.data / "models/ggml-small.bin"),
                "start_time": datetime.now(timezone.utc).isoformat()}
    config = defaults | config
    config["data"] = str(paths.data)
    for key in ("store", "output", "model"):
        if paths.isolated and not Path(config[key]).resolve().is_relative_to(paths.data.resolve()):
            config[key] = defaults[key]
    for key, executable in (("wacli", "wacli"), ("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("whisper", "whisper-cli")):
        path = paths.tools / executable
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"The app is missing its {executable} helper. Reinstall the app.")
        config[key] = str(path)
    for key in ("store", "output"):
        Path(config[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(config["model"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_text(paths.config, json.dumps(config, indent=2) + "\n")
    queue = Queue(paths.data / "jobs.sqlite", config["start_time"])
    queue.close()
    if not model_ready(paths, config) and verify_model(config["model"]):
        atomic_text(paths.data / "model-verified.json", json.dumps({"sha256": MODEL_SHA256, "mtime_ns": Path(config["model"]).stat().st_mtime_ns}))
    return config


def auth_status(config):
    result = execute([config["wacli"], "--store", config["store"], "--read-only", "--json", "auth", "status"], 15)
    response = json.loads(result)
    if not response.get("success"):
        raise RuntimeError("Unable to read the linked WhatsApp session.")
    return response["data"].get("authenticated") is True


@contextmanager
def queue_snapshot(database):
    sources = [database, Path(str(database) + "-wal")]

    def signature():
        result = []
        for path in sources:
            try:
                stat = path.stat()
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                result.append(None)
        return result

    with tempfile.TemporaryDirectory(prefix="transcriber-status-") as folder:
        target = Path(folder) / "jobs.sqlite"
        for _ in range(5):
            before = signature()
            for source in sources:
                copy = Path(folder) / source.name
                try:
                    shutil.copyfile(source, copy)
                except FileNotFoundError:
                    copy.unlink(missing_ok=True)
            if before == signature():
                with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    yield db
                return
            time.sleep(0.02)
        raise RuntimeError("The transcript list is busy. Please retry.")


def snapshot(paths, auth_probe=auth_status):
    config = read_json(paths.config)
    state = {"schema_version": 1, "configured": bool(config), "authenticated": False,
             "paused": (paths.data / "PAUSED").exists(), "model_ready": False,
             "worker_alive": False, "connection": "stopped", "processing": False,
             "counts": {}, "recent": [], "model_path": "", "output_folder": "",
             "migration_needed": bool(paths.legacy_plist and paths.legacy_plist.exists()),
             "migration_phase": read_json(paths.data / "migration.json", {}).get("phase", "none")}
    if not config:
        return state
    state.update(model_ready=model_ready(paths, config), model_path=config["model"], output_folder=config["output"])
    try:
        state["authenticated"] = bool(auth_probe(config))
    except (OSError, RuntimeError, ValueError, TimeoutError):
        state["authentication_error"] = "WhatsApp status is temporarily unavailable."
    runtime = read_json(paths.data / "runtime.json", {})
    try:
        if not runtime.get("pid"):
            raise ProcessLookupError()
        os.kill(int(runtime["pid"]), 0)
        alive = time.time() - runtime.get("heartbeat", 0) < 30
    except (OSError, ValueError):
        alive = False
    state.update(worker_alive=alive, connection=runtime.get("connection", "connecting") if alive else "stopped",
                 processing=bool(alive and runtime.get("processing")), worker_pid=runtime.get("pid"))
    database = paths.data / "jobs.sqlite"
    if database.exists():
        with queue_snapshot(database) as db:
            state["counts"] = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
            for key, raw, output in db.execute("SELECT key,message,output FROM jobs WHERE state='complete' ORDER BY rowid DESC LIMIT 10"):
                message = json.loads(raw)
                path = Path(output or "")
                if not output or path.suffix != ".txt" or not path.resolve().is_relative_to(Path(config["output"]).resolve()):
                    continue
                try:
                    with path.open() as handle:
                        text = handle.read(65536).partition("\n\n")[2].strip()
                    state["recent"].append({"id": key, "title": message.get("chat_name") or "WhatsApp chat",
                                            "timestamp": message["timestamp"], "text": text, "path": str(path)})
                except OSError:
                    continue
    return state


def set_paused(paths, paused):
    paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = paths.data / "PAUSED"
    if paused:
        atomic_text(marker, "Paused by the menu bar app.\n")
    else:
        marker.unlink(missing_ok=True)


def watch_parent(stream, stop, done=None):
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        while stream.read(1):
            pass
        stop()
        return
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while done is None or not done.is_set():
            if selector.select(timeout=0.1) and not os.read(descriptor, 8192):
                stop()
                return


@contextmanager
def supervise_parent(stream, stop):
    done = threading.Event()
    thread = threading.Thread(target=watch_parent, args=(stream, stop, done), daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1)


class Migration:
    def __init__(self, paths, stop=None, start=None, active=None, cancelled=None):
        self.paths = paths
        self.journal = paths.data / "migration.json"
        self.target = f"gui/{os.getuid()}/{LEGACY_LABEL}"
        self.stop = stop or self.stop_legacy
        self.start = start or self.start_legacy
        self.active = active or self.legacy_active
        self.cancelled = cancelled or (lambda: False)

    def check_cancelled(self):
        if self.cancelled():
            raise InterruptedError("Takeover cancelled.")

    def legacy_active(self):
        return self.launchctl(["print", self.target], timeout=5, check=False) == 0

    def launchctl(self, arguments, timeout=20, check=True):
        process = start_process(["/bin/launchctl", *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            process.communicate(timeout=timeout)
            if check and process.returncode:
                raise RuntimeError("The previous receiver startup could not be updated. Please retry.")
            return process.returncode
        finally:
            stop_process(process)

    def stop_legacy(self):
        if self.legacy_active():
            self.launchctl(["bootout", self.target])

    def start_legacy(self):
        self.launchctl(["bootstrap", f"gui/{os.getuid()}", str(self.paths.legacy_plist)])

    def save(self, state):
        atomic_text(self.journal, json.dumps(state, indent=2))

    def prepare(self):
        paths = self.paths
        previous = read_json(self.journal, {})
        if self.cancelled() and previous.get("phase") in ("prepared", "disabled"):
            self.rollback(restart=False)
        self.check_cancelled()
        if previous.get("phase") == "disabled":
            return previous | {"needed": True}
        if previous.get("phase") == "prepared" and paths.legacy_plist and not paths.legacy_plist.exists():
            if not (Path(previous["backup"]) / "legacy-agent.plist").exists():
                raise RuntimeError("Previous startup recovery is incomplete. Please restore the original receiver.")
            previous["phase"] = "disabled"
            self.save(previous)
            return previous | {"needed": True}
        if not paths.legacy_plist or not paths.legacy_plist.exists():
            return {"needed": False}
        if previous.get("phase") == "prepared":
            state = previous
            backup = Path(state["backup"])
        else:
            backup = paths.data / "migration-backups" / uuid.uuid4().hex
            backup.mkdir(parents=True, mode=0o700)
            state = {"needed": True, "phase": "prepared", "backup": str(backup), "was_active": self.active(),
                     "original_plist": str(paths.legacy_plist)}
        self.save(state)
        try:
            self.stop()
            self.check_cancelled()
            with (paths.data / "worker.lock").open("a") as lock:
                deadline = time.monotonic() + 15
                while True:
                    self.check_cancelled()
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("The previous receiver is still stopping. Please retry.")
                        time.sleep(0.2)
                for source in (paths.data / "jobs.sqlite", Path(read_json(paths.config)["store"]) / "session.db",
                               Path(read_json(paths.config)["store"]) / "wacli.db"):
                    if source.exists():
                        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as origin, closing(sqlite3.connect(backup / source.name)) as destination:
                            deadline = time.monotonic() + 10
                            def progress(*_):
                                self.check_cancelled()
                                if time.monotonic() > deadline:
                                    raise TimeoutError("The previous receiver database is busy. Please retry.")
                            origin.backup(destination, pages=256, progress=progress, sleep=0.05)
                    self.check_cancelled()
                shutil.copy2(paths.legacy_plist, backup / "legacy-agent.plist")
                self.check_cancelled()
                paths.legacy_plist.unlink()
                state["phase"] = "disabled"
                self.save(state)
                self.check_cancelled()
            return state
        except Exception as error:
            self.rollback(restart=not self.cancelled() and not isinstance(error, InterruptedError))
            raise

    def commit(self):
        state = read_json(self.journal, {})
        if state:
            state["phase"] = "committed"
            self.save(state)

    def rollback(self, restart=True):
        state = read_json(self.journal, {})
        if not state or not self.paths.legacy_plist:
            return
        backup = Path(state["backup"]) / "legacy-agent.plist"
        if backup.exists() and not self.paths.legacy_plist.exists():
            self.paths.legacy_plist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, self.paths.legacy_plist)
        if restart and state.get("was_active") and self.paths.legacy_plist.exists() and not self.active():
            self.start()
        state["phase"] = "restored"
        self.save(state)
