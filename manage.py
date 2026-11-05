import argparse
import getpass
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from transcriber import Queue, execute, stop_process


LABEL = "com.jdelaire.whatsapp-transcriber"
RUNTIME = Path.home() / ".local/share/whatsapp-transcriber"
DATA = Path.home() / "Library/Application Support/WhatsAppTranscriber"
CONFIG = RUNTIME / "config.json"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
TARGET = f"gui/{os.getuid()}/{LABEL}"


def install():
    os.umask(0o077)
    for directory in (RUNTIME, DATA, DATA / "store", DATA / "models", Path.home() / "WhatsApp Transcripts", PLIST.parent):
        directory.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {
        "data": str(DATA), "store": str(DATA / "store"),
        "output": str(Path.home() / "WhatsApp Transcripts"),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "model": str(DATA / "models/ggml-small.bin"),
    }
    for key, executable in (("wacli", "wacli"), ("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("whisper", "whisper-cli"), ("qrencode", "qrencode")):
        path = shutil.which(executable)
        if not path:
            raise SystemExit(f"Missing {executable}. Install dependencies first.")
        config[key] = path
    if not Path(config["model"]).is_file():
        raise SystemExit("Download multilingual ggml-small.bin into the models directory first.")
    source = Path(__file__).resolve().parent
    for name in ("transcriber.py", "manage.py"):
        destination = RUNTIME / name
        if (source / name).resolve() != destination.resolve():
            shutil.copyfile(source / name, destination)
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG.chmod(0o600)
    queue = Queue(DATA / "jobs.sqlite", config["start_time"])
    queue.close()
    plist = {
        "Label": LABEL,
        "ProgramArguments": [shutil.which("python3"), str(RUNTIME / "transcriber.py"), "run", "--config", str(CONFIG)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5,
        "ExitTimeOut": 20,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"},
    }
    PLIST.write_bytes(plistlib.dumps(plist))
    if not (DATA / "store/wacli.db").exists():
        (DATA / "NEEDS_PAIRING").write_text("Scan the WhatsApp pairing QR to start.\n")
    print(f"Installed. Transcripts: {config['output']}")
    print(f"Pair: {shutil.which('python3')} {RUNTIME / 'manage.py'} pair")


def start():
    if (DATA / "NEEDS_PAIRING").exists():
        raise SystemExit("Pair WhatsApp first.")
    result = subprocess.run(["launchctl", "print", TARGET], capture_output=True)
    if result.returncode:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
    else:
        subprocess.run(["launchctl", "kickstart", TARGET], check=True)
    print("Automatic transcription service started.", flush=True)


def stop():
    subprocess.run(["launchctl", "bootout", TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def authenticated(config, command=execute):
    result = command([config["wacli"], "--store", config["store"], "--read-only", "--json", "auth", "status"], 30)
    envelope = json.loads(result)
    if not envelope.get("success"):
        raise RuntimeError("Could not verify WhatsApp authentication")
    return envelope["data"].get("authenticated") is True


def prepare_pair(config, command=execute):
    if not authenticated(config, command):
        try:
            command([config["wacli"], "--store", config["store"], "--timeout", "30s", "--json", "auth", "logout"], 35)
        except RuntimeError as error:
            if "not authenticated" not in str(error).lower():
                raise


def render_qr(config, code, path):
    subprocess.run([config["qrencode"], "-s", "8", "-m", "4", "-o", str(path)], input=code, text=True, check=True)
    terminal = subprocess.run([config["qrencode"], "-t", "ANSIUTF8", "-m", "2"], input=code, text=True, capture_output=True, check=True)
    print("\033[2J\033[H" + terminal.stdout, end="", flush=True)
    print(f"QR ready: {path}", flush=True)


def record_pair_event(event):
    name = event.get("event")
    if name not in {"auth_starting", "qr_code", "pair_code", "connected", "disconnected", "pair_success", "authenticated", "handoff", "error", "logged_out", "process_exit"}:
        return
    entry = {"at": datetime.now(timezone.utc).isoformat(), "event": name}
    if name == "error":
        reason = str(event.get("error") or (event.get("data") or {}).get("message") or "").lower()
        entry["kind"] = next((kind for kind, terms in (
            ("timeout", ("timed out", "timeout")),
            ("passkey", ("passkey",)),
            ("outdated", ("outdated",)),
            ("connection", ("connection", "websocket", "tls")),
        ) if any(term in reason for term in terms)), "unspecified")
    if name == "process_exit" and isinstance(event.get("exit_code"), int):
        entry["exit_code"] = event["exit_code"]
    path = DATA / "pairing-events.jsonl"
    if path.exists() and path.stat().st_size > 131072:
        path.replace(DATA / "pairing-events.previous.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def pair(phone=None):
    if phone is not None:
        phone = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise SystemExit("Use an international WhatsApp number starting with + and country code.")
    os.umask(0o077)
    config = json.loads(CONFIG.read_text())
    stop()
    (DATA / "NEEDS_PAIRING").write_text("WhatsApp pairing required.\n")
    prepare_pair(config)
    qr = DATA / "pairing.png"
    failed = False
    handoff = False
    cancelled_for_handoff = False
    arguments = [
        config["wacli"], "--store", config["store"], "--json", "--events",
        "auth", "--history-days", "1", "--history-max-per-chat", "100", "--idle-exit", "10s"
    ]
    if phone is not None:
        arguments += ["--phone", phone]
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        for line in process.stderr:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_pair_event(event)
            if event.get("event") == "qr_code":
                render_qr(config, event["data"]["code"], qr)
            elif event.get("event") == "pair_code":
                print(f"Pairing code: {event['data']['code']}", flush=True)
                print("On your phone: Linked devices, Link a device, Link with phone number. Enter this code.", flush=True)
            elif event.get("event") in ("connected", "pair_success", "authenticated"):
                if not handoff and authenticated(config):
                    handoff = True
                    print("WhatsApp linked. Starting background capture; history will continue there.", flush=True)
                    record_pair_event({"event": "handoff"})
                    stop_process(process)
                elif not handoff:
                    print("WhatsApp connected. Waiting for the linked session to be saved.", flush=True)
            elif event.get("event") in ("logged_out", "error") or event.get("success") is False:
                reason = event.get("error") or (event.get("data") or {}).get("message") or event.get("event") or "Pairing was rejected"
                if handoff and event.get("event") == "error" and (reason == "context canceled" or reason.endswith(": context canceled")):
                    cancelled_for_handoff = True
                    continue
                failed = True
                print(f"Pairing failed: {reason}", flush=True)
        result = process.wait()
        record_pair_event({"event": "process_exit", "exit_code": result})
        if handoff and result == -9:
            raise SystemExit("Initial sync did not stop gracefully. Session is preserved; background capture was not started.")
        expected_exit = result == 0 or (handoff and cancelled_for_handoff and result == 1)
        if failed or not expected_exit:
            raise SystemExit(f"Pairing exited with code {result}. Run pair again.")
        if not authenticated(config):
            raise SystemExit("WhatsApp has not authenticated this receiver. Run pair again.")
        (DATA / "NEEDS_PAIRING").unlink(missing_ok=True)
        start()
    except KeyboardInterrupt:
        print("Pairing cancelled.", flush=True)
    finally:
        stop_process(process)
        process.stderr.close()
        qr.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "pair", "start", "stop", "status"])
    phone = parser.add_mutually_exclusive_group()
    phone.add_argument("--phone", action="store_true", help="privately prompt for your WhatsApp number")
    phone.add_argument("--phone-file", type=Path, help="read your WhatsApp number from a private file")
    args = parser.parse_args()
    if (args.phone or args.phone_file) and args.command != "pair":
        parser.error("phone options are only supported with pair")
    if args.command == "install":
        install()
    elif args.command == "pair":
        number = args.phone_file.read_text().strip() if args.phone_file else getpass.getpass("WhatsApp number with country code: ") if args.phone else None
        pair(phone=number)
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
        print("Automatic transcription stopped.")
    else:
        subprocess.run([sys.executable, str(RUNTIME / "transcriber.py"), "status", "--config", str(CONFIG)], check=True)
        result = subprocess.run(["launchctl", "print", TARGET], capture_output=True, text=True)
        print("Login service loaded." if result.returncode == 0 else "Login service not loaded.")


if __name__ == "__main__":
    main()
