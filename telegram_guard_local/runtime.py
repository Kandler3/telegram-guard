"""One bounded operation at a time; session never escapes through MCP output."""
import asyncio
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from .engine import Broker
from .files import checked, read_json
from .model import GuardError, Policy, Send, chat_id, exact, integer
from .store import Store
from .transport import TelegramTransport, make_client
from .attachments import validate_attachment_paths

class SessionLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None
    def __enter__(self):
        checked(self.path, missing=True)
        self.stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise GuardError("session_busy_try_later", 409) from None
        return self
    def __exit__(self, *args):
        if self.stream is not None:
            import os
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None

class Runtime:
    def __init__(self, home, client_factory=make_client, app_server_url=None, persistent=False):
        self.home = Path(home).absolute()
        self.private = self.home / "private"
        self.client_factory = client_factory
        self.mutex = asyncio.Lock()
        self.jobs = OrderedDict()
        self.response_timeout = 20
        self.persistent = persistent
        self._connection_context = self._live_broker = None
        self.download_root = Path.home() / "Downloads" / "Telegram Guard"
        from .stream import NativeStream
        self.stream = NativeStream(self) if persistent else None
        from .app_server import AppServer
        from .subscriptions import Subscriptions
        self.subscriptions = Subscriptions(self, AppServer(app_server_url) if app_server_url else None)
        self.protected_roots = (self.home, Path.home() / ".codex" / "auth.json",
                                Path.home() / ".codex" / "config.toml", Path.home() / ".codex" / "secrets")

    def policy(self):
        try:
            checked(self.home, directory=True)
            return Policy.parse(read_json(self.home / "whitelist.json"))
        except Exception:
            raise GuardError("policy_unavailable", 503) from None

    def chats(self):
        return {"chats":[{"chat_id":p.chat,"label":p.label} for p in self.policy().peers.values()]}

    async def list_chats(self, search="", limit=100, offset=0):
        if not isinstance(search, str) or len(search) > 200:
            raise GuardError("invalid_search")
        integer(limit, 1, 100)
        integer(offset, 0, 10000)
        policy = self.policy()
        if not policy.read_default:
            rows = [{"chat_id":p.chat,"label":p.label,"can_read":policy.can_read(p.chat),
                     "can_write":policy.can_write(p.chat)} for p in policy.peers.values()
                    if (policy.can_read(p.chat) or policy.can_write(p.chat)) and
                    search.casefold() in (p.label + " " + str(p.chat)).casefold()]
            return {"chats":rows[offset:offset+limit],"next_offset":offset+limit if len(rows)>offset+limit else None,
                    "read_default":False}
        async with self.mutex:
            async with self.connection() as broker:
                return await broker.transport.dialogs(search, limit, offset, self.policy)

    @asynccontextmanager
    async def connection(self):
        if not self.persistent:
            async with self._new_connection() as broker:
                yield broker
            return
        if self._live_broker and not self._live_broker.transport.client.is_connected():
            await self._connection_context.__aexit__(None,None,None)
            self._connection_context = self._live_broker = None
        if self._live_broker is None:
            context = self._new_connection()
            broker = await context.__aenter__()
            self._connection_context, self._live_broker = context, broker
        yield self._live_broker

    async def close(self):
        await self.subscriptions.close()
        if self._connection_context:
            await self._connection_context.__aexit__(None,None,None)
            self._connection_context = self._live_broker = None

    async def read_operation(self, operation, target=None, **args):
        from .operations import read_operation
        return await asyncio.wait_for(read_operation(self,operation,target,**args),120 if operation=="download" else 60)

    async def mutate(self, operation, target, request_id, authorization, **args):
        from .operations import mutate
        return await mutate(self,operation,target,request_id,authorization,**args)

    @asynccontextmanager
    async def _new_connection(self):
        checked(self.private, directory=True)
        # Refuse reparse tricks in session/database files, including files Telethon may use.
        for name in ("telegram.session","telegram.session-journal","audit.sqlite3",
                     "audit.sqlite3-wal","audit.sqlite3-shm","session.lock"):
            checked(self.private / name, missing=True)
        with SessionLock(self.private / "session.lock"):
            config = read_json(self.private / "account.json")
            exact(config, ("version","api_id","api_hash","account_id"))
            if config["version"] != 1 or config["account_id"] is None:
                raise GuardError("human_login_required", 503)
            integer(config["api_id"], 1, 2**31 - 1)
            integer(config["account_id"], 1, 2**63 - 1)
            client = self.client_factory(config, self.private)
            try:
                if self.stream:
                    self.stream.attach(client)
                await asyncio.wait_for(client.connect(), 15)
                if not await asyncio.wait_for(client.is_user_authorized(), 15):
                    raise GuardError("human_login_required", 503)
                me = await asyncio.wait_for(client.get_me(), 15)
                if not me or me.bot or me.id != config["account_id"]:
                    raise GuardError("account_mismatch", 503)
                store = Store(self.private / "audit.sqlite3")
                try:
                    yield Broker(self.policy, TelegramTransport(client), store, self.protected_roots)
                finally:
                    store.close()
            except GuardError:
                raise
            except Exception:
                raise GuardError("telegram_connection_unavailable", 503) from None
            finally:
                await client.disconnect()

    async def send(self, target, text, request_id, authorization, content=None, options=None):
        body = {"chat_id":target, "text":text, "request_id":request_id, "authorization":authorization}
        if content is not None:
            body["content"] = content
        if options is not None:
            body["options"] = options
        intent = Send.parse(body)
        policy = self.policy()
        policy.require(intent.chat)
        if intent.content and "from_chat_id" in intent.content:
            policy.require_read(intent.content["from_chat_id"])
        intent.check_expiry()
        validate_attachment_paths(intent.content, self.protected_roots)
        previous = self.jobs.get(intent.request_id)
        if previous and not previous[2].done():
            if previous[0] != intent.digest():
                raise GuardError("request_id_conflict", 409)
            return {"state": "pending", "request_id": intent.request_id, "chat_id": intent.chat}
        # Bound queued work and retained tasks, without cancelling active sends.
        if sum(not job[2].done() for job in self.jobs.values()) >= 8:
            raise GuardError("sender_busy", 409)
        for rid, job in list(self.jobs.items()):
            if len(self.jobs) < 100:
                break
            if job[2].done():
                del self.jobs[rid]
        task = asyncio.create_task(self._send(body))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self.jobs[intent.request_id] = (intent.digest(), intent.chat, task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.response_timeout)
        except TimeoutError:
            # Upload continues in this MCP process. Do not start another request.
            return {"state": "pending", "request_id": intent.request_id, "chat_id": intent.chat}

    async def _send(self, body):
        async with self.mutex:
            intent = Send.parse(body)
            self.policy().require(intent.chat)
            intent.check_expiry()
            async with self.connection() as broker:
                # Account connection happens first, so expiry/policy are rechecked immediately before sending.
                return await broker.send(body)

    async def history(self, target, limit=20):
        self.policy().require_read(chat_id(target))
        integer(limit, 1, 50)
        if self.stream:
            return await self.read_operation("history",target,limit=limit)
        async with self.mutex:
            async with self.connection() as broker:
                return await broker.history({"chat_id":target,"limit":limit})

    def status(self, request_id):
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise GuardError("invalid_request_id") from None
        job = self.jobs.get(request_id)
        if job:
            self.policy().require(job[1])
        # Durable receipts take precedence even if connection teardown fails or hangs.
        try:
            result = self._stored_status(request_id)
        except FileNotFoundError:
            if not job:
                raise GuardError("request_not_found", 404) from None
        except GuardError as error:
            if error.code != "request_not_found" or not job:
                raise
        else:
            return result
        if job:
            if not job[2].done():
                return {"state": "pending", "request_id": request_id, "chat_id": job[1]}
            if job[2].cancelled():
                return {"state": "unknown", "request_id": request_id, "chat_id": job[1],
                        "error_code": "send_cancelled"}
            error = job[2].exception()
            if error and not (isinstance(error, GuardError) and error.code == "delivery_unknown_do_not_resend"):
                from .engine import send_failure_code
                return {"state": "failed", "request_id": request_id, "chat_id": job[1],
                        "error_code": send_failure_code(error)}
        raise GuardError("request_not_found", 404)

    def _stored_status(self, request_id):
        checked(self.private, directory=True)
        path = self.private / "audit.sqlite3"
        checked(path)
        store = Store(path)
        try:
            return Broker(self.policy, None, store).status({"request_id":request_id})
        finally:
            store.close()


    async def watch(self, target, after_id=None, wait_seconds=30, monitoring_until=None):
        target=chat_id(target)
        self.policy().require_read(target)
        if after_id is not None:
            integer(after_id,0,2**31-1)
        integer(wait_seconds,0,45)
        if self.stream:
            return await self.stream.watch(target,after_id,wait_seconds,monitoring_until=monitoring_until)
        raise GuardError("streaming_requires_shared_broker",503)
