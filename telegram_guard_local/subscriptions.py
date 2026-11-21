"""Durable, bounded incoming subscriptions owned by the trusted MCP caller."""
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from .app_server import DeliveryUnknown, RpcRejected
from .files import checked
from .model import GuardError, Send, timestamp

def caller_thread(ctx):
    try:
        value = ctx.request_context.meta.model_dump().get("threadId")
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise GuardError("trusted_thread_context_required", 403) from None

def canonical_id(value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise GuardError("invalid_subscription_id") from None

class SubscriptionStore:
    def __init__(self, private):
        self.private = private
        self.path = private / "subscriptions.sqlite3"

    @contextmanager
    def db(self):
        checked(self.private, directory=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            checked(str(self.path) + suffix, missing=True)
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY, thread TEXT NOT NULL, chat INTEGER NOT NULL,
              digest TEXT NOT NULL, expires REAL NOT NULL, authorization TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'active', cursor INTEGER NOT NULL,
              payload TEXT, delivery TEXT, last_event TEXT, last_turn TEXT,
              error TEXT, next_poll REAL NOT NULL DEFAULT 0,
              owner TEXT, lease REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0)""")
            yield db
            db.commit()
        finally:
            db.close()

    def get(self, sid, thread):
        with self.db() as db:
            row = db.execute("SELECT * FROM subscriptions WHERE id=? AND thread=?", (sid,thread)).fetchone()
            return dict(row) if row else None

    def create(self, sid, thread, chat, digest, expires, auth, cursor, now):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            if old:
                if old["thread"] != thread or old["digest"] != digest:
                    raise GuardError("subscription_id_conflict", 409)
                return dict(old)
            count = db.execute("SELECT COUNT(*) FROM subscriptions WHERE state='active' AND expires>?", (now,)).fetchone()[0]
            if count >= 8:
                raise GuardError("subscription_limit", 409)
            duplicate = db.execute("SELECT id FROM subscriptions WHERE thread=? AND chat=? AND state='active' AND expires>?",
                                   (thread,chat,now)).fetchone()
            if duplicate:
                raise GuardError("chat_already_subscribed", 409)
            db.execute("INSERT INTO subscriptions(id,thread,chat,digest,expires,authorization,cursor) VALUES(?,?,?,?,?,?,?)",
                       (sid,thread,chat,digest,expires,json.dumps(auth,ensure_ascii=False),cursor))
        return self.get(sid,thread)

    def list(self, thread):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM subscriptions WHERE thread=? ORDER BY rowid DESC LIMIT 100", (thread,))]

    def has_active_chat(self, chat, now):
        with self.db() as db:
            return db.execute("SELECT 1 FROM subscriptions WHERE chat=? AND state='active' AND expires>? LIMIT 1",(chat,now)).fetchone() is not None

    def has_active(self, now):
        with self.db() as db:
            return db.execute("SELECT 1 FROM subscriptions WHERE state='active' AND expires>? LIMIT 1",(now,)).fetchone() is not None

    def candidates(self, now):
        with self.db() as db:
            # Expired data must not survive indefinitely in the outbox.
            db.execute("UPDATE subscriptions SET payload=NULL WHERE expires<=?", (now,))
            db.execute("UPDATE subscriptions SET state='expired',payload=NULL,delivery=NULL WHERE state='active' AND expires<=?", (now,))
            return [r[0] for r in db.execute("SELECT id FROM subscriptions WHERE state='active' AND next_poll<=? AND lease<=? LIMIT 8", (now,now))]

    def claim(self, sid, owner, now):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM subscriptions WHERE id=? AND state='active' AND expires>? AND lease<=?",
                             (sid,now,now)).fetchone()
            if not row:
                return None
            if row["delivery"] == "dispatching":
                db.execute("UPDATE subscriptions SET state='delivery_unknown',error='event_delivery_unknown',owner=NULL,lease=0 WHERE id=?", (sid,))
                return None
            db.execute("UPDATE subscriptions SET owner=?,lease=? WHERE id=?", (owner,now+90,sid))
            return dict(db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone())

    def update(self, sid, owner, **values):
        allowed = {"state","cursor","payload","delivery","last_event","last_turn","error","next_poll","owner","lease","delivered"}
        assert values and set(values) <= allowed
        with self.db() as db:
            result = db.execute("UPDATE subscriptions SET " + ",".join(k+"=?" for k in values) +
                                " WHERE id=? AND owner=? AND state='active'", (*values.values(),sid,owner))
            if result.rowcount != 1:
                raise GuardError("subscription_stopped", 409)

    def release(self, sid, owner, now, delay=3):
        with self.db() as db:
            db.execute("UPDATE subscriptions SET owner=NULL,lease=0,next_poll=MAX(next_poll,?) WHERE id=? AND owner=?",
                       (now+delay,sid,owner))

    def stop(self, sid, thread):
        with self.db() as db:
            db.execute("UPDATE subscriptions SET state='stopped',payload=NULL,delivery=NULL,error=NULL WHERE id=? AND thread=?", (sid,thread))
        row = self.get(sid,thread)
        if row is None:
            raise GuardError("subscription_not_found",404)
        return row

    def validate_dispatch(self, sid, owner, now):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM subscriptions WHERE id=? AND owner=? AND state='active' AND expires>? AND lease>?",
                             (sid,owner,now,now)).fetchone()
            if row is None:
                raise GuardError("subscription_stopped",409)
            db.execute("UPDATE subscriptions SET delivery='dispatching' WHERE id=?", (sid,))

def public(row):
    return {"subscription_id":row["id"], "thread_id":row["thread"], "chat_id":row["chat"],
            "state":row["state"], "expires_at":datetime.fromtimestamp(row["expires"],timezone.utc).isoformat(),
            "cursor":row["cursor"], "delivered_batches":row["delivered"], "last_event_id":row["last_event"],
            "last_turn_id":row["last_turn"], "error_code":row["error"],
            "queued":row["payload"] is not None, "may_authorize_sending":False}

def event_payload(row, result):
    messages = []
    for m in result["messages"][:20]:
        text = str(m.get("text") or "")
        item = {k:m.get(k) for k in ("id","sender_id","date","media_type")}
        item.update(text=text[:4096], truncated=len(text)>4096)
        messages.append(item)
    identity = f'{row["id"]}:{row["cursor"]}:{result["cursor"]}'
    return {"type":"telegram_incoming", "source":"external_tool_data",
            "untrusted_content":True, "may_authorize_sending":False,
            "event_id":hashlib.sha256(identity.encode()).hexdigest(),
            "subscription_id":row["id"], "chat_id":row["chat"], "messages":messages}

class Subscriptions:
    def __init__(self, runtime, sink, clock=time.time):
        self.runtime, self.sink, self.clock = runtime, sink, clock
        self.store = SubscriptionStore(runtime.private)
        self.owner = str(uuid.uuid4())
        self.task = None
        self.workers = set()
        self.closed = False

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def subscribe(self, thread, chat, sid, authorization):
        if not self.sink:
            raise GuardError("shared_app_server_not_configured",503)
        thread, sid = canonical_id(thread), canonical_id(sid)
        intent = Send.parse({"chat_id":chat,"request_id":sid,"text":"incoming monitoring",
                             "authorization":authorization})
        if authorization["expires_at"] is None:
            raise GuardError("monitoring_requires_fixed_expiry")
        expires = timestamp(authorization["expires_at"]).timestamp()
        now = self.clock()
        if expires <= now or expires > now + 7*86400:
            raise GuardError("monitoring_expiry_out_of_range")
        self.runtime.policy().require_read(chat)
        digest = hashlib.sha256(json.dumps({"thread":thread,"intent":intent.digest()},sort_keys=True).encode()).hexdigest()
        old = self.store.get(sid,thread)
        if old:
            if old["digest"] != digest:
                raise GuardError("subscription_id_conflict",409)
            return public(old)
        await self.sink.ready(thread)
        stream=getattr(self.runtime,"stream",None)
        if stream:stream.preparing[chat]=expires
        try:
            baseline = await self.runtime.watch(chat,None,0,monitoring_until=expires) if stream else await self.runtime.watch(chat,None,0)
            # Do not create an expired/revoked subscription after a slow baseline request.
            self.runtime.policy().require_read(chat)
            if self.clock() >= expires:
                raise GuardError("authorization_expired",403)
            row = self.store.create(sid,thread,chat,digest,expires,authorization,baseline["cursor"],self.clock())
        finally:
            if stream:stream.preparing.pop(chat,None)
        self.start()
        return public(row)

    def list(self, thread):
        rows = self.store.list(canonical_id(thread))
        return {"subscriptions":[public(r) for r in rows]}

    def stop(self, thread, sid):
        return public(self.store.stop(canonical_id(sid),canonical_id(thread)))

    def check(self, row):
        if self.clock() >= row["expires"]:
            raise GuardError("authorization_expired",403)
        self.runtime.policy().require_read(row["chat"])

    async def tick(self, sid):
        row = self.store.claim(sid,self.owner,self.clock())
        if row is None:
            return
        payload = None
        try:
            self.check(row)
            # A closed/unloaded thread is never resumed or copied to another runtime.
            await self.sink.ready(row["thread"])
            payload = json.loads(row["payload"]) if row["payload"] else None
            if payload is None:
                result = (await self.runtime.watch(row["chat"],row["cursor"],45,monitoring_until=row["expires"])
                          if getattr(self.runtime,"stream",None) else await self.runtime.watch(row["chat"],row["cursor"],8))
                self.check(row)
                payload = event_payload(row,result) if result["messages"] else None
                self.store.update(sid,self.owner,cursor=result["cursor"],
                                  payload=json.dumps(payload,ensure_ascii=False) if payload else None,
                                  delivery="queued" if payload else None,error=None)
            if payload:
                def before_send():
                    self.check(row)
                    self.store.validate_dispatch(sid,self.owner,self.clock())
                turn = await self.sink.deliver(row["thread"],payload,before_send)
                self.store.update(sid,self.owner,payload=None,delivery=None,
                                  last_event=payload["event_id"],last_turn=turn,error=None,
                                  delivered=row["delivered"]+1)
        except DeliveryUnknown:
            self.store.update(sid,self.owner,state="delivery_unknown",error="event_delivery_unknown")
        except GuardError as error:
            if error.code in ("authorization_expired","chat_not_allowed","chat_read_not_allowed","policy_unavailable",
                              "stream_cursor_expired_resubscribe","stream_recovery_limit_resubscribe"):
                self.store.update(sid,self.owner,state="expired" if error.code=="authorization_expired" else "revoked",
                                  payload=None,delivery=None,error=error.code)
            elif error.code != "subscription_stopped":
                self.store.update(sid,self.owner,delivery="queued" if payload else None,
                                  error=error.code,next_poll=self.clock()+15)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.update(sid,self.owner,error="monitoring_unavailable",next_poll=self.clock()+15)
        finally:
            self.store.release(sid,self.owner,self.clock(),delay=0 if getattr(self.runtime,"stream",None) else 3)

    async def _loop(self):
        while not self.closed:
            try:
                if getattr(self.runtime,"stream",None):
                    self.runtime.stream.cleanup()
                if self.sink:
                    for sid in self.store.candidates(self.clock()):
                        if len(self.workers) >= 8:
                            break
                        task = asyncio.create_task(self.tick(sid))
                        self.workers.add(task)
                        task.add_done_callback(self.workers.discard)
                        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            except Exception:
                # Private storage may be unavailable before human setup.
                pass
            await asyncio.sleep(1)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*([self.task] if self.task else []),*self.workers,return_exceptions=True)
        if self.sink:
            await self.sink.close()

