"""Browser QA only: an isolated temporary whitelist and synthetic dialogs."""
import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from telegram_guard_local.files import write_json
from telegram_guard_local.whitelist_editor import Editor, EditorServer

root = Path(__file__).parents[1] / ".test-runs"
root.mkdir(exist_ok=True)
home = Path(tempfile.mkdtemp(prefix="editor-browser-", dir=root))
(home / "private").mkdir()
names = ["Анна Смирнова", "Команда проекта", "Дизайн-команда", "Михаил Волков", "Поездка в горы", "Избранное",
         "Проект <img src=x onerror=alert(1)>", "Обсуждения продукта"]
rows = []
for i, name in enumerate(names + [f"Тестовый чат {i:03}" for i in range(110)]):
    cid = 100 + i
    peer = {"chat_id": cid, "label": name, "kind": "user", "entity_id": cid, "access_hash": 987654321}
    rows.append({"chat_id": str(cid), "label": name, "username": f"test_{cid}", "kind": "user", "reason": "", "peer": peer})
rows.append({"chat_id": "-1000000000999", "label": "Новости технологий", "kind": "channel", "username": "news_test",
             "reason": "Каналы не поддерживаются", "peer": None})
write_json(home / "whitelist.json", {"version": 1, "chats": [rows[0]["peer"], rows[1]["peer"]],
                                   "messages_per_minute": 6, "messages_per_day": 200})
async def loader(_):
    return rows
editor = Editor(home, search="Дизайн", loader=loader)
server = EditorServer(editor)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print(json.dumps({"url": server.origin + "/#token=" + editor.bootstrap, "origin": server.origin,
                  "home": str(home)}, ensure_ascii=False), flush=True)
try:
    while not editor.closed and time.monotonic() < editor.deadline:
        time.sleep(.1)
finally:
    server.shutdown()
    server.server_close()
