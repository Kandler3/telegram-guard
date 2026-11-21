// Offline native-stdio prototype probe. Never uses real CODEX_HOME or Telegram.
// Usage: node tests/probe_shared_app_server.mjs ABSOLUTE_CODEX_EXE
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createServer as createTcpServer } from 'node:net';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, writeFile, appendFile, readFile } from 'node:fs/promises';
import { resolve, join, isAbsolute } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { gunzipSync, zstdDecompressSync } from 'node:zlib';

const binary = process.argv[2];
assert(binary && isAbsolute(binary), 'Provide an absolute Codex binary path');
const root = resolve('.test-runs');
await mkdir(root, { recursive: true });
const home = await mkdtemp(join(root, 'authenticated-stdio-'));
const requests = [];
const sockets = [];
let child;
let relayChild;
let stderr = '';
let releaseHeld;
let holdNext = false;
let askClientTool = false;
let clientToolRequested = false;
let modelOffline = false;
const listen = server => new Promise((ok, bad) => {
  server.once('error', bad);
  server.listen(0, '127.0.0.1', () => ok(server.address().port));
});
const until = async (test, label, ms = 15000) => {
  const end = Date.now() + ms;
  while (!test()) {
    if (Date.now() > end) throw new Error('Timed out: ' + label);
    if (child?.exitCode != null) throw new Error('App Server exited: ' + stderr.slice(-3000));
    await sleep(30);
  }
};
const mock = createServer(async (req, res) => {
  try {
    if (req.method !== 'POST' || !req.url.startsWith('/v1/responses')) {
      res.writeHead(404); res.end(); return;
    }
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    let bytes = Buffer.concat(chunks);
    if (req.headers['content-encoding'] === 'gzip') bytes = gunzipSync(bytes);
    if (req.headers['content-encoding'] === 'zstd') bytes = zstdDecompressSync(bytes);
    const body = JSON.parse(bytes.toString());
    requests.push(body);
    if (modelOffline) { res.destroy(); return; }
    if (holdNext) {
      holdNext = false;
      await new Promise(ok => { releaseHeld = ok; });
    }
    const id = 'resp_probe_' + requests.length;
    const item = { type: 'message', id: 'msg_' + id, role: 'assistant',
      status: 'completed', phase: 'final_answer',
      content: [{ type: 'output_text', text: 'PROBE_ACK', annotations: [] }] };
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    let sequence = 0;
    const event = (type, data) => res.write('event: ' + type + '\ndata: ' +
      JSON.stringify({ type, sequence_number: sequence++, ...data }) + '\n\n');
    if (askClientTool && !clientToolRequested) {
      clientToolRequested = true;
      const call = { type: 'function_call', id: 'fc_probe', call_id: 'call_probe', name: 'probe_ui_tool', arguments: '{}' };
      event('response.output_item.added', { output_index: 0, item: call });
      event('response.output_item.done', { output_index: 0, item: call });
      event('response.completed', { response: { id, status: 'completed', output: [call] } });
      res.end(); return;
    }
    event('response.created', { response: { id, status: 'in_progress', output: [] } });
    event('response.output_item.added', { output_index: 0, item: { ...item, status: 'in_progress', content: [] } });
    event('response.content_part.added', { item_id: item.id, output_index: 0, content_index: 0,
      part: { type: 'output_text', text: '', annotations: [] } });
    event('response.output_text.delta', { item_id: item.id, output_index: 0, content_index: 0, delta: 'PROBE_ACK' });
    event('response.output_text.done', { item_id: item.id, output_index: 0, content_index: 0, text: 'PROBE_ACK' });
    event('response.content_part.done', { item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] });
    event('response.output_item.done', { output_index: 0, item });
    event('response.completed', { response: { id, status: 'completed', output: [item],
      usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2,
        input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } } });
    res.end();
  } catch (error) { res.writeHead(500); res.end(String(error)); }
});

