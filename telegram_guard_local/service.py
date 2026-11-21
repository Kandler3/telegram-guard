"""One local owner of the Telegram session for all Codex MCP processes.

Loopback-only, token-authenticated bounded JSON IPC. The endpoint and secret live
inside protected private state and are never MCP arguments, logs or output.
"""
import argparse
import asyncio
import inspect
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from .files import checked, read_json, write_json
from .model import GuardError, exact, integer
from .runtime import Runtime, SessionLock

VERSION="2.6.0"
MAX_PACKET=512*1024


async def exchange(endpoint,method,args,timeout=150):
    integer(endpoint['port'],1,65535)
    if not isinstance(endpoint.get('token'),str) or len(endpoint['token'])!=64:
        raise GuardError('local_broker_invalid_endpoint',503)
    async def request():
        reader,writer=await asyncio.open_connection('127.0.0.1',endpoint['port'],limit=MAX_PACKET)
        try:
            packet=json.dumps({'token':endpoint['token'],'method':method,'args':args},ensure_ascii=False).encode()+b'\n'
            if len(packet)>MAX_PACKET:raise GuardError('local_broker_request_too_large')
            writer.write(packet);await writer.drain()
            data=await reader.readline()
            if not data or len(data)>MAX_PACKET:raise GuardError('local_broker_invalid_response',503)
            result=json.loads(data)
            if 'error' in result:
                raise GuardError(result['error'],result.get('status',503))
            return result['result']
        finally:
            writer.close()
            await writer.wait_closed()
    return await asyncio.wait_for(request(),timeout)


class RemoteSubscriptions:
    def __init__(self,proxy):
        self.proxy=proxy
        self.sink=bool(proxy.app_server_url)
        self.task=None
    def start(self):
        # Restore only previously authorized, unexpired subscriptions. This does not connect Telegram without work.
        if self.task is None:
            self.task=asyncio.create_task(self.proxy.ensure())
            self.task.add_done_callback(lambda t:t.exception() if not t.cancelled() else None)
    async def close(self):
        if self.task:
            await asyncio.gather(self.task,return_exceptions=True)
    async def subscribe(self,thread,chat,sid,authorization):
        return await self.proxy.call('subscribe',thread=thread,chat=chat,sid=sid,authorization=authorization)
    async def list(self,thread):return await self.proxy.call('subscriptions',thread=thread)
    async def stop(self,thread,sid):return await self.proxy.call('unsubscribe',thread=thread,sid=sid)


class ServiceRuntime:
    def __init__(self,home,app_server_url=None):
        self.home=Path(home).absolute()
        self.private=self.home/'private'
        self.app_server_url=app_server_url
        self.subscriptions=RemoteSubscriptions(self)
        self.lock=asyncio.Lock()

    async def existing(self):
        try:
            endpoint=read_json(self.private/'broker-endpoint.json')
            response=await exchange(endpoint,'ping',{},timeout=2)
            if response.get('version')!=VERSION:
                raise GuardError('local_broker_upgrade_required',503)
            if self.app_server_url and response.get('app_server_url')!=self.app_server_url:
                await exchange(endpoint,'configure_app_server',{'url':self.app_server_url},timeout=3)
            return endpoint
        except GuardError:
            raise
        except (OSError,ValueError,TimeoutError,KeyError):
            pass
        return None

    async def ensure(self):
        async with self.lock:
            endpoint=await self.existing()
            if endpoint:return endpoint
            checked(self.private,directory=True)
            # Cross-process startup ownership; ordinary readers never contend for the Telegram session lock.
            deadline=asyncio.get_running_loop().time()+25
            while True:
                try:
                    lock=SessionLock(self.private/'broker-launch.lock')
                    lock.__enter__()
                    break
                except GuardError:
                    if asyncio.get_running_loop().time()>deadline:raise GuardError('local_broker_start_timeout',503) from None
                    await asyncio.sleep(.1)
            try:
                endpoint=await self.existing()
                if endpoint:return endpoint
                env=os.environ.copy()
                if self.app_server_url:env['TELEGRAM_GUARD_APP_SERVER_URL']=self.app_server_url
                subprocess.Popen([sys.executable,'-I','-B','-m','telegram_guard_local.service','--home',str(self.home)],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=env,close_fds=True)
                while asyncio.get_running_loop().time()<deadline:
                    endpoint=await self.existing()
                    if endpoint:return endpoint
                    await asyncio.sleep(.1)
                raise GuardError('local_broker_start_timeout',503)
            finally:
                lock.__exit__(None,None,None)

    async def call(self,method,**args):
        endpoint=await self.ensure()
        # Never automatically replay an operation after an uncertain network outcome.
        try:
            return await exchange(endpoint,method,args)
        except (OSError,TimeoutError,ValueError,KeyError):
            raise GuardError('local_broker_outcome_unknown_check_request_id',503) from None
    async def list_chats(self,search='',limit=100,offset=0):return await self.call('list_chats',search=search,limit=limit,offset=offset)
    async def history(self,target,limit=20):return await self.call('history',target=target,limit=limit)
    async def watch(self,target,after_id=None,wait_seconds=30):return await self.call('watch',target=target,after_id=after_id,wait_seconds=wait_seconds)
    async def status(self,request_id):return await self.call('status',request_id=request_id)
    async def send(self,target,text,request_id,authorization,content=None,options=None):
        return await self.call('send',target=target,text=text,request_id=request_id,authorization=authorization,content=content,options=options)
    async def read_operation(self,operation,target=None,**args):return await self.call('read_operation',operation=operation,target=target,**args)
    async def mutate(self,operation,target,request_id,authorization,**args):
        return await self.call('mutate',operation=operation,target=target,request_id=request_id,authorization=authorization,**args)


