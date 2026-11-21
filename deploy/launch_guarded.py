"""Human launcher. Check the selected configuration before starting a new Codex runtime."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--codex")
    parser.add_argument("--directory",default=str(Path.cwd()))
    parser.add_argument("--check-only",action="store_true")
    args=parser.parse_args()
    codex_home=Path(os.environ.get("CODEX_HOME") or str(Path.home()/".codex"))
    target=codex_home/"telegram-local.config.toml"
    config=tomllib.loads(target.read_text(encoding="utf-8"))
    if config.get("approval_policy")!="never" or config.get("default_permissions")!="telegram-local":
        raise SystemExit("Guard profile was changed; launch refused")
    features=config.get("features",{})
    required_off=("plugins","apps","computer_use","browser_use","browser_use_external",
                  "browser_use_full_cdp_access","in_app_browser","hooks")
    if any(features.get(name) is not False for name in required_off):
        raise SystemExit("Unsafe capabilities in the profile; launch refused")
    root=Path(args.directory).resolve()
    # Legacy sandbox settings override permission profiles; refuse that combination.
    for folder in (root,*root.parents):
        candidate=folder/".codex/config.toml"
        if candidate.exists():
            parsed=tomllib.loads(candidate.read_text(encoding="utf-8"))
            if "sandbox_mode" in parsed or "sandbox_workspace_write" in parsed:
                raise SystemExit("Legacy project sandbox settings exist; migrate before launch")
    base=tomllib.loads((codex_home/"config.toml").read_text(encoding="utf-8"))
    if "sandbox_mode" in base or "sandbox_workspace_write" in base:
        raise SystemExit("Legacy user sandbox settings exist; migrate before launch")
    binary=args.codex
    if binary is None and os.name=="nt":
        candidates=list((Path.home()/"AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"))
        candidates.sort(key=lambda x:x.stat().st_mtime,reverse=True)
        binary=str(candidates[0]) if candidates else None
    binary=binary or shutil.which("codex")
    if not binary:
        raise SystemExit("Codex executable not found; supply --codex")
    invocation=[binary,"--no-daemon","-p","telegram-local","-C",str(root)]
    listing=subprocess.run(invocation+["mcp","list","--json"],
                           capture_output=True,text=True,check=True)
    servers=json.loads(listing.stdout)
    if {x["name"] for x in servers if x.get("enabled")}!={"telegram_guard"}:
        raise SystemExit("Additional MCP servers are enabled; launch refused")
    sender=config["mcp_servers"]["telegram_guard"]
    fixed_home=Path(sender["args"][-1])
    fs=config["permissions"]["telegram-local"]["filesystem"]
    if fs.get(str(fixed_home))!="read" or fs.get(str(fixed_home/"private"))!="deny":
        raise SystemExit("Missing Telegram file protections; launch refused")
    print("Guard profile checked: only Telegram MCP, closed private directory, read-only sender/config.",flush=True)
    if args.check_only:
        return
    # Starts a fresh runtime with the selected profile, not an existing unrestricted daemon.
    raise SystemExit(subprocess.call(invocation))

if __name__=="__main__":
    main()
