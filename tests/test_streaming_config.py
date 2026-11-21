import importlib.util
from pathlib import Path
import tomllib


def test_upgrade_preserves_other_config_and_explicit_tool_boundary():
    path=Path(__file__).parents[1]/'deploy'/'configure_streaming.py'
    spec=importlib.util.spec_from_file_location('streaming_config',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original='''model="unchanged"
[mcp_servers.telegram_guard]
command="python.exe"
enabled_tools=["telegram_chats"]
tool_timeout_sec=90
[mcp_servers.telegram_guard.env]
TELEGRAM_GUARD_APP_SERVER_URL="ws://127.0.0.1:48963"
[mcp_servers.other]
enabled=true
'''
    after=tomllib.loads(module.configure(original))
    assert after['model']=='unchanged' and after['mcp_servers']['other']=={'enabled':True}
    tg=after['mcp_servers']['telegram_guard']
    assert len(tg['enabled_tools'])==21 and tg['tool_timeout_sec']==180
    assert tg['env']['TELEGRAM_GUARD_APP_SERVER_URL']=='ws://127.0.0.1:48963'
