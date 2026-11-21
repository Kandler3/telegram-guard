"""Apply a tested public launcher configuration, preserving proof of the old owned server."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from telegram_guard_local.files import checked,read_json,write_json
from telegram_guard_local.shared_desktop import validate,processes,owned_server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',default=str(Path.home()/'.codex'/'telegram-local'))
    args=parser.parse_args()
    home=checked(Path(args.home),directory=True)
    proposed=read_json(Path(__file__).with_name('shared-desktop.json'))
    validate(proposed)
    target=home/'shared-desktop.json'
    previous=read_json(target)
    # Repair changes executable paths/version only; endpoint and user home remain unchanged.
    if proposed['port']!=previous['port'] or proposed['codex_home']!=previous['codex_home']:
        raise ValueError('Refusing to change shared endpoint or Codex home during executable repair')
    receipt_path=home/'shared-server-receipt.json'
    receipt=read_json(receipt_path) if receipt_path.exists() else None
    owner=owned_server(receipt,processes(),previous)
    backups=home/'launcher-backups'
    checked(backups,directory=True,missing=True);backups.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    write_json(backups/('shared-desktop.'+stamp+'.json'),previous)
    if receipt:
        write_json(backups/('shared-server-receipt.'+stamp+'.json'),receipt)
    if owner:
        write_json(receipt_path,dict(receipt,executable=owner['ExecutablePath']))
    write_json(target,proposed)
    print(json.dumps({'launcher_config':'repaired','version':proposed['version'],
                      'old_owned_server_preserved':owner is not None,'desktop_interrupted':False}))


if __name__=='__main__':main()
