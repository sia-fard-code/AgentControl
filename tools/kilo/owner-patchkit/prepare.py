"""Prepare a pinned source overlay. Never install, build, or replace a live runtime."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import urllib.request
import tempfile

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
PREFIX = "packages/opencode/src/"
MANAGER = PREFIX + "kilocode/background-process/index.ts"
STAGING_ROOT = Path(tempfile.gettempdir()) / "kilo"


def patch_id():
    """Fingerprint pinned inputs and implementation, excluding its own registered value."""
    digest = hashlib.sha256()
    metadata = {k: v for k, v in MANIFEST.items() if k != "patch_id"}
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
    for path in (Path(__file__), ROOT / "owner-journal.mjs", ROOT / "owner-journal.d.mts",
                 ROOT.parent / "lifecycle-process-identity.mjs"):
        digest.update(path.name.encode() + b"\0" + path.read_text(encoding="utf-8").replace("\r\n", "\n").encode())
    return "sha256:" + digest.hexdigest()


def replace_once(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("source anchor count does not match the pinned release")
    return text.replace(old, new)


def verify_sources(sources, expected):
    if set(sources) != set(expected):
        raise ValueError("source file set does not match the pinned release")
    for name, value in sources.items():
        if not isinstance(value, bytes) or len(value) > 1024 * 1024:
            raise ValueError("source size/type rejected")
        actual = hashlib.sha1(b"blob " + str(len(value)).encode("ascii") + b"\0" + value).hexdigest()
        if actual != expected[name]:
            raise ValueError("source content does not match the pinned release")


def transform_manager(text):
    def change(old, new, count=1):
        nonlocal text
        text = replace_once(text, old, new, count)

    change('import { BackgroundProcessRunner } from "./runner"',
           'import { BackgroundProcessRunner } from "./runner"\n'
           'import { createOwnerJournal, withCause, withDisposeCause, type StopReason } from "./owner-journal.mjs"\n'
           'import { OWNER_PATCH_ID } from "./owner-patch-id.mjs"')
    change('  const log = Log.create({ service: "background-process" })',
           '  const log = Log.create({ service: "background-process" })\n'
           '  const diagnostics = createOwnerJournal(path.join(Global.Path.data, "lifecycle"),\n'
           '    { version: "7.8.18", coverage: "manager_v1", patchID: OWNER_PATCH_ID })')
    change('    const result: NodeJS.ProcessEnv = modelEnv({',
           '    const result: NodeJS.ProcessEnv = modelEnv({\n      ...diagnostics.env(),')
    change('    try {\n      if (lifetime === "persistent") {',
           '    try {\n      void diagnostics.register(active.info, false, () => !stopped(proc)).catch(() => {})\n      if (lifetime === "persistent") {')
    change('    proc.once("exit", (code, signal) => {',
           '    proc.once("exit", (code, signal) => {\n      diagnostics.exited(active.info, code, signal)')
    change('          exited(active, 0, null)\n          await forget(shared, active)',
           '          diagnostics.disappeared(active.info)\n          exited(active, 0, null)\n          await forget(shared, active)')
    signature = '  async function terminate(state: State, active: Active, opts?: { remove?: boolean; silent?: boolean }) {'
    change(signature, signature + '\n    return diagnostics.stop(active.info, () => terminateImpl(state, active, opts))\n  }\n\n'
           + signature.replace('function terminate(', 'function terminateImpl('))
    signature = '  async function rollback(active: Active) {'
    change(signature, signature + '\n    return withCause("launch_rollback", () => diagnostics.stop(active.info, () => rollbackImpl(active)))\n  }\n\n'
           + signature.replace('function rollback(', 'function rollbackImpl('))
    change('    await terminate(current, active)\n    return clone(active.info)',
           '    await withCause("explicit_stop", () => terminate(current, active))\n    return clone(active.info)')
    change('    await terminate(current, active, { remove: true })\n    return launch(current, input, id)',
           '    await withCause("explicit_restart", () => terminate(current, active, { remove: true }))\n    return launch(current, input, id)')
    change('  export async function stopSession(sessionID: SessionID) {',
           '  export async function stopSession(sessionID: SessionID, reason: StopReason = "session_stop") {')
    change('        await terminate(current, active, { remove: true })',
           '        await withCause(reason, () => terminate(current, active, { remove: true }))')
    change('                  terminate(state, active, { remove: true, silent: true }),',
           '                  withDisposeCause(() => terminate(state, active, { remove: true, silent: true })),')
    shutdown = ('withCause("runtime_shutdown", async () => {\n'
                '      diagnostics.boundary()\n      try { await runtime.dispose() } finally { diagnostics.dispose() }\n    })')
    change('  KiloShutdown.register(() => runtime.dispose())', '  KiloShutdown.register(() => ' + shutdown + ')')
    change('  export function shutdown() {\n    return runtime.dispose()\n  }',
           '  export function shutdown() {\n    return ' + shutdown + '\n  }')
    change('      shared.processes.set(active.info.id, active)\n      await output(active)',
           '      shared.processes.set(active.info.id, active)\n      void diagnostics.register(active.info, true).catch(() => {})\n      await output(active)')

    change('        const child = spawn("taskkill",',
           '        const reported = diagnostics.issue(active.info, "taskkill")\n        const child = spawn("taskkill",')
    change('        child.once("exit", () => resolve())\n        child.once("error", () => resolve())',
           '        child.once("exit", (code) => { reported(code); resolve() })\n'
           '        child.once("error", () => { reported(null, true); resolve() })')
    change('        await Filesystem.write(active.control, "stop", 0o600)',
           '        const reported = diagnostics.issue(active.info, "control_file")\n'
           '        try { await Filesystem.write(active.control, "stop", 0o600); reported() }\n'
           '        catch (error) { reported(null, true); throw error }', 2)
    for signal, count in (("SIGTERM", 2), ("SIGKILL", 3)):
        command = f'process.kill(-pid, "{signal}")'
        method = "sigterm_group" if signal == "SIGTERM" else "sigkill_group"
        change(command, f'{{ const reported = diagnostics.issue(active.info, "{method}");\n'
               f'        try {{ {command}; reported() }} catch (error) {{ reported(null, true); throw error }} }}', count)
        command = f'process.kill(pid, "{signal}")'
        change(command, '{ const reported = diagnostics.issue(active.info, "process_terminate");\n'
               f'        try {{ {command}; reported() }} catch (error) {{ reported(null, true); throw error }} }}')
    command = '        const out = await Process.run(["taskkill", "/pid", String(pid), "/f", "/t"], { nothrow: true })'
    change(command, '        const reported = diagnostics.issue(active.info, "taskkill")\n' + command + '\n        reported(out.code)')
    return text


def transformed_sources(sources):
    result = {name: value.decode("utf-8") for name, value in sources.items()}
    result[MANAGER] = transform_manager(result[MANAGER])
    calls = {
        PREFIX + "session/session.ts": ('BackgroundProcess.stopSession(sessionID)', 'BackgroundProcess.stopSession(sessionID, "session_removed")'),
        PREFIX + "kilocode/tool/task-background-process.ts": ('BackgroundProcess.stopSession(sessionID)', 'BackgroundProcess.stopSession(sessionID, "task_finished")'),
        PREFIX + "kilocode/server/httpapi/handlers/background-process.ts": ('BackgroundProcess.stopSession(ctx.params.sessionID)', 'BackgroundProcess.stopSession(ctx.params.sessionID, "api_session_stop")'),
        PREFIX + "kilocode/server/httpapi/handlers/sandbox.ts": ('BackgroundProcess.stopSession(ctx.params.sessionID)', 'BackgroundProcess.stopSession(ctx.params.sessionID, "sandbox_enabled")'),
    }
    for name, (old, new) in calls.items():
        result[name] = replace_once(result[name], old, new)
    for name in ("owner-journal.mjs", "owner-journal.d.mts"):
        result[PREFIX + "kilocode/background-process/" + name] = (ROOT / name).read_text(encoding="utf-8")
    result[PREFIX + "kilocode/lifecycle-process-identity.mjs"] = (ROOT.parent / "lifecycle-process-identity.mjs").read_text(encoding="utf-8")
    result[PREFIX + "kilocode/background-process/owner-patch-id.mjs"] = f'export const OWNER_PATCH_ID = "{patch_id()}";\n'
    result[PREFIX + "kilocode/background-process/owner-patch-id.d.mts"] = 'export declare const OWNER_PATCH_ID: string;\n'
    return result


def prepare(output, source=None):
    output = Path(output).resolve()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("output must be a fresh staging directory under an existing parent")
    if STAGING_ROOT.resolve() not in output.parents:
        raise ValueError("output must be inside the designated temporary Kilo staging root")
    if source is not None:
        source = Path(source).resolve()
        if output == source or source in output.parents or output in source.parents:
            raise ValueError("staging must be separate from the input source")
        sources = {name: (source / name).read_bytes() for name in MANIFEST["files"]}
    else:
        sources = {}
        for name in MANIFEST["files"]:
            url = f'https://raw.githubusercontent.com/Kilo-Org/kilocode/{MANIFEST["upstream_commit"]}/{name}'
            with urllib.request.urlopen(url, timeout=30) as response:
                sources[name] = response.read(1024 * 1024 + 1)
    sources = {name: value.replace(b"\r\n", b"\n") for name, value in sources.items()}
    verify_sources(sources, MANIFEST["files"])
    if MANIFEST.get("patch_id") != patch_id():
        raise ValueError("patch implementation fingerprint registry is stale")
    patched = transformed_sources(sources)  # every hash/anchor validates before any write
    diff = []
    for name, value in patched.items():
        original = sources.get(name, b"").decode("utf-8")
        diff.extend(difflib.unified_diff(original.splitlines(keepends=True), value.splitlines(keepends=True),
                                        fromfile="a/" + name, tofile="b/" + name))
    output.mkdir()
    for section, values in (("upstream", sources), ("source", patched)):
        for name, value in values.items():
            path = output / section / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(value if isinstance(value, bytes) else value.encode("utf-8"))
    (output / "owner-attribution.patch").write_text("".join(diff), encoding="utf-8", newline="\n")
    report = {"schema": 1, "runtime_version": MANIFEST["runtime_version"],
              "upstream_commit": MANIFEST["upstream_commit"], "installed": False,
              "patch_id": patch_id(),
              "files": {name: hashlib.sha256(value.encode("utf-8")).hexdigest() for name, value in patched.items()}}
    (output / "PREPARED.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", action="store_true", help="fetch only the exact pinned public source files")
    group.add_argument("--source", type=Path, help="read a local exact-version checkout without modifying it")
    group.add_argument("--print-patch-id", action="store_true", help="print the current implementation fingerprint without writing")
    parser.add_argument("--output", type=Path, help="fresh output inside the designated temporary Kilo staging root")
    args = parser.parse_args()
    if args.print_patch_id:
        print(patch_id())
        return 0
    if args.output is None:
        parser.error("--output is required when preparing a source overlay")
    try:
        report = prepare(args.output, source=args.source)
    except Exception as error:
        print(f"Preparation failed ({type(error).__name__}); no installed runtime was modified.")
        return 1
    print(f'Prepared Kilo {report["runtime_version"]}: {len(report["files"])} source files. NOT installed or built.')
    print(f"Staging: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
