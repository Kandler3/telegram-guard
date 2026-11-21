import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'deploy/agent-wake-runtime.ps1'


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def node_file(path, timestamp=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (timestamp, timestamp))
    return path


def resolve(tmp_path, configured, valid):
    # Mock only the capability probe. Directory discovery and path validation run normally.
    code=(f'. {ps_quote(HELPER)}\n'
          'function Test-AgentWakeNodeRuntime { param([string]$Executable) '
          f'@({",".join(ps_quote(p) for p in valid)}) -contains $Executable }}\n'
          f'$selected = Get-AgentWakeNodeRuntime -ConfiguredNode {ps_quote(configured)} '
          f'-LocalAppData {ps_quote(tmp_path / "local")} -UserProfile {ps_quote(tmp_path / "user")}\n'
          '$selected | ConvertTo-Json -Compress')
    return subprocess.run(['pwsh.exe', '-NoProfile', '-NonInteractive', '-Command', code],
                          cwd=tmp_path, capture_output=True, text=True, timeout=15)


def cua(tmp_path, name, timestamp=100):
    return node_file(tmp_path / 'local/OpenAI/Codex/runtimes/cua_node' / name / 'bin/node.exe', timestamp)


def primary(tmp_path):
    return node_file(tmp_path / 'user/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')


def test_existing_compatible_configured_runtime_is_preserved(tmp_path):
    old=node_file(tmp_path/'configured.exe');new=cua(tmp_path,'new',200)
    result=resolve(tmp_path,old,[old,new])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(old)


def test_deleted_runtime_is_replaced_with_discovered_cua_runtime(tmp_path):
    new=cua(tmp_path,'new')
    result=resolve(tmp_path,tmp_path/'deleted.exe',[new])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(new)


def test_incompatible_configured_runtime_is_skipped(tmp_path):
    old=node_file(tmp_path/'incompatible.exe');new=cua(tmp_path,'new')
    result=resolve(tmp_path,old,[new])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(new)


def test_newest_compatible_cua_is_selected_before_primary(tmp_path):
    old=cua(tmp_path,'old',100);new=cua(tmp_path,'new',200);fallback=primary(tmp_path)
    result=resolve(tmp_path,tmp_path/'deleted.exe',[old,new,fallback])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(new)


def test_incompatible_newest_cua_does_not_block_older_compatible_runtime(tmp_path):
    old=cua(tmp_path,'old',100);cua(tmp_path,'bad-new',200)
    result=resolve(tmp_path,tmp_path/'deleted.exe',[old])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(old)


def test_primary_runtime_is_used_when_desktop_runtime_was_removed(tmp_path):
    fallback=primary(tmp_path)
    result=resolve(tmp_path,tmp_path/'deleted.exe',[fallback])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(fallback)


def test_relative_configured_executable_cannot_use_working_directory(tmp_path):
    node_file(tmp_path/'node.exe');fallback=primary(tmp_path)
    result=resolve(tmp_path,'node.exe',['node.exe',fallback])
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==str(fallback)


def test_missing_runtimes_produce_actionable_failure(tmp_path):
    result=resolve(tmp_path,tmp_path/'missing.exe',[])
    assert result.returncode!=0
    assert 'No compatible Codex Node runtime' in result.stderr


def test_capability_probe_rejects_non_executable_file(tmp_path):
    bogus=node_file(tmp_path/'not-a-program.exe')
    code=f'. {ps_quote(HELPER)}; Test-AgentWakeNodeRuntime -Executable {ps_quote(bogus)} | ConvertTo-Json'
    result=subprocess.run(['pwsh.exe','-NoProfile','-NonInteractive','-Command',code],
                          capture_output=True,text=True,timeout=12)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout) is False


@pytest.mark.parametrize('configured_exists',[True,False])
def test_real_node_bootstrap_preserves_arguments_environment_cwd_and_exit_code(tmp_path,configured_exists):
    node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    if not node.is_file():pytest.skip('Bundled Codex Node is not installed')
    launcher=tmp_path/'agent-wake.ps1'
    shutil.copy2(ROOT/'deploy/agent-wake.ps1',launcher)
    shutil.copy2(HELPER,tmp_path/HELPER.name)
    # Synthetic CLI only: does not run a job or contact an App Server.
    (tmp_path/'cli.mjs').write_text("console.log(JSON.stringify({args:process.argv.slice(2),cwd:process.cwd(),thread:process.env.CODEX_THREAD_ID,runtime:process.execPath})); process.exitCode=7;",encoding='utf-8')
    home=tmp_path/'codex';(home/'telegram-local').mkdir(parents=True)
    settings=home/'telegram-local/shared-desktop.json'
    configured=node if configured_exists else tmp_path/'deleted-desktop-node.exe'
    settings.write_text(json.dumps({'node':str(configured),'port':48964}),encoding='utf-8')
    before=settings.read_bytes()
    arguments=['doctor','--text','text with spaces',"literal ' quote",'--','parameter-value']
    env=dict(os.environ,CODEX_HOME=str(home),CODEX_THREAD_ID='synthetic-thread',
             LOCALAPPDATA=str(tmp_path/'local'),USERPROFILE=str(Path.home()))
    result=subprocess.run(['pwsh.exe','-NoProfile','-NonInteractive','-File',str(launcher),*arguments],
                          env=env,cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode==7,result.stderr
    output=json.loads(result.stdout)
    assert output['args']==arguments
    assert Path(output['cwd'])==tmp_path and output['thread']=='synthetic-thread'
    assert Path(output['runtime'])==node
    assert settings.read_bytes()==before