class LocalBroker:
    def __init__(self,runtime,token):
        self.runtime,self.token=runtime,token
        self.stopping=asyncio.Event()
        self.tasks=set()
        self.last_request=time.monotonic()

    async def dispatch(self,method,args):
        routes={'list_chats':self.runtime.list_chats,'history':self.runtime.history,'watch':self.runtime.watch,
                'status':self.runtime.status,'send':self.runtime.send,'read_operation':self.runtime.read_operation,
                'mutate':self.runtime.mutate,'subscribe':self.runtime.subscriptions.subscribe,
                'subscriptions':self.runtime.subscriptions.list,'unsubscribe':self.runtime.subscriptions.stop}
        if method=='ping':return {'version':VERSION,'app_server_url':getattr(self.runtime.subscriptions.sink,'url',None)}
        if method=='configure_app_server':
            from .app_server import AppServer
            exact(args,('url',))
            if self.runtime.subscriptions.sink and self.runtime.subscriptions.sink.url!=args['url']:
                raise GuardError('local_broker_app_server_mismatch',409)
            if not self.runtime.subscriptions.sink:self.runtime.subscriptions.sink=AppServer(args['url'])
            return {'state':'configured'}
        if method=='editor_dialogs':
            from .whitelist_editor import collect_dialogs
            async with self.runtime.mutex:
                async with self.runtime.connection() as broker:
                    return await asyncio.wait_for(collect_dialogs(broker.transport.client),75)
        if method=='shutdown':
            if self.runtime.jobs and any(not j[2].done() for j in self.runtime.jobs.values()):
                raise GuardError('sender_busy',409)
            self.stopping.set();return {'state':'stopping'}
        if method not in routes:raise GuardError('unsupported_local_operation')
        result=routes[method](**args)
        return await result if inspect.isawaitable(result) else result

    async def handle(self,reader,writer):
        task=asyncio.current_task()
        if len(self.tasks)>=64:
            writer.close();return
        self.tasks.add(task)
        try:
            data=await asyncio.wait_for(reader.readline(),5)
            if len(data)>MAX_PACKET:raise GuardError('local_broker_request_too_large')
            packet=json.loads(data)
            exact(packet,('token','method','args'))
            if not isinstance(packet['token'],str) or not secrets.compare_digest(packet['token'],self.token):
                raise GuardError('local_broker_unauthorized',403)
            if not isinstance(packet['args'],dict):raise GuardError('invalid_fields')
            if packet['method']!='ping':self.last_request=time.monotonic()
            result=await self.dispatch(packet['method'],packet['args'])
            response={'result':result}
        except GuardError as error:
            response={'error':error.code,'status':error.status}
        except Exception:
            response={'error':'local_broker_operation_failed','status':503}
        try:
            data=json.dumps(response,ensure_ascii=False).encode()+b'\n'
            if len(data)>MAX_PACKET:data=b'{"error":"local_broker_response_too_large","status":503}\n'
            writer.write(data);await writer.drain()
        except (OSError,ConnectionError):pass
        finally:
            self.tasks.discard(task)
            writer.close()
            await writer.wait_closed()

    async def run(self):
        path=self.runtime.private/'broker-endpoint.json'
        with SessionLock(self.runtime.private/'broker-owner.lock'):
            server=await asyncio.start_server(self.handle,'127.0.0.1',0,limit=MAX_PACKET)
            port=server.sockets[0].getsockname()[1]
            write_json(path,{'version':VERSION,'port':port,'token':self.token})
            self.runtime.subscriptions.start()
            try:
                async with server:
                    while not self.stopping.is_set():
                        try:await asyncio.wait_for(self.stopping.wait(),5)
                        except TimeoutError:pass
                        active=self.runtime.subscriptions.store.has_active(time.time())
                        jobs=any(not j[2].done() for j in self.runtime.jobs.values())
                        if not active and not jobs and not self.tasks and time.monotonic()-self.last_request>300:
                            break
            finally:
                # Complete in-flight calls before releasing the session; never cancel a send during idle shutdown.
                if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)
                await self.runtime.close()
                path.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',required=True)
    parser.add_argument('--stop',action='store_true')
    args=parser.parse_args()
    os.umask(0o077)
    if args.stop:
        async def stop():
            endpoint=await ServiceRuntime(args.home).existing()
            if endpoint:await exchange(endpoint,'shutdown',{})
            print('Local broker stopped or absent.')
        asyncio.run(stop())
    else:
        runtime=Runtime(args.home,app_server_url=os.environ.get('TELEGRAM_GUARD_APP_SERVER_URL'),persistent=True)
        asyncio.run(LocalBroker(runtime,secrets.token_hex(32)).run())


if __name__=='__main__':main()
