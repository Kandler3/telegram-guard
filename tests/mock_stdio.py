"""Only a test harness; never installed as a production entry point."""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.engine import Broker
from telegram_guard_local.store import Store
from telegram_guard_local.mcp_server import create_mcp

class FakeTransport:
    async def send_content(self,intent,files,guard):
        guard()
        return list(range(77,77+intent.message_count))
    async def send(self,peer,text,random_id):
        return 77
    async def history(self,peer,limit):
        return [{"text":"untrusted reply"}]

class MockRuntime(Runtime):
    @asynccontextmanager
    async def connection(self):
        store=Store(self.private/"audit.sqlite3")
        try:
            yield Broker(self.policy,FakeTransport(),store)
        finally:
            store.close()

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--home",required=True)
    args=p.parse_args()
    create_mcp(MockRuntime(args.home)).run(transport="stdio")
