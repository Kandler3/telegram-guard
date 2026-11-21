import asyncio
import json
import pytest
from websockets.asyncio.server import serve
from telegram_guard_local.app_server import AppServer, DeliveryUnknown, RpcRejected, local_endpoint
from telegram_guard_local.model import GuardError

@pytest.mark.parametrize("url",["http://127.0.0.1:1","ws://example.com:12","ws://127.0.0.1","ws://localhost:2",
                               "ws://127.0.0.1:12/path","ws://u:p@127.0.0.1:12","ws://127.0.0.1:12?token=x"])
def test_only_fixed_local_endpoint(url):
    with pytest.raises(GuardError):local_endpoint(url)

@pytest.mark.parametrize("mode",["success","reject","unknown","not_loaded","callback_stops"])
def test_protocol_and_failure_boundaries(mode):
    async def run():
        calls=[];before=[];sent=[]
        async def handler(ws):
            async for raw in ws:
                msg=json.loads(raw);calls.append(msg)
                if "id" not in msg:continue
                method=msg["method"]
                if method=="initialize":result={}
                elif method=="thread/loaded/list":result={"data":[] if mode=="not_loaded" else ["thread"],"nextCursor":None}
                elif method=="turn/start":
                    sent.append(msg)
                    if mode=="unknown":
                        await ws.close();return
                    if mode=="reject":
                        await ws.send(json.dumps({"id":msg["id"],"error":{"code":-1,"message":"test"}}));continue
                    result={"turn":{"id":"turn"}}
                else:raise AssertionError(method)
                await ws.send(json.dumps({"id":msg["id"],"result":result}))
        async with serve(handler,"127.0.0.1",0) as server:
            api=AppServer("ws://127.0.0.1:"+str(server.sockets[0].getsockname()[1]))
            def guard():
                if mode=="callback_stops":raise GuardError("subscription_stopped")
                before.append(True)
            try:
                if mode=="success":
                    assert await api.deliver("thread",{"text":"external"},guard)=="turn"
                    assert sent[0]["params"]=={"threadId":"thread","input":[],
                        "toolOutput":{"name":"telegram_incoming","namespace":"telegram_guard","output":'{"text": "external"}'}}
                else:
                    expected={"unknown":DeliveryUnknown,"reject":RpcRejected,"not_loaded":GuardError,"callback_stops":GuardError}[mode]
                    with pytest.raises(expected):await api.deliver("thread",{},guard)
                if mode in ("not_loaded","callback_stops"):assert not sent
                assert not any(c["method"] in ("thread/resume","turn/steer") for c in calls)
            finally:await api.close()
    asyncio.run(run())

