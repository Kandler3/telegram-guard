import importlib.util
from pathlib import Path
import subprocess

import pytest


def test_failed_install_verification_restores_every_previous_file(tmp_path,monkeypatch):
    script=Path(__file__).parents[1]/'deploy/install_auto_launcher.py'
    spec=importlib.util.spec_from_file_location('auto_installer_test',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=tmp_path/'repo';(root/'deploy').mkdir(parents=True)
    (root/'telegram_guard_local').mkdir();(root/'backups').mkdir()
    for name in ('launcher_compat.py','shared_desktop.py'):
        (root/'telegram_guard_local'/name).write_text('new_code=True\n')
    (root/'deploy/start-codex.pyw').write_text('new_code=True\n')
    home=tmp_path/'home';package=home/'venv/Lib/site-packages/telegram_guard_local'
    package.mkdir(parents=True)
    (package/'shared_desktop.py').write_text('previous_code=True\n')
    (home/'start-codex.pyw').write_text('previous_launcher=True\n')
    before={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    monkeypatch.setattr(module,'__file__',str(root/'deploy/install_auto_launcher.py'))
    monkeypatch.setattr('sys.argv',['install_auto_launcher.py','--home',str(home)])
    def fail(*args,**kwargs):raise subprocess.CalledProcessError(1,args[0])
    monkeypatch.setattr(module.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError):module.main()
    after={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    assert before==after
    assert len(list((root/'backups').rglob('rollback.ps1')))==1
