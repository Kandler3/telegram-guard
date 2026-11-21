import asyncio
import copy
import http.client
import io
import json
import threading
from types import SimpleNamespace
import pytest
from telegram_guard_local.files import read_json, write_json
from telegram_guard_local.model import GuardError
from telegram_guard_local.runtime import SessionLock
from telegram_guard_local.whitelist_editor import Editor, EditorServer, load_dialogs, launch


def peer(cid=123, label="Анна Смирнова"):
    return {"chat_id": cid, "label": label, "kind": "user", "entity_id": cid, "access_hash": 9988776655}


def row(p):
    return {"chat_id": str(p["chat_id"]), "label": p["label"], "kind": "user", "username": "example",
            "reason": "", "peer": p}


@pytest.fixture
def editor(tmp_path):
    (tmp_path / "private").mkdir()
    write_json(tmp_path / "whitelist.json", {"version": 1, "chats": [peer()], "messages_per_minute": 4, "messages_per_day": 80})
    async def loader(home):
        return [row(peer()), row(peer(456, 'Дизайн <img src=x onerror=alert(1)>')),
                {"chat_id": "-1000000000789", "label": "Новости", "kind": "channel", "username": "news",
                 "reason": "Каналы не поддерживаются", "peer": None}]
    return Editor(tmp_path, search="Дизайн", loader=loader)


def test_editor_load_is_read_only_and_hides_peer_secrets(editor):
    path = editor.home / "whitelist.json"
    original = path.read_bytes()
    response = editor.chats()
    assert response["chats"][0]["can_write"] is True
    assert response["chats"][1]["can_write"] is False  # Search is not selection.
    assert response["chats"][2]["write_selectable"] is False
    assert "access_hash" not in json.dumps(response) and "9988776655" not in json.dumps(response)
    assert path.read_bytes() == original


def test_editor_saves_read_blacklist_without_changing_write(editor):
    data=editor.chats()
    assert all(r['can_read'] for r in data['chats'])
    result=editor.save({'revision':data['revision'],'read':{'default':True,'allow':[],'deny':['123']},'write':['123']})
    from telegram_guard_local.model import Policy
    policy=Policy.parse(read_json(editor.home/'whitelist.json'))
    assert policy.can_write(123) and not policy.can_read(123) and policy.can_read(456)
    assert next(r for r in result['chats'] if r['chat_id']=='123')['can_write']
    result=editor.save({'revision':result['revision'],'read':{'default':True,'allow':[],'deny':[]},'write':['123']})
    policy=Policy.parse(read_json(editor.home/'whitelist.json'))
    assert policy.can_read(999) and policy.can_write(123) and not policy.can_write(456)


