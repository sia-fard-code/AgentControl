import { test, expect } from 'bun:test';
import { existsSync } from 'node:fs';

const source = new URL('../tools/kilo/lifecycle-process-identity.mjs', import.meta.url);

test('native Kilo birth identity is exact and unknown PIDs stay unknown', async () => {
  expect(existsSync(source)).toBe(true);
  const { processBirth } = await import(source.href);
  const first = await processBirth(process.pid);
  expect(first).toBeTruthy();
  expect(await processBirth(process.pid)).toBe(first);
  expect(await processBirth(-1)).toBeNull();
  expect(await processBirth(true)).toBeNull();
  expect(await processBirth(4294967294)).toBeNull();
});
