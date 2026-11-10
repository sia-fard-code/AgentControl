// Exact, read-only process birth tokens. No command-line or environment queries.
import { readFile } from 'node:fs/promises';

let native;
async function windows() {
  if (!native) native = import('bun:ffi').then(ffi => ffi.dlopen('kernel32.dll', {
    OpenProcess: { args: ['u32', 'i32', 'u32'], returns: 'u64' },
    GetProcessTimes: { args: ['u64', 'ptr', 'ptr', 'ptr', 'ptr'], returns: 'i32' },
    CloseHandle: { args: ['u64'], returns: 'i32' },
  }).symbols).catch(() => null);
  return native;
}

export async function processBirth(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1 || pid > 0xffffffff) return null;
  try {
    if (process.platform === 'win32') {
      const kernel = await windows();
      if (!kernel) return null;
      const handle = kernel.OpenProcess(0x1000, 0, pid);
      if (handle === 0n) return null;
      try {
        const times = Array.from({ length: 4 }, () => new BigUint64Array(1));
        if (!kernel.GetProcessTimes(handle, ...times) || times[0][0] === 0n) return null;
        return `winft:${times[0][0]}`;
      } finally {
        kernel.CloseHandle(handle);
      }
    }
    if (process.platform === 'linux') {
      const [boot, stat] = await Promise.all([
        readFile('/proc/sys/kernel/random/boot_id', 'utf8'), readFile(`/proc/${pid}/stat`, 'utf8'),
      ]);
      const ticks = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/)[19];
      if (/^[0-9a-f-]{36}$/.test(boot.trim()) && /^[0-9]+$/.test(ticks)) return `linux:${boot.trim()}:${ticks}`;
    }
  } catch {}
  return null;
}
