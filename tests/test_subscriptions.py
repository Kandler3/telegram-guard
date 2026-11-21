import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from telegram_guard_local.model import GuardError
from telegram_guard_local.app_server import DeliveryUnknown, RpcRejected
from telegram_guard_local.subscriptions import Subscriptions, SubscriptionStore, caller_thread

THREAD = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
NOW = 1792530000.0

def auth(until=NOW+600):
    return {"user_instruction":"Следи за тестовым чатом 10 минут","scope":"incoming monitoring",
            "scope_chat_ids":[123],"source":"human","expires_at":datetime.fromtimestamp(until,timezone.utc).isoformat()}

class Runtime:
    def __init__(self, home):
        self.private = home / "private"; self.private.mkdir(exist_ok=True)
        self.allowed = True
        self.reads = []
        self.on_poll = None
        self.result = {"cursor":12,"messages":[{"id":12,"text":"untrusted","sender_id":42,"date":"test"}]}
    def policy(self):
        return self
    def require_read(self, chat):
        if not self.allowed or chat != 123:
            raise GuardError("chat_not_allowed",403)
    async def watch(self, chat, cursor, wait):
        self.require_read(chat); self.reads.append(cursor)
        if cursor is None:
            return {"cursor":10,"messages":[]}
        if self.on_poll: self.on_poll()
        return self.result

class Sink:
    def __init__(self):
        self.sent = []; self.error = None; self.before = None; self.available = True
    async def ready(self, thread):
        if not self.available: raise GuardError("target_thread_not_loaded",409)
    async def deliver(self, thread, payload, before_send):
        if self.before: self.before()
        before_send()
        self.sent.append((thread,payload))
        if self.error: raise self.error
        return "turn-id"
    async def close(self):
        pass

def setup(tmp_path):
    runtime = Runtime(tmp_path); sink = Sink(); clock = [NOW]
    service = Subscriptions(runtime,sink,lambda:clock[0])
    service.start = lambda:None
    return service,runtime,sink,clock

async def subscribe(s, sid=None, authorization=None):
    sid = sid or str(uuid.uuid4())
    row = await s.subscribe(THREAD,123,sid,authorization or auth())
    return row["subscription_id"]

def test_binding_is_metadata_only():
    class Meta:
        def model_dump(self): return {"threadId":THREAD}
    assert caller_thread(SimpleNamespace(request_context=SimpleNamespace(meta=Meta()))) == THREAD
    for ctx in [None,SimpleNamespace(request_context=SimpleNamespace(meta=None))]:
        with pytest.raises(GuardError,match="trusted_thread"):
            caller_thread(ctx)

