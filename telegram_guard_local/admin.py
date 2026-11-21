"""Human onboarding. Run outside the restricted agent profile."""
import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path
from .files import checked, read_json, write_json
from .model import Policy, chat_id
from .runtime import SessionLock
from .transport import make_client
from .policy_migration import migrate_policy

def default_home():
    if os.name == "nt":
        return Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "telegram-local"
    return Path.home() / ".local/share/codex-telegram-guard"

async def connected(home, operation, login=False):
    private = home / "private"
    checked(private, directory=True)
    with SessionLock(private / "session.lock"):
        config = read_json(private / "account.json")
        client = make_client(config, private)
        try:
            await client.connect()
            if login:
                await client.start(phone=lambda: input("Phone (+country code): ").strip(),
                    code_callback=lambda: getpass.getpass("Telegram login code: "),
                    password=lambda: getpass.getpass("Telegram 2FA password: "))
            elif not await client.is_user_authorized():
                raise RuntimeError("Run login first")
            me = await client.get_me()
            if not me or me.bot or (config["account_id"] is not None and me.id != config["account_id"]):
                raise RuntimeError("Personal account mismatch")
            if login:
                config["account_id"] = me.id
                write_json(private / "account.json", config)
                print("Personal Telegram account connected. Session stays local and is never exported.")
            await operation(client, config)
        finally:
            await client.disconnect()

async def no_operation(client, config):
    pass

async def dialogs(client, config):
    async for dialog in client.iter_dialogs():
        print(json.dumps({"chat_id":dialog.id,"label":dialog.name},ensure_ascii=False))

async def allow(home, client, target, label):
    from telethon import types
    found = None
    async for dialog in client.iter_dialogs():
        if dialog.id == target:
            found = dialog
            break
    if not found:
        raise RuntimeError("Numeric chat ID not found in this account's dialogs")
    peer, entity = found.input_entity, found.entity
    if isinstance(entity, types.Channel) and (entity.broadcast or not entity.megagroup):
        raise RuntimeError("Only personal chats and groups are supported")
    if isinstance(peer, types.InputPeerUser):
        kind,eid,ah = "user",peer.user_id,peer.access_hash
    elif isinstance(peer, types.InputPeerChat):
        kind,eid,ah = "chat",peer.chat_id,None
    elif isinstance(peer, types.InputPeerChannel):
        kind,eid,ah = "channel",peer.channel_id,peer.access_hash
    else:
        raise RuntimeError("Unsupported peer")
    with SessionLock(home / "private" / "whitelist.lock"):
        policy = migrate_policy(read_json(home / "whitelist.json"))
        policy["chats"] = [p for p in policy["chats"] if p["chat_id"] != target] + [
            {"chat_id":target,"label":label or found.name,"kind":kind,"entity_id":eid,"access_hash":ah}]
        policy["write"]["allow"] = list(dict.fromkeys(policy["write"]["allow"] + [target]))
        policy["read"]["deny"] = [i for i in policy["read"]["deny"] if i != target]
        if not policy["read"]["default"]:
            policy["read"]["allow"] = list(dict.fromkeys(policy["read"]["allow"] + [target]))
        Policy.parse(policy)
        write_json(home / "whitelist.json", policy)
    print("Allowed chat ID: " + str(target))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(default_home()))
    sub = parser.add_subparsers(dest="command",required=True)
    for name in ("init","login","dialogs","show"):
        sub.add_parser(name)
    p = sub.add_parser("allow")
    p.add_argument("--chat",type=int,required=True)
    p.add_argument("--label")
    p = sub.add_parser("remove")
    p.add_argument("--chat",type=int,required=True)
    args = parser.parse_args()
    home = Path(args.home).absolute()
    os.umask(0o077)
    if args.command in ("init","login") and not sys.stdin.isatty():
        raise SystemExit("Enter Telegram credentials yourself in an interactive terminal")
    if args.command == "init":
        home.mkdir(parents=True,exist_ok=True)
        checked(home,directory=True)
        private = home / "private"
        private.mkdir(exist_ok=True)
        checked(private,directory=True)
        account = private / "account.json"
        if account.exists():
            raise SystemExit("Already initialized; refusing to replace credentials")
        api_id = int(input("Telegram api_id (my.telegram.org): "))
        api_hash = getpass.getpass("Telegram api_hash: ").strip().lower()
        if api_id <= 0 or len(api_hash) != 32 or any(c not in "0123456789abcdef" for c in api_hash):
            raise SystemExit("Invalid API credentials")
        write_json(account,{"version":1,"api_id":api_id,"api_hash":api_hash,"account_id":None})
        if not (home / "whitelist.json").exists():
            write_json(home / "whitelist.json",migrate_policy({"version":1,"chats":[],"messages_per_minute":6,"messages_per_day":200}))
        print("Initialized. Run login, dialogs, then allow.")
    elif args.command == "login":
        asyncio.run(connected(home,no_operation,login=True))
    elif args.command == "dialogs":
        asyncio.run(connected(home,dialogs))
    elif args.command == "allow":
        asyncio.run(connected(home,lambda c,cfg:allow(home,c,chat_id(args.chat),args.label)))
    else:
        policy = read_json(home / "whitelist.json")
        Policy.parse(policy)
        if args.command == "remove":
            with SessionLock(home / "private" / "whitelist.lock"):
                policy = migrate_policy(read_json(home / "whitelist.json"))
                Policy.parse(policy)
                target = chat_id(args.chat)
                policy["write"]["allow"] = [i for i in policy["write"]["allow"] if i != target]
                policy["read"]["allow"] = [i for i in policy["read"]["allow"] if i != target]
                policy["read"]["deny"] = list(dict.fromkeys(policy["read"]["deny"] + [target]))
                write_json(home / "whitelist.json",policy)
            print("Removed chat. Already in-flight sends cannot be recalled.")
        else:
            parsed = Policy.parse(policy)
            print(json.dumps({"read_default":parsed.read_default,"chats":[{"chat_id":p.chat,"label":p.label,
                "can_read":parsed.can_read(p.chat),"can_write":parsed.can_write(p.chat)} for p in parsed.peers.values()]},ensure_ascii=False))

if __name__ == "__main__":
    main()
