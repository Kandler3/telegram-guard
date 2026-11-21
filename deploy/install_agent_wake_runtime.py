"""Install only read-only Node discovery for agent-wake; do not touch jobs/settings."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import shutil


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',type=Path,default=Path.home()/'.codex'/'agent-wake')
    args=parser.parse_args()
    from telegram_guard_local.files import checked
    home=checked(args.home,directory=True)
    checked(home/'cli.mjs')
    root=Path(__file__).resolve().parents[1]
    pairs=[(checked(root/'deploy'/name),checked(home/name,missing=True))
           for name in ('agent-wake.ps1','agent-wake-runtime.ps1')]
    backup=checked(root/'backups',directory=True)/('agent-wake-runtime-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir()
    restore=[]
    for source,target in pairs:
        saved=backup/source.name if target.exists() else None
        if saved:shutil.copy2(target,saved)
        restore.append((saved,target))
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    rollback=backup/'rollback.ps1'
    rollback.write_text("$ErrorActionPreference = 'Stop'\n"+''.join(
        f'Copy-Item -LiteralPath {quote(saved)} -Destination {quote(target)} -Force\n' if saved else
        f'Remove-Item -LiteralPath {quote(target)} -ErrorAction SilentlyContinue\n'
        for saved,target in restore),encoding='utf-8')
    try:
        for source,target in pairs:
            shutil.copy2(source,target)
            if source.read_bytes()!=target.read_bytes():raise ValueError('Installed file mismatch')
    except BaseException:
        for saved,target in restore:
            if saved:shutil.copy2(saved,target)
            else:target.unlink(missing_ok=True)
        raise
    print('Installed agent-wake Node discovery. No settings, jobs or running processes changed.')
    print('Rollback:',rollback)


if __name__=='__main__':main()
