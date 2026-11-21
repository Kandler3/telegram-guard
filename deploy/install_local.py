"""Install local MCP and create a dedicated guarded Codex configuration profile."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

def toml_string(value):
    return json.dumps(str(value),ensure_ascii=False)

def profile(home, codex_home, python, existing_servers):
    lines = [
        'approval_policy = "never"',
        'default_permissions = "telegram-local"',
        '',
        '[permissions.telegram-local]',
        'description = "Workspace edits; Telegram code and policy read-only; session inaccessible."',
        'extends = ":workspace"',
        '',
        '[permissions.telegram-local.filesystem]',
        '":root" = "read"',
        '":minimal" = "read"',
        toml_string(Path(sys.base_prefix)) + ' = "read"',
        toml_string(home) + ' = "read"',
        toml_string(home / "private") + ' = "deny"',
        toml_string(codex_home) + ' = "read"',
        '',
        '[permissions.telegram-local.network]',
        'enabled = true',
        '',
        '[features]',
        'plugins = false',
        'apps = false',
        'computer_use = false',
        'browser_use = false',
        'browser_use_external = false',
        'browser_use_full_cdp_access = false',
        'in_app_browser = false',
        'hooks = false',
        '',
    ]
    for name in sorted(set(existing_servers) - {"telegram_guard"}):
        lines += ['[mcp_servers.' + toml_string(name) + ']', 'enabled = false', '']
    lines += [
        '[mcp_servers.telegram_guard]',
        'command = ' + toml_string(python),
        'args = ["-I", "-B", "-m", "telegram_guard_local.mcp_server", "--home", ' + toml_string(home) + ']',
        'enabled = true',
        'startup_timeout_sec = 20',
        'tool_timeout_sec = 90',
        '',
    ]
    # Runtime profile must not inherit unused HTTP credentials from the older disabled entry.
    text = "\n".join(lines)
    tomllib.loads(text)
    return text

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home")
    parser.add_argument("--codex-home",default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--replace-profile",action="store_true")
    args = parser.parse_args()
    from_home = Path(__file__).resolve().parent.parent
    codex_home = Path(args.codex_home).resolve()
    home = Path(args.home).resolve() if args.home else codex_home / "telegram-local"
    config = codex_home / "config.toml"
    current = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    if current.get("sandbox_mode") is not None:
        raise SystemExit("Legacy sandbox_mode exists: migrate it before relying on the new profile")
    target_profile = codex_home / "telegram-local.config.toml"
    if target_profile.exists():
        old = tomllib.loads(target_profile.read_text(encoding="utf-8"))
        if not args.replace_profile or old.get("default_permissions") != "telegram-local":
            raise SystemExit("telegram-local profile already exists; refusing to overwrite it")
        import datetime
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        shutil.copy2(target_profile,target_profile.with_name("telegram-local.config.toml." + stamp + ".bak"))
    home.mkdir(parents=True,exist_ok=True)
    (home / "private").mkdir(exist_ok=True)
    runtime = home / "venv"
    venv.create(runtime,with_pip=True)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python),"-m","pip","install","-q",str(from_home)],check=True)
    whitelist = home / "whitelist.json"
    if not whitelist.exists():
        whitelist.write_text(json.dumps({"version":2,"chats":[],"messages_per_minute":6,"messages_per_day":200,
            "read":{"default":True,"allow":[],"deny":[]},"write":{"allow":[]}},indent=2),encoding="utf-8")
    target_profile.write_text(profile(home,codex_home,python,current.get("mcp_servers",{})),encoding="utf-8")
    launcher = home / "telegram-setup.ps1"
    launcher.write_text(
        "param([Parameter(Mandatory=$true)][ValidateSet('init','login','dialogs','allow','remove','show')][string]$Action,[long]$Chat,[string]$Label)\n"
        "$taskArgs = @('-I','-B','-m','telegram_guard_local.admin','--home', $PSScriptRoot, $Action)\n"
        "if ($Action -in @('allow','remove')) { $taskArgs += @('--chat', $Chat.ToString()) }\n"
        "if ($Action -eq 'allow' -and $Label) { $taskArgs += @('--label',$Label) }\n"
        "& (Join-Path $PSScriptRoot 'venv\\Scripts\\python.exe') @taskArgs\n"
        "exit $LASTEXITCODE\n",encoding="utf-8")
    shutil.copyfile(from_home / "deploy" / "telegram-whitelist.ps1", home / "telegram-whitelist.ps1")
    print("Local MCP installed; whitelist is empty; no Telegram login was attempted.")
    print("Profile: " + str(target_profile))
    print("Human setup: " + str(launcher))
    print("Start Codex with: codex --no-daemon -p telegram-local")

if __name__ == "__main__":
    main()
