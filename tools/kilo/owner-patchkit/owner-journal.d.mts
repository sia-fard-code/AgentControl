export type StopReason = 'explicit_stop' | 'explicit_restart' | 'session_stop' | 'session_removed'
  | 'task_finished' | 'sandbox_enabled' | 'instance_dispose' | 'runtime_shutdown'
  | 'launch_rollback' | 'api_session_stop' | 'unknown';
type Info = { id: string; pid?: number; sessionID: string; lifetime: string; time?: { started: number } };
export function withCause<T>(reason: StopReason, action: () => T): T;
export function withDisposeCause<T>(action: () => T): T;
export function createOwnerJournal(directory: string, options?: { version?: string; coverage?: string; patchID?: string }): {
  path: string; ownerID: string; ready: Promise<void>;
  env(): Record<string, string>;
  register(info: Info, adopted?: boolean, alive?: () => boolean): Promise<void>;
  stop<T>(info: Info, action: () => T | Promise<T>): Promise<T>;
  issue(info: Info, method: string): (code?: number | null, failed?: boolean) => boolean;
  exited(info: Info, code: number | null, signal: string | null): void;
  disappeared(info: Info): void;
  boundary(): void;
  dispose(): void;
};
