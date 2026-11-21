from telegram_guard_local.shared_desktop import server_args, desktop_running, owned_server
from telegram_guard_local.tools import TOOLS
import pytest
def test_launcher_preserves_user_permissions_and_only_exposes_loopback():
    args=server_args({"codex":"codex.exe","port":48963})
    assert args[-3:]==["app-server","--listen","ws://127.0.0.1:48963"]
    assert not any("approval_policy" in a or "sandbox_mode" in a or "danger-full-access" in a for a in args)
    assert 'features.windows_sandbox_service=true' in args
    assert 'mcp_servers.telegram_guard.env.TELEGRAM_GUARD_APP_SERVER_URL="ws://127.0.0.1:48963"' in args
    import json
    assert 'mcp_servers.telegram_guard.enabled_tools='+json.dumps(TOOLS) in args
def test_stop_requires_owned_pid_birth_and_executable():
    config={"codex":r"C:\app\codex.exe"}
    row={"pid":123,"birth":"now","ExecutablePath":config["codex"]}
    assert owned_server({"pid":123,"birth":"now"},[row],config)==row
    assert owned_server({"pid":123,"birth":"old"},[row],config) is None
    assert owned_server({"pid":123,"birth":"now"},[dict(row,ExecutablePath="other.exe")],config) is None
    assert desktop_running([{"ExecutablePath":r"C:\Program Files\WindowsApps\OpenAI.Codex_123\app\ChatGPT.exe"}])


def test_outdated_shared_server_is_recycled_only_when_owned_idle_and_desktop_closed(monkeypatch,tmp_path):
    from telegram_guard_local import shared_desktop as module
    row={'pid':123,'birth':'now','ExecutablePath':r'C:\app\codex.exe'}
    receipt={'pid':123,'birth':'now'};config={'codex':row['ExecutablePath'],'port':48963}
    calls=[]
    async def idle(url):return True
    monkeypatch.setattr(module,'server_idle',idle)
    monkeypatch.setattr(module,'processes',lambda:[row])
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:calls.append(a[0]))
    assert module.refresh_owned_server(tmp_path,config,receipt,row) is None
    assert calls==[['taskkill.exe','/PID','123','/T','/F']]
    calls.clear()
    assert module.refresh_owned_server(tmp_path,config,dict(receipt,telegram_tools=TOOLS),row)==row
    assert not calls
    async def busy(url):return False
    monkeypatch.setattr(module,'server_idle',busy)
    with pytest.raises(ValueError,match='still active'):module.refresh_owned_server(tmp_path,config,receipt,row)
    assert not calls
    monkeypatch.setattr(module,'server_idle',idle)
    monkeypatch.setattr(module,'processes',lambda:[dict(row,birth='another')])
    with pytest.raises(ValueError,match='changed'):module.refresh_owned_server(tmp_path,config,receipt,row)
    assert not calls


def test_recorded_old_executable_preserves_ownership_after_app_upgrade():
    row={'pid':123,'birth':'now','ExecutablePath':r'C:\old\codex.exe'}
    receipt={'pid':123,'birth':'now','executable':row['ExecutablePath']}
    assert owned_server(receipt,[row],{'codex':r'C:\new\codex.exe'})==row
    assert owned_server(receipt,[dict(row,ExecutablePath=r'C:\foreign\codex.exe')],{'codex':r'C:\new\codex.exe'}) is None


def test_old_server_is_replaced_even_when_tool_list_is_current(monkeypatch,tmp_path):
    from telegram_guard_local import shared_desktop as module
    old=r'C:\old\codex.exe'
    row={'pid':123,'birth':'now','ExecutablePath':old}
    receipt={'pid':123,'birth':'now','executable':old,'telegram_tools':TOOLS}
    config={'codex':r'C:\new\codex.exe','port':48963}
    async def idle(url):return True
    calls=[]
    monkeypatch.setattr(module,'server_idle',idle)
    monkeypatch.setattr(module,'processes',lambda:[row])
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:calls.append(a[0]))
    assert module.refresh_owned_server(tmp_path,config,receipt,row) is None
    assert len(calls)==1 and calls[0][2]=='123'


def test_resolve_finds_updated_package_even_when_previous_paths_still_exist(monkeypatch,tmp_path):
    from telegram_guard_local import shared_desktop as module
    from types import SimpleNamespace
    paths={}
    for name in ('codex','desktop','node'):
        p=tmp_path/(name+'.exe');p.touch();paths[name]=str(p)
    config=dict(paths,version='test',port=48963,codex_home=str(tmp_path))
    desktop=tmp_path/'updated'/'ChatGPT.exe'
    binary=desktop.parent/'resources'/'codex.exe'
    binary.parent.mkdir(parents=True);desktop.touch();binary.touch()
    monkeypatch.setattr(module,'installed_desktop',lambda:str(desktop))
    monkeypatch.setattr(module,'installed_node',lambda previous:paths['node'])
    monkeypatch.setattr(module.subprocess,'run',lambda args,**kwargs:SimpleNamespace(
        stdout='v24.0.0' if args[0]==paths['node'] else 'codex-cli completely-new-build'))
    resolved=module.resolve_configuration(config)
    assert resolved['desktop']==str(desktop) and resolved['codex']==str(binary)
    assert resolved['version']=='codex-cli completely-new-build'
    assert resolved['port']==config['port'] and resolved['codex_home']==config['codex_home']
    assert config['version']=='test' # Input remains untouched.




def test_native_launcher_does_not_prestart_core_or_require_browser_environment(monkeypatch,tmp_path):
    from telegram_guard_local import shared_desktop as module
    from telegram_guard_local.files import write_json
    config={'codex':'codex.exe','desktop':'ChatGPT.exe','node':'node.exe',
            'version':'test','port':48963,'codex_home':str(tmp_path)}
    write_json(tmp_path/'shared-desktop.json',config)
    monkeypatch.setattr(module,'resolve_configuration',lambda value:value)
    from telegram_guard_local import launcher_compat
    monkeypatch.setattr(launcher_compat,'ensure_compatible',lambda *args,**kwargs:'cached')
    monkeypatch.setattr(module,'installed_desktop',lambda:'ChatGPT.exe')
    monkeypatch.setattr(module,'processes',lambda:[])
    monkeypatch.setattr(module,'desktop_environment',lambda home,config:{'sentinel':'preserved'})
    monkeypatch.setattr(module,'refresh_owned_server',lambda *args:None)
    calls=[]
    monkeypatch.setattr(module.subprocess,'Popen',lambda args,**kwargs:calls.append((args,kwargs)))
    import socket
    class FreePort:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,value):pass
        def connect_ex(self,address):return 1
    monkeypatch.setattr(socket,'socket',FreePort)
    module.run(tmp_path,'start')
    assert len(calls)==1 and calls[0][0]==['ChatGPT.exe']
    assert calls[0][1]['env']=={'sentinel':'preserved'}
