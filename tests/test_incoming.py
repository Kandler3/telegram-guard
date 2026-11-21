"""Cursor and authorization contract, exercised against the native stream."""
import asyncio
from types import SimpleNamespace
import pytest
from telegram_guard_local.files import write_json
from telegram_guard_local.model import GuardError
from test_stream import FakeRuntime,policy,message


def test_watch_requires_read_before_connection(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);raw=policy();raw['read']['deny']=[999];write_json(tmp_path/'whitelist.json',raw)
        with pytest.raises(GuardError,match='chat_read_not_allowed'):await r.watch(999)
        assert not r.client.baselines and not r.client.recoveries
    asyncio.run(run())


def test_baseline_grants_no_permission_and_skips_history(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path)
        result=await r.watch(123,None,0)
        assert result['state']=='baseline' and result['cursor']==10
        assert result['messages']==[] and result['may_authorize_sending'] is False
    asyncio.run(run())


def test_idle_watch_rechecks_revocation_without_reading_history(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);await r.watch(123,None,0)
        pending=asyncio.create_task(r.watch(123,10,3))
        await asyncio.sleep(.01)
        raw=policy();raw['read']['deny']=[123];write_json(tmp_path/'whitelist.json',raw)
        with pytest.raises(GuardError,match='chat_read_not_allowed'):await pending
        assert r.client.baselines==1 and r.client.recoveries==0
    asyncio.run(run())


def test_native_own_echo_advances_cursor_without_incoming_event(tmp_path):
    async def run():
        r=FakeRuntime(tmp_path);await r.watch(123,None,0)
        await r.stream.on_message(SimpleNamespace(chat_id=123,message=message(11,out=True)))
        result=await r.watch(123,10,0)
        assert result['cursor']==11 and result['messages']==[]
    asyncio.run(run())


@pytest.mark.parametrize('cursor,wait',[(True,1),(-1,1),(0,46),(0,True)])
def test_watch_validates_cursor_and_wait(tmp_path,cursor,wait):
    async def run():
        r=FakeRuntime(tmp_path)
        with pytest.raises(GuardError):await r.watch(123,cursor,wait)
        assert not r.client.baselines and not r.client.recoveries
    asyncio.run(run())
