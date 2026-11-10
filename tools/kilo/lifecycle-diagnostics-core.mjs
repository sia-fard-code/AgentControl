// Record intent and observations, never commands, tool output, or error bodies.
import { mkdirSync, openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

const actions = new Set(['start', 'monitor', 'stop', 'restart', 'status', 'logs', 'list']);
const statuses = new Set(['starting', 'running', 'ready', 'stopping', 'stopped', 'exited', 'failed']);
const reportedReasons = new Set(['exit', 'stop', 'restart', 'timeout', 'shutdown', 'session_disposed', 'group_switch']);
const identity = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const positiveInteger = value => Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff;

export function createHooks(path, options = {}) {
  const instanceID = randomUUID();
  const birth = typeof options.birth === 'string' && /^(winft:[0-9]{1,20}|linux:[0-9a-f-]{36}:[0-9]{1,20})$/.test(options.birth)
    ? options.birth : null;
  let warned = false;
  let disposed = false;
  function record(event, input = {}, fields = {}) {
    const row = { schema: 1, ts: new Date().toISOString(), observer: 'kilo', diagnostics_version: 2,
      instanceID, pid: process.pid, ppid: process.ppid, birth, event };
    if (event.startsWith('plugin_')) Object.assign(row, {
      plugin_version: 2, pre_kill: false, actual_exit_hook: false, birth_available: !!birth,
    });
    for (const name of ['sessionID', 'callID']) {
      if (identity(input[name])) row[name] = input[name];
    }
    if (identity(fields.processID)) row.processID = fields.processID;
    if (actions.has(fields.action)) row.action = fields.action;
    if (statuses.has(fields.status)) row.status = fields.status;
    if (reportedReasons.has(fields.reported_reason)) row.reported_reason = fields.reported_reason;
    if (positiveInteger(fields.child_pid)) row.child_pid = fields.child_pid;
    if (Number.isSafeInteger(fields.started_ms) && fields.started_ms >= 0) row.started_ms = fields.started_ms;
    if (['session', 'persistent', 'parent'].includes(fields.lifetime)) row.lifetime = fields.lifetime;
    if (['explicit_stop', 'explicit_restart'].includes(fields.reason)) row.reason = fields.reason;
    let fd;
    try {
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      fd = openSync(path, 'a', 0o600);
      const bytes = Buffer.from(JSON.stringify(row) + '\n');
      let offset = 0;
      while (offset < bytes.length) {
        const written = writeSync(fd, bytes, offset, bytes.length - offset);
        if (written <= 0) throw new Error('diagnostic write unavailable');
        offset += written;
      }
      fsyncSync(fd);
    } catch {
      if (!warned) {
        warned = true;
        try { process.stderr.write('WARNING: Kilo lifecycle journal unavailable\n'); } catch {}
      }
    } finally {
      if (fd !== undefined) { try { closeSync(fd); } catch {} }
    }
  }
  record('plugin_started');
  const timer = setInterval(() => record('plugin_heartbeat'), options.heartbeatMs ?? 10000);
  timer.unref?.();
  return {
    dispose: async () => {
      if (disposed) return;
      disposed = true;
      clearInterval(timer);
      record('plugin_disposed');
    },
    'tool.execute.before': async (input, output) => {
      if (input.tool !== 'background_process') return;
      const args = output.args ?? {};
      if (!actions.has(args.action)) return;
      record(['stop', 'restart'].includes(args.action) ? 'stop_requested' : 'background_requested', input, {
        action: args.action, processID: args.id,
        reason: args.action === 'stop' ? 'explicit_stop' : args.action === 'restart' ? 'explicit_restart' : undefined,
        lifetime: args.persistent === true ? 'persistent' : args.inherit === true ? 'parent' : 'session',
      });
    },
    'tool.execute.after': async (input, output) => {
      if (input.tool !== 'background_process') return;
      const metadata = output.metadata ?? {};
      // The runtime's PID line is metadata embedded in its header. Copy no other output.
      const header = typeof output.output === 'string'
        ? /^id: ([A-Za-z0-9_.:-]+)\r?\nstatus: [a-z]+\r?\npid: ([1-9][0-9]{0,9})(?:\r?\n|$)/.exec(output.output.slice(0, 512)) : null;
      record('background_observed', input, { action: input.args?.action,
        processID: metadata.processID ?? input.args?.id, status: metadata.status,
        reported_reason: metadata.reason,
        child_pid: header && header[1] === metadata.processID ? Number(header[2]) : undefined });
    },
    event: async ({ event }) => {
      if (event.type === 'background_process.updated') {
        const info = event.properties?.info;
        if (info && typeof info === 'object') record('background_observed', { sessionID: info.sessionID }, {
          processID: info.id, child_pid: info.pid, status: info.status, lifetime: info.lifetime,
          started_ms: info.time?.started,
        });
      }
      if (event.type === 'background_process.deleted') record('background_deleted',
        { sessionID: event.properties?.sessionID }, { processID: event.properties?.processID });
      // These documented notifications do not prove any particular child was killed.
      if (event.type === 'server.instance.disposed') record('instance_disposed');
      if (event.type === 'session.deleted') record('session_deleted', { sessionID: event.properties?.info?.id });
      if (event.type === 'session.error') record('session_error', { sessionID: event.properties?.sessionID });
      if (event.type === 'session.idle') record('session_idle', { sessionID: event.properties?.sessionID });
    },
  };
}
