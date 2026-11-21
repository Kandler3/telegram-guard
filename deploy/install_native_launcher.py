"""Install only launcher/event-client code. Never restart Desktop or inspect private state."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',type=Path,default=Path.home()/'.codex'/'telegram-local')
    args=parser.parse_args()
    home=args.home.resolve()
    root=Path(__file__).resolve().parents[1]
    package=home/'venv'/'Lib'/'site-packages'/'telegram_guard_local'
    pairs=[(root/'telegram_guard_local'/name,package/name) for name in
        ('shared_desktop.py','desktop_stdio.py','app_server.py','native_bridge.py')]
    pairs.append((root/'deploy'/'agent-wake-rpc.mjs',home.parent/'agent-wake'/'rpc.mjs'))
    pairs.append((root/'deploy'/'native-shared-desktop.json',home/'shared-desktop.json'))
    for source,target in pairs:
        if not source.is_file() or not target.parent.is_dir():raise ValueError('Missing installation input')
        if source.suffix=='.py':compile(source.read_bytes(),str(source),'exec')
    backup=root/'backups'/('native-launcher-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(parents=True)
    restore=[]
    for source,target in pairs:
        if target.is_file():
            saved=backup/source.name
            shutil.copy2(target,saved)
            restore.append((saved,target))
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    rollback=backup/'rollback.ps1'
    rollback.write_text("$ErrorActionPreference = 'Stop'\n"+''.join(
        f'Copy-Item -LiteralPath {quote(saved)} -Destination {quote(target)} -Force\n'
        for saved,target in restore)+"Write-Output 'Previous launcher restored for next launch; no process interrupted.'\n",encoding='utf-8')
    try:
        for source,target in pairs:
            shutil.copy2(source,target)
            if source.read_bytes()!=target.read_bytes():raise ValueError('Installed readback mismatch')
        subprocess.run([str(home/'venv'/'Scripts'/'python.exe'),'-I','-B','-c',
            'import telegram_guard_local.native_bridge, telegram_guard_local.desktop_stdio, telegram_guard_local.shared_desktop, telegram_guard_local.app_server; print("Installed launcher imports: OK")'],
            check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except BaseException:
        for saved,target in restore:shutil.copy2(saved,target)
        raise
    print('Rollback:',rollback)
    print('Installed for next launch. Desktop, active tasks, credentials and Telegram policy unchanged.')

if __name__=='__main__':main()
