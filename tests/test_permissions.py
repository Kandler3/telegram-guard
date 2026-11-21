import asyncio
import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from telethon import types
from telegram_guard_local.model import GuardError, Policy, Peer
from telegram_guard_local.policy_migration import migrate_policy, migrate_home
from telegram_guard_local.files import read_json, write_json
from telegram_guard_local.engine import Broker
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.store import Store
from telegram_guard_local.transport import TelegramTransport
from telegram_guard_local.subscriptions import Subscriptions


def legacy():
    return {"version":1,"chats":[{"chat_id":123,"label":"Ivan","kind":"user","entity_id":123,"access_hash":456}],
            "messages_per_minute":4,"messages_per_day":90}


def test_migration_preserves_existing_read_write_and_enables_future_reads():
    old=legacy(); new=migrate_policy(old); p=Policy.parse(new)
    assert old==legacy() and new["chats"]==old["chats"]
    assert new["messages_per_minute"]==4 and new["messages_per_day"]==90
    assert p.can_read(123) and p.can_write(123)
    assert p.can_read(999) and p.can_read(-1000000000099) and not p.can_write(999)
    assert migrate_policy(new)==new
    assert not Policy.parse(old).can_read(999)  # Legacy files remain restrictive until explicit migration.


def test_migration_is_idempotent_with_a_protected_local_backup(tmp_path):
    (tmp_path/"private").mkdir(); write_json(tmp_path/"whitelist.json",legacy())
    assert migrate_home(tmp_path)["migrated"]
    backups=list(tmp_path.glob('whitelist.before-v2.*.json'))
    assert len(backups)==1 and read_json(backups[0])==legacy()
    assert not migrate_home(tmp_path)["migrated"] and len(list(tmp_path.glob('whitelist.before-v2.*.json')))==1


def test_read_deny_and_write_allow_are_independent():
    raw=migrate_policy(legacy());raw["read"]["deny"]=[123]
    p=Policy.parse(raw)
    assert p.can_write(123) and not p.can_read(123)
    assert p.require_write(123).chat==123
    with pytest.raises(GuardError,match='chat_read_not_allowed'):p.require_read(123)
    raw["write"]["allow"]=[];raw["read"]["deny"]=[]
    p=Policy.parse(raw)
    assert p.can_read(123) and not p.can_write(123)
    with pytest.raises(GuardError,match='chat_not_allowed'):p.require_write(123)


def test_read_opt_in_and_unknown_blacklist_entries():
    raw=migrate_policy(legacy());raw["read"]={"default":False,"allow":[999],"deny":[888]}
    p=Policy.parse(raw)
    assert p.can_read(999) and p.require_read(999) is None
    assert not p.can_read(123) and not p.can_read(888) and not p.can_write(999)


@pytest.mark.parametrize('mutate',[
    lambda p:p['read'].update(default=1), lambda p:p['read'].update(deny=[True]),
    lambda p:p['read'].update(allow=[123],deny=[123]), lambda p:p['read'].update(deny=[123,123]),
    lambda p:p['write'].update(allow=[999]), lambda p:p['write'].update(deny=[]),
    lambda p:p.update(version=True), lambda p:p['read'].update(deny=['123']),
])
def test_malformed_permissions_fail_closed(mutate):
    raw=migrate_policy(legacy());mutate(raw)
    with pytest.raises(GuardError):Policy.parse(raw)


def test_runtime_denies_read_and_write_before_credentials(tmp_path):
    raw=migrate_policy(legacy());raw['read']['deny']=[123]
    write_json(tmp_path/'whitelist.json',raw)
    calls=[];r=Runtime(tmp_path,client_factory=lambda *a:calls.append(a))
    with pytest.raises(GuardError,match='chat_read_not_allowed'):asyncio.run(r.history(123))
    with pytest.raises(GuardError,match='chat_read_not_allowed'):asyncio.run(r.watch(123))
    auth={'user_instruction':'test','source':'human','scope':'test','scope_chat_ids':[999],'expires_at':None}
    with pytest.raises(GuardError,match='chat_not_allowed'):asyncio.run(r.send(999,'test',str(uuid.uuid4()),auth))
    assert calls==[]


def test_read_only_unknown_peer_resolution_and_revocation_after_history(tmp_path):
    raw=migrate_policy(legacy());raw['write']['allow']=[]
    calls=[]
    class Transport:
        async def resolve_peer(self,target):
            calls.append(('resolve',target));return Peer(target,'read-only','user',target,777)
        async def history(self,peer,limit):
            calls.append(('history',peer.chat));return [{'id':1,'text':'synthetic'}]
    broker=Broker(lambda:Policy.parse(raw),Transport(),None)
    assert asyncio.run(broker.history({'chat_id':999,'limit':5}))['messages'][0]['id']==1
    assert calls==[('resolve',999),('history',999)] and not Policy.parse(raw).can_write(999)
    class Revoked(Transport):
        async def history(self,peer,limit):
            raw['read']['deny']=[999];return [{'text':'must not escape'}]
    with pytest.raises(GuardError,match='chat_read_not_allowed'):
        asyncio.run(Broker(lambda:Policy.parse(raw),Revoked(),None).history({'chat_id':999,'limit':5}))


