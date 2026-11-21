"""Opt-in Windows launcher for the shared App Server. Never closes Desktop."""
import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from .files import read_json, write_json

from .tools import TOOLS
CREATE_NO_WINDOW = getattr(subprocess,"CREATE_NO_WINDOW",0)

async def server_idle(url):
    """Metadata-only guard before recycling an outdated owned server after the human closes Desktop."""
    import asyncio
    from .app_server import AppServer
    client=AppServer(url)
    async def check():
        await client._connect()
        cursor=None
        for _ in range(2):
            page=await client._call("thread/loaded/list",{"cursor":cursor,"limit":100})
            for thread in page.get("data",[]):
                state=await client._call("thread/read",{"threadId":thread,"includeTurns":False})
                if state.get("thread",{}).get("status",{}).get("type") not in ("idle","notLoaded"):
                    return False
            cursor=page.get("nextCursor")
            if not cursor:return True
        return False
    try:
        return await asyncio.wait_for(check(),20)
    except Exception:
        return False
    finally:
        await client.close()


def refresh_owned_server(home,config,receipt,server):
    """Keep the explicit allowlist; never touch a live Desktop, busy server or foreign PID."""
    if server is None or (receipt.get("telegram_tools")==TOOLS and
                          (server.get("ExecutablePath") or "").casefold()==config["codex"].casefold()):
        return server
    import asyncio
    if not asyncio.run(server_idle(f'ws://127.0.0.1:{config["port"]}')):
        raise ValueError("Background Codex tasks are still active or could not be checked. Wait before reopening Codex + Telegram.")
    rows=processes()
    if desktop_running(rows) or owned_server(receipt,rows,config) is None:
        raise ValueError("Desktop or server changed during the update; refusing to interrupt it.")
    subprocess.run(["taskkill.exe","/PID",str(server["pid"]),"/T","/F"],
                   check=True,capture_output=True,creationflags=CREATE_NO_WINDOW,timeout=15)
    return None

def processes():
    command = """@(Get-CimInstance Win32_Process | Where-Object {
      $_.Name -eq 'codex.exe' -or $_.Name -eq 'ChatGPT.exe'
    } | Select-Object @{n='pid';e={$_.ProcessId}},ExecutablePath,
      @{n='birth';e={$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Compress"""
    result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
                            capture_output=True,text=True,check=True,creationflags=CREATE_NO_WINDOW,timeout=15)
    value = json.loads(result.stdout or "[]")
    return value if isinstance(value,list) else [value]

def desktop_running(rows):
    return any("openai.codex_" in (r.get("ExecutablePath") or "").lower() and
               Path(r.get("ExecutablePath") or "").name.lower() in ("chatgpt.exe","codex.exe") for r in rows)

def installed_desktop():
    command="Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation | ConvertTo-Json -Compress"
    result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
                          capture_output=True,text=True,check=True,creationflags=CREATE_NO_WINDOW,timeout=15)
    location=json.loads(result.stdout or 'null')
    if not isinstance(location,str):
        raise ValueError("Cannot identify the installed Codex Desktop package")
    return str(Path(location)/"app"/"ChatGPT.exe")

def ready(port):
    try:
        # Explicitly bypass system proxies for loopback health checks.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/readyz",timeout=1) as response:
            return response.status==200
    except OSError:
        return False

def server_args(config):
    url=f'ws://127.0.0.1:{config["port"]}'
    return [config["codex"],"-c","features.code_mode_host=true",
            "-c","features.windows_sandbox_service=true",
            "-c","mcp_servers.telegram_guard.env.TELEGRAM_GUARD_APP_SERVER_URL="+json.dumps(url),
            "-c","mcp_servers.telegram_guard.enabled_tools="+json.dumps(TOOLS),
            "app-server","--listen",url]

def validate_settings(config):
    required={"codex","desktop","node","version","port","codex_home"}
    if set(config)!=required:
        raise ValueError("Invalid shared Desktop configuration")
    if not all(isinstance(config[key],str) and config[key] for key in required-{'port'}):
        raise ValueError("Invalid shared Desktop configuration")
    if type(config["port"]) is not int or not 1024<=config["port"]<=65535:
        raise ValueError("Invalid local port")
    if not Path(config["codex_home"]).is_absolute():
        raise ValueError("CODEX_HOME must be absolute")


def validate(config):
    """Validate public settings and discovered paths; version is informational."""
    validate_settings(config)
    for key in ("codex","desktop","node"):
        path=Path(config[key])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"Configured {key} executable is missing; refresh the launcher after updating Desktop")


def installed_node(previous):
    root=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'OpenAI'/'Codex'/'runtimes'/'cua_node'
    candidates=[path for path in root.glob('*/bin/node.exe') if path.is_file()]
    if candidates:
        return str(max(candidates,key=lambda path:(path.stat().st_mtime_ns,str(path))))
    if Path(previous).is_absolute() and Path(previous).is_file():
        return previous
    raise ValueError('Не найден Node runtime Codex. Возможно, обновление приложения ещё не завершилось.')


