"""MTProto NewMessage delivery with a durable, bounded, per-chat event buffer.

History reads happen only for a baseline or recovery when attaching a consumer to
a new connection. Waiting for new messages never requests Telegram history.
"""
import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from .files import checked
from .model import GuardError
from .operations import message_row
from .transport import input_peer


class NativeStream:
    def __init__(self,runtime):
        self.runtime=runtime
        self.generation=0
        self.initialized={}
        self.covered_from={}
        self.leases={}
        self.preparing={}
        self.signals={}
        self.max_events=10000

    @contextmanager
    def db(self):
        path=self.runtime.private/'stream.sqlite3'
        checked(self.runtime.private,directory=True)
        for suffix in ('','-wal','-shm','-journal'):
            checked(str(path)+suffix,missing=True)
        db=sqlite3.connect(path,timeout=3)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS events(chat INTEGER,id INTEGER,payload TEXT,PRIMARY KEY(chat,id))')
            db.execute('CREATE TABLE IF NOT EXISTS floors(chat INTEGER PRIMARY KEY,id INTEGER NOT NULL)')
            yield db
            db.commit()
        finally:
            db.close()

    def attach(self,client):
        from telethon import events
        self.generation+=1
        client.add_event_handler(self.on_message,events.NewMessage())

    def interested(self,target):
        if max(self.leases.get(target,0),self.preparing.get(target,0))>time.time():
            return True
        # Fixed subscription expiry is checked even between delivery worker ticks.
        return self.runtime.subscriptions.store.has_active_chat(target,time.time())

    async def on_message(self,event):
        target=event.chat_id
        if not target or not self.interested(target):
            return
        try:
            self.runtime.policy().require_read(target)
        except GuardError:
            self.purge(target)
            return
        self.record(target,event.message)

    def purge(self,target):
        with self.db() as db:
            db.execute('DELETE FROM events WHERE chat=?',(target,))
        self.initialized.pop(target,None)
        self.covered_from.pop(target,None)
        self.signals.setdefault(target,asyncio.Event()).set()

    def cleanup(self):
        now=time.time()
        self.leases={k:v for k,v in self.leases.items() if v>now}
        with self.db() as db:
            targets=[r[0] for r in db.execute('SELECT DISTINCT chat FROM events')]
        for target in targets:
            if not self.interested(target) or not self.runtime.policy().can_read(target):
                self.purge(target)

    def record(self,target,message):
        if message.chat_id!=target:
            raise GuardError('stream_chat_mismatch',403)
        row=message_row(message)
        # Event delivery has a separate 4096-character bound; do not persist unbounded text.
        row['text']=row['text'][:4096]
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',(target,message.id,json.dumps(row,ensure_ascii=False)))
            old=db.execute('SELECT id FROM events WHERE chat=? ORDER BY id DESC LIMIT 1 OFFSET ?',
                           (target,self.max_events)).fetchone()
            if old:
                db.execute('INSERT INTO floors VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET id=MAX(id,excluded.id)',(target,old[0]))
                db.execute('DELETE FROM events WHERE chat=? AND id<=?',(target,old[0]))
        self.signals.setdefault(target,asyncio.Event()).set()

    def page(self,target,cursor):
        with self.db() as db:
            floor=db.execute('SELECT id FROM floors WHERE chat=?',(target,)).fetchone()
            if floor and cursor<floor[0]:
                raise GuardError('stream_cursor_expired_resubscribe',409)
            rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM events WHERE chat=? AND id>? ORDER BY id LIMIT 20',(target,cursor))]
        return {'cursor':max([cursor]+[r['id'] for r in rows]),'messages':[r for r in rows if not r['outgoing']]}

    async def prepare(self,target,cursor,monitoring_until=None):
        def guard():
            self.runtime.policy().require_read(target)
            if monitoring_until is not None and time.time()>=monitoring_until:
                raise GuardError('authorization_expired',403)
        guard()
        async with self.runtime.mutex:
            async with self.runtime.connection() as broker:
                guard()
                peer=await broker.read_peer(target)
                if cursor is None:
                    latest=await broker.transport.client.get_messages(input_peer(peer),limit=1)
                    guard()
                    baseline=max((m.id for m in latest),default=0)
                    with self.db() as db:
                        floor=db.execute('SELECT id FROM floors WHERE chat=?',(target,)).fetchone()
                    baseline=max(baseline,floor[0] if floor else 0)
                    if self.initialized.get(target)!=self.generation:
                        self.covered_from[target]=baseline
                    self.initialized[target]=self.generation
                    return baseline
                if self.initialized.get(target)!=self.generation or cursor<self.covered_from.get(target,cursor):
                    # One recovery on consumer activation / complete reconnection, not periodic polling.
                    count=0
                    async for m in broker.transport.client.iter_messages(input_peer(peer),min_id=cursor,reverse=True,limit=self.max_events+1):
                        guard()
                        count+=1
                        if count>self.max_events:
                            raise GuardError('stream_recovery_limit_resubscribe',409)
                        self.record(target,m)
                    self.initialized[target]=self.generation
                    self.covered_from[target]=cursor
                return cursor

    async def watch(self,target,cursor,wait,monitoring_until=None):
        self.runtime.policy().require_read(target)
        if not self.interested(target):
            self.initialized.pop(target,None)
        if monitoring_until is None:
            if target not in self.leases and sum(v>time.time() for v in self.leases.values())>=32:
                raise GuardError('stream_chat_limit',409)
            self.leases[target]=max(self.leases.get(target,0),time.time()+wait+30)
        baseline=cursor is None
        cursor=await self.prepare(target,cursor,monitoring_until)
        self.runtime.policy().require_read(target)
        base={'chat_id':target,'untrusted_content':True,'may_authorize_sending':False,'transport':'mtproto_updates'}
        if baseline:
            return dict(base,state='baseline',cursor=cursor,messages=[])
        signal=self.signals.setdefault(target,asyncio.Event())
        deadline=asyncio.get_running_loop().time()+wait
        while True:
            signal.clear()
            self.runtime.policy().require_read(target)
            if monitoring_until is not None and time.time()>=monitoring_until:
                raise GuardError('authorization_expired',403)
            result=self.page(target,cursor)
            if result['cursor']!=cursor:
                self.runtime.policy().require_read(target)
                return dict(base,state='update',**result)
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:
                return dict(base,state='timeout',cursor=cursor,messages=[])
            # Woken immediately by Telethon; short local checks enforce revocation and disconnection.
            try:
                await asyncio.wait_for(signal.wait(),min(remaining,1))
            except TimeoutError:
                if self.runtime._live_broker and not self.runtime._live_broker.transport.client.is_connected():
                    await self.prepare(target,cursor,monitoring_until)