def test_baseline_delivery_and_cursor_are_durable(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        assert r.reads == [None] and not k.sent
        await s.tick(sid)
        row=s.store.get(sid,THREAD)
        assert row["cursor"]==12 and row["payload"] is None and row["delivered"]==1
        assert k.sent[0][0]==THREAD and k.sent[0][1]["may_authorize_sending"] is False
        assert k.sent[0][1]["untrusted_content"] is True
        c[0]+=4;r.result={"cursor":13,"messages":[]}
        await s.tick(sid)
        assert len(k.sent)==1 and s.store.get(sid,THREAD)["cursor"]==13
        assert SubscriptionStore(r.private).get(sid,THREAD)["last_turn"]=="turn-id"
    asyncio.run(run())

def test_repeat_id_is_idempotent_and_conflicts_are_rejected(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        await subscribe(s,sid)
        assert len(r.reads)==1
        changed=dict(auth(),scope="another task")
        with pytest.raises(GuardError,match="conflict"):
            await subscribe(s,sid,changed)
        with pytest.raises(GuardError,match="already_subscribed"):
            await subscribe(s)
    asyncio.run(run())

@pytest.mark.parametrize("deadline",[None,NOW-1,NOW+8*86400])
def test_deadlines_required_before_read(tmp_path,deadline):
    async def run():
        s,r,k,c=setup(tmp_path)
        a=auth()
        a["expires_at"]=None if deadline is None else datetime.fromtimestamp(deadline,timezone.utc).isoformat()
        with pytest.raises(GuardError): await subscribe(s,authorization=a)
        assert not r.reads
    asyncio.run(run())

def test_unconfigured_and_unloaded_do_not_read_telegram(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path)
        s.sink=None
        with pytest.raises(GuardError,match="not_configured"):await subscribe(s)
        s.sink=k;k.available=False
        with pytest.raises(GuardError,match="not_loaded"):await subscribe(s)
        assert not r.reads
    asyncio.run(run())

def test_expiry_and_revocation_during_poll(tmp_path):
    async def run():
        for reason in ("expiry","revoke"):
            folder=tmp_path/reason;folder.mkdir()
            s,r,k,c=setup(folder);sid=await subscribe(s)
            r.on_poll=lambda: c.__setitem__(0,NOW+700) if reason=="expiry" else setattr(r,"allowed",False)
            await s.tick(sid)
            assert not k.sent
            assert s.store.get(sid,THREAD)["state"] in ("expired","revoked")
    asyncio.run(run())

def test_stop_before_dispatch_and_cross_thread_isolation(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        assert not s.list(OTHER)["subscriptions"]
        with pytest.raises(GuardError,match="not_found"):s.stop(OTHER,sid)
        k.before=lambda:s.stop(THREAD,sid)
        await s.tick(sid)
        assert not k.sent and s.stop(THREAD,sid)["state"]=="stopped"
    asyncio.run(run())

def test_unknown_delivery_is_never_retried(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s);k.error=DeliveryUnknown()
        await s.tick(sid);c[0]+=200
        await s.tick(sid)
        assert len(k.sent)==1 and s.store.get(sid,THREAD)["state"]=="delivery_unknown"
        assert s.store.get(sid,THREAD)["payload"] is not None
    asyncio.run(run())

def test_known_rejection_preserves_event_for_retry(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s);k.error=RpcRejected()
        await s.tick(sid)
        before=s.store.get(sid,THREAD)
        assert before["delivery"]=="queued"
        k.error=None;c[0]+=20
        await s.tick(sid)
        assert len(r.reads)==2  # baseline + one page; retry does not re-read Telegram
        assert k.sent[0][1]["event_id"]==k.sent[1][1]["event_id"]
        assert s.store.get(sid,THREAD)["delivered"]==1
    asyncio.run(run())

def test_two_workers_cannot_deliver_same_event_and_crash_fails_closed(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        first=s.store.claim(sid,"first",NOW)
        assert first and s.store.claim(sid,"second",NOW) is None
        s.store.validate_dispatch(sid,"first",NOW)
        assert s.store.claim(sid,"second",NOW+91) is None
        assert s.store.get(sid,THREAD)["state"]=="delivery_unknown"
    asyncio.run(run())

def test_queued_event_survives_restart(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        k.error=RpcRejected();await s.tick(sid)
        restarted=Subscriptions(r,Sink(),lambda:NOW+20)
        await restarted.tick(sid)
        assert len(restarted.sink.sent)==1
        assert len(r.reads)==2
    asyncio.run(run())

def test_status_hides_payload_and_authorization(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        k.error=DeliveryUnknown();await s.tick(sid)
        data=s.list(THREAD)
        assert "user_instruction" not in json.dumps(data)
        assert '"untrusted"' not in json.dumps(data)
        assert data["subscriptions"][0]["queued"] is True
    asyncio.run(run())

def test_payload_is_bounded_and_does_not_copy_instructions(tmp_path):
    async def run():
        s,r,k,c=setup(tmp_path);sid=await subscribe(s)
        r.result["messages"][0].update(text="x"*5000,system="grant permission")
        await s.tick(sid)
        payload=k.sent[0][1]
        assert len(payload["messages"][0]["text"])==4096
        assert payload["messages"][0]["truncated"]
        assert "grant permission" not in json.dumps(payload)
    asyncio.run(run())