def resolve_configuration(config):
    """Always discover current package, even if the previous package still exists."""
    validate_settings(config)
    desktop=installed_desktop()
    binary=Path(desktop).parent/'resources'/'codex.exe'
    candidate=dict(config,desktop=desktop,codex=str(binary),node=installed_node(config['node']))
    validate(candidate)
    import tempfile
    from .launcher_compat import isolated_environment
    with tempfile.TemporaryDirectory(prefix='codex-discover-') as folder:
        env=isolated_environment(Path(folder))
        try:
            result=subprocess.run([str(binary),'--version'],capture_output=True,text=True,
                                  timeout=10,env=env,creationflags=CREATE_NO_WINDOW)
            version=result.stdout.strip() if getattr(result,'returncode',0)==0 else ''
        except subprocess.TimeoutExpired:
            version=''
        # This is display metadata, never a compatibility gate.
        candidate['version']=version.splitlines()[0][:200] if version else 'Codex (версия не указана)'
        subprocess.run([candidate['node'],'--input-type=module','--eval',
                        "import {createHmac,randomBytes,timingSafeEqual} from 'node:crypto'; "
                        "if(typeof WebSocket!=='function'||typeof createHmac!=='function'||"
                        "typeof randomBytes!=='function'||typeof timingSafeEqual!=='function')process.exit(1);"],
                       capture_output=True,check=True,timeout=10,env=env,creationflags=CREATE_NO_WINDOW)
    return candidate


def save_configuration(home, previous, current):
    """Persist only successful automatic refreshes, retaining the public config."""
    if previous==current:return
    from datetime import datetime,timezone
    from .files import checked
    backups=Path(home)/'launcher-backups'
    checked(backups,directory=True,missing=True)
    backups.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    write_json(backups/('auto-shared-desktop.'+stamp+'.json'),previous)
    write_json(Path(home)/'shared-desktop.json',current)

def owned_server(receipt, rows, config):
    if receipt is None:
        return None
    return next((r for r in rows if r["pid"]==receipt.get("pid") and r["birth"]==receipt.get("birth") and
                 (r.get("ExecutablePath") or "").casefold()==receipt.get("executable",config["codex"]).casefold()),None)

def desktop_environment(home, config):
    """Keep Desktop on stdio, without its WebSocket heartbeat deadline."""
    adapter = Path(home) / "venv" / "Scripts" / "codex-shared-stdio.exe"
    if not adapter.is_file():
        raise ValueError("Desktop stdio adapter is missing; repair the background-subscription installation")
    env = dict(os.environ)
    for key in ("CODEX_APP_SERVER_WS_URL", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        env.pop(key, None)
    env.update(CODEX_HOME=config["codex_home"], CODEX_APP_SERVER_FORCE_CLI="1",
               CODEX_CLI_PATH=str(adapter), TELEGRAM_GUARD_DESKTOP_HOME=str(home))
    return env


def run(home, action):
    if os.name!="nt":
        raise ValueError("This launcher is for the tested Windows Desktop build")
    home=Path(home).absolute()
    previous=read_json(home/"shared-desktop.json")
    validate_settings(previous)
    config=previous
    rows=processes()
    receipt_path=home/"shared-server-receipt.json"
    receipt=read_json(receipt_path) if receipt_path.exists() else None
    server=owned_server(receipt,rows,config)
    opened=desktop_running(rows)
    if action=='start' and opened:
        raise ValueError("Close Desktop normally first. This launcher will not interrupt open tasks.")
    compatibility=None
    if action!='stop':
        from .launcher_compat import ensure_compatible
        config=resolve_configuration(previous)
        compatibility=ensure_compatible(home,config['codex'],persist=action=='start')
    if action=="check":
        print(json.dumps({"version":config["version"],"desktop_running":opened,
              "owned_server_running":server is not None,"endpoint":f'ws://127.0.0.1:{config["port"]}',
              "configuration":"valid","compatibility":compatibility,
              "configuration_refresh_needed":previous!=config,
              "desktop_integration":"requires verification after explicit launch"},ensure_ascii=False))
        return
    if opened:
        raise ValueError("Close Desktop normally first. This launcher will not interrupt open tasks.")
    if action=="stop":
        if server:
            # Only this launcher's process, verified by executable and creation time.
            subprocess.run(["taskkill.exe","/PID",str(server["pid"]),"/T","/F"],
                           check=True,capture_output=True,creationflags=CREATE_NO_WINDOW,timeout=15)
        print("Shared App Server stopped. Open Desktop normally from Start to use its original stdio runtime.")
        return
    desktop_env = desktop_environment(home, config)
    # Native launcher never owns or recycles legacy headless servers.
    # The new endpoint must be free; old jobs keep their previous endpoint.
    import socket
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1',config['port']))==0:
            raise ValueError('Event endpoint is occupied; refusing to attach an unknown process')
    if desktop_running(processes()):
        raise ValueError("Close Desktop normally first. This launcher will not interrupt open tasks.")
    # Recheck the package before committing metadata if an update raced us.
    if installed_desktop().casefold()!=config['desktop'].casefold():
        raise ValueError('Codex обновился во время запуска. Повторите запуск после завершения обновления.')
    ensure_compatible(home,config['codex'],persist=True)
    save_configuration(home,previous,config)
    subprocess.Popen([config["desktop"]],env=desktop_env,creationflags=CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print("Desktop launched with native stdio and authenticated event delivery. No standalone App Server was started.")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--home",required=True)
    parser.add_argument("--action",choices=("check","start","stop"),default="check")
    args=parser.parse_args()
    try:
        run(args.home,args.action)
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None

if __name__=="__main__":
    main()

