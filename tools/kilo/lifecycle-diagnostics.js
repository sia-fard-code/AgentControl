// Only the plugin object is exported. The .mjs helper is not auto-discovered as a plugin.
import { homedir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createHooks } from './lifecycle-diagnostics-core.mjs';
import { processBirth } from './lifecycle-process-identity.mjs';

const server = async () => createHooks(join(process.env.XDG_DATA_HOME || join(homedir(), '.local', 'share'), 'kilo', 'lifecycle',
  `${new Date().toISOString().slice(0, 10)}-${process.pid}-${randomUUID()}.jsonl`),
  { birth: await processBirth(process.pid) });

export default { id: 'lifecycle-diagnostics', server };
