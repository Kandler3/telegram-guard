"""Export the installed Telethon request surface without connecting to Telegram."""
import ast
import csv
import importlib
import inspect
import json
from pathlib import Path
import pkgutil

import telethon
from telethon.tl import functions
from telethon.tl.alltlobjects import LAYER
from telethon.tl.tlobject import TLRequest


def main():
    root = Path(__file__).resolve().parents[1]
    references = {}
    exposed = []
    for source in sorted((root / "telegram_guard_local").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                parts = []
                current = node
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name) and current.id == "functions":
                    name = ".".join(reversed(parts))
                    if name.endswith("Request"):
                        references.setdefault(name, set()).add(source.name)
            if source.name == "mcp_server.py" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("telegram_"):
                    exposed.append(node.name)
    rows = []
    modules = [functions] + [importlib.import_module(m.name) for m in pkgutil.iter_modules(functions.__path__, functions.__name__ + ".")]
    for module in modules:
        namespace = "" if module is functions else module.__name__.rsplit(".", 1)[-1]
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, TLRequest):
                continue
            key = f"{namespace}.{name}" if namespace else name
            rows.append({
                "namespace": namespace or "core",
                "request_class": name,
                "parameters": str(inspect.signature(cls)),
                "constructor_id": f"0x{cls.CONSTRUCTOR_ID:08x}",
                "guard_direct_reference_files": ";".join(sorted(references.get(key, []))),
                "policy_decision": "",
            })
    rows.sort(key=lambda row: (row["namespace"], row["request_class"]))
    base = root / "deploy" / f"telegram-api-layer-{LAYER}"
    with base.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    document = {
        "telethon_version": telethon.__version__, "layer": LAYER,
        "request_count": len(rows), "guard_tools": sorted(exposed),
        "notes": [
            "Local generated request classes; no Telegram connection was made.",
            "Request class identifiers are library names, not necessarily wire method spelling.",
            "Direct source references are not a reachability or permission classification: high-level client methods also invoke RPCs internally.",
            "Not every RPC is available to a personal account; Telegram enforces account type, membership, administrator and other requirements.",
            "policy_decision is intentionally blank for human policy design.",
        ],
        "event_transport": {"current": "polling", "requested": "MTProto Updates with reconnect and gap recovery", "implemented": False},
        "requests": rows,
    }
    base.with_suffix(".json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(rows), "tools": sorted(exposed), "files": [str(base.with_suffix(ext)) for ext in (".csv", ".json")]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
