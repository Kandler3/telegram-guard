"""Protocol fault checks; synthetic subprocess only, no Codex/Telegram account."""
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading

from websockets.sync.client import connect


def test_native_stdio_approval_collision_disconnect_and_restart(tmp_path):
    home=tmp_path/'.test-runs'/'fixture';home.mkdir(parents=True)
    (home/'config.toml').write_text('')
    fixture=home/'fake.py'
    fixture.write_text('''import json,sys
approved=False
for line in sys.stdin:
 m=json.loads(line)
 if 'method' not in m:
  if m.get('id')==1:
   approved=True
   print(json.dumps({'method':'fixture/approvalResolved','params':m.get('result')}),flush=True)
  continue
 method=m['method']
 if method=='initialized':continue
 if method=='initialize':
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
  print(json.dumps({'id':1,'method':'item/commandExecution/requestApproval','params':{'synthetic':True}}),flush=True)
 elif method=='thread/loaded/list':
  print(json.dumps({'id':m['id'],'result':{'data':['synthetic-thread'],'approvalResolved':approved}}),flush=True)
 elif method=='turn/start':
  with open('dispatch-count.txt','a') as f:f.write('1\\n')
  if m['params']['toolOutput']['output']=='lose-response':continue
  print(json.dumps({'id':m['id'],'result':{'turn':{'id':'synthetic-turn'}}}),flush=True)
''')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    env=dict(os.environ,CODEX_HOME=str(home))
    bridge=Path(__file__).parent/'prototypes'/'native_stdio_bridge.py'
    child=subprocess.Popen([sys.executable,'-I','-B',str(bridge),sys.executable,str(port),str(fixture)],
        env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    events=queue.Queue()
    def reader():
        for line in child.stdout:events.put(json.loads(line))
    threading.Thread(target=reader,daemon=True).start()
    def send(value):child.stdin.write((json.dumps(value)+'\n').encode());child.stdin.flush()
    def event(method=None):
        value=events.get(timeout=5)
        if method:assert value.get('method')==method
        return value
    def open_side():
        import time
        deadline=time.monotonic()+5
        while True:
            try:return connect(f'ws://127.0.0.1:{port}',proxy=None,open_timeout=1)
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.02)
    try:
        with open_side() as side:
            send({'id':1,'method':'initialize','params':{}})
            assert event()=={'id':1,'result':{}}
            assert event('item/commandExecution/requestApproval')['id']==1
            # Both Desktop and event client use ID 1; routing must keep them apart.
            side.send(json.dumps({'id':1,'method':'thread/loaded/list','params':{}}))
            response=json.loads(side.recv(timeout=5))
            assert response['id']==1 and response['result']['approvalResolved'] is False
            send({'id':1,'result':{'decision':'decline'}})
            assert event('fixture/approvalResolved')['params']=={'decision':'decline'}
            side.send(json.dumps({'id':2,'method':'turn/start','params':{'threadId':'synthetic-thread','input':[],
                'toolOutput':{'name':'telegram_incoming_probe','output':'lose-response'}}}))
            import time
            deadline=time.monotonic()+5
            count=home/'dispatch-count.txt'
            while not count.exists():
                assert time.monotonic()<deadline;time.sleep(.02)
        with open_side() as replacement:
            replacement.send(json.dumps({'id':1,'method':'thread/loaded/list','params':{}}))
            assert json.loads(replacement.recv(timeout=5))['result']['approvalResolved'] is True
            assert count.read_text().splitlines()==['1'] # Lost response never triggers replay.
        send({'id':9,'method':'thread/loaded/list','params':{}})
        assert event()['id']==9 # Side disconnection did not kill Desktop transport.
    finally:
        child.stdin.close()
        try:child.wait(timeout=8)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
    assert child.returncode==0,child.stderr.read().decode()
