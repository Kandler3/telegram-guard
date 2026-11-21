"""Shared send policy and durable idempotency. No HTTP server."""
from __future__ import annotations
import asyncio
import secrets
import uuid
from dataclasses import replace
from .attachments import prepare_attachments
from .model import GuardError, Send, chat_id, exact, integer

def send_failure_code(error):
    # Never expose str(error): it can include a request, text or secrets.
    if isinstance(error, TimeoutError):
        return "telegram_timeout"
    if isinstance(error, GuardError):
        return error.code
    from telethon.errors import RPCError
    if isinstance(error, RPCError):
        return "telegram_rpc_" + type(error).__name__
    return "send_internal_" + type(error).__name__

class Broker:
    def __init__(self, policy_loader, transport, store, protected_roots=()):
        self.policy_loader, self.transport, self.store = policy_loader, transport, store
        self.protected_roots = protected_roots
        self.lock = asyncio.Lock()

    def policy(self):
        try:
            return self.policy_loader()
        except Exception:
            raise GuardError("policy_unavailable", 503) from None

    async def send(self, body):
        intent = Send.parse(body)
        async with self.lock:
            policy = self.policy()
            policy.require(intent.chat)
            intent.check_expiry()
            if intent.content and "from_chat_id" in intent.content:
                policy.require_read(intent.content["from_chat_id"])
            async with prepare_attachments(intent.content, self.protected_roots) as files:
                intent = replace(intent, attachments={k: {"size": v.size, "sha256": v.sha256} for k, v in files.items()})
                source = await self.read_peer(intent.content["from_chat_id"]) if intent.content and "from_chat_id" in intent.content else None
                return await self._send(intent, files, source)

    async def _send(self, intent, files, resolved_source=None):
        def guard():
            policy = self.policy()
            peer = policy.require(intent.chat)
            intent.check_expiry()
            source = None
            if intent.content and "from_chat_id" in intent.content:
                source = policy.require_read(intent.content["from_chat_id"]) or resolved_source
            return peer, source

        peer, _ = guard()
        result = self.store.reserve(intent, self.policy())
        if result:
            return result
        try:
            # Recheck after waits and uploads, before each network operation.
            guard()
            if intent.content is not None or intent.options:
                mids = await self.transport.send_content(intent, files, guard)
            elif hasattr(self.transport, "send_guarded_text"):
                mids = [await self.transport.send_guarded_text(peer, intent.text, secrets.randbits(63) or 1, guard)]
            else:
                mids = [await self.transport.send(peer, intent.text, secrets.randbits(63) or 1)]
            if (len(mids) != intent.message_count or len(set(mids)) != len(mids)
                    or any(type(mid) is not int or mid <= 0 for mid in mids)):
                raise RuntimeError("Invalid Telegram receipt")
            self.store.finish(intent.request_id, mids)
        except asyncio.CancelledError:
            self.store.unknown(intent.request_id, "send_cancelled")
            raise
        except Exception as error:
            self.store.unknown(intent.request_id, send_failure_code(error))
            raise GuardError("delivery_unknown_do_not_resend", 409) from None
        return {"state": "sent", "message_id": mids[0], "message_ids": mids,
                "request_id": intent.request_id, "replayed": False}

    def chats(self):
        return [{"chat_id": p.chat, "label": p.label} for p in self.policy().peers.values()]

    async def history(self, body):
        exact(body, ("chat_id", "limit"))
        target = chat_id(body["chat_id"])
        self.policy().require_read(target)
        limit = integer(body["limit"], 1, 50)
        peer = await self.read_peer(target)
        try:
            messages = await self.transport.history(peer, limit)
        except Exception:
            raise GuardError("telegram_read_unavailable", 503) from None
        self.policy().require_read(target)
        return {"messages": messages, "untrusted_content": True}

    async def read_peer(self, target):
        peer = self.policy().require_read(target)
        if peer is None:
            peer = await self.transport.resolve_peer(target)
        self.policy().require_read(target)
        return peer

    def status(self, body):
        exact(body, ("request_id",))
        try:
            rid = str(uuid.UUID(body["request_id"]))
        except (ValueError, TypeError, AttributeError):
            raise GuardError("invalid_request_id") from None
        result = self.store.status(rid)
        self.policy().require(result["chat_id"])
        return result

