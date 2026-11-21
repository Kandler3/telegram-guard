import asyncio
import copy
import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from telethon import functions, types
from telegram_guard_local.attachments import prepare_attachments, attachment_path
from telegram_guard_local.engine import Broker
from telegram_guard_local.model import GuardError, Policy, Send
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.store import Store
from telegram_guard_local.transport import TelegramTransport
from telegram_guard_local.files import write_json
from telegram_guard_local.mcp_server import create_mcp
from test_local import body, policy


class Client:
    """Serialization-checked Telegram substitute; never makes network requests."""
    def __init__(self):
        self.requests = []
        self.uploads = []
        self.on_upload = None
        self.fail_receipt = False
        self.fail_send = False
        self.source = SimpleNamespace(media=types.MessageMediaGeo(types.GeoPoint(2., 1., 0)), noforwards=False)

    async def upload_file(self, stream, **kwargs):
        self.uploads.append((kwargs["file_name"], stream.read(), kwargs["file_size"]))
        if self.on_upload:
            self.on_upload()
        await kwargs["progress_callback"](kwargs["file_size"], kwargs["file_size"])
        return types.InputFile(len(self.uploads), 1, kwargs["file_name"], "")

    async def get_me(self, input_peer=False):
        return types.InputPeerUser(789, 555)

    async def get_messages(self, peer, **kwargs):
        self.source_peer = peer
        return self.source

    async def __call__(self, request):
        bytes(request)
        self.requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                return types.MessageMediaPhoto(photo=types.Photo(id=11, access_hash=22, file_reference=b"ref",
                    date=datetime.now(timezone.utc), sizes=[], dc_id=1))
            return types.MessageMediaDocument(document=types.Document(id=11, access_hash=22, file_reference=b"ref",
                date=datetime.now(timezone.utc), mime_type=request.media.mime_type, size=4, dc_id=1,
                attributes=request.media.attributes))
        if self.fail_send:
            raise TimeoutError("must never expose payload")
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            rids = [x.random_id for x in request.multi_media]
        elif isinstance(request, functions.messages.ForwardMessagesRequest):
            rids = request.random_id
        else:
            rids = [request.random_id]
        if self.fail_receipt:
            rids = rids[:-1]
        return types.Updates(updates=[types.UpdateMessageID(id=100+i, random_id=rid) for i, rid in enumerate(rids)],
            users=[], chats=[], date=datetime.now(timezone.utc), seq=1)


def setup(tmp_path):
    raw = policy()
    raw["messages_per_minute"] = 20
    client = Client()
    store = Store(tmp_path / "audit.sqlite3")
    broker = Broker(lambda: Policy.parse(raw), TelegramTransport(client), store, (tmp_path / "protected",))
    return broker, client, store, raw


def payload(content, **kwargs):
    p = body()
    p["text"] = ""
    p["content"] = content
    p.update(kwargs)
    return p


def file_content(tmp_path, **kwargs):
    path = tmp_path / "report.dat"
    path.write_bytes(b"sample bytes")
    return dict(type="file", path=str(path), **kwargs)


@pytest.mark.parametrize("kind,attribute", [
    ("document", types.DocumentAttributeFilename), ("photo", None),
    ("video", types.DocumentAttributeVideo), ("animation", types.DocumentAttributeAnimated),
    ("audio", types.DocumentAttributeAudio), ("voice", types.DocumentAttributeAudio),
    ("video_note", types.DocumentAttributeVideo), ("sticker", types.DocumentAttributeSticker),
])
def test_file_types_real_tl_serialization(tmp_path, kind, attribute):
    broker, client, store, _ = setup(tmp_path)
    content = file_content(tmp_path, kind=kind, width=512, height=512, duration=1.5)
    result = asyncio.run(broker.send(payload(content)))
    assert result["message_ids"] == [100]
    assert client.uploads == [("report.dat", b"sample bytes", 12)]
    request = client.requests[-1]
    assert request.send_as is None and request.peer.user_id == 123
    assert isinstance(request, functions.messages.SendMediaRequest)
    if attribute:
        assert any(isinstance(a, attribute) for a in request.media.attributes)
    else:
        assert isinstance(request.media, types.InputMediaUploadedPhoto)
    if kind == "document":
        assert request.media.force_file
    if kind == "voice":
        assert next(a for a in request.media.attributes if isinstance(a, types.DocumentAttributeAudio)).voice
    if kind == "video_note":
        assert next(a for a in request.media.attributes if isinstance(a, types.DocumentAttributeVideo)).round_message
    store.close()


