import io
import json
import os
from pathlib import Path
import queue
import threading

import pytest
from websockets.sync.server import serve

from telegram_guard_local.desktop_stdio import RelayError, relay, is_server_start
from telegram_guard_local.shared_desktop import desktop_environment


class Output:
    def __init__(self):
        self.messages = queue.Queue()

    def write(self, value):
        self.messages.put(json.loads(value))

    def flush(self):
        pass


def start_relay(endpoint):
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, 'rb')
    writer = os.fdopen(write_fd, 'wb', buffering=0)
    output = Output()
    errors = []

    def run():
        try:
            relay(endpoint, source, output)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return writer, output, worker, errors, source


def test_model_failure_keeps_control_pipe_and_forwards_approval_without_answering():
    seen = []

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            seen.append(request)
            if request.get('id') == 'offline':
                ws.send(json.dumps({'id': 'offline', 'error': {'code': -32000, 'message': 'offline'}}))
                ws.send(json.dumps({'id': 987, 'method': 'item/commandExecution/requestApproval', 'params': {}}))
            elif request.get('id') == 'online':
                ws.send(json.dumps({'id': 'online', 'result': {'text': 'Снова в сети'}}))

    with serve(handler, '127.0.0.1', 0, ping_interval=None) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        writer, output, worker, errors, source = start_relay(f'ws://127.0.0.1:{server.socket.getsockname()[1]}')
        try:
            writer.write(b'{"id":"offline","method":"turn/start","params":{}}\n')
            assert output.messages.get(timeout=3)['error']['message'] == 'offline'
            approval = output.messages.get(timeout=3)
            assert approval['method'] == 'item/commandExecution/requestApproval'
            # Only the human/Desktop provides the approval response.
            writer.write(b'{"id":987,"result":{"decision":"decline"}}\n')
            writer.write(b'{"id":"online","method":"thread/loaded/list","params":{}}\n')
            assert output.messages.get(timeout=3)['result']['text'] == 'Снова в сети'
            assert worker.is_alive()
        finally:
            writer.close()
            worker.join(4)
            source.close()
        assert not worker.is_alive()
        assert not errors
        assert [request['id'] for request in seen] == ['offline', 987, 'online']


def test_actual_server_disconnect_is_explicit_and_never_replays():
    seen = []

    def handler(ws):
        seen.append(json.loads(ws.recv()))
        ws.close()

    with serve(handler, '127.0.0.1', 0, ping_interval=None) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        writer, output, worker, errors, source = start_relay(f'ws://127.0.0.1:{server.socket.getsockname()[1]}')
        try:
            writer.write(b'{"id":1,"method":"turn/start","params":{}}\n')
            worker.join(4)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], RelayError)
            assert str(errors[0]) == 'desktop_stdio_server_disconnected'
            assert len(seen) == 1
        finally:
            writer.close()
            source.close()


@pytest.mark.parametrize('url', ['wss://127.0.0.1:1234', 'ws://example.com:1234',
                                     'ws://127.0.0.1:1234/?token=x'])
def test_relay_rejects_nonlocal_or_credential_endpoints(url):
    with pytest.raises(Exception):
        relay(url, io.BytesIO(), io.BytesIO())


def test_desktop_uses_stdio_and_preserves_unrelated_environment(tmp_path, monkeypatch):
    adapter = tmp_path / 'venv' / 'Scripts' / 'codex-shared-stdio.exe'
    adapter.parent.mkdir(parents=True)
    adapter.touch()
    monkeypatch.setenv('CODEX_APP_SERVER_WS_URL', 'ws://127.0.0.1:1')
    monkeypatch.setenv('CODEX_THREAD_ID', 'stale-thread')
    monkeypatch.setenv('UNRELATED_SETTING', 'preserved')
    env = desktop_environment(tmp_path, {'codex_home': str(tmp_path / 'codex')})
    assert env['CODEX_APP_SERVER_FORCE_CLI'] == '1'
    assert env['CODEX_CLI_PATH'] == str(adapter)
    assert env['UNRELATED_SETTING'] == 'preserved'
    assert 'CODEX_APP_SERVER_WS_URL' not in env and 'CODEX_THREAD_ID' not in env


def test_missing_adapter_refuses_before_launch(tmp_path):
    with pytest.raises(ValueError, match='adapter is missing'):
        desktop_environment(tmp_path, {'codex_home': str(tmp_path)})


@pytest.mark.parametrize('args,expected', [
    (['app-server', '--analytics-default-enabled'], True),
    (['-c', 'features.code_mode_host=true', 'app-server'], True),
    (['app-server', '--startup-client-info', '{}'], True),
    (['--version'], False),
    (['app-server', '--print-startup-config'], False),
    (['app-server', '--help'], False),
    (['app-server', 'generate-json-schema'], False),
    (['app-server', 'daemon', 'stop'], False),
    (['exec', 'app-server'], False),
])
def test_only_actual_app_server_start_is_relayed(args, expected):
    assert is_server_start(args) == expected


def test_cli_delegation_explicitly_inherits_stdio_handles_on_windows(monkeypatch):
    import sys
    from telegram_guard_local import desktop_stdio as module
    monkeypatch.setattr(module, 'read_json', lambda p: {'codex': 'real-codex.exe'})
    monkeypatch.setattr(sys, 'argv', ['adapter.exe', '--version'])
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return 0
    monkeypatch.setattr(module.subprocess, 'call', run)
    with pytest.raises(SystemExit) as stopped:
        module.codex_main()
    assert stopped.value.code == 0
    assert calls[0][0] == ['real-codex.exe', '--version']
    assert calls[0][1]['stdout'] is sys.stdout
    assert calls[0][1]['stderr'] is sys.stderr
    assert calls[0][1]['stdin'] is sys.stdin



def test_native_adapter_preserves_desktop_args_and_service_environment(monkeypatch,tmp_path):
    import sys
    from telegram_guard_local import desktop_stdio as module
    from telegram_guard_local import native_bridge
    monkeypatch.setenv('TELEGRAM_GUARD_DESKTOP_HOME',str(tmp_path/'telegram-local'))
    monkeypatch.setenv('BROWSER_USE_ACCESS_PIPE_PATH','synthetic-native-pipe')
    monkeypatch.setenv('CODEX_APP_TOOLS_PIPE_PATH','synthetic-app-tools')
    monkeypatch.setattr(module,'read_json',lambda path:{'codex':'real-codex.exe','port':48963})
    monkeypatch.setattr(sys,'argv',['adapter.exe','app-server','--analytics-default-enabled'])
    calls=[]
    async def run(*args,**kwargs):calls.append((args,kwargs));return 0
    monkeypatch.setattr(native_bridge,'run',run)
    with pytest.raises(SystemExit) as stopped:module.codex_main()
    assert stopped.value.code==0
    args,kwargs=calls[0]
    assert args[0]=='real-codex.exe'
    assert args[1][-2:]==['app-server','--analytics-default-enabled']
    assert '--listen' not in args[1]
    assert kwargs['environment']['BROWSER_USE_ACCESS_PIPE_PATH']=='synthetic-native-pipe'
    assert kwargs['environment']['CODEX_APP_TOOLS_PIPE_PATH']=='synthetic-app-tools'
    assert kwargs['environment']['CODEX_CLI_PATH']=='real-codex.exe'
