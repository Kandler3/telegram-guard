import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from telegram_guard_local.engine import Broker
from telegram_guard_local.files import write_json
from telegram_guard_local.model import GuardError
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.transport import TelegramTransport


def policy():
    return {'version':2,'chats':[{'chat_id':123,'label':'Synthetic','kind':'user','entity_id':123,'access_hash':456}],
            'messages_per_minute':60,'messages_per_day':1000,'read':{'default':True,'allow':[],'deny':[]},'write':{'allow':[123]}}


def message(mid,out=False,target=123,text='synthetic'):
    return SimpleNamespace(id=mid,chat_id=target,sender_id=777,out=out,message=text,date=datetime.now(timezone.utc),
                           media=None,file=None,poll=None)


class Client:
    def __init__(self):
        self.baselines=0;self.recoveries=0;self.handlers=[];self.recovery=[];self.connected=True
    def add_event_handler(self,handler,builder):self.handlers.append(handler)
    def is_connected(self):return self.connected
    async def get_messages(self,peer,**kwargs):
        assert kwargs=={'limit':1}
        self.baselines+=1;return [message(10)]
    async def iter_messages(self,peer,**kwargs):
        self.recoveries+=1
        for item in self.recovery:yield item


class FakeRuntime(Runtime):
    def __init__(self,home):
        (home/'private').mkdir()
        write_json(home/'whitelist.json',policy())
        super().__init__(home,persistent=True)
        self.client=Client()
        self.stream.attach(self.client)
        self._live_broker=Broker(self.policy,TelegramTransport(self.client),None)
    @asynccontextmanager
    async def connection(self):yield self._live_broker


def test_native_updates_wake_waiter_without_history_polling_and_release_mutex(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        assert (await r.watch(123,None,0))['cursor']==10
        pending=asyncio.create_task(r.watch(123,10,2))
        await asyncio.sleep(.02)
        async with asyncio.timeout(.2):
            async with r.mutex:pass
        assert not pending.done()
        await r.client.handlers[0](SimpleNamespace(chat_id=123,message=message(11)))
        result=await pending
        assert result['transport']=='mtproto_updates' and result['messages'][0]['id']==11
        for _ in range(5):assert (await r.watch(123,11,0))['state']=='timeout'
        assert r.client.baselines==1 and r.client.recoveries==0
    asyncio.run(run())


def test_recovery_once_per_connection_deduplicates_and_preserves_bursts(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        r.client.recovery=[message(i,out=i==25) for i in range(11,41)]
        first=await r.watch(123,10,0)
        assert first['cursor']==30 and len(first['messages'])==19
        second=await r.watch(123,30,0)
        assert [m['id'] for m in second['messages']]==list(range(31,41))
        assert r.client.recoveries==1
        r.stream.attach(r.client)
        await r.watch(123,10,0)
        assert r.client.recoveries==2
        with r.stream.db() as db:assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==30
    asyncio.run(run())


def test_unrequested_and_blacklisted_chats_are_not_buffered(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        await r.stream.on_message(SimpleNamespace(chat_id=999,message=message(1,target=999)))
        await r.watch(123,None,0)
        await r.stream.on_message(SimpleNamespace(chat_id=123,message=message(11)))
        raw=policy();raw['read']['deny']=[123];write_json(tmp_path/'whitelist.json',raw)
        await r.stream.on_message(SimpleNamespace(chat_id=123,message=message(12)))
        with r.stream.db() as db:assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
        with pytest.raises(GuardError,match='chat_read_not_allowed'):await r.watch(123,10,0)
    asyncio.run(run())


def test_stream_overflow_fails_explicitly_instead_of_silently_skipping(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);await r.watch(123,None,0)
        r.stream.max_events=2
        for i in range(11,15):r.stream.record(123,message(i))
        with pytest.raises(GuardError,match='cursor_expired'):await r.watch(123,10,0)
        assert (await r.watch(123,12,0))['cursor']==14
    asyncio.run(run())


def test_buffer_survives_runtime_restart(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);await r.watch(123,None,0);r.stream.record(123,message(11))
        restarted=Runtime(tmp_path,persistent=True)
        assert restarted.stream.page(123,10)['messages'][0]['id']==11
    asyncio.run(run())


def test_fixed_monitoring_does_not_create_unbounded_watch_interest(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        import time
        await r.watch(123,None,0,monitoring_until=time.time()+100)
        assert not r.stream.interested(123)
        await r.stream.on_message(SimpleNamespace(chat_id=123,message=message(11)))
        with r.stream.db() as db:assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    asyncio.run(run())


def test_older_consumer_recovers_even_after_another_consumer_established_a_baseline(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);await r.watch(123,None,0)
        r.client.recovery=[message(i) for i in range(1,11)]
        result=await r.watch(123,0,0)
        assert [m['id'] for m in result['messages']]==list(range(1,11))
        assert r.client.recoveries==1
    asyncio.run(run())


def test_persistent_runtime_reuses_one_authorized_connection_and_closes_it_cleanly(tmp_path):
    async def run():
        (tmp_path/'private').mkdir();write_json(tmp_path/'whitelist.json',policy())
        write_json(tmp_path/'private'/'account.json',{'version':1,'api_id':1,'api_hash':'synthetic','account_id':999})
        class PersistentClient(Client):
            def __init__(self):super().__init__();self.connects=0;self.disconnects=0
            async def connect(self):self.connects+=1
            async def disconnect(self):self.disconnects+=1;self.connected=False
            async def is_user_authorized(self):return True
            async def get_me(self):return SimpleNamespace(id=999,bot=False)
            async def get_messages(self,peer,**kwargs):return [message(10)]
        client=PersistentClient()
        r=Runtime(tmp_path,client_factory=lambda *args:client,persistent=True)
        await r.history(123,1);await r.history(123,1)
        assert client.connects==1 and client.disconnects==0 and len(client.handlers)==1
        await r.close()
        assert client.disconnects==1
    asyncio.run(run())
