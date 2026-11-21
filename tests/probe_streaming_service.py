"""Installed-runtime subprocess smoke test using only a synthetic home, no Telegram account."""
import asyncio
import json
from pathlib import Path
import tempfile
from telegram_guard_local.files import write_json
from telegram_guard_local.service import ServiceRuntime


async def run(home):
    (home/'private').mkdir()
    write_json(home/'whitelist.json',{'version':1,'chats':[{'chat_id':123,'label':'Synthetic only',
        'kind':'user','entity_id':123,'access_hash':456}],'messages_per_minute':6,'messages_per_day':100})
    first,second=ServiceRuntime(home),ServiceRuntime(home)
    try:
        endpoints=await asyncio.gather(first.ensure(),second.ensure())
        assert endpoints[0]==endpoints[1]
        chats=await asyncio.gather(first.list_chats(),second.list_chats())
        assert all(page['chats'][0]['label']=='Synthetic only' for page in chats)
        assert not (home/'private'/'telegram.session').exists()
        assert not (home/'private'/'account.json').exists()
        print(json.dumps({'automatic_subprocess_start':True,'two_clients_one_broker':True,
                          'telegram_account_accessed':False,'requests':len(chats)}))
    finally:
        await first.call('shutdown')
        for _ in range(100):
            if not (home/'private'/'broker-endpoint.json').exists():break
            await asyncio.sleep(.1)


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='streaming-service-',dir=Path(__file__).resolve().parents[1]/'.test-runs') as directory:
        asyncio.run(run(Path(directory)))
