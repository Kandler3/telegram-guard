"""Strict wire validation, intentionally independent of Telegram and HTTP."""
from __future__ import annotations
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

class GuardError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status

def exact(obj, required, optional=()):
    if not isinstance(obj, dict) or set(obj) - set(required) - set(optional) or set(required) - set(obj):
        raise GuardError("invalid_fields")

def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise GuardError("invalid_integer")
    return value

def chat_id(value):
    result = integer(value, -(2**63) + 1, 2**63 - 1)
    if result == 0:
        raise GuardError("invalid_chat")
    return result

def nonempty(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise GuardError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise GuardError("invalid_unicode") from None
    return value

def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise GuardError("invalid_expiry") from None

@dataclass(frozen=True)
class Send:
    chat: int
    text: str
    request_id: str
    authorization: dict
    content: dict | None = None
    options: dict | None = None
    attachments: dict | None = None

    @classmethod
    def parse(cls, body):
        from .content import parse_content, parse_options
        exact(body, ("chat_id", "request_id", "authorization"), ("text", "content", "options"))
        target = chat_id(body["chat_id"])
        content = parse_content(body["content"]) if body.get("content") is not None else None
        text = body.get("text", "")
        if not isinstance(text, str):
            raise GuardError("invalid_text")
        if text != "" or content is None:
            text = nonempty(text, 4096)
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise GuardError("text_too_long")
        options = parse_options(body["options"] if "options" in body else {}, text)
        if content:
            if "from_chat_id" in content:
                chat_id(content["from_chat_id"])
            if options.get("link_preview"):
                raise GuardError("link_preview_requires_text")
            if content["type"] == "file" and text:
                if content["caption"] or content["entities"]:
                    raise GuardError("duplicate_caption")
                content["caption"] = text
                content["entities"] = options.pop("entities", [])
                text = ""
                content = parse_content(content)
            if content["type"] not in ("existing_media",) and (text or options.get("entities")):
                raise GuardError("caption_not_supported")
        try:
            rid = str(uuid.UUID(body["request_id"]))
        except (ValueError, AttributeError, TypeError):
            raise GuardError("invalid_request_id") from None
        a = body["authorization"]
        exact(a, ("user_instruction", "scope", "scope_chat_ids", "source", "expires_at"))
        nonempty(a["user_instruction"], 4000)
        nonempty(a["scope"], 1000)
        nonempty(a["source"], 500)
        if not isinstance(a["scope_chat_ids"], list) or not 1 <= len(a["scope_chat_ids"]) <= 100:
            raise GuardError("invalid_scope")
        ids = [chat_id(i) for i in a["scope_chat_ids"]]
        if len(ids) != len(set(ids)) or target not in ids:
            raise GuardError("authorization_chat_mismatch", 403)
        if a["expires_at"] is not None:
            timestamp(a["expires_at"])
        return cls(target, text, rid, dict(a), content, options)

    @property
    def message_count(self):
        if self.content and self.content["type"] == "album":
            return len(self.content["items"])
        if self.content and self.content["type"] == "forward":
            return len(self.content["message_ids"])
        return 1

    def check_expiry(self):
        if self.authorization["expires_at"] is not None and timestamp(self.authorization["expires_at"]) <= datetime.now(timezone.utc):
            raise GuardError("authorization_expired", 403)

    def digest(self):
        # A request ID is bound to the complete intent, not just the text.
        payload = {"chat_id": self.chat, "text": self.text, "authorization": self.authorization}
        # Preserve v2.1 text digests so prior UUIDs remain idempotent after upgrade.
        if self.content is not None:
            payload["content"] = self.content
        if self.options:
            payload["options"] = self.options
        if self.attachments:
            payload["attachments"] = self.attachments
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

@dataclass(frozen=True)
class Peer:
    chat: int
    label: str
    kind: str
    entity_id: int
    access_hash: int | None

    @classmethod
    def parse(cls, p):
        exact(p, ("chat_id", "label", "kind", "entity_id", "access_hash"))
        cid = chat_id(p["chat_id"])
        entity_id = integer(p["entity_id"], 1, 2**63 - 1)
        kind = p["kind"]
        expected = {"user": entity_id, "chat": -entity_id, "channel": -(10**12 + entity_id)}
        if kind not in expected or cid != expected[kind]:
            raise GuardError("invalid_peer_identity")
        ah = p["access_hash"]
        if kind == "chat":
            if ah is not None:
                raise GuardError("invalid_access_hash")
        else:
            integer(ah, -(2**63), 2**63 - 1)
        return cls(cid, nonempty(p["label"], 200), kind, entity_id, ah)

@dataclass(frozen=True)
class Policy:
    peers: object
    per_minute: int
    per_day: int
    read_default: bool = False
    read_allow: frozenset = frozenset()
    read_deny: frozenset = frozenset()
    write_allow: frozenset | None = None

    @classmethod
    def parse(cls, body):
        version = body.get("version") if isinstance(body, dict) else None
        fields = ("version", "chats", "messages_per_minute", "messages_per_day")
        exact(body, fields if version == 1 else (*fields, "read", "write"))
        if type(version) is not int or version not in (1, 2) or not isinstance(body["chats"], list):
            raise GuardError("invalid_policy")
        peers = [Peer.parse(p) for p in body["chats"]]
        if len(peers) != len({p.chat for p in peers}):
            raise GuardError("duplicate_chat")
        ids = {p.chat for p in peers}
        default, allow, deny, write = False, frozenset(ids), frozenset(), frozenset(ids)
        if version == 2:
            exact(body["read"], ("default", "allow", "deny"))
            exact(body["write"], ("allow",))
            default = body["read"]["default"]
            if type(default) is not bool:
                raise GuardError("invalid_read_default")
            def parse_ids(values):
                if not isinstance(values, list) or len(values) > 10000:
                    raise GuardError("invalid_permission_ids")
                parsed = [chat_id(i) for i in values]
                if len(parsed) != len(set(parsed)):
                    raise GuardError("duplicate_chat")
                return frozenset(parsed)
            allow, deny, write = (parse_ids(body["read"]["allow"]), parse_ids(body["read"]["deny"]),
                                  parse_ids(body["write"]["allow"]))
            if allow & deny or not write <= ids:
                raise GuardError("invalid_permission_ids")
        return cls(MappingProxyType({p.chat: p for p in peers}),
                   integer(body["messages_per_minute"], 1, 60),
                   integer(body["messages_per_day"], 1, 10000), default, allow, deny, write)

    def require(self, chat):
        """Legacy name remains an explicit write check."""
        return self.require_write(chat)

    def can_read(self, chat):
        cid = chat_id(chat)
        return cid not in self.read_deny and (self.read_default or cid in self.read_allow)

    def can_write(self, chat):
        cid = chat_id(chat)
        return cid in (self.write_allow if self.write_allow is not None else self.peers)

    def require_read(self, chat):
        cid = chat_id(chat)
        if not self.can_read(cid):
            raise GuardError("chat_read_not_allowed", 403)
        return self.peers.get(cid)

    def require_write(self, chat):
        cid = chat_id(chat)
        if not self.can_write(cid):
            raise GuardError("chat_not_allowed", 403)
        try:
            return self.peers[cid]
        except KeyError:
            raise GuardError("chat_not_allowed", 403) from None
