import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

const moduleURL = new URL('../tools/kilo/owner-patchkit/owner-journal.mjs', import.meta.url);
async function fixture(t, options = {}) {
  assert.ok(existsSync(moduleURL), 'owner-side termination journaling is missing');
  const mod = await import(moduleURL);
  const directory = mkdtempSync(join(tmpdir(), 'synthetic-owner-'));
  const owner = mod.createOwnerJournal(directory, { version: '7.8.18', coverage: 'manager_v1',
    birth: async pid => `winft:${pid}`, heartbeatMs: 100000, ...options });
  await owner.ready;
  t.after(() => { owner.dispose(); rmSync(directory, { recursive: true, force: true }); });
  return { ...mod, owner, directory, rows: () => readFileSync(owner.path, 'utf8').trim().split('\n').map(JSON.parse) };
}

const info = (id = 'bgp_synthetic') => ({ id, pid: 4321, sessionID: 'ses_synthetic', lifetime: 'session',
  command: 'SYNTHETIC_PRIVATE', output: 'SYNTHETIC_PRIVATE', token: 'SYNTHETIC_PRIVATE' });

test('stop intent is durable before action and null is never fabricated as zero', async t => {
  const f = await fixture(t);
  const target = info();
  await f.owner.register(target);
  await f.withCause('instance_dispose', () => f.owner.stop(target, async () => {
    assert.equal(f.rows().at(-1).event, 'stop_requested');
    assert.equal(f.rows().at(-1).reason, 'instance_dispose');
  }));
  f.owner.disappeared(target);
  assert.equal(f.rows().at(-1).exit_code, null);
  f.owner.exited(target, 0, null);
  assert.equal(f.rows().at(-1).exit_code, 0);
  assert.ok(!JSON.stringify(f.rows()).includes('SYNTHETIC_PRIVATE'));
});

test('concurrent termination causes cannot overwrite each other', async t => {
  const f = await fixture(t);
  const a = info('bgp_a'), b = info('bgp_b');
  await Promise.all([f.owner.register(a), f.owner.register(b)]);
  await Promise.all([
    f.withCause('explicit_stop', () => f.owner.stop(a, async () => { await new Promise(r => setTimeout(r, 10)); })),
    f.withCause('runtime_shutdown', () => f.owner.stop(b, async () => {})),
  ]);
  for (const [id, reason] of [['bgp_a', 'explicit_stop'], ['bgp_b', 'runtime_shutdown']]) {
    const events = f.rows().filter(row => row.processID === id && row.event.startsWith('stop_'));
    assert.equal(events.length, 2);
    assert.ok(events.every(row => row.reason === reason));
    assert.equal(events[0].operation_id, events[1].operation_id);
  }
});

test('action exception is preserved, not serialized into diagnostic text', async t => {
  const f = await fixture(t);
  const target = info();
  await f.owner.register(target);
  const original = new Error('SYNTHETIC_PRIVATE');
  await assert.rejects(f.withCause('explicit_restart', () => f.owner.stop(target, () => { throw original; })), err => err === original);
  assert.equal(f.rows().at(-1).outcome, 'failed');
  assert.ok(!JSON.stringify(f.rows()).includes('SYNTHETIC_PRIVATE'));
});

test('real synthetic child is terminated only after owner intent is flushed', async t => {
  const f = await fixture(t);
  const child = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill(); });
  const target = { ...info(), pid: child.pid };
  await f.owner.register(target);
  await f.withCause('session_stop', () => f.owner.stop(target, async () => {
    assert.equal(f.rows().at(-1).event, 'stop_requested');
    child.kill();
    const result = await exited;
    f.owner.exited(target, result.code, result.signal);
  }));
  assert.ok(f.rows().some(row => row.event === 'owner_child_exit'));
});

test('delayed OS result keeps its original cause instead of the caller context', async t => {
  const f = await fixture(t);
  const target = info();
  await f.owner.register(target);
  const done = f.withCause('explicit_stop', () => f.owner.issue(target, 'taskkill'));
  f.withCause('runtime_shutdown', () => done(0));
  const events = f.rows().filter(row => row.event.startsWith('kill_'));
  assert.equal(events[0].reason, 'explicit_stop');
  assert.equal(events[1].reason, 'explicit_stop');
  assert.equal(events[0].operation_id, events[1].operation_id);
});

test('diagnostic storage failure never prevents the original stop action', async t => {
  const f = await fixture(t);
  const target = info();
  await f.owner.register(target);
  rmSync(f.directory, { recursive: true, force: true });
  writeFileSync(f.directory, 'synthetic blocked journal directory');
  let called = false;
  const value = await f.withCause('explicit_stop', () => f.owner.stop(target, () => { called = true; return 88; }));
  assert.equal(value, 88);
  assert.equal(called, true);
});

test('a stop never waits for an unresolved identity initialization', async t => {
  assert.ok(existsSync(moduleURL));
  const mod = await import(moduleURL);
  const directory = mkdtempSync(join(tmpdir(), 'synthetic-owner-pending-'));
  const owner = mod.createOwnerJournal(directory, { birth: () => new Promise(() => {}), heartbeatMs: 100000 });
  t.after(() => { owner.dispose(); rmSync(directory, { recursive: true, force: true }); });
  let called = false;
  const stopped = mod.withCause('explicit_stop', () => owner.stop(info(), () => { called = true; return 7; }));
  assert.equal(await Promise.race([stopped, new Promise(resolve => setTimeout(() => resolve('blocked'), 50))]), 7);
  assert.equal(called, true);
});

test('disposal replaces a stale creator cause but preserves an actual shutdown', async t => {
  const f = await fixture(t);
  const target = info();
  await f.owner.register(target);
  await f.withCause('explicit_stop', () => f.withDisposeCause(() => f.owner.stop(target, () => {})));
  assert.equal(f.rows().at(-1).reason, 'instance_dispose');
  await f.withCause('runtime_shutdown', () => f.withDisposeCause(() => f.owner.stop(target, () => {})));
  assert.equal(f.rows().at(-1).reason, 'runtime_shutdown');
});

test('a helper-only module cannot advertise owner coverage without a patch fingerprint', async t => {
  const f = await fixture(t);
  assert.equal(f.rows()[0].pre_kill, false);
});

test('future or adopted PID births are not asserted as original target identity', async t => {
  const future = `winft:${(BigInt(Date.now() + 1000) * 10000n) + 116444736000000000n}`;
  const queried = [];
  const f = await fixture(t, { birth: async pid => { queried.push(pid); return future; } });
  const target = { ...info(), time: { started: Date.now() } };
  await f.owner.register(target);
  assert.equal(f.rows().at(-1).target_birth, null);
  queried.length = 0;
  await f.owner.register(target, true);
  assert.equal(f.rows().at(-1).target_birth, null);
  assert.deepEqual(queried, []);
});
