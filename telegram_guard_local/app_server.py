"""Narrow App Server client: existing threads and external tool output only."""
import asyncio
import json
import os
import secrets
import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlsplit
from .model import GuardError

class DeliveryUnknown(GuardError):
    def __init__(self):
        super().__init__("event_delivery_unknown", 503)

class RpcRejected(GuardError):
    def __init__(self):
        super().__init__("app_server_rejected", 503)

def local_endpoint(value):
    try:
        u = urlsplit(value)
        valid = (u.scheme == "ws" and u.hostname == "127.0.0.1" and
                 u.port and not u.username and not u.password and
                 u.path in ("", "/") and not u.query and not u.fragment)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise GuardError("invalid_local_app_server_endpoint")
    return value

class AppServer:
    def __init__(self, url):
        self.url = local_endpoint(url)
        self.ws = None
        self.reader = None
        self.pending = {}
        self.counter = 0
        self.lock = asyncio.Lock()

    async def _receive(self, ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                # Approval requests are never answered by this bridge.
                # Desktop owns the interactive approval flow.
                if "method" in msg:
                    continue
                future = self.pending.pop(msg.get("id"), None)
                if future and not future.done():
                    if "error" in msg:
                        future.set_exception(RpcRejected())
                    else:
                        future.set_result(msg.get("result"))
        except Exception:
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(GuardError("app_server_disconnected", 503))
            self.pending.clear()
            if self.ws is ws:
                self.ws = None

    async def _call(self, method, params, before_send=None):
        self.counter += 1
        rid = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            if before_send:
                before_send()
            await self.ws.send(json.dumps({"id":rid, "method":method, "params":params}))
            return await asyncio.wait_for(future, 12)
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()

    async def _connect(self):
        if self.ws is not None:
            return
        from websockets.asyncio.client import connect
        try:
            ws = await connect(self.url, proxy=None, compression=None, open_timeout=5,
                               close_timeout=2, max_size=4*1024*1024)
            self.ws = ws
            self.reader = asyncio.create_task(self._receive(ws))
            auth_file=os.environ.get('TELEGRAM_GUARD_EVENT_BRIDGE_AUTH_FILE')
            if auth_file:
                from .files import read_json
                key=read_json(Path(auth_file))['key']
                nonce=secrets.token_hex(32)
                challenge=await self._call('bridge/challenge',{'nonce':nonce})
                server=challenge['nonce']
                def sign(domain):
                    return hmac.new(key.encode(),f'{domain}:{nonce}:{server}'.encode(),hashlib.sha256).hexdigest()
                if not hmac.compare_digest(challenge['signature'],sign('server')):
                    raise GuardError('event_bridge_identity_mismatch',503)
                await self._call('bridge/authenticate',{'role':'telegram_guard','signature':sign('client-telegram_guard')})
            await self._call("initialize", {"clientInfo":{"name":"telegram_guard_bridge","version":"2.6.0"},
                                           "capabilities":{"experimentalApi":True}})
            await ws.send(json.dumps({"method":"initialized","params":{}}))
        except Exception:
            await self.close()
            raise GuardError("app_server_unavailable", 503) from None

    async def _loaded(self, thread):
        cursor = None
        for _ in range(20):
            response = await self._call("thread/loaded/list", {"cursor":cursor,"limit":100})
            if thread in response.get("data", []):
                return
            cursor = response.get("nextCursor")
            if not cursor:
                break
        raise GuardError("target_thread_not_loaded", 409)

    async def ready(self, thread):
        async with self.lock:
            await self._connect()
            await self._loaded(thread)

    async def deliver(self, thread, payload, before_send):
        async with self.lock:
            await self._connect()
            await self._loaded(thread)
            dispatched = False
            def commit_dispatch():
                nonlocal dispatched
                before_send()
                dispatched = True
            try:
                response = await self._call("turn/start", {
                    "threadId":thread, "input":[],
                    "toolOutput":{"name":"telegram_incoming", "namespace":"telegram_guard",
                                  "output":json.dumps(payload, ensure_ascii=False)}
                }, commit_dispatch)
                turn = response["turn"]["id"]
                if not isinstance(turn, str) or not turn:
                    raise ValueError()
                return turn
            except RpcRejected:
                raise
            except BaseException:
                if dispatched:
                    raise DeliveryUnknown() from None
                raise

    async def close(self):
        ws, self.ws = self.ws, None
        if ws:
            await ws.close()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.reader = None

