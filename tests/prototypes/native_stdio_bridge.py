"""OFFLINE PROTOTYPE ONLY. Never install as the Desktop launcher.

One real stdio App Server; event-side RPC is a restricted test compatibility
facade, not a second App Server connection. No approvals are answered here.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from websockets.asyncio.server import serve


async def main():
    binary, port = sys.argv[1], int(sys.argv[2])
    home=Path(os.environ['CODEX_HOME']).resolve()
    if '.test-runs' not in home.parts or not (home/'config.toml').is_file():
        raise RuntimeError('Prototype requires an isolated .test-runs CODEX_HOME')
    args=[binary,'-c','features.plugins=false','app-server']
    if len(sys.argv)>3:
        fixture=Path(sys.argv[3]).resolve()
        if not fixture.is_relative_to(home):raise RuntimeError('Fixture must be inside isolated home')
        args=[binary,'-I','-B',str(fixture)]
    child=subprocess.Popen(args,
        env=dict(os.environ),cwd=home,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=sys.stderr,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    loop=asyncio.get_running_loop()
    lock=threading.Lock()
    pending={}
    counter=0
    desktop_ready=False
    done=asyncio.Event()
    def write(message):
        data=(json.dumps(message,separators=(',',':'))+'\n').encode()
        with lock:
            child.stdin.write(data);child.stdin.flush()
    def forward(message, target):
        nonlocal counter
        counter+=1
        original=message['id']
        wire='native-probe-'+str(counter)
        pending[wire]=(target,original,message.get('method'))
        write(dict(message,id=wire))
    def desktop_read():
        try:
            for raw in sys.stdin.buffer:
                message=json.loads(raw)
                if 'method' in message and 'id' in message:
                    forward(message,None)
                else:
                    write(message) # Notifications and answers to server requests stay untouched.
        finally:
            loop.call_soon_threadsafe(done.set)
    async def route(message):
        nonlocal desktop_ready
        if 'id' in message and 'method' not in message:
            item=pending.pop(message['id'],None)
            if item is None:return
            target,original,method=item
            message=dict(message,id=original)
            if target is not None:
                try:await target.send(json.dumps(message))
                except Exception:pass # Ambiguous delivery is never replayed.
                return
            if method=='initialize' and 'result' in message:desktop_ready=True
        sys.stdout.buffer.write((json.dumps(message)+'\n').encode());sys.stdout.buffer.flush()
    def native_read():
        try:
            for raw in child.stdout:
                asyncio.run_coroutine_threadsafe(route(json.loads(raw)),loop).result()
        finally:loop.call_soon_threadsafe(done.set)
    async def event_side(ws):
        async for raw in ws:
            message=json.loads(raw)
            method=message.get('method');rid=message.get('id')
            if method=='initialize':
                await ws.send(json.dumps({'id':rid,'result':{}}));continue
            if method=='initialized':continue
            params=message.get('params',{})
            allowed=method=='thread/loaded/list' or (method=='turn/start' and
                set(params)=={'threadId','input','toolOutput'} and params['input']==[] and
                isinstance(params['toolOutput'],dict) and
                params['toolOutput'].get('name')=='telegram_incoming_probe')
            if not allowed or not desktop_ready:
                await ws.send(json.dumps({'id':rid,'error':{'code':-32601,'message':'event request unavailable'}}));continue
            forward(message,ws)
    try:
        async with serve(event_side,'127.0.0.1',port,ping_interval=None,max_size=65536):
            threading.Thread(target=desktop_read,daemon=True).start()
            threading.Thread(target=native_read,daemon=True).start()
            await done.wait()
    finally:
        if child.poll() is None:
            child.stdin.close()
            try:await asyncio.to_thread(child.wait,5)
            except subprocess.TimeoutExpired:
                child.terminate();await asyncio.to_thread(child.wait,5)

if __name__=='__main__':asyncio.run(main())
