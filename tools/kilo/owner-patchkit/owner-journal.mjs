// Source-patch helper. Merely installing the SDK plugin does not activate this module.
import { AsyncLocalStorage } from 'node:async_hooks';
import { randomUUID } from 'node:crypto';
import { mkdirSync, openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import { join } from 'node:path';
import { processBirth } from '../lifecycle-process-identity.mjs';

const causes = new AsyncLocalStorage();
const reasons = new Set(['explicit_stop', 'explicit_restart', 'session_stop', 'session_removed',
  'task_finished', 'sandbox_enabled', 'instance_dispose', 'runtime_shutdown', 'launch_rollback', 'api_session_stop', 'unknown']);
const methods = new Set(['taskkill', 'control_file', 'sigterm_group', 'sigkill_group', 'native_job', 'process_terminate']);
const signals = new Set(['SIGTERM', 'SIGKILL', 'SIGINT', 'SIGBREAK', 'SIGHUP']);
const id = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const pid = value => Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff;
const birthToken = value => typeof value === 'string' && /^(winft:[0-9]{1,20}|linux:[0-9a-f-]{36}:[0-9]{1,20})$/.test(value);
const exitCode = value => Number.isSafeInteger(value) && value >= -2147483648 && value <= 0xffffffff ? value : null;

export function withCause(reason, action) {
  return causes.run({ ...causes.getStore(), reason: reasons.has(reason) ? reason : 'unknown' }, action);
}

export function withDisposeCause(action) {
  const reason = causes.getStore()?.reason === 'runtime_shutdown' ? 'runtime_shutdown' : 'instance_dispose';
  return causes.run({ reason }, action);
}

export function createOwnerJournal(directory, options = {}) {
  const ownerID = randomUUID();
  const path = join(directory, `owner-${process.pid}-${ownerID}.jsonl`);
  const identities = new WeakMap();
  let ownerBirth = null, timer, disposed = false, warned = false;
  const clock = options.now ?? Date.now;
  const getBirth = options.birth ?? processBirth;
  const version = /^[0-9]+\.[0-9]+\.[0-9]+$/.test(options.version ?? '') ? options.version : undefined;
  const patchID = /^sha256:[0-9a-f]{64}$/.test(options.patchID ?? '') ? options.patchID : undefined;
  const manager = options.coverage === 'manager_v1' && !!patchID;

  async function boundedBirth(pid) {
    let timeout;
    try {
      return await Promise.race([Promise.resolve().then(() => getBirth(pid)),
        new Promise(resolve => { timeout = setTimeout(() => resolve(null), 250); })]);
    } catch { return null; }
    finally { clearTimeout(timeout); }
  }

  function record(event, info, fields = {}, useCause = true) {
    let fd;
    try {
      const row = { schema: 1, ts: new Date(clock()).toISOString(), observer: 'kilo_owner', event,
        diagnostics_version: 2, owner_id: ownerID, owner_pid: process.pid, owner_birth: ownerBirth,
        pid: process.pid, ppid: process.ppid, birth: ownerBirth,
        platform: process.platform === 'win32' ? 'nt' : 'posix' };
      if (patchID) row.patch_id = patchID;
      if (info) {
        if (id(info.id)) row.processID = info.id;
        if (id(info.sessionID)) row.sessionID = info.sessionID;
        if (['session', 'parent', 'persistent'].includes(info.lifetime)) row.lifetime = info.lifetime;
        const identity = identities.get(info);
        if (pid(identity?.pid ?? info.pid)) row.target_pid = identity?.pid ?? info.pid;
        row.target_birth = identity?.birth ?? null;
      }
      const context = useCause ? causes.getStore() : undefined;
      if (context) {
        row.reason = reasons.has(context.reason) ? context.reason : 'unknown';
        if (id(context.operationID)) row.operation_id = context.operationID;
      }
      if (id(fields.operation_id)) row.operation_id = fields.operation_id;
      if (methods.has(fields.method)) row.method = fields.method;
      if (['returned', 'failed'].includes(fields.outcome)) row.outcome = fields.outcome;
      if (Object.hasOwn(fields, 'exit_code')) {
        row.exit_code = exitCode(fields.exit_code);
        row.windows_status = row.exit_code === null ? null : row.exit_code >>> 0;
        row.exit_signal = signals.has(fields.exit_signal) ? fields.exit_signal : null;
      }
      if (Object.hasOwn(fields, 'api_exit_code')) row.api_exit_code = exitCode(fields.api_exit_code);
      if (fields.profile === true) Object.assign(row, { runtime_version: version, pre_kill: manager,
        rollback_hook: manager, actual_exit_hook: manager, runner_hook: false, birth_available: !!ownerBirth });
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      fd = openSync(path, 'a', 0o600);
      const bytes = Buffer.from(JSON.stringify(row) + '\n');
      let offset = 0;
      while (offset < bytes.length) {
        const written = writeSync(fd, bytes, offset, bytes.length - offset);
        if (written <= 0) throw new Error('diagnostic write unavailable');
        offset += written;
      }
      fsyncSync(fd);
      return true;
    } catch {
      if (!warned) {
        warned = true;
        try { process.stderr.write('WARNING: Kilo owner diagnostics unavailable\n'); } catch {}
      }
      return false;
    } finally {
      if (fd !== undefined) { try { closeSync(fd); } catch {} }
    }
  }

  const ready = boundedBirth(process.pid).then(value => {
    ownerBirth = birthToken(value) ? value : null;
    if (disposed) return;
    record('owner_started', undefined, { profile: true });
    timer = setInterval(() => record('owner_heartbeat', undefined, { profile: true }), options.heartbeatMs ?? 10000);
    timer.unref?.();
  });

  return {
    path, ownerID, ready,
    env() {
      return { KILO_LIFECYCLE_OWNER_ID: ownerID, KILO_LIFECYCLE_OWNER_PID: String(process.pid),
        ...(patchID ? { KILO_LIFECYCLE_PATCH_ID: patchID } : {}),
        ...(ownerBirth ? { KILO_LIFECYCLE_OWNER_BIRTH: ownerBirth } : {}) };
    },
    async register(info, adopted = false, alive = () => true) {
      await ready;
      let value = !adopted && pid(info.pid) ? await boundedBirth(info.pid) : null;
      // A newly observed PID is not proof of an adopted process's original birth.
      // For spawned children also require the still-held ChildProcess and spawn-time consistency.
      try {
        const started = info.time?.started;
        const created = typeof value === 'string' && value.startsWith('winft:')
          ? Number((BigInt(value.slice(6)) - 116444736000000000n) / 10000n) : null;
        if (!alive() || !Number.isFinite(started) || created === null || created > started + 1 || started - created > 5000) value = null;
      } catch { value = null; }
      identities.set(info, { pid: info.pid, birth: birthToken(value) ? value : null });
      record(adopted ? 'owner_process_adopted' : 'owner_process_started', info, {}, false);
    },
    async stop(info, action) {
      const context = { ...causes.getStore(), operationID: randomUUID() };
      return causes.run(context, async () => {
        record('stop_requested', info);
        try {
          const result = await action();
          record('stop_result', info, { outcome: 'returned' });
          return result;
        } catch (error) {
          record('stop_result', info, { outcome: 'failed' });
          throw error;
        }
      });
    },
    issue(info, method) {
      const context = { ...causes.getStore() };
      const operationID = context.operationID ?? randomUUID();
      record('kill_issued', info, { method, operation_id: operationID });
      return (code = null, failed = false) => causes.run(context, () => record('kill_result', info,
        { method, operation_id: operationID, api_exit_code: code, outcome: failed ? 'failed' : 'returned' }));
    },
    exited(info, code, signal) {
      record('owner_child_exit', info, { exit_code: code, exit_signal: signal }, false);
    },
    disappeared(info) {
      record('owner_disappearance', info, { exit_code: null, exit_signal: null }, false);
    },
    boundary() {
      record('owner_boundary');
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clearInterval(timer);
      record('owner_disposed', undefined, {}, false);
    },
  };
}