def test_write_only_send_does_not_need_read_permission(tmp_path):
    raw=migrate_policy(legacy());raw['read']['deny']=[123]
    calls=[]
    class Transport:
        async def send(self,peer,text,rid):calls.append(peer.chat);return 42
    store=Store(tmp_path/'audit.sqlite3')
    try:
        auth={'user_instruction':'send synthetic','source':'human','scope':'test','scope_chat_ids':[123],'expires_at':None}
        result=asyncio.run(Broker(lambda:Policy.parse(raw),Transport(),store).send({'chat_id':123,'text':'test','request_id':str(uuid.uuid4()),'authorization':auth}))
        assert result['state']=='sent' and calls==[123]
    finally:store.close()


def test_forward_uses_read_only_source_and_write_only_destination(tmp_path):
    raw=migrate_policy(legacy());raw['read']['deny']=[123]
    calls=[]
    class Transport:
        async def resolve_peer(self,target):return Peer(target,'source','user',target,777)
        async def send_content(self,intent,files,guard):
            destination,source=guard();calls.append((destination.chat,source.chat));return [42]
    store=Store(tmp_path/'audit.sqlite3')
    auth={'user_instruction':'forward synthetic','source':'human','scope':'test','scope_chat_ids':[123],'expires_at':None}
    payload={'chat_id':123,'request_id':str(uuid.uuid4()),'authorization':auth,
             'content':{'type':'forward','from_chat_id':999,'message_ids':[1]}}
    broker=Broker(lambda:Policy.parse(raw),Transport(),store)
    try:
        assert asyncio.run(broker.send(payload))['state']=='sent'
        assert calls==[(123,999)] and not Policy.parse(raw).can_write(999)
        raw['read']['deny'].append(999);payload['request_id']=str(uuid.uuid4())
        with pytest.raises(GuardError,match='chat_read_not_allowed'):asyncio.run(broker.send(payload))
        assert calls==[(123,999)]
    finally:store.close()


def test_transport_resolves_read_peer_without_accepting_mismatched_identity():
    class Client:
        async def get_input_entity(self,target):return types.InputPeerUser(target,12345)
    assert asyncio.run(TelegramTransport(Client()).resolve_peer(999)).chat==999
    class Wrong:
        async def get_input_entity(self,target):return types.InputPeerUser(123,12345)
    with pytest.raises(GuardError,match='chat_resolution_unavailable'):
        asyncio.run(TelegramTransport(Wrong()).resolve_peer(999))


def test_dialog_search_pagination_and_permissions_do_not_export_hashes():
    raw=migrate_policy(legacy());raw['read']['deny']=[222]
    class Client:
        async def iter_dialogs(self):
            for cid in [123,222,333,444]:
                yield SimpleNamespace(id=cid,name=f'Chat {cid}',entity=SimpleNamespace(username=f'user{cid}'))
    transport=TelegramTransport(Client())
    first=asyncio.run(transport.dialogs('Chat',2,0,lambda:Policy.parse(raw)))
    assert [p['chat_id'] for p in first['chats']]==[123,333] and first['next_offset']==2
    assert first['chats'][0]['can_write'] and not first['chats'][1]['can_write']
    last=asyncio.run(transport.dialogs('Chat',2,2,lambda:Policy.parse(raw)))
    assert [p['chat_id'] for p in last['chats']]==[444] and last['next_offset'] is None
    assert 'access_hash' not in str(first)


def test_subscription_only_needs_read_and_stops_when_read_is_revoked(tmp_path):
    raw=migrate_policy(legacy());raw['write']['allow']=[]
    (tmp_path/'private').mkdir()
    class ReadRuntime:
        private=tmp_path/'private'
        def policy(self):return Policy.parse(raw)
        async def watch(self,chat,cursor,wait):
            self.policy().require_read(chat)
            return {'cursor':10 if cursor is None else 11,'messages':[] if cursor is None else [{'id':11,'text':'test'}]}
    class Sink:
        async def ready(self,thread):pass
        async def deliver(self,thread,payload,before):before();return 'turn'
        async def close(self):pass
    service=Subscriptions(ReadRuntime(),Sink(),lambda:1000.)
    service.start=lambda:None
    thread,sid=str(uuid.uuid4()),str(uuid.uuid4())
    auth={'user_instruction':'monitor synthetic','source':'human','scope':'test','scope_chat_ids':[123],
          'expires_at':datetime.fromtimestamp(1600,timezone.utc).isoformat()}
    async def run():
        result=await service.subscribe(thread,123,sid,auth)
        assert result['state']=='active'
        raw['read']['deny']=[123]
        await service.tick(sid)
        assert service.store.get(sid,thread)['state']!='active'
    asyncio.run(run())
