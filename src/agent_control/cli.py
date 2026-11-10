from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from .feature_flags import COST_DISABLED_MESSAGE, cost_enabled
from .models import Diagnostic
from .paths import AppPaths
from .storage import StateLocks
from .tool_calls import TOOLS as _TOOL_CHOICES

if TYPE_CHECKING:
    from .tool_import import ImportReport


def _public_origin_arg(value: str) -> str:
    from .dashboard import normalize_public_origin

    try:
        return normalize_public_origin(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-control",
        description="Machine-wide delegated-agent monitoring and accounting",
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--machine-index", type=Path)
    parser.add_argument("--pricing", type=Path)
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument(
        "--public-origin", type=_public_origin_arg, metavar="HTTPS_ORIGIN",
        help="trusted HTTPS origin behind an authenticated proxy; binding stays loopback",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("sync")
    diagnose = subparsers.add_parser("diagnose", help="read one run's metadata-only diagnosis")
    diagnose.add_argument("--run-id", required=True)
    incidents = subparsers.add_parser("incidents", help="read a bounded interruption cohort report")
    incidents.add_argument("--days", type=int, default=7)
    incidents.add_argument("--limit", type=int, default=200)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--task-sources", type=Path, default=None)
    projects = subparsers.add_parser("projects")
    projects.add_argument(
        "action", choices=["list", "approve", "reject", "disable", "rename"]
    )
    projects.add_argument("project_id", nargs="?")
    projects.add_argument("--display-name")
    projects.add_argument("--note")
    pricing = subparsers.add_parser(
        "pricing", help="experimental cost estimation (disabled unless AGENTCONTROL_ENABLE_COST=1)"
    )
    pricing.add_argument("action", choices=["show", "preview", "append"])
    pricing.add_argument("--model")
    pricing.add_argument("--effective-at")
    pricing.add_argument("--input", type=float)
    pricing.add_argument("--output", type=float)
    pricing.add_argument("--cache-read", type=float)
    pricing.add_argument("--cache-write", type=float)
    pricing.add_argument("--note", default="")
    pricing.add_argument("--confirm-preview-sha256")
    backfill = subparsers.add_parser("backfill-index")
    backfill.add_argument("--repo", type=Path, required=True)
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument("--min-age-minutes", type=int, default=15)
    import_usage = subparsers.add_parser("import-usage")
    import_usage.add_argument("--apply", action="store_true")
    import_usage.add_argument(
        "--tool", action="append", choices=_TOOL_CHOICES, dest="tools"
    )
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--projecta", type=Path)
    migrate.add_argument("--projectb", type=Path)
    migrate.add_argument("--report", type=Path)
    migrate.add_argument("--confirm-sha256")
    return parser


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_paths(args: argparse.Namespace) -> AppPaths:
    base = AppPaths.production()
    return AppPaths(
        repo_root=base.repo_root,
        state_dir=args.state_dir or base.state_dir,
        machine_index=args.machine_index or base.machine_index,
        baseline_pricing=args.pricing or base.baseline_pricing,
        web_dir=args.web_dir or base.web_dir,
    )


def _run_sync(args: argparse.Namespace) -> int:
    from .liveness import PSUTIL_AVAILABLE, PsutilProcessProbe
    from .sync import Synchronizer

    if not PSUTIL_AVAILABLE:
        print(
            "warning: psutil is not importable; engine processes cannot be "
            "verified, so open runs will show as unverified",
            file=sys.stderr,
        )
    paths = _resolve_paths(args)
    report = Synchronizer(paths, StateLocks(), _utc_now, PsutilProcessProbe()).run()
    print(json.dumps(asdict(report), sort_keys=True, allow_nan=False))
    return 0


_DASHBOARD_RUNNING = (
    "The dashboard is running. Stop it, run import-usage --apply, "
    "then start it again."
)


def _tool_importer_for(
    paths: AppPaths,
    locks: StateLocks,
    clock: Callable[[], datetime],
    *,
    home: Path,
    env: Mapping[str, str],
) -> Callable[[], ImportReport] | None:
    """Build the sync loop's importer for the enabled tools with existing sources."""
    from . import tool_import

    settings, _ = tool_import.load_usage_sources(paths.usage_sources)
    tools: list[str] = []
    sources: dict[str, Path] = {}
    for tool in _TOOL_CHOICES:
        setting = settings.get(tool)
        if setting is None or not setting.enabled:
            continue
        source = tool_import.resolve_source(tool, setting, home=home, env=env)
        if source is None or not _path_exists(source):
            continue
        tools.append(tool)
        sources[tool] = source
    if not tools:
        return None
    from . import tool_calls

    # One cache for the loop's lifetime: each sync re-parses only appended bytes.
    store = tool_calls.ToolCallCache(paths.tool_usage)

    def importer() -> ImportReport:
        return tool_import.import_tool_usage(
            paths, locks, tools=tuple(tools), sources=dict(sources),
            apply=True, now=clock(), home=home, store=store,
        )

    return importer


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _print_import_report(report: ImportReport) -> None:
    for s in report.tools:
        print(
            f"{s.tool}: {s.calls_seen} calls ({s.new_calls} new, "
            f"{s.revised_calls} revised), {s.sessions} sessions, "
            f"{s.first_at or '-'} to {s.last_at or '-'}, sent {s.tokens_sent}, "
            f"returned {s.tokens_returned}, cache read {s.cache_read}, "
            f"cache write {s.cache_write}"
        )
    print(f"New projects pending approval: {len(report.new_project_roots)}")
    for root in report.new_project_roots:
        print(f"  {root}")
    if report.diagnostics:
        print(f"{len(report.diagnostics)} diagnostics:")
        for diagnostic in report.diagnostics:
            print(f"  {diagnostic.code} {diagnostic.source or '-'}")


def _run_import_usage(args: argparse.Namespace) -> int:
    from . import tool_import
    from .instance_lock import InstanceLockError, acquire_instance_lock

    paths = _resolve_paths(args)
    home, env = Path.home(), os.environ
    settings, _ = tool_import.load_usage_sources(paths.usage_sources)
    if args.tools:
        selected = list(dict.fromkeys(args.tools))
    else:
        selected = []
        for tool in _TOOL_CHOICES:
            default = tool_import.default_source(tool, home=home, env=env)
            if default is not None and _path_exists(default):
                selected.append(tool)
    if not selected:
        print("No tool sources found.")
        return 1
    sources: dict[str, Path] = {}
    unlocated: list[Diagnostic] = []
    for tool in selected:
        source = tool_import.resolve_source(
            tool, settings.get(tool), home=home, env=env
        )
        if source is None:  # e.g. cline without APPDATA
            unlocated.append(
                Diagnostic(f"{tool}_source_missing", f"{tool}: no source located")
            )
        else:
            sources[tool] = source
    readable = [tool for tool in selected if tool in sources]
    locks = StateLocks()

    def run_import(apply: bool) -> ImportReport:
        report = tool_import.import_tool_usage(
            paths, locks, tools=readable, sources=sources, apply=apply,
            now=_utc_now(), home=home,
        )
        return replace(report, diagnostics=report.diagnostics + tuple(unlocated))

    if not args.apply:
        report = run_import(False)
        _print_import_report(report)
        print("Dry run: nothing written.")
        return 0
    try:
        lock = acquire_instance_lock(paths.state_dir)
    except InstanceLockError:
        print(_DASHBOARD_RUNNING, file=sys.stderr)
        return 2
    try:
        report = run_import(True)
    finally:
        lock.release()
    updated = dict(settings)
    for tool in selected:
        previous = settings.get(tool)
        updated[tool] = tool_import.SourceSetting(
            enabled=True, path=previous.path if previous is not None else None
        )
    tool_import.write_usage_sources(paths.usage_sources, updated, locks.write)
    _print_import_report(report)
    print(f"Imported. Enabled: {', '.join(selected)}.")
    return 0


def _migration_sources(args: argparse.Namespace) -> list:
    from .migration import MigrationSource

    sources = []
    for name, root in (("projecta", args.projecta), ("projectb", args.projectb)):
        if root is None:
            continue
        sources.append(
            MigrationSource(
                name=name,
                root=root,
                session_dir=root / ".claude" / "agent-runs",
            )
        )
    return sources


def _run_migrate(args: argparse.Namespace) -> int:
    from .migration import plan_migration, write_migration_report

    paths = _resolve_paths(args)
    sources = _migration_sources(args)
    if not sources:
        print(json.dumps({"error": "no migration sources provided"}))
        return 2
    if args.apply:
        print(json.dumps({"error": "apply requires operator confirmation flags"}))
        return 2
    report = plan_migration(paths, sources, clock=_utc_now)
    if args.report is not None:
        digest = write_migration_report(args.report, report)
    else:
        digest = None
    print(
        json.dumps(
            {
                "report_id": report.report_id,
                "logical_runs": report.logical_runs,
                "pricing_decision": report.pricing_decision,
                "applicable": report.applicable,
                "report_sha256": digest,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_projects(args: argparse.Namespace) -> int:
    from .projects import ProjectRegistry, load_projects

    paths = _resolve_paths(args)
    locks = StateLocks()
    registry = ProjectRegistry(paths.projects, locks, _utc_now)
    if args.action == "list":
        projects, _ = load_projects(paths.projects)
        print(
            json.dumps(
                [
                    {
                        "project_id": project.project_id,
                        "display_name": project.display_name,
                        "root": project.root,
                        "state": project.state,
                    }
                    for project in projects.values()
                ],
                sort_keys=True,
            )
        )
        return 0
    if not args.project_id:
        print(json.dumps({"error": "project_id is required"}))
        return 2
    if args.action == "rename":
        if not args.display_name:
            print(json.dumps({"error": "--display-name is required"}))
            return 2
        project = registry.rename(args.project_id, args.display_name, note=args.note)
    else:
        state = {"approve": "approved", "reject": "rejected", "disable": "disabled"}[
            args.action
        ]
        project = registry.transition(args.project_id, state, note=args.note)
    print(
        json.dumps(
            {"project_id": project.project_id, "state": project.state},
            sort_keys=True,
        )
    )
    return 0


def _pricing_candidate(args: argparse.Namespace) -> dict:
    return {
        "model": args.model,
        "effective_at": args.effective_at,
        "rates": {
            "input": args.input,
            "output": args.output,
            "cache_read": args.cache_read,
            "cache_write": args.cache_write,
        },
        "note": args.note,
    }


def _run_pricing(args: argparse.Namespace) -> int:
    from .ledger import highest_valid_revisions, load_ledger
    from .pricing import (
        append_pricing_version,
        load_baseline,
        load_pricing_history,
        preview_pricing,
    )

    paths = _resolve_paths(args)
    if args.action == "show":
        baseline = load_baseline(paths.baseline_pricing)
        history, _ = load_pricing_history(paths.pricing_history)
        print(
            json.dumps(
                {
                    "baseline": {
                        model: (asdict(rates) if rates is not None else None)
                        for model, rates in baseline.items()
                    },
                    "history": [asdict(version) for version in history],
                },
                sort_keys=True,
            )
        )
        return 0

    runs = list(highest_valid_revisions(load_ledger(paths.ledger)[0]).values())
    baseline = load_baseline(paths.baseline_pricing)
    history, _ = load_pricing_history(paths.pricing_history)
    candidate = _pricing_candidate(args)
    if args.action == "preview":
        preview = preview_pricing(candidate, runs, baseline, history, now=_utc_now())
        print(json.dumps(asdict(preview), sort_keys=True, allow_nan=False))
        return 0
    version, preview = append_pricing_version(
        paths.pricing_history,
        candidate,
        runs,
        baseline,
        now=_utc_now(),
        locks=StateLocks(),
    )
    print(
        json.dumps(
            {"version_id": version.version_id, "preview": asdict(preview)},
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


def _run_serve(args: argparse.Namespace) -> int:  # pragma: no cover - blocking loop
    from .dashboard import DashboardApp, create_server
    from .instance_lock import InstanceLockError, acquire_instance_lock
    from .liveness import PSUTIL_AVAILABLE, PsutilProcessProbe
    from .sync import Synchronizer
    from .task_sources import TaskSourcesError, load_task_sources

    task_status_sources = None
    if args.task_sources is not None:
        try:
            task_status_sources = load_task_sources(args.task_sources)
        except TaskSourcesError as exc:
            print(f"error: {exc.code}", file=sys.stderr)
            return 2

    if not PSUTIL_AVAILABLE:
        print(
            "warning: psutil is not importable; engine processes cannot be "
            "verified, so open runs will show as unverified",
            file=sys.stderr,
        )
    paths = _resolve_paths(args)
    try:
        lock = acquire_instance_lock(paths.state_dir)
    except InstanceLockError:
        pid = _read_lock_pid(paths.state_dir)
        suffix = f" (pid {pid})" if pid is not None else ""
        print(
            "error: another agent-control serve is already using "
            f"{paths.state_dir}{suffix}",
            file=sys.stderr,
        )
        return 2
    try:
        locks = StateLocks()
        process_probe = PsutilProcessProbe()
        synchronizer = Synchronizer(
            paths,
            locks,
            _utc_now,
            process_probe,
            min_interval_seconds=2.0,
            tool_importer=_tool_importer_for(
                paths, locks, _utc_now, home=Path.home(), env=os.environ
            ),
        )
        app = DashboardApp(
            paths, locks, synchronizer, _utc_now,
            public_origin=args.public_origin,
            task_status_sources=task_status_sources,
        )
        try:
            httpd = create_server(app, port=args.port)
        except OSError as exc:
            if _is_address_in_use(exc):
                print(
                    f"error: port {args.port} is already in use "
                    "(is another agent-control serve running?)",
                    file=sys.stderr,
                )
                return 2
            raise
        print(f"AgentControl listening on http://127.0.0.1:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
        return 0
    finally:
        if "process_probe" in locals():
            close = getattr(process_probe, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        lock.release()


def _read_lock_pid(state_dir: Path) -> int | None:
    try:
        text = (state_dir / "serve.lock").read_text()
    except OSError:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _is_address_in_use(exc: OSError) -> bool:
    if exc.errno == errno.EADDRINUSE:
        return True
    return getattr(exc, "winerror", None) in (10048, 10013)


def _run_backfill(args: argparse.Namespace) -> int:
    import time

    from .backfill import apply_backfill, plan_backfill
    from .discovery import repo_index_path

    paths = _resolve_paths(args)
    repo = args.repo
    min_age_seconds = max(0, int(args.min_age_minutes)) * 60
    plan = plan_backfill(
        repo,
        machine_index=paths.machine_index,
        now=time.time(),
        min_age_seconds=min_age_seconds,
    )
    index_path = repo_index_path(repo)
    for i in range(0, len(plan.records), 2):
        start = plan.records[i]
        end = plan.records[i + 1] if i + 1 < len(plan.records) else {}
        print(
            f"plan {start['run_id']} label={start['label']} "
            f"status={end.get('status')} tool_calls={end.get('tool_calls')}"
        )
    for run_id, reason in plan.skipped:
        print(f"skip {run_id}: {reason}")
    if args.apply:
        written = apply_backfill(repo, plan)
        print(f"appended {written} records to {index_path}")
    else:
        print(
            f"dry run: nothing written; re-run with --apply to append "
            f"{len(plan.records)} records to {index_path}"
        )
    return 0


def _run_diagnosis(args: argparse.Namespace) -> int:
    from .ledger import highest_valid_revisions, load_ledger
    from .run_diagnosis import build_incident_report, diagnose_run

    paths = _resolve_paths(args)
    records, diagnostics = load_ledger(paths.ledger)
    latest = highest_valid_revisions(records)
    if args.command == "diagnose":
        record = latest.get(args.run_id)
        if record is None:
            print(json.dumps({"error": "unknown_run"}))
            return 1
        report = diagnose_run(record)
    else:
        try:
            report = build_incident_report(latest.values(), now=_utc_now(),
                                           days=args.days, max_runs=args.limit)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        report["ledger_invalid_records"] = len(diagnostics)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "sync":
        return _run_sync(args)
    if args.command in {"diagnose", "incidents"}:
        return _run_diagnosis(args)
    if args.command == "backfill-index":
        return _run_backfill(args)
    if args.command == "migrate":
        return _run_migrate(args)
    if args.command == "projects":
        return _run_projects(args)
    if args.command == "pricing":
        if not cost_enabled():
            print(f"error: {COST_DISABLED_MESSAGE}", file=sys.stderr)
            return 2
        return _run_pricing(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "import-usage":
        return _run_import_usage(args)
    parser.error(f"{args.command} is not implemented yet")
    return 2
