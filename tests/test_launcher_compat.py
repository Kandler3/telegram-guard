import copy
import json
import os
from pathlib import Path

import pytest

from telegram_guard_local import launcher_compat as compat
from telegram_guard_local import shared_desktop as launcher
from telegram_guard_local.files import read_json, write_json


def schema():
    methods=[]
    for method,params in compat.SAMPLES.items():
        fields={name:{} for name in params}
        if method=='turn/start':
            fields['toolOutput']={'type':'object','properties':{
                'name':{'type':'string'},'namespace':{'type':'string'},'output':{'type':'string'}},
                'required':['name','output']}
        methods.append({'type':'object','properties':{'id':{'type':'string'},
            'method':{'const':method},'params':{'type':'object','properties':fields,
                                             'required':list(params)}},
            'required':['id','method','params']})
    return {'$schema':'http://json-schema.org/draft-07/schema#','oneOf':methods}


def test_required_protocol_shapes_are_accepted():
    compat.check_schema(schema())


@pytest.mark.parametrize('method',list(compat.SAMPLES))
def test_missing_method_is_an_actual_incompatibility(method):
    document=schema()
    document['oneOf']=[entry for entry in document['oneOf'] if entry['properties']['method']['const']!=method]
    with pytest.raises(compat.CompatibilityError,match=method):compat.check_schema(document)


@pytest.mark.parametrize('field',['toolOutput','namespace','output'])
def test_removed_optional_fields_cannot_be_hidden_by_additional_properties(field):
    document=schema()
    props=document['oneOf'][-1]['properties']['params']['properties']
    target=props if field=='toolOutput' else props['toolOutput']['properties']
    del target[field]
    with pytest.raises(compat.CompatibilityError):compat.check_schema(document)


def test_changed_payload_type_is_rejected():
    document=schema()
    document['oneOf'][-1]['properties']['params']['properties']['toolOutput']['properties']['output']={'type':'array'}
    with pytest.raises(compat.CompatibilityError,match='turn/start'):compat.check_schema(document)


def test_external_schema_reference_is_never_retrieved():
    document=schema();document['definitions']={'foreign':{'$ref':'https://invalid.test/schema'}}
    with pytest.raises(compat.CompatibilityError,match='внешних'):compat.check_schema(document)


def test_positive_cache_is_keyed_by_content_and_checker_revision(tmp_path,monkeypatch):
    binary=tmp_path/'codex.exe';binary.write_bytes(b'first build')
    calls=[]
    monkeypatch.setattr(compat,'probe',lambda binary:calls.append(str(binary)))
    assert compat.ensure_compatible(tmp_path,binary,persist=True)=='verified'
    assert compat.ensure_compatible(tmp_path,binary,persist=True)=='cached'
    relocated=tmp_path/'relocated.exe';relocated.write_bytes(binary.read_bytes())
    assert compat.ensure_compatible(tmp_path,relocated)=='cached'
    assert len(calls)==1
    binary.write_bytes(b'second build, same version string')
    assert compat.ensure_compatible(tmp_path,binary,persist=True)=='verified'
    monkeypatch.setattr(compat,'CHECK_REVISION',compat.CHECK_REVISION+1)
    assert compat.ensure_compatible(tmp_path,binary,persist=True)=='verified'
    assert len(calls)==3


def test_read_only_check_does_not_create_cache(tmp_path,monkeypatch):
    binary=tmp_path/'codex.exe';binary.write_bytes(b'build')
    monkeypatch.setattr(compat,'probe',lambda binary:None)
    assert compat.ensure_compatible(tmp_path,binary)=='verified'
    assert not (tmp_path/'launcher-compatibility.json').exists()


def test_failed_probe_does_not_overwrite_last_good_cache(tmp_path,monkeypatch):
    binary=tmp_path/'codex.exe';binary.write_bytes(b'good')
    monkeypatch.setattr(compat,'probe',lambda binary:None)
    compat.ensure_compatible(tmp_path,binary,persist=True)
    before=(tmp_path/'launcher-compatibility.json').read_bytes()
    binary.write_bytes(b'incompatible')
    def reject(binary):raise compat.CompatibilityError('missing capability')
    monkeypatch.setattr(compat,'probe',reject)
    with pytest.raises(compat.CompatibilityError):compat.ensure_compatible(tmp_path,binary,persist=True)
    assert (tmp_path/'launcher-compatibility.json').read_bytes()==before


def test_executable_change_during_probe_is_not_cached(tmp_path,monkeypatch):
    binary=tmp_path/'codex.exe';binary.write_bytes(b'before')
    monkeypatch.setattr(compat,'probe',lambda binary:Path(binary).write_bytes(b'after'))
    with pytest.raises(compat.CompatibilityError,match='во время проверки'):
        compat.ensure_compatible(tmp_path,binary,persist=True)
    assert not (tmp_path/'launcher-compatibility.json').exists()


def test_probe_environment_does_not_inherit_account_or_native_services(tmp_path,monkeypatch):
    for name in ('OPENAI_API_KEY','CODEX_HOME','CODEX_THREAD_ID','BROWSER_USE_ACCESS_PIPE_PATH',
                 'CODEX_APP_TOOLS_PIPE_PATH','HTTPS_PROXY','CUSTOM_SECRET'):
        monkeypatch.setenv(name,'must-not-leak')
    env=compat.isolated_environment(tmp_path)
    assert env['CODEX_HOME']==str(tmp_path)
    assert not any(value=='must-not-leak' for value in env.values())


