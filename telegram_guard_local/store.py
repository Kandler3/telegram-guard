"""Durable idempotency and attempted-send audit. No message bodies or Telegram secrets."""
import sqlite3
import time
import json
from .model import GuardError

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, digest TEXT NOT NULL, chat INTEGER NOT NULL,
          created REAL NOT NULL, state TEXT NOT NULL, message_id INTEGER,
          instruction TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL,
          expires_at TEXT
        )""")
        # Separate table preserves the schema and INSERT layout of existing audits.
        self.db.execute("""CREATE TABLE IF NOT EXISTS failure_diagnostics (
          request_id TEXT PRIMARY KEY, error_code TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS request_media (
          request_id TEXT PRIMARY KEY, message_count INTEGER NOT NULL, message_ids TEXT
        )""")

    def reserve(self, intent, policy):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT digest,state,message_id FROM requests WHERE id=?", (intent.request_id,)).fetchone()
            if row:
                if row[0] != intent.digest():
                    raise GuardError("request_id_conflict", 409)
                self.db.execute("COMMIT")
                if row[1] == "sent":
                    return {"state": "sent", "message_id": row[2], "message_ids": self.message_ids(intent.request_id, row[2]),
                            "request_id": intent.request_id, "replayed": True}
                raise GuardError("delivery_unknown_do_not_resend", 409)
            now = time.time()
            # Pending/unknown attempts also consume quota; failures cannot bypass the cap.
            for duration, limit in ((60, policy.per_minute), (86400, policy.per_day)):
                count = self.db.execute("""SELECT coalesce(sum(coalesce(m.message_count,1)),0)
                    FROM requests r LEFT JOIN request_media m ON m.request_id=r.id
                    WHERE r.created>?""", (now - duration,)).fetchone()[0]
                if count + intent.message_count > limit:
                    raise GuardError("rate_limit", 429)
            a = intent.authorization
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,NULL,?,?,?,?)",
                            (intent.request_id, intent.digest(), intent.chat, now, "pending",
                             a["user_instruction"], a["scope"], a["source"], a["expires_at"]))
            self.db.execute("INSERT INTO request_media VALUES (?,?,NULL)", (intent.request_id, intent.message_count))
            self.db.execute("COMMIT")
            return None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def replay(self, intent):
        """Check a prior operation before doing any preflight network reads."""
        row=self.db.execute("SELECT digest,state,message_id FROM requests WHERE id=?",(intent.request_id,)).fetchone()
        if row is None:return None
        if row[0]!=intent.digest():raise GuardError("request_id_conflict",409)
        if row[1]!="sent":raise GuardError("delivery_unknown_do_not_resend",409)
        return {"state":"sent","message_id":row[2],"message_ids":self.message_ids(intent.request_id,row[2]),
                "request_id":intent.request_id,"replayed":True}

    def finish(self, rid, message_ids):
        if type(message_ids) is int:
            message_ids = [message_ids]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE request_media SET message_ids=? WHERE request_id=?", (json.dumps(message_ids), rid))
            self.db.execute("UPDATE requests SET state='sent', message_id=? WHERE id=?", (message_ids[0], rid))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def message_ids(self, rid, fallback):
        row = self.db.execute("SELECT message_ids FROM request_media WHERE request_id=?", (rid,)).fetchone()
        return json.loads(row[0]) if row and row[0] else ([fallback] if fallback else [])

    def unknown(self, rid, error_code=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE requests SET state='unknown' WHERE id=?", (rid,))
            if error_code:
                self.db.execute("INSERT OR REPLACE INTO failure_diagnostics VALUES (?,?)", (rid,error_code))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def status(self, rid):
        row = self.db.execute("SELECT chat,state,message_id FROM requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise GuardError("request_not_found", 404)
        result = {"chat_id": row[0], "state": row[1], "message_id": row[2], "request_id": rid}
        result["message_ids"] = self.message_ids(rid, row[2])
        diagnostic = self.db.execute("SELECT error_code FROM failure_diagnostics WHERE request_id=?", (rid,)).fetchone()
        if diagnostic:
            result["error_code"] = diagnostic[0]
        return result

    def close(self):
        self.db.close()
