"""Human-only loopback editor. The launcher never returns its browser capability."""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .files import checked, read_json, write_json
from .model import GuardError, Policy, Peer
from .runtime import SessionLock
from .policy_migration import migrate_policy

TTL = 30 * 60
MAX_CHATS = 10000
ASSETS = Path(__file__).with_name("editor_assets")


def revision(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def load_dialogs(home):
    """Only the human browser invokes this; message bodies are never requested."""
    from .service import ServiceRuntime
    return await ServiceRuntime(home).call('editor_dialogs')


async def collect_dialogs(client):
    """Internal metadata-only collector, sharing the broker's single Telegram connection."""
    from telethon import types
    rows = []

    async def collect(client, config):
        async for dialog in client.iter_dialogs():
            if len(rows) >= MAX_CHATS:
                raise GuardError("too_many_chats", 422)
            entity, peer = dialog.entity, dialog.input_entity
            label = (dialog.name or "Без названия").replace("\x00", "")[:200]
            username = getattr(entity, "username", None) or ""
            row = {"chat_id": str(dialog.id), "label": label, "username": username,
                   "kind": "group", "reason": "", "peer": None}
            row["write_supported"] = not (isinstance(entity, types.Channel) and (entity.broadcast or not entity.megagroup))
            if not row["write_supported"]:
                row.update(kind="channel", reason="Канал: только чтение")
            if getattr(entity, "deleted", False) or isinstance(entity, (types.ChatForbidden, types.ChannelForbidden)):
                row.update(reason="Чат недоступен")
            else:
                record = {"chat_id": dialog.id, "label": label}
                if isinstance(peer, types.InputPeerUser):
                    record.update(kind="user", entity_id=peer.user_id, access_hash=peer.access_hash)
                    row["kind"] = "bot" if getattr(entity, "bot", False) else "user"
                elif isinstance(peer, types.InputPeerChat):
                    record.update(kind="chat", entity_id=peer.chat_id, access_hash=None)
                elif isinstance(peer, types.InputPeerChannel):
                    record.update(kind="channel", entity_id=peer.channel_id, access_hash=peer.access_hash)
                else:
                    row["reason"] = "Этот тип чата не поддерживается"
                if not row["reason"] or not row["write_supported"]:
                    try:
                        Peer.parse(record)
                        row["peer"] = record
                    except GuardError:
                        row["reason"] = "Чат недоступен для добавления"
            rows.append(row)

    try:
        await asyncio.wait_for(collect(client, None), timeout=75)
    except GuardError:
        raise
    except Exception:
        raise GuardError("dialogs_unavailable", 503) from None
    return rows


class Editor:
    def __init__(self, home, search="", loader=load_dialogs, clock=time.monotonic, ttl=TTL):
        self.home = Path(home).absolute()
        self.search = search
        self.loader, self.clock = loader, clock
        self.deadline = clock() + ttl
        self.lock = threading.RLock()
        self.bootstrap = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.cookie = "tg_editor_" + secrets.token_hex(8)
        self.rows = None
        self.closed = False

    def live(self):
        if self.closed or self.clock() >= self.deadline:
            raise GuardError("editor_expired", 410)

    def unlock(self, token):
        with self.lock:
            self.live()
            if not isinstance(token, str) or not self.bootstrap or not hmac.compare_digest(token, self.bootstrap):
                raise GuardError("browser_required", 403)
            self.bootstrap = None

    def policy(self):
        policy = read_json(self.home / "whitelist.json")
        Policy.parse(policy)
        return policy

    def public(self, policy):
        raw = policy
        policy = migrate_policy(policy)
        parsed = Policy.parse(policy)
        known = {str(p["chat_id"]): p for p in policy["chats"]}
        rows = {r["chat_id"]: r for r in self.rows}
        for cid in set(known) | {str(i) for i in (*policy["read"]["allow"], *policy["read"]["deny"])}:
            if cid not in rows:
                peer = known.get(cid)
                rows[cid] = {"chat_id": cid, "label": peer["label"] if peer else cid, "username": "",
                             "kind": "user" if peer and peer["kind"] == "user" else "group",
                             "reason": "Нет в текущем списке Telegram", "peer": None}
        public = []
        for cid, row in rows.items():
            # Access hashes and Telegram session data never leave the helper.
            public.append({k: row[k] for k in ("chat_id", "label", "username", "kind", "reason")} |
                          {"can_read": parsed.can_read(int(cid)), "can_write": parsed.can_write(int(cid)),
                           "read_selectable": True,
                           "write_selectable": (row["peer"] is not None and row.get("write_supported", row["kind"] != "channel")) or parsed.can_write(int(cid))})
        return {"chats": public, "revision": revision(raw),
                "read": {"default":policy["read"]["default"],"allow":[str(i) for i in policy["read"]["allow"]],
                         "deny":[str(i) for i in policy["read"]["deny"]]},
                "write":[str(i) for i in policy["write"]["allow"]]}

    def chats(self, refresh=False):
        with self.lock:
            self.live()
            if self.rows is None or refresh:
                self.rows = asyncio.run(self.loader(self.home))
            self.live()
            return self.public(self.policy())

    def save(self, body):
        with self.lock:
            self.live()
            if not isinstance(body, dict) or set(body) != {"revision", "read", "write"} or self.rows is None:
                raise GuardError("invalid_selection")
            read = body["read"]
            if not isinstance(read, dict) or set(read) != {"default", "allow", "deny"} or type(read["default"]) is not bool:
                raise GuardError("invalid_selection")
            def ids(values):
                if (not isinstance(values, list) or len(values) > MAX_CHATS or
                    any(not isinstance(i, str) or not re.fullmatch(r"-?[1-9][0-9]{0,18}", i) for i in values) or
                    len(set(values)) != len(values)):
                    raise GuardError("invalid_selection")
                return values
            write, allow, deny = ids(body["write"]), ids(read["allow"]), ids(read["deny"])
            # All human writers use the same lock; stale browser tabs cannot overwrite changes.
            with SessionLock(self.home / "private" / "whitelist.lock"):
                policy = self.policy()
                if body["revision"] != revision(policy):
                    raise GuardError("whitelist_changed", 409)
                original = policy
                policy = migrate_policy(policy)
                candidates = {r["chat_id"]: r["peer"] for r in self.rows if r["peer"] is not None and r.get("write_supported", r["kind"] != "channel")}
                current = {str(p["chat_id"]): p for p in policy["chats"]}
                old_write = {str(i) for i in policy["write"]["allow"]}
                known = set(current) | {r["chat_id"] for r in self.rows} | {str(i) for i in (*policy["read"]["allow"], *policy["read"]["deny"])}
                if any(cid not in old_write and cid not in candidates for cid in write) or not set(allow + deny) <= known:
                    raise GuardError("unknown_chat", 422)
                for cid in write:
                    if cid not in current:
                        current[cid] = candidates[cid]
                for row in self.rows:
                    if row["chat_id"] in set(allow + deny) and row["peer"] is not None and row["chat_id"] not in current:
                        current[row["chat_id"]] = row["peer"]
                updated = dict(policy, chats=list(current.values()),
                               read={"default":read["default"],"allow":[int(i) for i in allow],"deny":[int(i) for i in deny]},
                               write={"allow":[int(i) for i in write]})
                Policy.parse(updated)
                if len((json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()) > 1024 * 1024:
                    raise GuardError("too_many_chats", 422)
                self.live()
                if updated != original:
                    write_json(self.home / "whitelist.json", updated)
                return self.public(updated)


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, editor):
        self.editor = editor
        super().__init__(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "TelegramGuardEditor"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log URLs, cookies, selections or account metadata.

    def reply(self, status, value, mime="application/json; charset=utf-8", cookie=None):
        raw = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
        self.send_response(status)
        for key, val in {"Content-Type": mime, "Content-Length": str(len(raw)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                         "X-Frame-Options": "DENY",
                         "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"}.items():
            self.send_header(key, val)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def host(self):
        if self.headers.get_all("Host") != [self.server.origin.removeprefix("http://")]:
            raise GuardError("invalid_origin", 403)
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise GuardError("invalid_origin", 403)
        self.server.editor.live()

    def auth(self):
        if self.headers.get("X-Editor-Request") != "1":
            raise GuardError("browser_required", 403)
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            value = cookies[self.server.editor.cookie].value
        except (KeyError, ValueError):
            raise GuardError("browser_required", 403) from None
        if not hmac.compare_digest(value, self.server.editor.session) or self.server.editor.bootstrap:
            raise GuardError("browser_required", 403)

    def body(self):
        if (self.headers.get("Origin") != self.server.origin or self.headers.get("X-Editor-Request") != "1" or
            self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding") or
            len(self.headers.get_all("Content-Length", [])) != 1):
            raise GuardError("invalid_origin", 403)
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 256 * 1024:
                raise ValueError()
            def unique(pairs):
                out = {}
                for key, value in pairs:
                    if key in out:
                        raise ValueError()
                    out[key] = value
                return out
            value = json.loads(self.rfile.read(size), object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise GuardError("invalid_request") from None

    def do_GET(self):
        try:
            self.host()
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, mime = assets[self.path]
                return self.reply(200, (ASSETS / name).read_bytes(), mime)
            self.auth()
            if self.path == "/api/session":
                e = self.server.editor
                return self.reply(200, {"csrf": e.csrf, "search": e.search, "seconds_left": max(0, int(e.deadline - e.clock()))})
            if self.path == "/api/chats":
                return self.reply(200, self.server.editor.chats())
            raise GuardError("not_found", 404)
        except GuardError as error:
            self.reply(error.status, {"error": error.code})
        except Exception:
            self.reply(503, {"error": "editor_unavailable"})

    def do_POST(self):
        try:
            self.host()
            body = self.body()
            e = self.server.editor
            if self.path == "/api/unlock":
                if set(body) != {"token"}:
                    raise GuardError("invalid_request")
                e.unlock(body["token"])
                return self.reply(200, {"ok": True}, cookie=f"{e.cookie}={e.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}")
            self.auth()
            if not hmac.compare_digest(self.headers.get("X-Editor-CSRF", ""), e.csrf):
                raise GuardError("invalid_origin", 403)
            if self.path == "/api/save":
                return self.reply(200, e.save(body))
            if self.path == "/api/refresh" and not body:
                return self.reply(200, e.chats(refresh=True))
            if self.path == "/api/close" and not body:
                with e.lock:
                    e.closed = True
                return self.reply(200, {"closed": True})
            raise GuardError("not_found", 404)
        except GuardError as error:
            self.reply(error.status, {"error": error.code})
        except Exception:
            self.reply(503, {"error": "editor_unavailable"})


def serve(home, search):
    editor = Editor(home, search)
    server = EditorServer(editor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The capability is passed directly to the user's browser, never to the agent.
        opened = webbrowser.open(server.origin + "/#token=" + editor.bootstrap, new=2)
        if not opened:
            print(json.dumps({"state": "browser_open_failed"}), flush=True)
            return
        print(json.dumps({"state": "opened", "url": server.origin + "/", "expires_in_minutes": 30,
                          "human_action_required": True}), flush=True)
        sys.stdout.close()
        while not editor.closed and time.monotonic() < editor.deadline:
            time.sleep(.5)
    finally:
        editor.closed = True
        server.shutdown()
        server.server_close()


def launch(home, search):
    if not isinstance(search, str) or len(search) > 200 or any(ord(c) < 32 for c in search):
        raise GuardError("invalid_search")
    child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "telegram_guard_local.whitelist_editor",
                              "--home", str(home), "--search", search, "--serve"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = []
    reader = threading.Thread(target=lambda: result.append(child.stdout.readline()), daemon=True)
    reader.start()
    reader.join(12)
    if reader.is_alive() or not result or not result[0]:
        child.terminate()
        child.wait(timeout=5)
        raise GuardError("editor_launch_failed", 503)
    value = json.loads(result[0])
    # Whitelist data, bootstrap and cookie never cross this boundary.
    if set(value) not in ({"state"}, {"state", "url", "expires_in_minutes", "human_action_required"}):
        raise GuardError("editor_launch_failed", 503)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--search", default="")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        home = checked(Path(args.home).absolute(), directory=True)
        if args.serve:
            serve(home, args.search)
        else:
            print(json.dumps(launch(home, args.search), ensure_ascii=False))
    except Exception:
        raise SystemExit("Unable to open the local whitelist editor; no whitelist changes were made.") from None


if __name__ == "__main__":
    main()
