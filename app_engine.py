import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from app_state import Paths, Migration, initialize, snapshot, set_paused, verify_model, promote_model, watch_parent, supervise_parent, read_json
from app_pairing import Pairing
import transcriber


OUTPUT_LOCK = threading.Lock()


def emit(event):
    with OUTPUT_LOCK:
        try:
            print(json.dumps({"schema_version": 1} | event, ensure_ascii=False), flush=True)
        except BrokenPipeError:
            transcriber.STOP.set()


class Runtime:
    def __init__(self, paths):
        self.paths = paths
        self.lock = threading.Lock()
        self.owned = False
        self.state = {"pid": os.getpid(), "heartbeat": time.time(), "connection": "connecting", "processing": False}

    def save(self):
        transcriber.atomic_text(self.paths.data / "runtime.json", json.dumps(self.state))

    def event(self, value):
        with self.lock:
            if value.get("event") == "ownership":
                self.owned = True
            if not self.owned:
                return
            if value.get("event") == "connection":
                self.state["connection"] = value["state"]
            elif value.get("event") == "processing":
                self.state["processing"] = value["active"]
            self.state["heartbeat"] = time.time()
            self.save()
        emit(value)

    def heartbeat(self):
        while not transcriber.STOP.wait(5):
            with self.lock:
                if self.owned:
                    self.state["heartbeat"] = time.time()
                    self.save()

    def finish(self):
        with self.lock:
            if not self.owned:
                return
            self.state.update(connection="stopped", processing=False, heartbeat=0)
            self.save()
            self.owned = False


def run(paths):
    config = read_json(paths.config)
    if not config:
        raise ValueError("Complete app setup first.")
    if (paths.data / "PAUSED").exists():
        emit({"event": "paused"})
        return
    if not snapshot(paths)["model_ready"]:
        raise ValueError("Download the speech model before starting transcription.")
    runtime = Runtime(paths)
    config["_event"] = runtime.event
    heartbeat = threading.Thread(target=runtime.heartbeat, daemon=True)
    heartbeat.start()
    try:
        with supervise_parent(sys.stdin.buffer, transcriber.STOP.set):
            transcriber.run(config)
    finally:
        transcriber.STOP.set()
        heartbeat.join(timeout=6)
        runtime.finish()
        emit({"event": "stopped"})


def pair(paths, phone):
    Pairing(read_json(paths.config), emit).run(phone)
    (paths.data / "NEEDS_PAIRING").unlink(missing_ok=True)


def main():
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: transcriber.STOP.set())
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["initialize", "status", "run", "pair", "pause", "resume", "migrate", "commit-migration", "restore", "restore-stopped", "verify-model", "transcribe"])
    parser.add_argument("--tools", required=True)
    parser.add_argument("--data-root")
    parser.add_argument("--input")
    parser.add_argument("--output")
    args = parser.parse_args()
    paths = Paths.current(args.tools, args.data_root)
    def process_event(event):
        with OUTPUT_LOCK:
            try:
                print(json.dumps(event), file=sys.stderr, flush=True)
            except BrokenPipeError:
                transcriber.STOP.set()
    transcriber.PROCESS_EVENT = process_event
    try:
        if args.command == "initialize":
            initialize(paths)
            result = snapshot(paths)
        elif args.command == "status":
            result = snapshot(paths)
        elif args.command == "run":
            run(paths)
            return
        elif args.command == "pair":
            config = read_json(paths.config)
            request = json.loads(sys.stdin.buffer.readline())
            with supervise_parent(sys.stdin.buffer, transcriber.STOP.set):
                pair(paths, request.get("phone", ""))
            return
        elif args.command in ("pause", "resume"):
            set_paused(paths, args.command == "pause")
            result = {"paused": args.command == "pause"}
        elif args.command in ("migrate", "commit-migration", "restore", "restore-stopped"):
            migration = Migration(paths, cancelled=transcriber.STOP.is_set)
            if args.command == "migrate":
                with supervise_parent(sys.stdin.buffer, transcriber.STOP.set):
                    result = migration.prepare()
            else:
                migration.commit() if args.command == "commit-migration" else migration.rollback(restart=args.command == "restore")
                result = {"finished": True}
        elif args.command == "verify-model":
            config = read_json(paths.config)
            if args.input:
                promote_model(Path(args.input), Path(config["model"]))
            initialize(paths)
            result = {"verified": snapshot(paths)["model_ready"]}
        else:
            if not args.input or not args.output:
                raise ValueError("Recording and output paths are required.")
            config = read_json(paths.config)
            destination = transcriber.Processor(config).transcribe(Path(args.input), Path(args.output))
            result = {"output": str(destination)}
        emit({"ok": True, "data": result})
    except (Exception, KeyboardInterrupt) as error:
        transcriber.STOP.set()
        message = "Operation cancelled." if isinstance(error, (KeyboardInterrupt, InterruptedError)) else str(error)
        emit({"event": "error", "ok": False, "message": message[-2000:]})
        raise SystemExit(1)
    finally:
        with transcriber.PROCESS_LOCK:
            processes = list(transcriber.OWNED_PROCESSES.values())
        for process in processes:
            transcriber.stop_process(process)


if __name__ == "__main__":
    main()
