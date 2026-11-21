"""Enable the installed Telegram MCP in normal Desktop, preserving other tools."""
import argparse, copy, datetime, json, os, re, tempfile, tomllib
from pathlib import Path

PROFILE = "telegram-desktop"
from telegram_guard_local.tools import TOOLS
def quote(value):
    return json.dumps(str(value), ensure_ascii=False)

def section(text, name, body):
    headers = list(re.finditer(r"(?m)^[ \t]*\[([^\r\n]+)\][ \t]*(?:#[^\r\n]*)?$", text))
    matches = [i for i, h in enumerate(headers) if h.group(1).strip() == name]
    if len(matches) > 1:
        raise ValueError("Duplicate config section")
    if not matches:
        return text.rstrip() + "\n\n" + body.rstrip() + "\n"
    i = matches[0]
    end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
    return text[:headers[i].start()] + body.rstrip() + "\n\n" + text[end:]

def configure(text, home, codex):
    text = text.replace(chr(13) + chr(10), chr(10))
    before = tomllib.loads(text)
    if "sandbox_mode" in before or "sandbox_workspace_write" in before:
        raise ValueError("Migrate legacy sandbox settings before using permission profiles")
    old = before.get("permissions", {}).get(PROFILE)
    if old and old.get("description") != "Desktop workspace with Telegram files protected.":
        raise ValueError("Refusing to replace an unrelated permission profile")
    if before.get("mcp_servers", {}).get("telegram_guard", {}).get("env"):
        raise ValueError("Remove obsolete Telegram MCP environment settings first")
    first = re.search(r"(?m)^[ \t]*\[", text)
    cut = first.start() if first else len(text)
    top = re.sub(r'(?m)^[ \t]*default_permissions[ \t]*=.*(?:\r?\n|$)', '', text[:cut])
    text = 'default_permissions = "' + PROFILE + '"\n' + top + text[cut:]
    python = home / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    text = section(text, "mcp_servers.telegram_guard", "\n".join([
        "[mcp_servers.telegram_guard]",
        "command = " + quote(python),
        'args = ["-I", "-B", "-m", "telegram_guard_local.mcp_server", "--home", ' + quote(home) + ']',
        "enabled = true", "enabled_tools = " + json.dumps(TOOLS),
        "startup_timeout_sec = 20", "tool_timeout_sec = 90",
    ]))
    text = section(text, "permissions." + PROFILE, "\n".join([
        "[permissions." + PROFILE + "]",
        'description = "Desktop workspace with Telegram files protected."',
        'extends = ":workspace"',
    ]))
    rules = [
        '":root" = "read"', '":minimal" = "read"',
        quote(home) + ' = "read"', quote(home / "private") + ' = "deny"',
        quote(codex / "config.toml") + ' = "read"',
        quote(codex / "AGENTS.md") + ' = "read"',
        quote(codex / "skills" / "telegram-send") + ' = "read"',
        quote(codex / "telegram-local.config.toml") + ' = "read"',
    ]
    text = section(text, "permissions." + PROFILE + ".filesystem",
                   "[permissions." + PROFILE + ".filesystem]\n" + "\n".join(rules))
    text = section(text, "permissions." + PROFILE + ".network",
                   "[permissions." + PROFILE + ".network]\nenabled = true")
    after = tomllib.loads(text)
    expected = copy.deepcopy(before)
    expected["default_permissions"] = PROFILE
    expected.setdefault("mcp_servers", {})["telegram_guard"] = after["mcp_servers"]["telegram_guard"]
    expected.setdefault("permissions", {})[PROFILE] = after["permissions"][PROFILE]
    if expected != after:
        raise ValueError("Unrelated settings would change")
    return text

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    p.add_argument("--home")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    codex = Path(args.codex_home).resolve()
    home = Path(args.home).resolve() if args.home else codex / "telegram-local"
    python = home / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise SystemExit("Install local runtime first")
    path = codex / "config.toml"
    original = path.read_bytes()
    candidate = configure(original.decode("utf-8-sig"), home, codex)
    if not args.apply:
        print("Config validated. Use --apply to enable Desktop integration.")
        return
    if candidate.encode("utf-8") != original:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".telegram-desktop." + stamp + ".bak")
        if path.read_bytes() != original:
            raise SystemExit("Config changed concurrently; retry")
        backup.write_bytes(original)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != original:
                raise SystemExit("Config changed concurrently; retry")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print("Backup:", backup)
    print("Telegram MCP enabled globally; other tools and approvals preserved.")
    print("Default permissions:", PROFILE)
    print("Restart MCP in Desktop; use Custom (config.toml) for the protected sandbox.")
if __name__ == "__main__":
    main()
