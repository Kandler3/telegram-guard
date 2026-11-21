import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest
from telethon import functions,types
from telegram_guard_local.engine import Broker
from telegram_guard_local.files import write_json
from telegram_guard_local.model import GuardError
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.store import Store
from telegram_guard_local.transport import TelegramTransport
from telegram_guard_local.operations import message_row,folder_row
from test_stream import policy,message


def poll_message(flags=None):
    from datetime import datetime,timezone
    return types.Message(id=285,peer_id=types.PeerUser(123),from_id=types.PeerUser(777),
        date=datetime.now(timezone.utc),message='',media=types.MessageMediaPoll(
            poll=types.Poll(id=1,hash=42,question=types.TextWithEntities('Какой вариант?',[]),
                answers=[types.PollAnswer(text=types.TextWithEntities('Первый',[]),option=b'\x00'),
                         types.PollAnswer(text=types.TextWithEntities('Второй',[]),option=b'\xff')],
                **(flags or {})),results=types.PollResults(total_voters=5)))


@pytest.mark.parametrize('flags',[{}, {'quiz':True}, {'closed':True}, {'multiple_choice':True}])
def test_real_telethon_poll_serializes_question_and_binary_option_tokens(flags):
    m=poll_message(flags)
    assert isinstance(m.poll,types.MessageMediaPoll)
    row=message_row(m)
    assert row['poll']=={'question':'Какой вариант?','answers':[
        {'text':'Первый','option':'AA=='},{'text':'Второй','option':'/w=='}]}
    assert json.loads(json.dumps(row,ensure_ascii=False))==row
    assert row['media_type']=='MessageMediaPoll' and row['id']==285


def auth(target=123):
    return {'user_instruction':'synthetic human instruction','scope':'test','scope_chat_ids':[target],
            'source':'human','expires_at':None}


class Client:
    def __init__(self):self.requests=[];self.reads=0;self.failure=False;self.wrong_chat=False;self.on_chunk=None
    async def __call__(self,request):
        self.requests.append(request)
        if self.failure:raise TimeoutError('never expose this diagnostic')
        return True
    async def get_messages(self,peer,ids=None,**kwargs):
        self.reads+=1
        def get(mid):
            m=message(mid,target=999 if self.wrong_chat else 123)
            m.media=types.MessageMediaDocument();m.file=SimpleNamespace(size=5,ext='.txt',name='../../evil.txt');m.noforwards=False
            return m
        return [get(i) for i in ids] if isinstance(ids,list) else get(ids)
    async def iter_download(self,media):
        yield b'hello'
        if self.on_chunk:self.on_chunk()
        yield b'!'


class FakeRuntime(Runtime):
    def __init__(self,home):
        (home/'private').mkdir();write_json(home/'whitelist.json',policy())
        super().__init__(home)
        self.client=Client();self.store=Store(home/'private'/'audit.sqlite3')
        self.download_root=home/'downloads'
    @asynccontextmanager
    async def connection(self):yield Broker(self.policy,TelegramTransport(self.client),self.store)


@pytest.mark.parametrize('operation,args',[
    ('message',{'message_id':285}),('history',{'limit':20}),('search',{'query':'вариант','limit':20})])
