import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const moduleURL = name => process.env.KILO_LIFECYCLE_TEST_DIR
  ? pathToFileURL(join(process.env.KILO_LIFECYCLE_TEST_DIR, name))
  : new URL(`../tools/kilo/${name}`, import.meta.url);

async function fixture(t, options = {}) {
  const url = moduleURL('lifecycle-diagnostics-core.mjs');
  assert.ok(existsSync(url), 'Kilo lifecycle instrumentation is missing');
  const { createHooks } = await import(url);
  const directory = mkdtempSync(join(tmpdir(), 'kilo-lifecycle-synthetic-'));
  const path = join(directory, 'events.jsonl');
  const hooks = createHooks(path, { birth: 'winft:123', ...options });
  t.after(() => { hooks.dispose?.(); rmSync(directory, { recursive: true, force: true }); });
  const allRows = () => existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').map(JSON.parse) : [];
  return { path, hooks, allRows, rows: () => allRows().filter(row => !row.event.startsWith('plugin_')) };
}

test('stop intent is durable before the tool executes, without arguments or output', async t => {
  const f = await fixture(t);
  await f.hooks['tool.execute.before'](
    { tool: 'background_process', sessionID: 'ses_synthetic', callID: 'call_synthetic' },
    { args: { action: 'stop', id: 'bgp_synthetic', command: 'SYNTHETIC_PRIVATE' } });
  const row = f.rows()[0];
  assert.equal(row.event, 'stop_requested');
  assert.equal(row.reason, 'explicit_stop');
  assert.equal(row.processID, 'bgp_synthetic');
  assert.ok(!readFileSync(f.path, 'utf8').includes('SYNTHETIC_PRIVATE'));
});

test('launch and observed status retain safe correlation metadata only', async t => {
  const f = await fixture(t);
  await f.hooks['tool.execute.after'](
    { tool: 'background_process', sessionID: 'ses_synthetic', callID: 'call_synthetic', args: { action: 'start', command: 'SYNTHETIC_PRIVATE' } },
    { output: 'id: bgp_synthetic\nstatus: running\npid: 4321\ncommand: SYNTHETIC_PRIVATE', metadata: { processID: 'bgp_synthetic', status: 'running', secret: 'SYNTHETIC_PRIVATE' } });
  const row = f.rows()[0];
  assert.equal(row.processID, 'bgp_synthetic');
  assert.equal(row.child_pid, 4321);
  assert.equal(row.status, 'running');
  assert.ok(!readFileSync(f.path, 'utf8').includes('SYNTHETIC_PRIVATE'));
});

test('disposal is an observation, not an invented kill reason', async t => {
  const f = await fixture(t);
  await f.hooks.event({ event: { type: 'server.instance.disposed', properties: { directory: 'SYNTHETIC_PRIVATE' } } });
  const row = f.rows()[0];
  assert.equal(row.event, 'instance_disposed');
  assert.ok(!('reason' in row));
  assert.ok(!readFileSync(f.path, 'utf8').includes('SYNTHETIC_PRIVATE'));
});

test('unknown events and invalid metadata are discarded', async t => {
  const f = await fixture(t);
  await f.hooks.event({ event: { type: 'SYNTHETIC_PRIVATE', properties: { text: 'SYNTHETIC_PRIVATE' } } });
  assert.deepEqual(f.rows(), []);
  await f.hooks['tool.execute.after'](
    { tool: 'background_process', sessionID: 'ses_synthetic', callID: 'call_synthetic', args: { action: 'monitor' } },
    { output: 'SYNTHETIC_PRIVATE', metadata: { status: 'SYNTHETIC_PRIVATE', reason: 'SYNTHETIC_PRIVATE' } });
  assert.ok(!readFileSync(f.path, 'utf8').includes('SYNTHETIC_PRIVATE'));
});

test('autoload entry exports only one plugin object', async () => {
  const module = await import(moduleURL('lifecycle-diagnostics.js'));
  assert.deepEqual(Object.keys(module), ['default']);
  assert.equal(module.default.id, 'lifecycle-diagnostics');
  assert.equal(typeof module.default.server, 'function');
});

test('child output cannot spoof a runtime PID header', async t => {
  const f = await fixture(t);
  await f.hooks['tool.execute.after'](
    { tool: 'background_process', sessionID: 'ses_synthetic', callID: 'call_synthetic', args: { action: 'start' } },
    { output: 'child stdout\npid: 4321\nSYNTHETIC_PRIVATE', metadata: { processID: 'bgp_synthetic', status: 'running' } });
  assert.ok(!('child_pid' in f.rows()[0]));
});

test('activation is advertised only after plugin initialization, without claiming pre-kill coverage', async t => {
  const f = await fixture(t);
  const start = f.allRows()[0];
  assert.equal(start.event, 'plugin_started');
  assert.equal(start.diagnostics_version, 2);
  assert.equal(start.birth, 'winft:123');
  assert.equal(start.pre_kill, false);
  await f.hooks.dispose();
  assert.equal(f.allRows().at(-1).event, 'plugin_disposed');
});

test('heartbeat proves the loaded plugin is still active and disposal stops it', async t => {
  const f = await fixture(t, { heartbeatMs: 10 });
  const deadline = Date.now() + 1000;
  while (!f.allRows().some(row => row.event === 'plugin_heartbeat') && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(f.allRows().some(row => row.event === 'plugin_heartbeat'));
  await f.hooks.dispose();
  const count = f.allRows().length;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(f.allRows().length, count);
});

test('runtime process update events retain safe IDs and never process output', async t => {
  const f = await fixture(t);
  await f.hooks.event({ event: { type: 'background_process.updated', properties: { info: {
    id: 'bgp_synthetic', sessionID: 'ses_synthetic', pid: 4321, status: 'running',
    command: 'SYNTHETIC_PRIVATE', output: 'SYNTHETIC_PRIVATE', time: { started: 123 },
  } } } });
  assert.equal(f.rows()[0].processID, 'bgp_synthetic');
  assert.equal(f.rows()[0].child_pid, 4321);
  assert.ok(!readFileSync(f.path, 'utf8').includes('SYNTHETIC_PRIVATE'));
});
