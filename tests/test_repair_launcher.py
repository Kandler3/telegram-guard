"""Repeated repairs must accept an existing ordinary backup directory."""
import importlib.util
from pathlib import Path
from telegram_guard_local.files import write_json,read_json


def test_repair_can_run_twice_without_changing_port_or_home(tmp_path,monkeypatch):
    script=Path(__file__).parents[1]/'deploy'/'repair_launcher.py'
    spec=importlib.util.spec_from_file_location('repair_launcher_test',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    config={'codex':'old.exe','desktop':'old-gui.exe','node':'old-node.exe',
            'version':'old','port':48963,'codex_home':str(tmp_path)}
    proposed=dict(config,codex='new.exe',desktop='new-gui.exe',node='new-node.exe',version='new')
    write_json(tmp_path/'shared-desktop.json',config)
    original_read=module.read_json
    monkeypatch.setattr(module,'read_json',lambda p:proposed if Path(p).parent==script.parent else original_read(p))
    monkeypatch.setattr(module,'validate',lambda c:None)
    monkeypatch.setattr(module,'processes',lambda:[])
    monkeypatch.setattr(module,'owned_server',lambda *args:None)
    monkeypatch.setattr('sys.argv',['repair_launcher.py','--home',str(tmp_path)])
    module.main();module.main()
    assert read_json(tmp_path/'shared-desktop.json')==proposed
    assert len(list((tmp_path/'launcher-backups').glob('shared-desktop.*.json')))==2
