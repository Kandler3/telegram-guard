"""Native Desktop stdio transport with an authenticated, restricted event endpoint."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from websockets.asyncio.server import serve

MAX_FRAME=100*1024*1024
MAX_EVENT=128*1024


def mac(key, domain, client, server):
    return hmac.new(key.encode(),f'{domain}:{client}:{server}'.encode(),hashlib.sha256).hexdigest()


def write_auth(path, key):
    path=Path(path)
    path.parent.mkdir(exist_ok=True,parents=True)
    if os.name=='nt':
        # Only this Windows identity and SYSTEM can access the new credential directory.
        sid=subprocess.run(['whoami.exe','/user','/fo','csv','/nh'],capture_output=True,text=True,
            check=True,creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip().split(',')[-1].strip('"')
        if not sid.startswith('S-1-'):raise ValueError('Cannot identify local event owner')
        subprocess.run(['icacls.exe',str(path.parent),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F',
            '*S-1-5-18:(OI)(CI)F'],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    temp=path.with_suffix('.'+secrets.token_hex(8)+'.tmp')
    try:
        with temp.open('x',encoding='utf-8') as f:json.dump({'key':key,'pid':os.getpid()},f)
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


async def run(binary, args, port, auth_file, *, environment=None, cwd=None):
    env=dict(os.environ if environment is None else environment)
    # No native service variable is fabricated, removed or required at bootstrap.
    child=subprocess.Popen([binary,*args],env=env,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=sys.stderr,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    loop=asyncio.get_running_loop();done=asyncio.Event();write_lock=threading.Lock()
    pending={};known_threads=set();counter=0;initialized=False
    key=secrets.token_hex(32)
    def native_write(message):
        data=(json.dumps(message,separators=(',',':'))+'\n').encode()
        with write_lock:child.stdin.write(data);child.stdin.flush()
    def send_native(message,target=None):
        nonlocal counter
        counter+=1;wire='guard-stdio-'+str(counter)
        pending[wire]=(target,message['id'],message.get('method'),time.monotonic())
        try:native_write(dict(message,id=wire))
        except Exception:
            pending.pop(wire,None);done.set();raise
    def output(message):
        sys.stdout.buffer.write((json.dumps(message)+'\n').encode());sys.stdout.buffer.flush()
    def desktop_message(message):
        try:
            if 'method' in message and 'id' in message:send_native(message)
            else:native_write(message)
        except Exception:done.set()
    def desktop_reader():
        try:
            while True:
                raw=sys.stdin.buffer.readline(MAX_FRAME+1)
                if not raw:break
                if len(raw)>MAX_FRAME:raise ValueError('Desktop frame too large')
                loop.call_soon_threadsafe(desktop_message,json.loads(raw))
        finally:loop.call_soon_threadsafe(done.set)
    async def route(message):
        nonlocal initialized
        result=message.get('result',{})
        if 'id' in message and 'method' not in message:
            item=pending.pop(message['id'],None)
            if item is None:return
            target,original,method,_=item
            message=dict(message,id=original)
            if target is not None:
                if method=='thread/loaded/list' and isinstance(result,dict):
                    message['result']=dict(result,data=[t for t in result.get('data',[]) if t in known_threads])
                try:await target.send(json.dumps(message))
                except Exception:pass # Never replay a request after a lost response.
                return
            if method=='initialize' and 'result' in message:initialized=True
            if isinstance(result,dict) and isinstance(result.get('thread'),dict):
                tid=result['thread'].get('id')
                if isinstance(tid,str):known_threads.add(tid)
        if message.get('method')=='thread/started':
            tid=message.get('params',{}).get('thread',{}).get('id')
            if isinstance(tid,str):known_threads.add(tid)
        output(message) # All native requests, including approvals, go only to Desktop.
    def native_reader():
        try:
            while True:
                raw=child.stdout.readline(MAX_FRAME+1)
                if not raw:break
                if len(raw)>MAX_FRAME:raise ValueError('Native frame too large')
                asyncio.run_coroutine_threadsafe(route(json.loads(raw)),loop).result()
        finally:loop.call_soon_threadsafe(done.set)
    def allowed(method,params,role):
        if not isinstance(params,dict):return False
        if method=='thread/loaded/list':return set(params)<= {'cursor','limit'}
        tid=params.get('threadId')
        if tid not in known_threads:return False
        if role=='agent_wake' and method=='thread/read':
            return set(params)<= {'threadId','includeTurns'} and params.get('includeTurns') is False
        if role=='agent_wake' and method=='thread/name/set':
            return set(params)=={'threadId','name'} and isinstance(params['name'],str) and len(params['name'])<=512
        if method!='turn/start' or set(params)!= {'threadId','input','toolOutput'} or params['input']!=[]:return False
        tool=params['toolOutput']
        pair={'telegram_guard':('telegram_guard','telegram_incoming'),'agent_wake':('agent_wake','event_completed')}[role]
        return (isinstance(tool,dict) and set(tool)<= {'namespace','name','output'} and
            (tool.get('namespace'),tool.get('name'))==pair and isinstance(tool.get('output'),str) and
            len(tool['output'].encode())<=MAX_EVENT)
    async def event_side(ws):
        role=None;challenge=None;expires=time.monotonic()+10
        async def reply(rid,result=None,error=None):
            await ws.send(json.dumps({'id':rid,**({'error':{'code':-32600,'message':error}} if error else {'result':result})}))
        try:
            async for raw in ws:
                try:
                    message=json.loads(raw);rid=message.get('id');method=message.get('method');params=message.get('params',{})
                    if role is None:
                        if time.monotonic()>expires:await ws.close();return
                        if method=='bridge/challenge' and isinstance(params.get('nonce'),str) and len(params['nonce'])==64:
                            challenge=(params['nonce'],secrets.token_hex(32))
                            await reply(rid,{'nonce':challenge[1],'signature':mac(key,'server',*challenge)});continue
                        if method=='bridge/authenticate' and challenge and params.get('role') in ('telegram_guard','agent_wake'):
                            expected=mac(key,'client-'+params['role'],*challenge)
                            if hmac.compare_digest(str(params.get('signature','')),expected):
                                role=params['role'];await reply(rid,{});continue
                        await ws.close(code=1008);return
                    if method=='initialize':await reply(rid,{'eventBridge':1});continue
                    if method=='initialized':continue
                    side_count=sum(1 for target,*_ in pending.values() if target is not None)
                    if not initialized or side_count>=64 or not allowed(method,params,role):
                        await reply(rid,error='Event request unavailable');continue
                    send_native(message,ws)
                except (ValueError,TypeError,KeyError):await ws.close(code=1008);return
        finally:
            for wire,item in list(pending.items()):
                if item[0] is ws:pending.pop(wire,None)
    async def expire():
        while not done.is_set():
            await asyncio.sleep(2)
            now=time.monotonic()
            for wire,item in list(pending.items()):
                if item[0] is not None and now-item[3]>15:
                    pending.pop(wire,None)
                    await item[0].close(code=1011) # Lost response remains unknown to the producer.
    threading.Thread(target=desktop_reader,daemon=True).start()
    threading.Thread(target=native_reader,daemon=True).start()
    server=None;expiry=None
    try:
        try:
            server=await serve(event_side,'127.0.0.1',port,ping_interval=None,max_size=MAX_EVENT+4096,
                max_queue=16,origins=[None],open_timeout=5,close_timeout=2)
            write_auth(auth_file,key)
            expiry=asyncio.create_task(expire())
        except Exception:
            if server:server.close();await server.wait_closed();server=None
            print('Event bridge unavailable; native Desktop transport remains active.',file=sys.stderr)
        await done.wait()
    finally:
        if expiry:expiry.cancel()
        if server:server.close();await server.wait_closed()
        if child.poll() is None:
            child.stdin.close()
            try:await asyncio.to_thread(child.wait,5)
            except subprocess.TimeoutExpired:child.terminate();await asyncio.to_thread(child.wait,5)
    return child.returncode or 0


if __name__=='__main__':
    home=Path(os.environ['CODEX_HOME']).resolve()
    if '.test-runs' not in home.parts:raise SystemExit('Direct bridge entry is for isolated tests only')
    args=['-c','features.plugins=false','app-server']
    if len(sys.argv)>3:
        fixture=Path(sys.argv[3]).resolve()
        if not fixture.is_relative_to(home):raise SystemExit('Fixture outside isolated home')
        args=['-I','-B',str(fixture)]
    raise SystemExit(asyncio.run(run(sys.argv[1],args,int(sys.argv[2]),
        home/'event-bridge'/'auth.json',cwd=home)))