class Rpc {
  constructor(ws) {
    this.ws = ws; this.next = 1; this.pending = new Map(); this.events = []; this.serverRequests = [];
    ws.addEventListener('message', e => {
      const msg = JSON.parse(e.data);
      if (msg.id != null && !msg.method) {
        const wait = this.pending.get(msg.id);
        if (wait) { clearTimeout(wait.timer); this.pending.delete(msg.id);
          msg.error ? wait.bad(new Error(JSON.stringify(msg.error))) : wait.ok(msg.result); }
      } else if (msg.id != null) {
        this.serverRequests.push(msg);
        if (this.name === 'telegram_probe_desktop' && msg.method === 'item/tool/call') {
          ws.send(JSON.stringify({ id: msg.id, result: { contentItems: [{ type: 'inputText', text: 'owner handled tool' }], success: true } }));
        }
      } else this.events.push(msg);
    });
  }
  call(method, params) {
    const id = this.next++;
    return new Promise((ok, bad) => {
      const timer = setTimeout(() => { this.pending.delete(id); bad(new Error('RPC timeout: ' + method)); }, 15000);
      this.pending.set(id, { ok, bad, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  notify(method, params) { this.ws.send(JSON.stringify({ method, params })); }
  async init(name) {
    this.name = name;
    await this.call('initialize', { clientInfo: { name, version: '0.0.1' }, capabilities: { experimentalApi: true } });
    this.notify('initialized', {});
  }
  close() {
    for (const wait of this.pending.values()) { clearTimeout(wait.timer); wait.bad(new Error('Probe closed')); }
    this.pending.clear(); this.ws.close();
  }
}
// Same JSONL interface used by Desktop's native stdio transport.
class StdioSocket extends EventTarget {
  constructor(proc) {
    super(); this.proc = proc;
    let buffer = '';
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', chunk => {
      buffer += chunk;
      let boundary;
      while ((boundary = buffer.indexOf('\n')) >= 0) {
        const data = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 1);
        if (data.trim()) this.dispatchEvent(new MessageEvent('message', { data }));
      }
    });
    proc.on('error', () => this.dispatchEvent(new Event('error')));
  }
  send(data) { this.proc.stdin.write(data + '\n'); }
  close() { this.proc.stdin.end(); }
}
async function connect(url) {
  let last;
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      const ws = new WebSocket(url);
      await new Promise((ok, bad) => {
        ws.addEventListener('open', ok, { once: true });
        ws.addEventListener('error', () => bad(new Error('WebSocket connection failed')), { once: true });
      });
      const rpc = new Rpc(ws); sockets.push(rpc); return rpc;
    } catch (error) { last = error; await sleep(100); }
  }
  throw last;
}
const contains = (request, marker) => (request.input ?? []).some(item =>
  item.type === 'function_call_output' && JSON.stringify(item.output).includes(marker));
const userContains = (request, marker) => (request.input ?? []).some(item =>
  item.role === 'user' && JSON.stringify(item).includes(marker));
const functionOutput = (client, marker) => client.events.some(e => e.params?.item?.type === 'functionCallOutput' &&
  JSON.stringify(e.params.item.output).includes(marker));