def test_poll_does_not_break_exact_message_history_or_search(tmp_path,operation,args):
    async def run():
        r=FakeRuntime(tmp_path)
        try:
            raw=policy();raw['write']['allow']=[];write_json(tmp_path/'whitelist.json',raw)
            class PollClient(Client):
                async def get_messages(self,peer,ids=None,**kwargs):
                    return poll_message() if ids else [message(286),poll_message(),message(283)]
                async def iter_messages(self,peer,**kwargs):
                    for m in [message(286),poll_message(),message(283)]:yield m
            r.client=PollClient()
            result=await r.read_operation(operation,123,**args)
            rows=[result['message']] if operation=='message' else result['messages']
            assert [row['id'] for row in rows]==([285] if operation=='message' else [286,285,283])
            assert next(row for row in rows if row['id']==285)['poll']['question']=='Какой вариант?'
            assert all(row['poll'] is None for row in rows if row['id']!=285)
            json.dumps(result,ensure_ascii=False)
            assert not r.client.requests
        finally:r.store.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation,args,request_type',[
 ('edit',{'message_id':1,'text':'new'},functions.messages.EditMessageRequest),
 ('delete',{'message_ids':[1,2]},functions.messages.DeleteMessagesRequest),
 ('reaction',{'message_id':1,'emoji':'👍'},functions.messages.SendReactionRequest),
 ('pin',{'message_id':1,'unpin':False,'silent':True},functions.messages.UpdatePinnedMessageRequest),
 ('read',{'message_id':1},functions.messages.ReadHistoryRequest),
 ('vote',{'message_id':1,'options':[base64.b64encode(b'0').decode()]},functions.messages.SendVoteRequest),
])
def test_chat_write_operations_are_audited_idempotent_and_use_fixed_rpc(tmp_path,operation,args,request_type):
    async def run():
        r=FakeRuntime(tmp_path);rid=str(uuid.uuid4())
        result=await r.mutate(operation,123,rid,auth(),**args)
        assert result['state']=='applied' and isinstance(r.client.requests[0],request_type)
        replay=await r.mutate(operation,123,rid,auth(),**args)
        assert replay['replayed'] and len(r.client.requests)==1
        assert r.status(rid)['state']=='sent'
        r.store.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation,args',[
 ('edit',{'message_id':1,'text':'new'}),('delete',{'message_ids':[1]}),('reaction',{'message_id':1,'emoji':'👍'}),
 ('pin',{'message_id':1,'unpin':False,'silent':True}),('read',{'message_id':1}),('vote',{'message_id':1,'options':[]})])
def test_read_only_does_not_allow_any_mutation(tmp_path,operation,args):
    async def run():
        r=FakeRuntime(tmp_path);raw=policy();raw['write']['allow']=[];write_json(tmp_path/'whitelist.json',raw)
        with pytest.raises(GuardError,match='chat_not_allowed'):await r.mutate(operation,123,str(uuid.uuid4()),auth(),**args)
        assert not r.client.requests and r.client.reads==0;r.store.close()
    asyncio.run(run())


def test_delete_cannot_target_global_message_ids_from_another_chat(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);r.client.wrong_chat=True
        with pytest.raises(GuardError,match='message_chat_mismatch'):
            await r.mutate('delete',123,str(uuid.uuid4()),auth(),message_ids=[1])
        assert not r.client.requests;r.store.close()
    asyncio.run(run())


def test_mutation_uncertainty_is_durable_and_never_replayed(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);r.client.failure=True;rid=str(uuid.uuid4())
        for _ in range(2):
            with pytest.raises(GuardError,match='delivery_unknown'):
                await r.mutate('reaction',123,rid,auth(),message_id=1,emoji='👍')
        assert len(r.client.requests)==1 and r.status(rid)['state']=='unknown';r.store.close()
    asyncio.run(run())


def test_download_requires_only_read_and_never_uses_remote_filename_as_path(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);raw=policy();raw['write']['allow']=[];write_json(tmp_path/'whitelist.json',raw)
        result=await r.read_operation('download',123,message_id=1)
        assert Path(result['path']).parent==r.download_root and Path(result['path']).read_bytes()==b'hello!'
        assert result['filename']=='../../evil.txt' and len(result['sha256'])==64
        assert not r.client.requests;r.store.close()
    asyncio.run(run())