@pytest.mark.parametrize("content,expected", [
    ({"type":"contact","phone_number":"+1234567","first_name":"Test"}, types.InputMediaContact),
    ({"type":"location","latitude":55.7,"longitude":37.6}, types.InputMediaGeoPoint),
    ({"type":"location","latitude":55.7,"longitude":37.6,"live_period":900}, types.InputMediaGeoLive),
    ({"type":"venue","latitude":55.7,"longitude":37.6,"title":"Office","address":"Street"}, types.InputMediaVenue),
    ({"type":"poll","question":"Choice?","answers":["A","B"]}, types.InputMediaPoll),
    ({"type":"poll","question":"Quiz?","answers":["A","B"],"quiz":True,"correct_answers":[1],"solution":"B"}, types.InputMediaPoll),
    ({"type":"dice","emoticon":"🎲"}, types.InputMediaDice),
    ({"type":"checklist","title":"Tasks","items":["One","Two"]}, types.InputMediaTodo),
    ({"type":"story","from_chat_id":123,"story_id":1}, types.InputMediaStory),
    ({"type":"existing_media","from_chat_id":123,"message_id":1}, types.InputMediaGeoPoint),
])
def test_structured_content(tmp_path, content, expected):
    broker, client, store, _ = setup(tmp_path)
    assert asyncio.run(broker.send(payload(content)))["state"] == "sent"
    assert isinstance(client.requests[-1].media, expected)
    store.close()


def test_album_receipts_quota_and_replay(tmp_path):
    broker, client, store, raw = setup(tmp_path)
    raw["messages_per_minute"] = 3
    item = file_content(tmp_path, kind="photo", caption="Photo", spoiler=True)
    p = payload({"type":"album","items":[item, item]})
    sent = asyncio.run(broker.send(p))
    request = client.requests[-1]
    assert isinstance(request, functions.messages.SendMultiMediaRequest)
    assert all(m.media.spoiler for m in request.multi_media)
    assert sent["message_ids"] == [100, 101]
    assert store.status(p["request_id"])["message_ids"] == [100, 101]
    assert asyncio.run(broker.send(p))["replayed"]
    assert len(client.uploads) == 2
    with pytest.raises(GuardError, match="rate_limit"):
        asyncio.run(broker.send(payload(p["content"])))
    assert len(client.uploads) == 2
    store.close()


def test_changed_file_same_uuid_conflicts_without_upload(tmp_path):
    broker, client, store, _ = setup(tmp_path)
    p = payload(file_content(tmp_path))
    asyncio.run(broker.send(p))
    Path(p["content"]["path"]).write_bytes(b"different bytes")
    with pytest.raises(GuardError, match="request_id_conflict"):
        asyncio.run(broker.send(p))
    assert len(client.uploads) == 1
    store.close()


def test_attachment_snapshot_is_immutable_and_removed(tmp_path):
    item = file_content(tmp_path)
    async def run():
        async with prepare_attachments(item) as files:
            record = files[item["path"]]
            Path(item["path"]).write_bytes(b"replacement")
            assert record.stream.read() == b"sample bytes"
            assert record.sha256 == hashlib.sha256(b"sample bytes").hexdigest()
        assert record.stream.closed
    asyncio.run(run())


@pytest.mark.parametrize("name", ["private/account.json", ".env", ".ssh/id_rsa", "telegram.session", "whitelist.json", "protected/data"])
def test_protected_files_never_read_or_uploaded(tmp_path, name):
    broker, client, store, _ = setup(tmp_path)
    # No actual secret files: all paths are synthetic and need not exist.
    with pytest.raises(GuardError, match="protected_attachment"):
        asyncio.run(broker.send(payload({"type":"file","path":str(tmp_path / name)})))
    assert not client.uploads and not client.requests
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 0
    store.close()


