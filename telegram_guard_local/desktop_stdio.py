"""Normal Desktop stdio transport relayed to the shared loopback App Server.

No heartbeat deadline, server restart, RPC replay or approval handling.
Actual local connection failure remains an explicit process failure.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading

from .app_server import local_endpoint
from .files import read_json

MAX_MESSAGE_BYTES = 100 * 1024 * 1024


class RelayError(Exception):
    pass


def relay(endpoint, source, destination, *, connect=None):
    """Opaque, bounded JSONL relay. Never reconnect or replay ambiguous RPCs."""
    local_endpoint(endpoint)
    if connect is None:
        from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    finished = threading.Event()
    input_error = []
    # Loopback control transport must survive offline intervals and clock jumps.
    with connect(endpoint, proxy=None, compression=None, ping_interval=None,
                 ping_timeout=None, open_timeout=5, close_timeout=2,
                 max_size=MAX_MESSAGE_BYTES, max_queue=16) as ws:
        def send_input():
            try:
                while True:
                    line = source.readline(MAX_MESSAGE_BYTES + 2)
                    if not line:
                        finished.set()
                        break
                    if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                        raise RelayError("desktop_stdio_invalid_frame")
                    text = line.decode("utf-8").rstrip("\r\n")
                    if text.strip():
                        ws.send(text)
            except Exception:
                input_error.append(True)
            finally:
                ws.close()  # Unblocks recv when Desktop closes stdin.

        threading.Thread(target=send_input, name="desktop-stdio-input", daemon=True).start()
        try:
            for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                destination.write(message.encode("utf-8") + b"\n")
                destination.flush()
        except ConnectionClosed:
            if not finished.is_set():
                raise RelayError("desktop_stdio_server_disconnected") from None
        if input_error:
            raise RelayError("desktop_stdio_input_failed")
        if not finished.is_set():
            raise RelayError("desktop_stdio_server_disconnected")


def codex_main():
    """Desktop executable override; delegate other CLI commands to the real CLI."""
    home = Path(os.environ.get("TELEGRAM_GUARD_DESKTOP_HOME",
                               str(Path.home() / ".codex" / "telegram-local")))
    config = read_json(home / "shared-desktop.json")
    args = sys.argv[1:]
    if not is_server_start(args):
        env = dict(os.environ)
        env["CODEX_CLI_PATH"] = config["codex"]
        raise SystemExit(subprocess.call([config["codex"], *args], env=env,
                                        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    # Start the same real stdio process Desktop requested, with its original environment.
    # Additional settings configure only the guarded event sink, not native services.
    import asyncio
    import json
    from .native_bridge import run
    from .tools import TOOLS
    auth_file=home.parent/'event-bridge'/'auth.json'
    overrides=[
        '-c','mcp_servers.telegram_guard.env.TELEGRAM_GUARD_APP_SERVER_URL='+json.dumps(f'ws://127.0.0.1:{config["port"]}'),
        '-c','mcp_servers.telegram_guard.env.TELEGRAM_GUARD_EVENT_BRIDGE_AUTH_FILE='+json.dumps(str(auth_file)),
        '-c','mcp_servers.telegram_guard.enabled_tools='+json.dumps(TOOLS)]
    env=dict(os.environ,CODEX_CLI_PATH=config['codex'])
    try:
        raise SystemExit(asyncio.run(run(config['codex'],[*overrides,*args],config['port'],auth_file,environment=env)))
    except (OSError,ValueError):
        print('Native Desktop transport failed to start. No requests were replayed.',file=sys.stderr)
        raise SystemExit(1) from None


def is_server_start(args):
    """CLI diagnostics/code generation must keep their real CLI behavior."""
    commands = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-c", "--config", "--enable", "--disable",
                   "--startup-client-info", "--cloud-skills-snapshot", "--code-mode-host"):
            skip = True
        elif not arg.startswith("-"):
            commands.append(arg)
    return (commands == ["app-server"] and
            not any(arg in args for arg in ("--help", "-h", "--version", "-V",
                                           "--print-startup-config", "--startup-sign-out")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    args = parser.parse_args()
    try:
        relay(args.endpoint, sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        print("Local shared App Server connection failed; requests were not replayed.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
