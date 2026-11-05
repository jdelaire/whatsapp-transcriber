import json
import os
import re
import selectors
import subprocess
import time

from app_state import auth_status
from transcriber import STOP, execute, start_process, stop_process


class Pairing:
    def __init__(self, config, emit):
        self.config = config
        self.emit = emit

    def run(self, phone):
        phone = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise ValueError("Enter your WhatsApp number with + and country code.")
        config = self.config
        if auth_status(config):
            self.emit({"event": "paired"})
            return
        try:
            doctor = json.loads(execute([config["wacli"], "--store", config["store"], "--read-only", "--json", "doctor"], 15))
            if (doctor.get("data") or {}).get("session_revoked"):
                execute([config["wacli"], "--store", config["store"], "--timeout", "30s", "--json", "auth", "logout"], 35)
        except RuntimeError as error:
            if "not authenticated" not in str(error).lower() and "no such file" not in str(error).lower():
                raise RuntimeError("The previous WhatsApp session could not be reset. Please retry.") from error
        process = start_process([
            config["wacli"], "--store", config["store"], "--json", "--events", "auth", "--phone", phone,
            "--history-days", "1", "--history-max-per-chat", "100", "--idle-exit", "10s"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stderr, selectors.EVENT_READ)
        buffer = b""
        connected = False
        handoff = False
        cancelled = False
        failed = None
        deadline = time.monotonic() + 180

        def event(line):
            nonlocal connected, cancelled, failed
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                return
            name = value.get("event")
            data = value.get("data") or {}
            if name == "pair_code":
                self.emit({"event": "pair_code", "code": data["code"]})
            elif name in ("connected", "pair_success", "authenticated"):
                connected = True
            elif name == "logged_out":
                failed = "WhatsApp rejected this link. Please try linking again."
            elif name == "error" or value.get("success") is False:
                reason = str(value.get("error") or data.get("message") or "WhatsApp linking failed.")
                if handoff and (reason == "context canceled" or reason.endswith(": context canceled")):
                    cancelled = True
                else:
                    failed = reason.replace(phone, "your number").replace(phone.lstrip("+"), "your number")

        try:
            self.emit({"event": "pairing"})
            while True:
                if STOP.is_set():
                    raise InterruptedError("Linking cancelled")
                if time.monotonic() > deadline:
                    raise TimeoutError("Linking timed out. Request a new code and try again.")
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 8192)
                    if chunk:
                        buffer += chunk
                        if len(buffer) > 65536:
                            raise RuntimeError("WhatsApp returned an invalid linking response.")
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            event(line)
                    else:
                        selector.unregister(process.stderr)
                if connected and not handoff and auth_status(config):
                    handoff = True
                    stop_process(process)
                    remainder = process.stderr.read()
                    for line in (buffer + remainder).splitlines():
                        event(line)
                    buffer = b""
                    break
                if failed:
                    raise RuntimeError(failed)
                if process.poll() is not None and not selector.get_map():
                    if buffer:
                        event(buffer)
                    break
            result = process.wait()
            expected = result == 0 or (handoff and cancelled and result == 1)
            if failed or not expected or not auth_status(config):
                raise RuntimeError(failed or "WhatsApp did not finish linking. Please retry.")
            self.emit({"event": "paired"})
        finally:
            selector.close()
            stop_process(process)
            process.stderr.close()