def test_save_updates_only_chats_preserving_limits_and_exact_existing_record(editor):
    data = editor.chats()
    response = editor.save({"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": ["123", "456"]})
    saved = read_json(editor.home / "whitelist.json")
    assert saved["messages_per_day"] == 80 and saved["messages_per_minute"] == 4
    assert saved["chats"][0] == peer()
    assert [p["chat_id"] for p in saved["chats"]] == [123, 456]
    assert response["revision"] != data["revision"]
    # All removals are supported; no automatic restoration of an empty selection.
    editor.save({"revision": response["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": []})
    assert read_json(editor.home / "whitelist.json")["write"]["allow"] == []


def test_missing_previously_allowed_chat_is_retained_until_human_removes(editor):
    saved = read_json(editor.home / "whitelist.json")
    saved["chats"].append(peer(999, "Удалённый из диалогов"))
    write_json(editor.home / "whitelist.json", saved)
    data = editor.chats()
    missing = next(r for r in data["chats"] if r["chat_id"] == "999")
    assert missing["can_write"] and missing["write_selectable"] and missing["reason"]
    result = editor.save({"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": ["999"]})
    assert read_json(editor.home / "whitelist.json")["write"]["allow"] == [999]
    result = editor.save({"revision": result["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": []})
    with pytest.raises(GuardError, match="unknown_chat"):
        editor.save({"revision": result["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": ["999"]})


@pytest.mark.parametrize("selection", [["999"], ["-1000000000789"], [123], [True], ["123", "123"], ["0"], ["0123"], ["1e3"], "123", ["1"] * 10001])
def test_invalid_or_unavailable_selection_does_not_write(editor, selection):
    data = editor.chats()
    original = (editor.home / "whitelist.json").read_bytes()
    with pytest.raises(GuardError):
        editor.save({"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": selection})
    assert (editor.home / "whitelist.json").read_bytes() == original


def test_conflicting_browser_does_not_overwrite_human_changes(editor):
    first = editor.chats()
    other = Editor(editor.home, loader=editor.loader)
    second = other.chats()
    other.save({"revision": second["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": ["456"]})
    with pytest.raises(GuardError, match="whitelist_changed"):
        editor.save({"revision": first["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": []})
    assert read_json(editor.home / "whitelist.json")["write"]["allow"] == [456]


def test_policy_lock_prevents_another_writer(editor):
    data = editor.chats()
    with SessionLock(editor.home / "private" / "whitelist.lock"):
        with pytest.raises(GuardError, match="session_busy_try_later"):
            editor.save({"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": []})


def test_expiry_after_telegram_load_drops_the_result(editor):
    async def expired(home):
        editor.deadline = editor.clock() - 1
        return []
    editor.loader = expired
    with pytest.raises(GuardError, match="editor_expired"):
        editor.chats()


def test_expired_save_does_not_write(editor):
    data = editor.chats()
    editor.deadline = editor.clock() - 1
    with pytest.raises(GuardError, match="editor_expired"):
        editor.save({"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": []})


class Browser:
    def __init__(self, server):
        self.server = server
        self.cookie = ""

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        h = {"X-Editor-Request": "1", "Cookie": self.cookie, "Sec-Fetch-Site": "same-origin"}
        if body is not None or raw is not None:
            h.update({"Origin": self.server.origin, "Content-Type": "application/json"})
        if headers:
            h.update(headers)
        connection.request(method, path, body=raw if raw is not None else json.dumps(body).encode() if body is not None else None, headers=h)
        response = connection.getresponse()
        content = response.read()
        out_headers = dict(response.getheaders())
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";")[0]
        connection.close()
        return response.status, content, out_headers

    def unlock(self):
        return self.request("POST", "/api/unlock", {"token": self.server.editor.bootstrap})


@pytest.fixture
def browser(editor):
    server = EditorServer(editor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Browser(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_public_link_reveals_no_chats_and_bootstrap_is_single_use(browser, editor):
    status, raw, headers = browser.request("GET", "/")
    assert status == 200 and editor.bootstrap.encode() not in raw
    assert b"frame-ancestors 'none'" in headers["Content-Security-Policy"].encode()
    assert browser.request("GET", "/api/chats")[0] == 403
    assert browser.request("GET", "/api/session")[0] == 403
    assert editor.rows is None
    token = editor.bootstrap
    status, _, headers = browser.unlock()
    assert status == 200 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert browser.request("POST", "/api/unlock", {"token": token})[0] == 403
    status, raw, _ = browser.request("GET", "/api/chats")
    assert status == 200 and "access_hash" not in raw.decode()


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"X-Editor-Request": ""}, {"Content-Type": "text/plain"}])
def test_foreign_browser_requests_cannot_unlock(browser, editor, headers):
    token = editor.bootstrap
    assert browser.request("POST", "/api/unlock", {"token": token}, headers)[0] == 403
    assert editor.bootstrap == token and editor.rows is None


def test_save_requires_cookie_origin_and_csrf(browser, editor):
    browser.unlock()
    data = json.loads(browser.request("GET", "/api/chats")[1])
    body = {"revision": data["revision"], "read": {"default": True, "allow": [], "deny": []}, "write": ["456"]}
    assert browser.request("POST", "/api/save", body)[0] == 403
    assert browser.request("POST", "/api/save", body, {"X-Editor-CSRF": editor.csrf, "Origin": "https://evil.example"})[0] == 403
    cookie = browser.cookie
    browser.cookie = ""
    assert browser.request("POST", "/api/save", body, {"X-Editor-CSRF": editor.csrf})[0] == 403
    browser.cookie = cookie
    assert browser.request("POST", "/api/save", body, {"X-Editor-CSRF": editor.csrf})[0] == 200
    assert read_json(editor.home / "whitelist.json")["write"]["allow"] == [456]


def test_request_validation_no_path_access_and_close(browser, editor):
    browser.unlock()
    headers = {"X-Editor-CSRF": editor.csrf}
    assert browser.request("POST", "/api/save", headers=headers, raw=b'{"read": {"default": True, "allow": [], "deny": []}, "write":[],"read": {"default": True, "allow": [], "deny": []}, "write":[123]}')[0] == 400
    assert browser.request("POST", "/api/save", headers=headers, raw=b'x' * (256 * 1024 + 1))[0] == 400
    assert browser.request("GET", "/../whitelist.json")[0] == 404
    assert browser.request("GET", "/private/account.json")[0] == 404
    assert browser.request("POST", "/api/close", {}, headers)[0] == 200
    assert browser.request("GET", "/api/chats")[0] == 410


def test_dialog_collector_uses_only_dialog_metadata(editor, monkeypatch):
    from telegram_guard_local.whitelist_editor import collect_dialogs
    from telethon import types
    calls = []
    class Client:
        async def iter_dialogs(self):
            calls.append("dialogs")
            yield SimpleNamespace(id=123, name="Анна", entity=types.User(id=123, username="anna"), input_entity=types.InputPeerUser(123, 456))
            yield SimpleNamespace(id=-1000000000789, name="Канал", entity=types.Channel(id=789, title="Канал", photo=types.ChatPhotoEmpty(), date=None, broadcast=True), input_entity=types.InputPeerChannel(789, 111))
    async def connected(home, operation):
        await operation(Client(), {})
    rows = asyncio.run(collect_dialogs(Client()))
    assert calls == ["dialogs"] and rows[0]["peer"]["access_hash"] == 456
    assert rows[1]["peer"] is not None and not rows[1]["write_supported"] and rows[1]["kind"] == "channel"


def test_launcher_only_returns_public_url(editor, monkeypatch):
    from telegram_guard_local import whitelist_editor
    launched = []
    def popen(args, **kwargs):
        launched.append(args)
        assert kwargs["stderr"] == whitelist_editor.subprocess.DEVNULL
        return SimpleNamespace(stdout=io.StringIO(json.dumps({"state": "opened", "url": "http://127.0.0.1:12345/", "expires_in_minutes": 30, "human_action_required": True}) + "\n"))
    monkeypatch.setattr(whitelist_editor.subprocess, "Popen", popen)
    result = launch(editor.home, "Дизайн")
    assert result["url"] == "http://127.0.0.1:12345/" and "token" not in json.dumps(result)
    assert launched[0][-3:] == ["--search", "Дизайн", "--serve"]
    with pytest.raises(GuardError, match="invalid_search"):
        launch(editor.home, "x\n--save")
