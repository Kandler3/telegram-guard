"""Explicit v1 -> v2 migration. No Telegram connection or credential access."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
from .files import read_json, write_json
from .model import Policy


def migrate_policy(body):
    Policy.parse(body)
    result = deepcopy(body)
    if body["version"] == 1:
        result.update(version=2, read={"default": True, "allow": [], "deny": []},
                      write={"allow": [p["chat_id"] for p in body["chats"]]})
    Policy.parse(result)
    return result


def migrate_home(home):
    from .runtime import SessionLock
    home = Path(home).absolute()
    with SessionLock(home / "private" / "whitelist.lock"):
        path = home / "whitelist.json"
        old = read_json(path)
        new = migrate_policy(old)
        backup = None
        if new != old:
            backup = home / ("whitelist.before-v2." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
            write_json(backup, old)
            write_json(path, new)
        assert read_json(path) == new
        assert new["chats"] == old["chats"]
        return {"version": 2, "read_default": new["read"]["default"],
                "write_chats": len(new["write"]["allow"]), "migrated": backup is not None,
                "existing_records_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    print(json.dumps(migrate_home(parser.parse_args().home)))
