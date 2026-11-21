import importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize("case",["valid","extra_mcp","legacy","private_unprotected","capability_enabled"])
def test_launcher_checks_effective_mcp_and_profile(tmp_path,monkeypatch,case):
    root=Path(__file__).parents[1]
    installer=load("installer_for_launch",root/"deploy/install_local.py")
    launcher=load("launcher_test",root/"deploy/launch_guarded.py")
    codex=tmp_path/"codex"
    home=codex/"telegram-local"
    codex.mkdir()
    profile=installer.profile(home,codex,home/"venv/python",["node_repl","telegram_guard"])
    if case=="private_unprotected":
        profile=profile.replace(json.dumps(str(home/"private"))+' = "deny"',
                                json.dumps(str(home/"private"))+' = "read"')
    if case=="capability_enabled":
        profile=profile.replace("plugins = false","plugins = true")
    (codex/"telegram-local.config.toml").write_text(profile)
    (codex/"config.toml").write_text('sandbox_mode="workspace-write"' if case=="legacy" else "")
    monkeypatch.setenv("CODEX_HOME",str(codex))
    monkeypatch.setattr(sys,"argv",["launch","--codex","fake.exe","--directory",str(tmp_path),"--check-only"])
    listings=[{"name":"telegram_guard","enabled":True}]
    if case=="extra_mcp":
        listings.append({"name":"raw_node","enabled":True})
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        assert "--strict-config" not in args
        return SimpleNamespace(stdout=json.dumps(listings))
    monkeypatch.setattr(launcher.subprocess,"run",run)
    if case=="valid":
        launcher.main()
        assert len(calls)==1
    else:
        with pytest.raises(SystemExit):
            launcher.main()
