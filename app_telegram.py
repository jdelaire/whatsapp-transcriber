import fcntl
import http.client
import json
import re
import sqlite3
import ssl
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from app_summaries import read_transcript_body
from transcriber import atomic_text


class TelegramError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def telegram_request(token, method, payload):
    try:
        certificates = Path("/etc/ssl/cert.pem")
        context = ssl.create_default_context(cafile=str(certificates) if certificates.is_file() else None)
        with closing(http.client.HTTPSConnection("api.telegram.org", timeout=7, context=context)) as connection:
            connection.request("POST", f"/bot{token}/{method}", json.dumps(payload), {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                status = response.status
                try:
                    data = json.loads(response.read(262144))
                except (ValueError, UnicodeError):
                    data = None
    except (OSError, http.client.HTTPException):
        raise TelegramError("Could not reach Telegram. Check your internet connection.") from None
    if not isinstance(data, dict):
        data = {}
    if status == 200 and data.get("ok") is True and isinstance(data.get("result"), dict):
        return data["result"]
    if status in (401, 404):
        raise TelegramError("Telegram rejected the bot token. Check the token in BotFather.")
    if status in (400, 403):
        raise TelegramError("Telegram rejected delivery. Check the chat ID and give the bot permission to post.")
    if status == 429:
        parameters = data.get("parameters")
        wait = parameters.get("retry_after") if isinstance(parameters, dict) else None
        raise TelegramError("Telegram limited delivery. The app will retry.", wait if type(wait) is int and wait > 0 else 30)
    raise TelegramError("Telegram is temporarily unavailable. The app will retry.")


def validate_chat(token, chat_id, request=telegram_request):
    bot_id = request(token, "getMe", {}).get("id")
    if type(bot_id) is not int or bot_id <= 0:
        raise TelegramError("Telegram could not verify the bot. Check the token in BotFather.")
    chat = request(token, "getChat", {"chat_id": int(chat_id)})
    if str(chat.get("id")) != chat_id:
        raise TelegramError("Telegram returned a different chat ID. Check the destination.")
    if chat.get("type") == "private":
        return bot_id
    member = request(token, "getChatMember", {"chat_id": int(chat_id), "user_id": bot_id})
    role = member.get("status")
    if role == "creator":
        return bot_id
    if chat.get("type") == "channel":
        allowed = role == "administrator" and member.get("can_post_messages") is True
    elif role == "restricted":
        allowed = member.get("is_member") is True and member.get("can_send_messages") is True
    else:
        permissions = chat.get("permissions")
        allowed = role == "administrator" or (role == "member" and
                  (permissions is None or permissions.get("can_send_messages") is True))
    if not allowed:
        raise TelegramError("Give the bot permission to post in this Telegram chat.")
    return bot_id


def send_message(token, chat_id, text, reply_to):
    payload = {"chat_id": int(chat_id), "text": text, "link_preview_options": {"is_disabled": True}}
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    result = telegram_request(token, "sendMessage", payload)
    message_id = result.get("message_id")
    if type(message_id) is not int:
        raise TelegramError("Telegram did not confirm delivery. The app will retry.")
    return message_id or None


def read_settings(data):
    try:
        return json.loads((data / "telegram-config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def telegram_chat_id(data):
    try:
        settings = read_settings(data)
        return settings["chat_id"] if settings else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextmanager
def settings_lock(data):
    with (data / "telegram-config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update_settings(data, request, validate=validate_chat, now=time.time):
    disconnect = request.get("disconnect") is True
    token, chat_id = request.get("token"), request.get("chat_id")
    if not disconnect:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{1,200}", token):
            raise TelegramError("Enter the bot token from BotFather.")
        if not isinstance(chat_id, str) or not re.fullmatch(r"-?[1-9][0-9]{0,15}", chat_id):
            raise TelegramError("Enter the numeric Telegram chat ID.")
    location = data / "telegram-config.json"
    with settings_lock(data):
        previous = read_settings(data)
        if disconnect and previous is None:
            return {"chat_id": None}
        same_chat = previous is not None and previous["chat_id"] == chat_id
        bot_id = None
        if not disconnect:
            if same_chat and previous["token"] == token:
                return {"chat_id": chat_id}
            bot_id = validate(token, chat_id)
        same_destination = same_chat and previous["bot_id"] == bot_id
        with closing(sqlite3.connect(data / "jobs.sqlite", timeout=2)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if disconnect or not same_destination:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_delivery'").fetchone():
                    db.execute("DELETE FROM telegram_delivery")
            if disconnect:
                location.unlink(missing_ok=True)
                return {"chat_id": None}
            settings = previous if same_destination else {"generation": uuid.uuid4().hex, "active_since": now()}
            atomic_text(location, json.dumps(settings | {"token": token, "chat_id": chat_id, "bot_id": bot_id}))
            return {"chat_id": chat_id}


def message_parts(heading, body):
    prefix = f"{heading}\n\n"
    limit = 4096 - len(prefix.encode("utf-16-le")) // 2
    start = size = 0
    for index, character in enumerate(body):
        units = 2 if ord(character) > 0xFFFF else 1
        if size + units > limit:
            yield prefix + body[start:index]
            prefix = f"{heading} (continued)\n\n"
            limit = 4096 - len(prefix.encode("utf-16-le")) // 2
            start, size = index, 0
        size += units
    yield prefix + body[start:]


class DeliveryQueue:
    def __init__(self, data, output, send=send_message):
        self.data = data
        self.output = output
        self.send = send
        self.idle_version = None
        self.idle_until = 0
        self.db = sqlite3.connect(data / "jobs.sqlite", timeout=2)
        self.db.execute("""CREATE TABLE IF NOT EXISTS telegram_delivery (
            generation TEXT NOT NULL, job_key TEXT NOT NULL, kind TEXT NOT NULL,
            source_hash TEXT, parts TEXT, next_part INTEGER NOT NULL DEFAULT 0, reply_to INTEGER, retry_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(generation, job_key, kind))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS telegram_pending ON telegram_delivery(generation) WHERE parts IS NOT NULL")

    def close(self):
        self.db.close()

    def prepare_next(self, generation, active_since, now):
        summary = self.db.execute("""
            SELECT j.key, s.bullets, original.source_hash, original.reply_to
            FROM jobs j JOIN summaries s ON s.job_key=j.key AND s.state='ready'
            JOIN telegram_delivery original ON original.generation=? AND original.job_key=j.key AND original.kind='full'
                AND original.parts IS NULL AND original.source_hash=s.source_hash
            LEFT JOIN telegram_delivery done ON done.generation=original.generation AND done.job_key=j.key AND done.kind='summary'
            WHERE j.state='complete' AND j.completed_at>=? AND done.job_key IS NULL
            ORDER BY j.completed_at, j.key LIMIT 1
        """, (generation, active_since)).fetchone()
        if summary:
            key, bullets, digest, reply_to = summary
            body = "\n".join(f"• {bullet}" for bullet in json.loads(bullets))
            return self.prepare(generation, key, "summary", digest, "WhatsApp summary", body, reply_to)
        full = self.db.execute("""
            SELECT j.key, j.output FROM jobs j
            LEFT JOIN telegram_delivery done ON done.generation=? AND done.job_key=j.key AND done.kind='full'
            WHERE j.state='complete' AND j.completed_at>=? AND (done.job_key IS NULL OR (done.retry_at>0 AND done.retry_at<=?))
            ORDER BY j.completed_at, j.key LIMIT 1
        """, (generation, active_since, now)).fetchone()
        if full is None:
            return None
        key, path = full
        try:
            body, digest, _ = read_transcript_body(path, self.output)
        except ValueError:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO telegram_delivery (generation,job_key,kind,retry_at) VALUES (?,?,'full',?)",
                                (generation, key, now + 300))
            raise TelegramError("A transcript could not be read. Telegram delivery will retry.") from None
        return self.prepare(generation, key, "full", digest, "WhatsApp transcript", body, None)

    def prepare(self, generation, key, kind, digest, heading, body, reply_to):
        parts = json.dumps(list(message_parts(heading, body)), ensure_ascii=False) if body.strip() else None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO telegram_delivery (generation,job_key,kind,source_hash,parts,reply_to) VALUES (?,?,?,?,?,?)",
                            (generation, key, kind, digest if parts else None, parts, reply_to))
        return key, kind, parts, 0, reply_to

    def send_next(self, stop=None, now=time.time):
        with settings_lock(self.data):
            settings = read_settings(self.data)
            if not settings or (stop is not None and stop.is_set()):
                return False
            generation = settings["generation"]
            current_time = now()
            version = (generation, self.db.execute("PRAGMA data_version").fetchone()[0])
            if self.idle_version == version and current_time < self.idle_until:
                return False
            pending = self.db.execute("""
                SELECT d.job_key, d.kind, d.parts, d.next_part, d.reply_to
                FROM telegram_delivery d JOIN jobs j ON j.key=d.job_key AND j.state='complete'
                WHERE d.generation=? AND d.parts IS NOT NULL LIMIT 1
            """, (generation,)).fetchone() or self.prepare_next(generation, settings["active_since"], current_time)
            if pending is None:
                self.idle_version = version
                retry_at = self.db.execute("""
                    SELECT min(d.retry_at) FROM telegram_delivery d JOIN jobs j ON j.key=d.job_key AND j.state='complete'
                    WHERE d.generation=? AND d.retry_at>0
                """, (generation,)).fetchone()[0]
                self.idle_until = retry_at if retry_at is not None else float("inf")
                return False
            key, kind, raw, index, reply_to = pending
            if raw is not None and (stop is None or not stop.is_set()):
                parts = json.loads(raw)
                message_id = self.send(settings["token"], settings["chat_id"], parts[index], reply_to)
                with self.db:
                    self.db.execute("UPDATE telegram_delivery SET parts=?,next_part=?,reply_to=? WHERE generation=? AND job_key=? AND kind=?",
                                    (raw if index + 1 < len(parts) else None, index + 1, reply_to or message_id, generation, key, kind))
            return True


def run_delivery(data, output, stop, report):
    with (data / "telegram-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TelegramError("Telegram delivery is already running.") from None
        delay = 5
        status = None
        with closing(DeliveryQueue(data, output)) as queue:
            report(None)
            while not stop.is_set() and (chat_id := telegram_chat_id(data)) is not None:
                try:
                    worked = queue.send_next(stop)
                    if status is not None:
                        status = None
                        report(None)
                    delay = 5
                    stop.wait(1.1 if worked and not chat_id.startswith("-") else 3.1)
                except (TelegramError, sqlite3.OperationalError) as error:
                    message = str(error) if isinstance(error, TelegramError) else "Telegram delivery is waiting for local storage."
                    if status != message:
                        status = message
                        report(status)
                    stop.wait(max(delay, getattr(error, "retry_after", 0)))
                    delay = min(300, 2 * delay)