@pytest.mark.parametrize('already_exists',[False,True])
def test_multiple_csv_downloads_reuse_directory_without_overwriting_files(tmp_path,already_exists):
    async def run():
        r=FakeRuntime(tmp_path)
        try:
            raw=policy();raw['write']['allow']=[];write_json(tmp_path/'whitelist.json',raw)
            class CsvClient(Client):
                async def get_messages(self,peer,ids=None,**kwargs):
                    m=await super().get_messages(peer,ids=ids,**kwargs)
                    m.file.ext='.csv'
                    m.file.name='ai-results.csv' if ids==1 else 'curators-links.csv'
                    return m
            r.client=CsvClient()
            if already_exists:r.download_root.mkdir()
            first=await r.read_operation('download',123,message_id=1)
            first_path=Path(first['path']);first_bytes=first_path.read_bytes()
            second=await r.read_operation('download',123,message_id=2)
            second_path=Path(second['path'])
            assert first_path!=second_path and first_path.parent==second_path.parent==r.download_root
            assert first_path.read_bytes()==first_bytes==second_path.read_bytes()==b'hello!'
            assert first['filename']=='ai-results.csv' and second['filename']=='curators-links.csv'
            assert first_path.suffix==second_path.suffix=='.csv'
            assert first_path.name!=first['filename'] and second_path.name!=second['filename']
            assert first['sha256']==second['sha256'] and first['size']==second['size']==6
            assert len(list(r.download_root.iterdir()))==2 and not r.client.requests
        finally:r.store.close()
    asyncio.run(run())


def test_download_refuses_a_regular_file_at_directory_path(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        try:
            r.download_root.write_bytes(b'existing file must survive')
            with pytest.raises(GuardError,match='unexpected_path_type'):
                await r.read_operation('download',123,message_id=1)
            assert r.download_root.read_bytes()==b'existing file must survive'
        finally:r.store.close()
    asyncio.run(run())


def test_download_revocation_removes_partial_file(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        def revoke():
            raw=policy();raw['read']['deny']=[123];write_json(tmp_path/'whitelist.json',raw)
        r.client.on_chunk=revoke
        with pytest.raises(GuardError,match='chat_read_not_allowed'):await r.read_operation('download',123,message_id=1)
        assert not list(r.download_root.iterdir());r.store.close()
    asyncio.run(run())


def test_blacklist_blocks_new_read_operations_before_connect(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);raw=policy();raw['read']['deny']=[123];write_json(tmp_path/'whitelist.json',raw)
        for op,args in [('info',{}),('download',{'message_id':1}),('message',{'message_id':1}),('members',{}),('topics',{}),('search',{'query':'test'})]:
            with pytest.raises(GuardError,match='chat_read_not_allowed'):await r.read_operation(op,123,**args)
        assert not r.client.requests and r.client.reads==0;r.store.close()
    asyncio.run(run())


def test_folder_metadata_hides_blacklisted_peers_without_leaking_hashes(tmp_path):
    r=FakeRuntime(tmp_path);raw=policy();raw['read']['deny']=[999];write_json(tmp_path/'whitelist.json',raw)
    f=types.DialogFilter(id=2,title=types.TextWithEntities('Work',[]),pinned_peers=[],
                         include_peers=[types.InputPeerUser(123,111),types.InputPeerUser(999,222)],exclude_peers=[])
    row=folder_row(f,r.policy())
    assert row['include_chat_ids']==[123] and 'access_hash' not in str(row);r.store.close()


def test_folder_list_includes_default_and_computes_actual_membership(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        class FolderClient(Client):
            async def __call__(self,request):
                assert isinstance(request,functions.messages.GetDialogFiltersRequest)
                return SimpleNamespace(filters=[types.DialogFilterDefault(),types.DialogFilter(
                    id=2,title=types.TextWithEntities('Work',[]),pinned_peers=[],include_peers=[types.InputPeerUser(123,456)],exclude_peers=[])])
            async def iter_dialogs(self):
                yield SimpleNamespace(id=123,entity=types.User(123))
                yield SimpleNamespace(id=999,entity=types.User(999))
        r.client=FolderClient()
        raw=policy();raw['read']['deny']=[999];write_json(tmp_path/'whitelist.json',raw)
        result=await r.read_operation('folders')
        assert result['folders'][0]['id']==0 and result['folders'][0]['chat_ids']==[123]
        assert result['folders'][1]['chat_ids']==[123];r.store.close()
    asyncio.run(run())