def test_hardlink_refused(tmp_path):
    source = tmp_path / "file"
    source.write_bytes(b"test")
    linked = tmp_path / "linked"
    os.link(source, linked)
    with pytest.raises(GuardError, match="not_regular"):
        attachment_path(str(linked))


def test_forward_requires_allowed_source_and_returns_all_ids(tmp_path):
    broker, client, store, _ = setup(tmp_path)
    p = payload({"type":"forward","from_chat_id":999,"message_ids":[10,11]})
    with pytest.raises(GuardError, match="chat_read_not_allowed"):
        asyncio.run(broker.send(p))
    assert not client.requests
    p["content"]["from_chat_id"] = 123
    assert asyncio.run(broker.send(p))["message_ids"] == [100,101]
    assert isinstance(client.requests[-1], functions.messages.ForwardMessagesRequest)
    store.close()


@pytest.mark.parametrize("failure", ["fail_receipt", "fail_send"])
def test_media_unknown_is_durable_and_never_retried(tmp_path, failure):
    broker, client, store, _ = setup(tmp_path)
    setattr(client, failure, True)
    p = payload(file_content(tmp_path))
    with pytest.raises(GuardError, match="delivery_unknown"):
        asyncio.run(broker.send(p))
    store.close()
    broker, _, store, _ = setup(tmp_path)
    broker.transport = TelegramTransport(client)
    assert store.status(p["request_id"])["state"] == "unknown"
    with pytest.raises(GuardError, match="delivery_unknown"):
        asyncio.run(broker.send(p))
    assert len(client.requests) == 1 and len(client.uploads) == 1
    store.close()


@pytest.mark.parametrize("action", ["revoke", "expire"])
def test_policy_rechecked_during_upload(tmp_path, action):
    broker, client, store, raw = setup(tmp_path)
    p = payload(file_content(tmp_path))
    if action == "revoke":
        client.on_upload = lambda: raw.update(chats=[])
    else:
        # Fixed future expiry becomes past under the injected check after uploading.
        original = Send.check_expiry
        def expired(self):
            raise GuardError("authorization_expired", 403)
        client.on_upload = lambda: setattr(Send, "check_expiry", expired)
    try:
        with pytest.raises(GuardError, match="delivery_unknown"):
            asyncio.run(broker.send(p))
        assert not client.requests
    finally:
        if action == "expire":
            Send.check_expiry = original
        store.close()


@pytest.mark.parametrize("content", [
    {"type":"unknown"}, {"type":"contact","phone_number":"+1","first_name":"A","send_as":42},
    {"type":"poll","question":"Q","answers":["A","B"],"quiz":True},
    {"type":"location","latitude":float('nan'),"longitude":1.},
    {"type":"forward","from_chat_id":123,"message_ids":[True]},
    {"type":"forward","from_chat_id":123,"message_ids":[1,1]},
    {"type":"file","path":"C:/fake","kind":"video"},
    {"type":"file","path":"C:/fake","filename":"../key"},
    {"type":"file","path":"C:/fake","kind":"sticker","caption":"wrong"},
    {"type":"contact","phone_number":"+1","first_name":"\ud800"},
])
def test_invalid_content_rejected_before_io(content):
    with pytest.raises(GuardError):
        Send.parse(payload(content))


def test_formatting_reply_and_caption(tmp_path):
    broker, client, store, _ = setup(tmp_path)
    p = payload(file_content(tmp_path), text="😀 bold", options={"reply_to":12,"top_msg_id":8,"silent":True,
        "entities":[{"kind":"bold","offset":3,"length":4}]})
    asyncio.run(broker.send(p))
    req = client.requests[-1]
    assert req.message == "😀 bold" and req.silent
    assert req.entities[0].offset == 3 and req.reply_to.top_msg_id == 8
    p["options"]["entities"][0]["offset"] = 1
    with pytest.raises(GuardError, match="invalid_options"):
        Send.parse(p)
    store.close()


