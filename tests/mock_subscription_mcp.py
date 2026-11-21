"""Synthetic incoming source for the App Server integration test."""
import argparse
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from telegram_guard_local.runtime import Runtime
from telegram_guard_local.mcp_server import create_mcp
class SyntheticRuntime(Runtime):
    def policy(self):
        class Policy:
            def require_read(self, chat):
                if chat != 123: raise ValueError("synthetic chat only")
        return Policy()
    async def watch(self, target, after_id=None, wait_seconds=30):
        if after_id is None:
            return {"cursor":10,"messages":[]}
        return {"cursor":11,"messages":[{"id":11,"sender_id":456,"date":"synthetic",
                "text":"guard_end_to_end_probe"}] if after_id<11 else []}
if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--home",required=True)
    args=parser.parse_args()
    Path(args.home,"private").mkdir(parents=True,exist_ok=True)
    create_mcp(SyntheticRuntime(args.home, app_server_url=os.environ["TELEGRAM_GUARD_APP_SERVER_URL"])).run(transport="stdio")

