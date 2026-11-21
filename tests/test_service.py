import asyncio
from types import SimpleNamespace
import pytest
from telegram_guard_local.model import GuardError
from telegram_guard_local.service import LocalBroker,exchange,VERSION


def test_local_ipc_authentication_allowlist_and_sanitized_errors():
    async def run():
        class Runtime:
            subscriptions=SimpleNamespace(sink=None,subscribe=lambda:None,list=lambda:None,stop=lambda:None)
            jobs={}
            async def list_chats(self,**args):return {'chats':[{'chat_id':123}]}
            async def history(self,**args):raise RuntimeError('secret must never escape')
            watch=status=send=read_operation=mutate=list_chats
        broker=LocalBroker(Runtime(),'a'*64)
        server=await asyncio.start_server(broker.handle,'127.0.0.1',0)
        endpoint={'port':server.sockets[0].getsockname()[1],'token':'a'*64}
        async with server:
            assert (await exchange(endpoint,'ping',{}))['version']==VERSION
            assert (await exchange(endpoint,'list_chats',{}))['chats'][0]['chat_id']==123
            for method,code in [('arbitrary_rpc','unsupported_local_operation'),('history','local_broker_operation_failed')]:
                with pytest.raises(GuardError,match=code):await exchange(endpoint,method,{})
            with pytest.raises(GuardError,match='unauthorized'):await exchange(dict(endpoint,token='b'*64),'ping',{})
    asyncio.run(run())
