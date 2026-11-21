"""Synthetic MCP caller-binding probe; reads only two non-secret context IDs."""
import os
from mcp.server.fastmcp import FastMCP, Context
mcp = FastMCP("context-probe")
@mcp.tool()
def probe_context(ctx: Context) -> dict:
    meta = ctx.request_context.meta
    return {"thread_env": os.environ.get("CODEX_THREAD_ID"),
            "session_env": os.environ.get("CODEX_SESSION_ID"),
            "meta": meta.model_dump() if meta else None}
if __name__ == "__main__":
    mcp.run(transport="stdio")
