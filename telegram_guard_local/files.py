"""Data paths are fixed by the trusted MCP launch configuration, never by tool arguments."""
import json
import os
import stat
from pathlib import Path
from .model import GuardError

def checked(path, *, directory=False, missing=False):
    path = Path(path).absolute()
    for ancestor in [*reversed(path.parents), path]:
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if ancestor == path and missing:
                return path
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise GuardError("reparse_path_refused")
    if directory != path.is_dir():
        raise GuardError("unexpected_path_type")
    return path

def read_json(path):
    path = checked(path)
    if path.stat().st_size > 1024 * 1024:
        raise GuardError("config_too_large")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise GuardError("duplicate_json_key")
            out[key] = value
        return out
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(GuardError("invalid_json")))

def write_json(path, value):
    import secrets
    path = Path(path).absolute()
    checked(path.parent, directory=True)
    checked(path, missing=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