def test_legacy_digest_unchanged():
    p = body()
    prior = {"chat_id":p["chat_id"], "text":p["text"], "authorization":p["authorization"]}
    assert Send.parse(p).digest() == hashlib.sha256(json.dumps(prior, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_background_upload_returns_pending_and_status(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "private").mkdir()
    write_json(home / "whitelist.json", policy())
    client = Client()
    entered, release = asyncio.Event(), asyncio.Event()
    normal_upload = client.upload_file
    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await normal_upload(*args, **kwargs)
    client.upload_file = slow
    class Mock(Runtime):
        @asynccontextmanager
        async def connection(self):
            store = Store(self.private / "audit.sqlite3")
            try:
                yield Broker(self.policy, TelegramTransport(client), store, self.protected_roots)
            finally:
                store.close()
    async def run():
        runtime = Mock(home)
        runtime.response_timeout = .02
        p = payload(file_content(tmp_path))
        args = (p["chat_id"], p["text"], p["request_id"], p["authorization"], p["content"])
        assert (await runtime.send(*args))["state"] == "pending"
        await entered.wait()
        assert runtime.status(p["request_id"])["state"] == "pending"
        assert (await runtime.send(*args))["state"] == "pending"
        release.set()
        await runtime.jobs[p["request_id"]][2]
        assert runtime.status(p["request_id"])["message_ids"] == [100]
        assert len(client.uploads) == 1
    asyncio.run(run())


def test_mcp_schema_exposes_content_without_new_tool_allowlist():
    async def run():
        tools = await create_mcp(None).list_tools()
        send = next(t for t in tools if t.name == "telegram_send")
        assert "content" in send.inputSchema["properties"]
        assert set(send.inputSchema["required"]) == {"chat_id", "request_id", "authorization"}
        assert "AlbumContent" in json.dumps(send.inputSchema)
    asyncio.run(run())


def test_persistent_receipt_survives_failed_disconnect(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "private").mkdir()
    write_json(home / "whitelist.json", policy())
    client = Client()
    class Mock(Runtime):
        @asynccontextmanager
        async def connection(self):
            store = Store(self.private / "audit.sqlite3")
            try:
                yield Broker(self.policy, TelegramTransport(client), store, self.protected_roots)
            finally:
                store.close()
            raise RuntimeError("connection teardown failed")
    async def run():
        runtime = Mock(home)
        p = payload(file_content(tmp_path))
        with pytest.raises(RuntimeError):
            await runtime.send(p["chat_id"], "", p["request_id"], p["authorization"], p["content"])
        assert runtime.status(p["request_id"])["state"] == "sent"
    asyncio.run(run())


def test_partial_album_receipt_stays_unknown(tmp_path):
    broker, client, store, _ = setup(tmp_path)
    client.fail_receipt = True
    item = file_content(tmp_path, kind="document")
    p = payload({"type":"album","items":[item,item]})
    with pytest.raises(GuardError, match="delivery_unknown"):
        asyncio.run(broker.send(p))
    assert store.status(p["request_id"])["state"] == "unknown"
    with pytest.raises(GuardError, match="delivery_unknown"):
        asyncio.run(broker.send(p))
    assert len([r for r in client.requests if isinstance(r, functions.messages.SendMultiMediaRequest)]) == 1
    store.close()


def test_media_channel_identity_pinned(tmp_path):
    broker, client, store, raw = setup(tmp_path)
    raw["chats"] = [{"chat_id":-1000000000123,"label":"Group","kind":"channel","entity_id":123,"access_hash":456}]
    p = payload(file_content(tmp_path), chat_id=-1000000000123)
    p["authorization"]["scope_chat_ids"] = [-1000000000123]
    asyncio.run(broker.send(p))
    assert client.requests[-1].send_as.user_id == 789
    store.close()


def test_invalid_album_combination_and_count(tmp_path):
    item = file_content(tmp_path)
    for items in ([item], [item]*11, [item, dict(item, kind="photo")]):
        with pytest.raises(GuardError, match="invalid_content"):
            Send.parse(payload({"type":"album","items":items}))


def test_relative_remote_and_alternate_stream_paths_refused():
    paths = ["relative/file.pdf", "https://example.com/file.pdf"]
    if os.name == "nt":
        paths += [r"\\server\share\document.pdf", r"C:\document.pdf:hidden", r"\\?\C:\document.pdf"]
    for path in paths:
        with pytest.raises(GuardError):
            attachment_path(path)
