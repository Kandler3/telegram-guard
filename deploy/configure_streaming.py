"""Update only the explicit Telegram tool list and its timeout, preserving other settings."""
import argparse
import copy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from telegram_guard_local.tools import TOOLS


def configure(text):
    before=tomllib.loads(text)
    if 'telegram_guard' not in before.get('mcp_servers',{}):
        raise ValueError('Telegram MCP is not configured')
    match=re.search(r'(?m)^\[mcp_servers\.telegram_guard\][ \t]*(?:#[^\r\n]*)?$',text)
    if not match:raise ValueError('Expected the existing Telegram MCP table')
    end=re.search(r'(?m)^\[',text[match.end():])
    stop=match.end()+end.start() if end else len(text)
    body=text[match.start():stop]
    body,count=re.subn(r'(?m)^enabled_tools[ \t]*=[ \t]*\[[\s\S]*?\]',
                       'enabled_tools = '+json.dumps(TOOLS),body)
    if count!=1:raise ValueError('Expected one explicit enabled_tools list; refusing to remove the boundary')
    body,count=re.subn(r'(?m)^tool_timeout_sec[ \t]*=[^\r\n]*','tool_timeout_sec = 180',body)
    if not count:body=body.rstrip()+'\ntool_timeout_sec = 180\n\n'
    result=text[:match.start()]+body+text[stop:]
    after=tomllib.loads(result)
    expected=copy.deepcopy(before)
    expected['mcp_servers']['telegram_guard']['enabled_tools']=TOOLS
    expected['mcp_servers']['telegram_guard']['tool_timeout_sec']=180
    if after!=expected:raise ValueError('Unrelated settings changed')
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex-home',default=str(Path.home()/'.codex'))
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    target=Path(args.codex_home)/'config.toml'
    original=target.read_bytes()
    updated=configure(original.decode('utf-8-sig'))
    if args.apply and updated.encode('utf-8')!=original:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup=target.with_name(target.name+'.telegram-2.6.'+stamp+'.bak')
        if target.read_bytes()!=original:raise ValueError('Configuration changed concurrently')
        backup.write_bytes(original)
        fd,tmp=tempfile.mkstemp(prefix='telegram-config-',suffix='.tmp',dir=target.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
                stream.write(updated);stream.flush();os.fsync(stream.fileno())
            if target.read_bytes()!=original:raise ValueError('Configuration changed concurrently')
            os.replace(tmp,target)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    print(json.dumps({'applied':args.apply,'explicit_telegram_tools':len(TOOLS),'tool_timeout_sec':180,'other_settings_preserved':True}))


if __name__=='__main__':main()
