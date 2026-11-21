"""Install automatic discovery only; never restart Desktop or touch Telegram policy."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',type=Path,default=Path.home()/'.codex'/'telegram-local')
    args=parser.parse_args()
    from telegram_guard_local.files import checked
    home=checked(args.home,directory=True)
    root=Path(__file__).resolve().parents[1]
    package=checked(home/'venv'/'Lib'/'site-packages'/'telegram_guard_local',directory=True)
    pairs=[(root/'telegram_guard_local'/name,package/name) for name in
           ('launcher_compat.py','shared_desktop.py')]
    pairs.append((root/'deploy'/'start-codex.pyw',home/'start-codex.pyw'))
    for source,target in pairs:
        checked(source);checked(target,missing=True)
        compile(source.read_bytes(),str(source),'exec')
    backups=checked(root/'backups',directory=True)
    backup=backups/('auto-launcher-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir()
    restore=[]
    for source,target in pairs:
        saved=backup/source.name
        if target.exists():shutil.copy2(target,saved)
        restore.append((saved if target.exists() else None,target))
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    rollback=backup/'rollback.ps1'
    rollback.write_text("$ErrorActionPreference = 'Stop'\n"+''.join(
        (f'Copy-Item -LiteralPath {quote(saved)} -Destination {quote(target)} -Force\n' if saved else
         f'Remove-Item -LiteralPath {quote(target)} -ErrorAction SilentlyContinue\n')
        for saved,target in restore)+"Write-Output 'Previous launcher restored; no process interrupted.'\n",
        encoding='utf-8')
    try:
        for source,target in pairs:
            shutil.copy2(source,target)
            if source.read_bytes()!=target.read_bytes():raise ValueError('Installed readback mismatch')
        # Verify and warm only the public capability cache. Account state and
        # the running Desktop/native bridge are never used by this check.
        code=('from pathlib import Path; '
              'from telegram_guard_local.files import read_json; '
              'from telegram_guard_local.shared_desktop import resolve_configuration; '
              'from telegram_guard_local.launcher_compat import ensure_compatible; '
              f'home=Path({str(home)!r}); '
              'config=resolve_configuration(read_json(home/"shared-desktop.json")); '
              'print("Installed auto launcher:",ensure_compatible(home,config["codex"],persist=True))')
        result=subprocess.run([str(home/'venv'/'Scripts'/'python.exe'),'-I','-B','-c',code],
                              capture_output=True,text=True,check=True,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        print(result.stdout.strip())
    except BaseException:
        for saved,target in restore:
            if saved:shutil.copy2(saved,target)
            else:target.unlink(missing_ok=True)
        raise
    print('Rollback:',rollback)
    print('Automatic launcher installed. Desktop and active tasks were not restarted.')


if __name__=='__main__':main()
