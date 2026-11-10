// Opt-in: exercise the exact patched functions, not a full Kilo/Effect runtime.
import { test, expect } from 'bun:test';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';

const root = process.env.KILO_OWNER_STAGING;
const run = root && process.platform === 'win32' ? test : test.skip;
const managerPath = 'packages/opencode/src/kilocode/background-process/index.ts';

function extract(source, name) {
  const match = new RegExp(`(?:async )?function ${name}\\([^]*?\\) \\{`).exec(source);
  if (!match) throw new Error(`Missing pinned function: ${name}`);
  const begin = match.index;
  let level = 1, index = begin + match[0].length;
  // Only pinned, transpiled functions whose quoted braces are balanced are used.
  for (; index < source.length && level; index++) {
    if (source[index] === '{') level++;
    if (source[index] === '}') level--;
  }
  if (level) throw new Error('Unbalanced pinned function');
  return source.slice(begin, index);
}

async function harness() {
  const report = JSON.parse(readFileSync(join(root!, 'PREPARED.json'), 'utf8'));
  expect(report.runtime_version).toBe('7.8.18');
  expect(report.installed).toBe(false);
  const source = new Bun.Transpiler({ loader: 'ts' }).transformSync(readFileSync(join(root!, 'source', managerPath), 'utf8'));
  const mod = await import(pathToFileURL(join(root!, 'source/packages/opencode/src/kilocode/background-process/owner-journal.mjs')).href);
  const directory = mkdtempSync(join(tmpdir(), 'synthetic-pinned-owner-'));
  const diagnostics = mod.createOwnerJournal(directory, { version: '7.8.18', coverage: 'manager_v1', patchID: report.patch_id });
  await diagnostics.ready;
  const child = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  const info = { id: 'bgp_synthetic', sessionID: 'ses_synthetic', pid: child.pid,
    lifetime: 'session', status: 'running', ready: false, ports: [], time: { started: Date.now(), updated: Date.now() } };
  const active = { info, proc: child, start: {} };
  const state = { processes: new Map([[info.id, active]]), shared: { processes: new Map() } };
  await diagnostics.register(info);
  child.once('exit', (code, signal) => diagnostics.exited(info, code, signal));
  const rows = () => readFileSync(diagnostics.path, 'utf8').trim().split('\n').map(JSON.parse);
  const bindings: Record<string, any> = {
    diagnostics, withCause: mod.withCause, withDisposeCause: mod.withDisposeCause,
    process: { platform: 'win32' }, KILL_MS: 3000, STOP_MS: 23000,
    terminal: status => ['exited', 'failed', 'stopped'].includes(status),
    stopped: proc => proc.exitCode !== null || proc.signalCode !== null,
    publish: () => {}, output: async () => {}, forget: async () => {},
    Instance: { restore: (_ctx, action) => action() }, Bus: { publish: async () => {} },
    BackgroundProcess: { Event: { Deleted: 'synthetic-deleted' } }, eventscope: () => 'synthetic-scope',
    log: { warn: () => {} },
    exited: (a, code, signal) => { a.info.status = 'stopped'; a.info.exitCode = code; a.info.signal = signal; },
    owner: (s, lifetime) => lifetime === 'persistent' ? s.shared.processes : s.processes,
    state: async () => state, find: (s, id) => s.processes.get(id), adopt: async () => {}, clone: value => ({ ...value }),
    probe: async () => child.exitCode === null && child.signalCode === null ? 'owned' : 'gone',
    Filesystem: { write: async () => {
      expect(rows().at(-1).event).toBe('kill_issued');
      child.kill();
    } },
    waitGone: async () => { await exited; },
    Process: { run: async (argv, _opts) => {
      expect(argv[0]).toBe('taskkill');
      expect(argv[2]).toBe(String(child.pid));
      expect(rows().at(-1).event).toBe('kill_issued');
      const proc = spawn(argv[0], argv.slice(1), { stdio: 'ignore', windowsHide: true });
      return await new Promise(resolve => proc.once('exit', code => resolve({ code })));
    } },
    spawn: (command, args, options) => {
      expect(command).toBe('taskkill');
      expect(args[1]).toBe(String(child.pid));
      expect(rows().at(-1).event).toBe('kill_issued');
      return spawn(command, args, options);
    },
  };
  function bind(name) {
    const fn = new Function(...Object.keys(bindings), `return (${extract(source, name)});`)(...Object.values(bindings));
    bindings[name] = fn;
    return fn;
  }
  bind('kill'); bind('terminateImpl'); bind('terminate'); bind('rollbackImpl'); bind('rollback');
  return { info, active, state, source, diagnostics, mod, rows, bind, bindings, exited,
    async cleanup() {
      if (child.exitCode === null && child.signalCode === null) child.kill();
      await exited;
      diagnostics.dispose();
      rmSync(directory, { recursive: true, force: true });
    } };
}

run('all staged TypeScript files parse for the pinned backend', () => {
  const report = JSON.parse(readFileSync(join(root!, 'PREPARED.json'), 'utf8'));
  for (const name of Object.keys(report.files).filter(name => name.endsWith('.ts'))) {
    new Bun.Transpiler({ loader: 'ts' }).transformSync(readFileSync(join(root!, 'source', name), 'utf8'));
  }
});

run('actual pinned explicit-stop functions flush intent before the Windows kill', async () => {
  const h = await harness();
  try {
    await h.bind('stop')(h.info.id);
    await h.exited;
    expect(h.rows().find(row => row.event === 'stop_requested').reason).toBe('explicit_stop');
    expect(h.rows().some(row => row.event === 'owner_child_exit')).toBe(true);
  } finally { await h.cleanup(); }
}, 15000);

for (const reason of ['session_stop', 'api_session_stop', 'session_removed', 'task_finished', 'sandbox_enabled']) {
  run(`actual pinned stopSession preserves ${reason}`, async () => {
    const h = await harness();
    try {
      await h.bind('stopSession')(h.info.sessionID, reason);
      await h.exited;
      expect(h.rows().find(row => row.event === 'stop_requested').reason).toBe(reason);
    } finally { await h.cleanup(); }
  }, 15000);
}

run('pinned silent finalizer and rollback have distinct durable causes', async () => {
  const h = await harness();
  try {
    expect(h.source).toContain('withDisposeCause(');
    await h.mod.withDisposeCause(() => h.bindings.terminate(h.state, h.active, { remove: true, silent: true }));
    await h.exited;
    expect(h.rows().find(row => row.event === 'stop_requested').reason).toBe('instance_dispose');
  } finally { await h.cleanup(); }
  const rollback = await harness();
  try {
    await rollback.bindings.rollback(rollback.active);
    await rollback.exited;
    expect(rollback.rows().find(row => row.event === 'stop_requested').reason).toBe('launch_rollback');
  } finally { await rollback.cleanup(); }
}, 20000);

run('persistent control-file success does not fabricate a process exit code', async () => {
  const h = await harness();
  try {
    h.info.lifetime = 'persistent';
    Object.assign(h.active, { control: 'synthetic-control' });
    await h.mod.withCause('explicit_stop', () => h.bindings.kill(h.active));
    await h.exited;
    const result = h.rows().find(row => row.event === 'kill_result' && row.method === 'control_file');
    expect(result.outcome).toBe('returned');
    expect(result.api_exit_code).toBeNull();
  } finally { await h.cleanup(); }
}, 15000);
