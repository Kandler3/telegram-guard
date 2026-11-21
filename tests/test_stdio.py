import asyncio,json,os,sys,uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def test_full_stdio_without_http_or_telegram(tmp_path):
    private=tmp_path/"private"
    private.mkdir()
    (tmp_path/"whitelist.json").write_text(json.dumps({"version":1,"chats":[
        {"chat_id":123,"label":"Ivan","kind":"user","entity_id":123,"access_hash":456}],
        "messages_per_minute":6,"messages_per_day":200}))
    async def run():
        params=StdioServerParameters(command=sys.executable,args=[
            "-B",str(Path(__file__).parent/"mock_stdio.py"),"--home",str(tmp_path)],env=dict(os.environ))
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools=await session.list_tools()
                from telegram_guard_local.tools import TOOLS
                assert {t.name for t in tools.tools}==set(TOOLS)
                chats=await session.call_tool("telegram_chats",{})
                assert not chats.isError and "Ivan" in str(chats)
                auth={"user_instruction":"напиши Ивану статус","scope":"current task",
                      "scope_chat_ids":[123],"source":"human","expires_at":None}
                rid=str(uuid.uuid4())
                payload={"chat_id":123,"text":"done","request_id":rid,"authorization":auth}
                assert not (await session.call_tool("telegram_send",payload)).isError
                assert not (await session.call_tool("telegram_send",payload)).isError
                status=await session.call_tool("telegram_send_status",{"request_id":rid})
                assert not status.isError and "77" in str(status)
                denied=await session.call_tool("telegram_send",dict(payload,chat_id=999,request_id=str(uuid.uuid4()),
                    authorization=dict(auth,scope_chat_ids=[999])))
                assert denied.isError and "chat_not_allowed" in str(denied)
                incomplete=await session.call_tool("telegram_send",{"chat_id":123,"text":"oops"})
                assert incomplete.isError
                override=await session.call_tool("telegram_send",dict(payload,
                    authorization=dict(auth,peer=999)))
                assert override.isError
                attachment=tmp_path.parent/(tmp_path.name+"-document.txt")
                attachment.write_text("Synthetic attachment; no personal data.")
                media_payload={"chat_id":123,"request_id":str(uuid.uuid4()),"authorization":auth,
                               "content":{"type":"file","path":str(attachment),"kind":"document"}}
                media=await session.call_tool("telegram_send",media_payload)
                assert not media.isError and "message_ids" in str(media)
                album_payload=dict(media_payload,request_id=str(uuid.uuid4()),content={"type":"album",
                                  "items":[media_payload["content"],media_payload["content"]]})
                album=await session.call_tool("telegram_send",album_payload)
                assert not album.isError and "78" in str(album)
                invalid=await session.call_tool("telegram_send",dict(media_payload,content={"type":"raw_rpc"}))
                assert invalid.isError
    asyncio.run(run())