try {
  const modelPort = await listen(mock);
  const reservation = createTcpServer();
  const rpcPort = await listen(reservation);
  await new Promise(ok => reservation.close(ok));
  await writeFile(join(home, 'config.toml'), `model = "probe-model"\nmodel_provider = "probe"\napproval_policy = "never"\nsandbox_mode = "read-only"\n[analytics]\nenabled = false\n[model_providers.probe]\nname = "Offline probe"\nbase_url = "http://127.0.0.1:${modelPort}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n`);
  if (false) {
    await appendFile(join(home, 'config.toml'), '\n[mcp_servers.context_probe]\ncommand = ' + JSON.stringify(process.argv[3]) + '\nargs = ["-B", ' + JSON.stringify(resolve('tests/mock_context_mcp.py')) + ']\n');
  }
  if (false) {
    await appendFile(join(home, 'config.toml'), '\n[mcp_servers.telegram_guard]\ncommand = ' + JSON.stringify(process.argv[3]) + '\nargs = ["-B", ' + JSON.stringify(resolve('tests/mock_subscription_mcp.py')) + ', "--home", ' + JSON.stringify(join(home, 'synthetic-guard')) + ']\n[mcp_servers.telegram_guard.env]\nTELEGRAM_GUARD_APP_SERVER_URL = "ws://127.0.0.1:' + rpcPort + '"\n');
  }
  // All model requests are served by loopback mock; isolate all Desktop services.
  const env = { ...process.env, CODEX_HOME: home, CODEX_SQLITE_HOME: join(home, 'sqlite'), RUST_LOG: 'warn' };
  for (const key of Object.keys(env)) if (/TOKEN|API_KEY|SECRET|^BROWSER_|^SKY_|CODEX_APP_|OPENAI_BASE_URL|CODEX_INTERNAL_ORIGINATOR|CODEX_THREAD_ID|CODEX_SESSION_ID/.test(key)) delete env[key];
  const python = process.argv[3];
  assert(python && isAbsolute(python), 'Provide absolute test Python path');
  const adapterArg=process.argv.indexOf('--installed-adapter');
  if(adapterArg>=0) {
    await mkdir(join(home,'telegram-local'),{recursive:true});
    await writeFile(join(home,'telegram-local','shared-desktop.json'),JSON.stringify({codex:binary,port:rpcPort}));
    await appendFile(join(home,'config.toml'),'\n[features]\nplugins = false\n[mcp_servers.telegram_guard]\ncommand = '+JSON.stringify(python)+'\nargs = ["-B",'+JSON.stringify(resolve('tests/mock_context_mcp.py'))+']\nenabled = false\n');
    child=spawn(process.argv[adapterArg+1],['app-server','--analytics-default-enabled'],{cwd:home,
      env:{...env,TELEGRAM_GUARD_DESKTOP_HOME:join(home,'telegram-local')},windowsHide:true,stdio:['pipe','pipe','pipe']});
  } else child = spawn(python, ['-I', '-B', resolve('telegram_guard_local/native_bridge.py'), binary, String(rpcPort)],
    {cwd: home, env, windowsHide: true, stdio: ['pipe','pipe','pipe']});
  relayChild=child;
  child.stderr.on('data', b => { stderr=(stderr+b).slice(-12000); });
  const desktop = new Rpc(new StdioSocket(child)); sockets.push(desktop);
  const bridge = await connect('ws://127.0.0.1:'+rpcPort);
  await desktop.init('telegram_probe_desktop');
  const {key}=JSON.parse(await readFile(join(home,'event-bridge','auth.json'),'utf8'));
  const {createHmac,randomBytes}=await import('node:crypto');
  const nonce=randomBytes(32).toString('hex');
  const challenge=await bridge.call('bridge/challenge',{nonce});
  const sign=domain=>createHmac('sha256',key).update(`${domain}:${nonce}:${challenge.nonce}`).digest('hex');
  assert.equal(challenge.signature,sign('server'));
  await bridge.call('bridge/authenticate',{role:'telegram_guard',signature:sign('client-telegram_guard')});
  await bridge.init('telegram_probe_bridge');
  const started = await desktop.call('thread/start', { ephemeral: true, cwd: home,
    model: 'probe-model', modelProvider: 'probe', approvalPolicy: 'never', sandbox: 'read-only',
    dynamicTools: [{ type: 'function', name: 'probe_ui_tool', description: 'Synthetic owner-only tool', inputSchema: { type: 'object', properties: {} } }] });
  const threadId = started.thread.id;
  if (false) {
    const context = await desktop.call('mcpServer/tool/call', { threadId, server: 'context_probe', tool: 'probe_context', arguments: {} });
    const binding = JSON.parse(context.content[0].text);
    assert.equal(binding.meta.threadId, threadId);
    assert.equal(binding.thread_env, null);
    console.log('PASS: trusted MCP metadata supplies caller threadId, independent of process environment.');
  }
  const loaded = await bridge.call('thread/loaded/list', {});
  assert(loaded.data.includes(threadId), 'Second connection must see the same loaded thread');
  const submit = marker => bridge.call('turn/start', { threadId, input: [],
    toolOutput: { name: 'telegram_incoming', namespace: 'telegram_guard',
      output: JSON.stringify({ synthetic: true, untrusted_content: true, may_authorize_sending: false, marker }) } });
  const first = await submit('probe_idle_001');
  await until(() => desktop.events.some(e => e.method === 'turn/completed' && e.params.turn.id === first.turn.id), 'idle event turn');
  assert(functionOutput(desktop, 'probe_idle_001'), 'Desktop client must receive functionCallOutput');
  assert(requests.some(r => contains(r, 'probe_idle_001')), 'Mock model must receive the event');
  console.log('PASS: one native stdio server; event side wakes idle thread; Desktop client receives functionCallOutput.');

  holdNext = true;
  const active = await submit('probe_active_002');
  await until(() => releaseHeld, 'held mock response');
  const queued = await submit('probe_queued_003');
  assert.equal(queued.turn.id, active.turn.id, 'Busy delivery must join the existing turn');
  releaseHeld(); releaseHeld = undefined;
  await until(() => requests.some(r => contains(r, 'probe_queued_003')), 'queued event reaches mock model');
  await until(() => desktop.events.some(e => e.method === 'turn/completed' && e.params.turn.id === active.turn.id), 'active turn completion');
  assert(functionOutput(desktop, 'probe_queued_003'), 'Owner must receive queued functionCallOutput');
  assert(!requests.some(r => ['probe_idle_001', 'probe_active_002', 'probe_queued_003'].some(m => userContains(r, m))), 'External event must never become user input');
  console.log('PASS: busy delivery uses the same turn and reaches the model as tool output; no event becomes user input.');
  if (false) {
    const subscriptionId = crypto.randomUUID();
    const response = await desktop.call('mcpServer/tool/call', { threadId, server: 'telegram_guard', tool: 'telegram_subscribe', arguments: {
      chat_id: 123, subscription_id: subscriptionId, authorization: { user_instruction: 'Monitor the synthetic test chat for one minute',
      scope: 'Synthetic integration test', scope_chat_ids: [123], source: 'human test fixture', expires_at: new Date(Date.now() + 60000).toISOString() }
    } });
    assert(!response.isError, JSON.stringify(response));
    const subscribed = JSON.parse(response.content[0].text);
    assert.equal(subscribed.thread_id, threadId);
    await until(() => contains(requests.at(-1), 'guard_end_to_end_probe'), 'MCP subscription delivers incoming event');
    await until(() => functionOutput(desktop, 'guard_end_to_end_probe'), 'Desktop receives subscription event');
    const stopped = await desktop.call('mcpServer/tool/call', { threadId, server: 'telegram_guard', tool: 'telegram_unsubscribe', arguments: { subscription_id: subscriptionId } });
    assert(!stopped.isError, JSON.stringify(stopped));
    assert.equal(JSON.parse(stopped.content[0].text).state, 'stopped');
    console.log('PASS: telegram_subscribe -> durable outbox -> App Server -> model/owner; telegram_unsubscribe stops it.');
  }
  askClientTool = true;
  const ownerTurn = await submit('probe_owner_tool_004');
  await until(() => desktop.serverRequests.some(r => r.method === 'item/tool/call'), 'owner receives tool callback for bridge-triggered turn');
  await until(() => desktop.events.some(e => e.method === 'turn/completed' && e.params.turn.id === ownerTurn.turn.id), 'owner-handled tool turn completes');
  console.log('PASS: bridge-triggered turn preserves owner client tool handling.');
  await assert.rejects(bridge.call('turn/start', {threadId,input:[{type:'text',text:'forbidden user message'}]}));
  await assert.rejects(bridge.call('config/value/write', {keyPath:'approval_policy',value:'never'}));
  console.log('PASS: event side rejects user messages and configuration changes.');

  if (true) {
    askClientTool = false;
    modelOffline = true;
    const failed = await submit('probe_offline_005');
    await until(() => desktop.events.some(e => e.method === 'turn/completed' && e.params.turn.id === failed.turn.id),
      'network-failed model turn completes', 45000);
    assert.equal(desktop.events.find(e => e.method === 'turn/completed' && e.params.turn.id === failed.turn.id).params.turn.status, 'failed');
    assert.equal(relayChild.exitCode, null, 'Model network failure must not exit the control adapter');
    modelOffline = false;
    const recovered = await submit('probe_online_006');
    await until(() => desktop.events.some(e => e.method === 'turn/completed' && e.params.turn.id === recovered.turn.id), 'model recovery');
    assert.equal(desktop.events.find(e => e.method === 'turn/completed' && e.params.turn.id === recovered.turn.id).params.turn.status, 'completed');
    console.log('PASS: model connection failure and recovery preserve the same runtime, thread and stdio control process.');
    if (process.argv.includes('--long-idle')) {
      console.log('Checking: idle control transport beyond Desktop 60-second heartbeat deadline.');
      await sleep(65000);
      assert((await desktop.call('thread/loaded/list', {})).data.includes(threadId));
      assert.equal(relayChild.exitCode, null);
      console.log('PASS: control transport remains usable after 65 seconds of idle time.');
    }
  }
  // Exercise the actual installed-producer client code, not just probe RPC helpers.
  await mkdir(join(home,'telegram-local'),{recursive:true});
  await writeFile(join(home,'telegram-local','shared-desktop.json'),JSON.stringify({port:rpcPort}));
  const guardCode = `import asyncio
from telegram_guard_local.app_server import AppServer
async def main():
 c=AppServer('ws://127.0.0.1:${rpcPort}')
 try:
  await c.deliver('${threadId}',{'marker':'actual_guard_client'},lambda:None)
 finally:await c.close()
asyncio.run(main())`;
  const guardProc=spawn(python,['-B','-c',guardCode],{cwd:resolve('.'),env:{...env,
    PYTHONPATH:resolve('.'),TELEGRAM_GUARD_EVENT_BRIDGE_AUTH_FILE:join(home,'event-bridge','auth.json')},
    windowsHide:true,stdio:['ignore','pipe','pipe']});
  let guardErr='';guardProc.stderr.on('data',b=>guardErr+=b);
  const guardExit=await new Promise(ok=>guardProc.once('exit',ok));
  assert.equal(guardExit,0,guardErr);
  await until(()=>requests.some(r=>contains(r,'actual_guard_client')),'actual Python Guard client');
  process.env.CODEX_HOME=home;
  const {pathToFileURL}=await import('node:url');
  const {AppServer:WakeClient}=await import(pathToFileURL(resolve('deploy/agent-wake-rpc.mjs')).href);
  const wakeClient=new WakeClient('ws://127.0.0.1:'+rpcPort);
  try {await wakeClient.deliver(threadId,{marker:'actual_wake_client'},async()=>{});}
  finally {wakeClient.close();}
  await until(()=>requests.some(r=>contains(r,'actual_wake_client')),'actual Node agent-wake client');
  console.log('PASS: actual Python Telegram Guard and Node agent-wake clients authenticate and deliver.');
  await writeFile(join(home, 'result.json'), JSON.stringify({ passed: true, modelRequests: requests.length,
    transport: 'authenticated-single-native-stdio',
    idleAndBusyEventsTested: true, ownerToolCallbackTested: true, actualProducerClientsTested: true,
    userInputAndConfigRejected: true, modelFailureAndRecoveryTested: true,
    realApiUsed: false, telegramUsed: false, desktopRestarted: false,
    actualDesktopIntegrationTested: false, installedAdapterTested: adapterArg>=0 }, null, 2));

} catch (error) {
  console.error(error.message);
  console.error('Isolated diagnostics:', stderr.slice(-4000));
  process.exitCode = 1;
} finally {
  releaseHeld?.();
  for (const rpc of sockets) rpc.close();
  if (relayChild && relayChild.exitCode == null) {
    await sleep(300);
    if (relayChild.exitCode == null) relayChild.kill();
  }
  if (child && child.exitCode == null) {
    if (process.platform === 'win32') {
      const kill = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      await new Promise(ok => kill.once('exit', ok));
    } else child.kill();
    child.stderr.destroy(); child.unref();
    await Promise.race([new Promise(ok => child.once('exit', ok)), sleep(3000)]);
  }
  mock.closeAllConnections();
  await new Promise(ok => mock.close(ok));
  console.log('Probe data directory:', home);
}



