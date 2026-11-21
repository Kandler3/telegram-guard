"""Only the local MCP process knows Telegram credentials or accepts MTProto requests."""
import asyncio
from pathlib import Path

def make_client(config, state):
    from telethon import TelegramClient
    return TelegramClient(str(Path(state) / "telegram"), config["api_id"], config["api_hash"],
                          request_retries=0, flood_sleep_threshold=0,
                          receive_updates=True, catch_up=True, sequential_updates=True,
                          connection_retries=3, auto_reconnect=True)

def input_peer(peer):
    from telethon import types
    if peer.kind == "user":
        return types.InputPeerUser(peer.entity_id, peer.access_hash)
    if peer.kind == "chat":
        return types.InputPeerChat(peer.entity_id)
    return types.InputPeerChannel(peer.entity_id, peer.access_hash)

class TelegramTransport:
    def __init__(self, client):
        self.client = client

    async def resolve_peer(self, target):
        from telethon import types, utils
        from .model import Peer, GuardError
        try:
            try:
                peer = await asyncio.wait_for(self.client.get_input_entity(target), 15)
            except (ValueError, TypeError):
                async def find():
                    async for dialog in self.client.iter_dialogs():
                        if dialog.id == target:
                            return dialog.input_entity
                    raise ValueError()
                peer = await asyncio.wait_for(find(), 45)
            if utils.get_peer_id(peer) != target:
                raise ValueError()
            if isinstance(peer, types.InputPeerUser):
                kind, eid, ah = "user", peer.user_id, peer.access_hash
            elif isinstance(peer, types.InputPeerChat):
                kind, eid, ah = "chat", peer.chat_id, None
            elif isinstance(peer, types.InputPeerChannel):
                kind, eid, ah = "channel", peer.channel_id, peer.access_hash
            else:
                raise ValueError()
            return Peer.parse({"chat_id":target,"label":str(target),"kind":kind,"entity_id":eid,"access_hash":ah})
        except Exception:
            raise GuardError("chat_resolution_unavailable", 503) from None

    async def dialogs(self, search, limit, offset, policy_loader):
        async def collect():
            visible = []
            scanned = 0
            async for dialog in self.client.iter_dialogs():
                scanned += 1
                if scanned > 10000:
                    from .model import GuardError
                    raise GuardError("too_many_chats", 422)
                cid, label = dialog.id, dialog.name or str(dialog.id)
                username = getattr(dialog.entity, "username", None) or ""
                policy = policy_loader()
                if not (policy.can_read(cid) or policy.can_write(cid)):
                    continue
                if search.casefold() not in (label + " @" + username + " " + str(cid)).casefold():
                    continue
                visible.append({"chat_id":cid,"label":label,"username":username})
                if len(visible) > offset + limit:
                    break
            policy = policy_loader()
            rows = [dict(row, can_read=policy.can_read(row["chat_id"]), can_write=policy.can_write(row["chat_id"]))
                    for row in visible[offset:offset+limit]
                    if policy.can_read(row["chat_id"]) or policy.can_write(row["chat_id"])]
            return {"chats":rows,"next_offset":offset+limit if len(visible)>offset+limit else None,
                    "read_default":policy.read_default,"untrusted_content":True}
        return await asyncio.wait_for(collect(), 60)

    async def send_content(self, intent, files, guard):
        from .media_transport import send_content
        return await send_content(self.client, intent, files, guard)

    async def send_guarded_text(self, peer, text, random_id, guard):
        return await self.send(peer, text, random_id, guard)

    async def send(self, peer, text, random_id, guard=None):
        from telethon import functions, types
        # Legacy text path: no parser, automatic splitting or caller-selected send_as.
        # send_as is for channels/supergroups. Private/basic chats already use
        # the authenticated personal account; don't send a channel-specific flag.
        sender = None
        if peer.kind == "channel":
            sender = await asyncio.wait_for(self.client.get_me(input_peer=True), timeout=15)
            if not isinstance(sender, types.InputPeerUser):
                raise RuntimeError("Cannot pin personal sender")
        if guard:
            peer, _ = guard()
        result = await asyncio.wait_for(self.client(functions.messages.SendMessageRequest(
            peer=input_peer(peer), message=text, random_id=random_id,
            no_webpage=True, send_as=sender)), timeout=30)
        if isinstance(result, types.UpdateShortSentMessage):
            return result.id
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return update.id
        # Telegram may have accepted the message; do not invent confirmation or retry.
        raise RuntimeError("No delivery receipt")

    async def history(self, peer, limit):
        messages = await asyncio.wait_for(self.client.get_messages(input_peer(peer), limit=limit), timeout=20)
        return [{"id": m.id, "sender_id": m.sender_id, "outgoing": bool(m.out),
                 "date": m.date.isoformat() if m.date else None, "text": m.message or ""}
                for m in messages]
