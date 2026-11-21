"""Real discovery/capability probe; Desktop launch is intercepted, no user state used."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from telegram_guard_local import shared_desktop as launcher
from telegram_guard_local.files import read_json, write_json


root=Path(__file__).resolve().parents[1]/'.test-runs'
root.mkdir(exist_ok=True)
home=Path(tempfile.mkdtemp(prefix='auto-launcher-',dir=root))
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
previous={'codex':str(home/'removed-cli.exe'),'desktop':str(home/'removed-desktop.exe'),
          'node':str(home/'removed-node.exe'),'version':'older-build',
          'port':port,'codex_home':str(home/'isolated-codex-home')}
write_json(home/'shared-desktop.json',previous)
adapter=home/'venv/Scripts/codex-shared-stdio.exe'
adapter.parent.mkdir(parents=True);adapter.touch() # Only existence is checked; never executed.
desktop=launcher.installed_desktop()
original_popen=subprocess.Popen
original_processes=launcher.processes
launches=[]
def intercept(args,**kwargs):
    if str(args[0]).casefold()==desktop.casefold():
        launches.append((args,kwargs))
        return SimpleNamespace(pid=0)
    return original_popen(args,**kwargs)
try:
    # The fake installation has no running Desktop. User processes are untouched.
    launcher.processes=lambda:[]
    subprocess.Popen=intercept
    launcher.run(home,'start')
finally:
    subprocess.Popen=original_popen
    launcher.processes=original_processes
current=read_json(home/'shared-desktop.json')
assert len(launches)==1 and launches[0][0]==[desktop]
assert current['codex']!=previous['codex'] and Path(current['codex']).is_file()
assert current['desktop']==desktop and current['version']!='older-build'
assert current['port']==port and current['codex_home']==previous['codex_home']
assert launches[0][1]['env']['CODEX_APP_SERVER_FORCE_CLI']=='1'
assert 'CODEX_APP_SERVER_WS_URL' not in launches[0][1]['env']
assert len(list((home/'launcher-backups').glob('auto-shared-desktop.*.json')))==1
before={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
launcher.run(home,'check')
after={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
assert before==after,'Check must not change config, cache or backups'
result={'passed':True,'new_version':current['version'],'old_paths_removed':True,
        'configuration_refreshed':True,'check_read_only':True,'desktop_launched':False,
        'real_account_used':False,'test_home':str(home)}
write_json(home/'result.json',result)
print(json.dumps(result))