def test_node_runtime_is_rediscovered_after_relocation(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    root=tmp_path/'OpenAI/Codex/runtimes/cua_node'
    old=root/'old/bin/node.exe';new=root/'new/bin/node.exe'
    for path in (old,new):path.parent.mkdir(parents=True);path.touch()
    os.utime(old,ns=(100,100));os.utime(new,ns=(200,200))
    assert launcher.installed_node(str(tmp_path/'missing.exe'))==str(new)


def config(tmp_path):
    return {'codex':'old-codex.exe','desktop':'old-desktop.exe','node':'old-node.exe',
            'version':'old','port':48964,'codex_home':str(tmp_path/'codex-home')}


def test_check_resolves_new_version_without_modifying_config_or_starting_process(tmp_path,monkeypatch,capsys):
    previous=config(tmp_path);write_json(tmp_path/'shared-desktop.json',previous)
    current=dict(previous,version='codex-cli future',codex='new.exe',desktop='new-gui.exe')
    monkeypatch.setattr(launcher,'processes',lambda:[])
    monkeypatch.setattr(launcher,'resolve_configuration',lambda value:current)
    seen=[]
    monkeypatch.setattr(compat,'ensure_compatible',lambda *a,**kw:seen.append(kw) or 'verified')
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**kw:pytest.fail('check must not spawn Desktop'))
    launcher.run(tmp_path,'check')
    result=json.loads(capsys.readouterr().out)
    assert result['configuration_refresh_needed'] and result['version']=='codex-cli future'
    assert read_json(tmp_path/'shared-desktop.json')==previous
    assert seen==[{'persist':False}]


def test_incompatible_upgrade_does_not_change_configuration_or_start_desktop(tmp_path,monkeypatch):
    previous=config(tmp_path);write_json(tmp_path/'shared-desktop.json',previous)
    monkeypatch.setattr(launcher,'processes',lambda:[])
    monkeypatch.setattr(launcher,'resolve_configuration',lambda value:dict(value,version='codex-cli future'))
    def incompatible(*args,**kwargs):raise compat.CompatibilityError('missing capability')
    monkeypatch.setattr(compat,'ensure_compatible',incompatible)
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**kw:pytest.fail('incompatible launch'))
    with pytest.raises(compat.CompatibilityError):launcher.run(tmp_path,'start')
    assert read_json(tmp_path/'shared-desktop.json')==previous


def test_successful_auto_refresh_preserves_access_settings_and_backup(tmp_path):
    previous=config(tmp_path);write_json(tmp_path/'shared-desktop.json',previous)
    current=dict(previous,codex='new-codex.exe',desktop='new-desktop.exe',version='future')
    launcher.save_configuration(tmp_path,previous,current)
    assert read_json(tmp_path/'shared-desktop.json')==current
    backups=list((tmp_path/'launcher-backups').glob('auto-shared-desktop.*.json'))
    assert len(backups)==1 and read_json(backups[0])==previous
    launcher.save_configuration(tmp_path,current,current)
    assert len(list((tmp_path/'launcher-backups').glob('*')))==1


def test_already_open_desktop_is_not_reconfigured_or_checked(tmp_path,monkeypatch):
    previous=config(tmp_path);write_json(tmp_path/'shared-desktop.json',previous)
    monkeypatch.setattr(launcher,'processes',lambda:[{
        'ExecutablePath':r'C:\Program Files\WindowsApps\OpenAI.Codex_future\app\ChatGPT.exe'}])
    monkeypatch.setattr(launcher,'resolve_configuration',lambda value:pytest.fail('must not reconfigure live Desktop'))
    with pytest.raises(ValueError,match='Close Desktop'):launcher.run(tmp_path,'start')
    assert read_json(tmp_path/'shared-desktop.json')==previous


def test_compatible_future_version_auto_updates_and_launches_only_desktop(tmp_path,monkeypatch):
    previous=config(tmp_path);write_json(tmp_path/'shared-desktop.json',previous)
    current=dict(previous,codex='future-codex.exe',desktop='future-gui.exe',version='future')
    monkeypatch.setattr(launcher,'processes',lambda:[])
    monkeypatch.setattr(launcher,'resolve_configuration',lambda value:current)
    monkeypatch.setattr(launcher,'installed_desktop',lambda:current['desktop'])
    monkeypatch.setattr(launcher,'desktop_environment',lambda home,c:{'sentinel':'unchanged'})
    monkeypatch.setattr(compat,'ensure_compatible',lambda *a,**kw:'cached')
    import socket
    class FreePort:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,value):pass
        def connect_ex(self,address):return 1
    monkeypatch.setattr(socket,'socket',FreePort)
    calls=[]
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**kw:calls.append((a,kw)))
    launcher.run(tmp_path,'start')
    assert read_json(tmp_path/'shared-desktop.json')==current
    assert len(calls)==1 and calls[0][0][0]==['future-gui.exe']
    assert calls[0][1]['env']=={'sentinel':'unchanged'}
