import hashlib
import json
import sqlite3
import stat
import time
import uuid
from pathlib import Path


PROMPT_VERSION = 1
ERROR_CODES = {"original_missing", "original_unreadable", "unsupported_language", "refused", "invalid_output", "context_limit", "generation_failed"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate_summaries(db, now):
    db.execute("BEGIN IMMEDIATE")
    try:
        if "completed_at" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
            db.execute("ALTER TABLE jobs ADD COLUMN completed_at REAL")
        for sql in (
            "CREATE INDEX IF NOT EXISTS summary_completed_jobs ON jobs(state,key)",
            "CREATE INDEX IF NOT EXISTS summary_new_completed_jobs ON jobs(state,completed_at,key)",
            "CREATE TABLE IF NOT EXISTS summaries (job_key TEXT PRIMARY KEY,source_hash TEXT,file_fingerprint TEXT,prompt_version INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','running','ready','failed')),bullets TEXT NOT NULL DEFAULT '[]',error_code TEXT,attempt_token TEXT,owner_id TEXT,settings_revision INTEGER,priority INTEGER NOT NULL CHECK(priority IN (0,1)),queue_order INTEGER NOT NULL,updated_at REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS summary_due ON summaries(state,priority,queue_order)",
            "CREATE INDEX IF NOT EXISTS summary_queue_order ON summaries(queue_order)",
        ):
            db.execute(sql)
        db.executemany("INSERT OR IGNORE INTO metadata(name,value) VALUES(?,?)", [
            ("summary_schema_version", "1"), ("summary_enabled", "true"),
            ("summary_settings_revision", "0"), ("summary_first_enabled_at", str(now)),
            ("summary_scan_cursor", compact({"after_key": None, "upper_key": None})),
        ])
        first = float(db.execute("SELECT value FROM metadata WHERE name='summary_first_enabled_at'").fetchone()[0])
        db.execute("INSERT OR IGNORE INTO metadata VALUES('summary_new_cursor',?)", (compact({"time": first, "key": ""}),))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def file_fingerprint(output, output_root):
    try:
        if not output:
            raise ValueError("original_missing")
        path = Path(output)
        resolved = path.resolve(strict=True)
        if path.suffix != ".txt" or resolved.suffix != ".txt" or not resolved.is_relative_to(Path(output_root).resolve()):
            raise ValueError("original_unreadable")
        record = resolved.stat()
        if not stat.S_ISREG(record.st_mode):
            raise ValueError("original_unreadable")
        return {"path": str(resolved), "device": record.st_dev, "inode": record.st_ino,
                "size": record.st_size, "mtime_ns": record.st_mtime_ns, "ctime_ns": record.st_ctime_ns}
    except FileNotFoundError:
        raise ValueError("original_missing") from None
    except OSError:
        raise ValueError("original_unreadable") from None


def transcript_body(raw):
    header, separator, body = raw.partition("\n\n")
    lines = header.split("\n")
    if separator and len(lines) == 4 and all(line.startswith(prefix) for line, prefix in zip(lines, ("Chat: ", "Sender: ", "Received: ", "Message: "))):
        return body
    return raw


def read_transcript_body(output, output_root):
    before = file_fingerprint(output, output_root)
    try:
        raw = Path(before["path"]).read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        raise ValueError("original_unreadable") from None
    if before != file_fingerprint(output, output_root):
        raise ValueError("original_unreadable")
    body = transcript_body(raw)
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest(), before


def pending_summary(error=None, fingerprint=None):
    return {"state": "failed" if error else "pending", "bullets": [], "error_code": error,
            "source_hash": None, "source_fingerprint": compact(fingerprint) if fingerprint else None, "prompt_version": PROMPT_VERSION}


def has_summaries(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='summaries'").fetchone() is not None


def read_settings(db):
    values = dict(db.execute("SELECT name,value FROM metadata WHERE name IN ('summary_enabled','summary_settings_revision','summary_first_enabled_at')"))
    return {"enabled": values.get("summary_enabled", "true") == "true", "revision": int(values.get("summary_settings_revision", "0")),
            "first_enabled_at": float(values["summary_first_enabled_at"]) if "summary_first_enabled_at" in values else None}


def project_summary(db, job_key, output, output_root):
    try:
        fingerprint = file_fingerprint(output, output_root)
    except ValueError as error:
        return pending_summary(str(error))
    if not has_summaries(db):
        return pending_summary(fingerprint=fingerprint)
    row = db.execute("SELECT state,bullets,error_code,source_hash,prompt_version,file_fingerprint FROM summaries WHERE job_key=?", (job_key,)).fetchone()
    if not row or row[4] != PROMPT_VERSION or row[5] != compact(fingerprint):
        return pending_summary(fingerprint=fingerprint)
    state, raw, error, digest, version, _ = row
    try:
        bullets = json.loads(raw)
        if state == "ready" and (not isinstance(bullets, list) or not 1 <= len(bullets) <= 3 or any(not isinstance(item, str) or not item.strip() for item in bullets)):
            return pending_summary("invalid_output")
    except (ValueError, TypeError):
        return pending_summary("invalid_output")
    return {"state": state, "bullets": bullets if state == "ready" else [], "error_code": error,
            "source_hash": digest, "source_fingerprint": compact(fingerprint), "prompt_version": version}


class SummaryStore:
    def __init__(self, database, output_root):
        self.database = Path(database)
        self.output_root = Path(output_root)
        self.db = None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def resolve(self, key):
        row = self.db.execute("SELECT target FROM aliases WHERE alias=?", (key,)).fetchone()
        return row[0] if row else key

    def job(self, key):
        key = self.resolve(key)
        row = self.db.execute("SELECT message,output,completed_at FROM jobs WHERE key=? AND state='complete'", (key,)).fetchone()
        if row is None:
            raise ValueError("Transcript unavailable.")
        return key, row

    def metadata(self, name):
        row = self.db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def owned(self, owner):
        return bool(owner) and self.metadata("summary_owner") == compact(owner)

    def reconcile_item(self, key, force=False):
        try:
            key, job = self.job(key)
        except ValueError:
            return False
        old = self.db.execute("SELECT source_hash,file_fingerprint,prompt_version,state,error_code FROM summaries WHERE job_key=?", (key,)).fetchone()
        body, digest, fingerprint, error = None, None, None, None
        try:
            fingerprint = file_fingerprint(job[1] or "", self.output_root)
            if not force and old and old[1] == compact(fingerprint) and old[2] == PROMPT_VERSION:
                return False
            body, digest, fingerprint = read_transcript_body(job[1] or "", self.output_root)
        except ValueError as failure:
            error = str(failure)
            if not force and old and old[0] is None and old[2] == PROMPT_VERSION and old[3] == "failed" and old[4] == error:
                return False
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            current = self.db.execute("SELECT output FROM jobs WHERE key=? AND state='complete'", (key,)).fetchone()
            if current is None or current[0] != job[1] or self.resolve(key) != key:
                return False
            if fingerprint is not None:
                try:
                    if fingerprint != file_fingerprint(job[1], self.output_root):
                        return False
                except ValueError:
                    return False
            current_summary = self.db.execute("SELECT source_hash,prompt_version FROM summaries WHERE job_key=?", (key,)).fetchone()
            if not force and digest is not None and current_summary == (digest, PROMPT_VERSION):
                self.db.execute("UPDATE summaries SET file_fingerprint=?,updated_at=? WHERE job_key=?", (compact(fingerprint), time.time(), key))
                return True
            settings = read_settings(self.db)
            priority = 0 if job[2] is not None and job[2] >= (settings["first_enabled_at"] or 0) else 1
            order = self.db.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM summaries").fetchone()[0]
            self.db.execute("INSERT INTO summaries(job_key,source_hash,file_fingerprint,prompt_version,state,bullets,error_code,priority,queue_order,updated_at) VALUES(?,?,?,?,?,'[]',?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET source_hash=excluded.source_hash,file_fingerprint=excluded.file_fingerprint,prompt_version=excluded.prompt_version,state=excluded.state,bullets='[]',error_code=excluded.error_code,attempt_token=NULL,owner_id=NULL,settings_revision=NULL,priority=excluded.priority,queue_order=excluded.queue_order,updated_at=excluded.updated_at",
                            (key, digest, compact(fingerprint) if fingerprint else None, PROMPT_VERSION, "failed" if error else "pending", error, priority, order, time.time()))
        return True

    def discover_new(self, limit=25):
        first = read_settings(self.db)["first_enabled_at"] or 0
        previous = self.metadata("summary_new_cursor")
        cursor = json.loads(previous) if previous else {"time": first, "key": ""}
        after = (cursor["time"], cursor["key"])
        upper = self.db.execute("SELECT completed_at,key FROM jobs WHERE state='complete' AND completed_at>=? ORDER BY completed_at DESC,key DESC LIMIT 1", (first,)).fetchone()
        if upper is None or upper <= after:
            return 0, False
        rows = self.db.execute("SELECT j.key,j.completed_at FROM jobs j WHERE j.state='complete' AND (j.completed_at,j.key)>(?,?) AND (j.completed_at,j.key)<=(?,?) AND NOT EXISTS(SELECT 1 FROM summaries s WHERE s.job_key=j.key) ORDER BY j.completed_at,j.key LIMIT ?", (*after, *upper, limit)).fetchall()
        changed = False
        next_position = after
        blocked = False
        for key, completed_at in rows:
            changed = self.reconcile_item(key) or changed
            exists = self.db.execute("SELECT 1 FROM summaries WHERE job_key=?", (key,)).fetchone()
            if not exists and self.db.execute("SELECT 1 FROM jobs WHERE key=? AND state='complete'", (key,)).fetchone():
                blocked = True
                break
            next_position = (completed_at, key)
        if not blocked and len(rows) < limit:
            next_position = upper
        if next_position > after:
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                if self.metadata("summary_new_cursor") == previous:
                    self.db.execute("INSERT OR REPLACE INTO metadata VALUES('summary_new_cursor',?)", (compact({"time": next_position[0], "key": next_position[1]}),))
        return len(rows), changed

    def reconcile(self, owner, hint=None):
        if not self.owned(owner):
            return {"visited": 0, "wrapped": False, "changed": False}
        visited, changed = self.discover_new()
        if hint and visited < 25:
            try:
                key, job = self.job(hint)
                if job[2] is None or job[2] < (read_settings(self.db)["first_enabled_at"] or 0) or self.db.execute("SELECT 1 FROM summaries WHERE job_key=?", (key,)).fetchone():
                    changed = self.reconcile_item(key) or changed
                    visited += 1
            except ValueError:
                pass
        cursor = json.loads(self.metadata("summary_scan_cursor"))
        upper = cursor["upper_key"] or self.db.execute("SELECT MAX(key) FROM jobs WHERE state='complete'").fetchone()[0]
        keys = []
        if visited < 25 and upper:
            keys = [row[0] for row in self.db.execute("SELECT key FROM jobs WHERE state='complete' AND key>? AND key<=? ORDER BY key LIMIT ?", (cursor["after_key"] or "", upper, 25 - visited))]
            for key in keys:
                changed = self.reconcile_item(key) or changed
        wrapped = not upper or (visited < 25 and (not keys or keys[-1] == upper or len(keys) < 25 - visited))
        next_cursor = {"after_key": None if wrapped else (keys[-1] if keys else cursor["after_key"]), "upper_key": None if wrapped else upper}
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            if self.owned(owner):
                self.db.execute("UPDATE metadata SET value=? WHERE name='summary_scan_cursor'", (compact(next_cursor),))
                if wrapped:
                    self.db.execute("DELETE FROM summaries WHERE NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.key=summaries.job_key AND jobs.state='complete')")
        return {"visited": visited + len(keys), "wrapped": wrapped, "changed": changed}

    def claim(self, owner):
        if not self.owned(owner) or not read_settings(self.db)["enabled"]:
            return {"claim": None}
        self.discover_new()
        if not self.db.execute("SELECT 1 FROM summaries s WHERE s.state='pending' AND EXISTS(SELECT 1 FROM jobs j WHERE j.key=s.job_key AND j.state='complete') LIMIT 1").fetchone():
            return {"claim": None}
        for _ in range(25):
            row = self.db.execute("SELECT s.job_key,s.source_hash FROM summaries s JOIN jobs j ON j.key=s.job_key WHERE s.state='pending' AND j.state='complete' ORDER BY s.priority,CASE WHEN s.priority=0 THEN j.completed_at END,CASE WHEN s.priority=0 THEN j.key END,s.queue_order LIMIT 1").fetchone()
            if row is None:
                return {"claim": None}
            key, digest = row
            if self.db.execute("SELECT 1 FROM summaries s WHERE s.state='running' AND EXISTS(SELECT 1 FROM jobs j WHERE j.key=s.job_key AND j.state='complete') LIMIT 1").fetchone():
                return {"claim": None}
            self.reconcile_item(key)
            _, job = self.job(key)
            try:
                body, current_hash, fingerprint = read_transcript_body(job[1] or "", self.output_root)
            except ValueError:
                self.reconcile_item(key, force=True)
                continue
            if current_hash != digest:
                continue
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                settings = read_settings(self.db)
                if not settings["enabled"] or not self.owned(owner):
                    return {"claim": None}
                if self.db.execute("SELECT 1 FROM summaries s WHERE s.state='running' AND EXISTS(SELECT 1 FROM jobs j WHERE j.key=s.job_key AND j.state='complete') LIMIT 1").fetchone():
                    return {"claim": None}
                token = str(uuid.uuid4())
                changed = self.db.execute("UPDATE summaries SET state='running',owner_id=?,attempt_token=?,settings_revision=?,updated_at=? WHERE job_key=? AND state='pending' AND source_hash=? AND prompt_version=? AND file_fingerprint=? AND EXISTS(SELECT 1 FROM jobs WHERE key=? AND state='complete' AND output=?)",
                                          (owner, token, settings["revision"], time.time(), key, digest, PROMPT_VERSION, compact(fingerprint), key, job[1])).rowcount
                if changed:
                    return {"claim": {"identity": {"owner": owner, "id": key, "token": token, "source_hash": digest, "prompt_version": PROMPT_VERSION, "settings_revision": settings["revision"]}, "text": body, "max_bullets": 1 if len(body) <= 280 else 3}}
        return {"claim": None}

    def check(self, identity, check_file=True):
        settings = read_settings(self.db)
        if not settings["enabled"] or settings["revision"] != identity.get("settings_revision") or not self.owned(identity.get("owner")) or identity.get("prompt_version") != PROMPT_VERSION:
            return False
        row = self.db.execute("SELECT s.file_fingerprint,j.output FROM summaries s JOIN jobs j ON j.key=s.job_key WHERE s.job_key=? AND s.state='running' AND j.state='complete' AND s.owner_id=? AND s.attempt_token=? AND s.source_hash=? AND s.prompt_version=? AND s.settings_revision=?",
                              (identity.get("id"), identity.get("owner"), identity.get("token"), identity.get("source_hash"), PROMPT_VERSION, settings["revision"])).fetchone()
        if row is None or self.resolve(identity.get("id")) != identity.get("id"):
            return False
        try:
            return not check_file or row[0] == compact(file_fingerprint(row[1], self.output_root))
        except ValueError:
            return False

    def finish(self, request):
        identity = request.get("identity", {})
        outcome = request.get("outcome")
        if outcome not in ("ready", "failed", "interrupted"):
            raise ValueError("Invalid summary outcome.")
        if not self.check(identity, check_file=outcome != "interrupted"):
            if self.owned(identity.get("owner")):
                self.reconcile_item(identity.get("id", ""))
            return {"accepted": False}
        body = ""
        if outcome != "interrupted":
            _, job = self.job(identity["id"])
            try:
                body, digest, _ = read_transcript_body(job[1], self.output_root)
            except ValueError:
                self.reconcile_item(identity["id"], force=True)
                return {"accepted": False}
            if digest != identity["source_hash"]:
                self.reconcile_item(identity["id"], force=True)
                return {"accepted": False}
        bullets = request.get("bullets", [])
        error = request.get("error_code")
        if outcome == "ready" and (not isinstance(bullets, list) or not 1 <= len(bullets) <= (1 if len(body) <= 280 else 3) or any(not isinstance(value, str) or not value.strip() for value in bullets)):
            outcome, error = "failed", "invalid_output"
        if outcome == "failed" and error not in ERROR_CODES:
            error = "generation_failed"
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            if not self.check(identity, check_file=outcome != "interrupted"):
                return {"accepted": False}
            self.db.execute("UPDATE summaries SET state=?,bullets=?,error_code=?,owner_id=NULL,attempt_token=NULL,settings_revision=NULL,updated_at=? WHERE job_key=?", ("pending" if outcome == "interrupted" else outcome, compact(bullets if outcome == "ready" else []), error if outcome == "failed" else None, time.time(), identity["id"]))
        return {"accepted": True}

    def handle(self, command, request):
        if not self.database.is_file():
            raise ValueError("Complete app setup first.")
        readonly = command in ("transcript", "summary-status") or (command == "summary-settings" and "enabled" not in request)
        self.db = sqlite3.connect(self.database.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=1, isolation_level=None)
        try:
            if readonly:
                if hasattr(self.db, "setconfig"):
                    self.db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                self.db.execute("PRAGMA query_only=ON")
                self.db.execute("BEGIN")
            if command == "transcript":
                key, row = self.job(request["id"])
                message = json.loads(row[0])
                summary = project_summary(self.db, key, row[1] or "", self.output_root)
                try:
                    body, digest, fingerprint = read_transcript_body(row[1] or "", self.output_root)
                    if summary["source_hash"] is not None and summary["source_hash"] != digest:
                        summary = pending_summary(fingerprint=fingerprint)
                except ValueError as error:
                    body, digest, fingerprint, summary = None, None, None, pending_summary(str(error))
                return {"id": key, "title": message.get("chat_name") or "WhatsApp chat", "timestamp": message["timestamp"],
                        "path": row[1] or "", "text": body, "source_hash": digest, "source_fingerprint": compact(fingerprint) if fingerprint else None, "summary": summary}
            if command == "summary-status":
                key, row = self.job(request["id"])
                return project_summary(self.db, key, row[1] or "", self.output_root)
            if command == "summary-settings" and readonly:
                return read_settings(self.db)
            if command == "summary-settings":
                enabled = request.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("Invalid summary setting.")
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    settings = read_settings(self.db)
                    if settings["enabled"] != enabled:
                        self.db.execute("UPDATE metadata SET value=? WHERE name='summary_enabled'", (compact(enabled),))
                        self.db.execute("UPDATE metadata SET value=? WHERE name='summary_settings_revision'", (str(settings["revision"] + 1),))
                        if not enabled:
                            self.db.execute("UPDATE summaries SET state='pending',attempt_token=NULL,owner_id=NULL,settings_revision=NULL WHERE state='running'")
                    return read_settings(self.db)
            if command == "summary-owner":
                owner = request.get("owner")
                if not isinstance(owner, str) or not owner:
                    raise ValueError("Invalid summary owner.")
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    recovered = self.db.execute("UPDATE summaries SET state='pending',attempt_token=NULL,owner_id=NULL,settings_revision=NULL WHERE state='running'").rowcount
                    self.db.execute("INSERT OR REPLACE INTO metadata(name,value) VALUES('summary_owner',?)", (compact(owner),))
                return {"recovered": recovered}
            if command == "summary-reconcile":
                return self.reconcile(request.get("owner"), request.get("id"))
            if command == "summary-claim":
                return self.claim(request.get("owner"))
            if command == "summary-check":
                return {"valid": self.check(request.get("identity", {}))}
            if command == "summary-finish":
                return self.finish(request)
            if command == "summary-retry":
                key, _ = self.job(request.get("id", ""))
                state = self.db.execute("SELECT state FROM summaries WHERE job_key=?", (key,)).fetchone()
                if not read_settings(self.db)["enabled"] or state != ("failed",):
                    return {"accepted": False}
                self.reconcile_item(key, force=True)
                return {"accepted": True}
            raise ValueError("Unknown summary operation.")
        finally:
            self.close()
