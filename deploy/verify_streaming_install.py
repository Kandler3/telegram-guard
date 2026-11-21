"""Verify only installed code, skill and declared tool configuration; no account access."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import tomllib
import zipfile
import telegram_guard_local
from telegram_guard_local.tools import TOOLS

root=Path(__file__).resolve().parents[1]
package=Path(telegram_guard_local.__file__).parent
wheel=root/'dist'/'codex_telegram_guard_local-2.6.0-py3-none-any.whl'
assert importlib.metadata.version('codex-telegram-guard-local')=='2.6.0'
assert telegram_guard_local.__version__=='2.6.0'
count=0
with zipfile.ZipFile(wheel) as archive:
    for source in (root/'telegram_guard_local').rglob('*'):
        if not source.is_file() or '__pycache__' in source.parts:continue
        relative=source.relative_to(root/'telegram_guard_local')
        assert source.read_bytes()==(package/relative).read_bytes()==archive.read('telegram_guard_local/'+relative.as_posix()),str(relative)
        count+=1
for source in (root/'skills'/'telegram-send').rglob('*'):
    if source.is_file() and '__pycache__' not in source.parts:
        installed=Path.home()/'.codex'/'skills'/'telegram-send'/source.relative_to(root/'skills'/'telegram-send')
        assert source.read_bytes()==installed.read_bytes(),str(source)
config=tomllib.loads((Path.home()/'.codex'/'config.toml').read_text(encoding='utf-8-sig'))
assert config['mcp_servers']['telegram_guard']['enabled_tools']==TOOLS
assert config['mcp_servers']['telegram_guard']['tool_timeout_sec']==180
print(json.dumps({'installed_version':'2.6.0','verified_code_files':count,'explicit_tools':len(TOOLS),
                  'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'skill_matches':True}))
