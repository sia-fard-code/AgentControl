from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import operator_log, steering
from .ledger import highest_valid_revisions, load_ledger
from .models import TaskStatusSource
from .task_state import lexical_root, read_task_status
from .narration import (
    Narration,
    guidance_entries,
    mask_secrets,
    masked_dict,
    read_narration,
    read_task_text,
)
from .feature_flags import COST_DISABLED_MESSAGE, cost_enabled as _cost_enabled_from_env
from .paths import AppPaths
from .process_control import KillDenied, NotVerified, terminate_engine
from .pricing import (
    append_pricing_version,
    load_baseline,
    load_pricing_history,
    preview_pricing,
)
from .projects import ProjectRegistry, load_projects
from .snapshot import SnapshotQuery, build_snapshot
from .storage import StateLocks, strict_json_loads
from .tool_calls import TOOLS, ToolCallCache

_MAX_BODY = 16 * 1024
_STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
    "panel.js": "text/javascript; charset=utf-8",
    "task-state.js": "text/javascript; charset=utf-8",
    "panel.css": "text/css; charset=utf-8",
}
_QUERY_TOOLS = ("engine", *TOOLS)
_MUTATION_STATES = {"approve": "approved", "reject": "rejected", "disable": "disabled"}
_PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def normalize_public_origin(value: str) -> str:
    message = "public origin must be a bare HTTPS origin with a valid hostname and port"
    if (
        not isinstance(value, str) or not value.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\%?#")
    ):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError(message) from None
    if (
        parsed.scheme != "https" or not host or len(host) > 253
        or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        )
    ):
        raise ValueError(message)
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


@dataclass(frozen=True)
class _CachedNarration:
    """A parsed transcript with its events masked once, held in memory only."""

    parsed: Narration
    events: tuple[dict, ...]
    brief: str | None
    masked_total: int


_COST_KEYS = frozenset({
    "cost", "cost_classification", "known_cost", "total_cost", "burn_cost",
    "tool_burn_cost_per_hour", "cost_per_hour", "cost_unknown_runs",
})


def _strip_cost(value):
    """Remove dollar estimates; token counters and run metadata are kept."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _COST_KEYS or key in ("spend", "pricing"):
                continue
            out[key] = _strip_cost(item)
        return out
    if isinstance(value, list):
        return [_strip_cost(item) for item in value]
    return value


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class DashboardApp:
    def __init__(
        self,
        paths: AppPaths,
        locks: StateLocks,
        synchronizer,
        clock: Callable[[], datetime],
        *,
        psutil_module=None,
        public_origin: str | None = None,
        task_status_sources: tuple[TaskStatusSource, ...] | list[TaskStatusSource] | None = None,
        cost_enabled: bool | None = None,
    ) -> None:
        # Experimental cost estimation is off unless explicitly enabled.
        self.cost_enabled = _cost_enabled_from_env() if cost_enabled is None else bool(cost_enabled)
        self.public_origin = (
            normalize_public_origin(public_origin) if public_origin is not None else None
        )
        # Startup injection only; caller owns parent source ACLs outside worker authority.
        self._task_status_sources = None if task_status_sources is None else tuple(task_status_sources)
        self.paths = paths
        self.locks = locks
        self.synchronizer = synchronizer
        self.clock = clock
        self.registry = ProjectRegistry(paths.projects, locks, clock)
        self._activity_cache: "OrderedDict[str, tuple[object, dict]]" = OrderedDict()
        self._activity_lock = threading.Lock()
        self._ledger_lock = threading.Lock()
        self._ledger_signature: object = object()
        self._ledger_runs: list | None = None
        self._tool_cache = ToolCallCache(paths.tool_usage)
        self._tool_lock = threading.Lock()
        self._psutil_module = psutil_module
        self._narration_cache: "OrderedDict[str, tuple[object, _CachedNarration | None]]" = (
            OrderedDict()
        )
        self._narration_lock = threading.Lock()
        self._steers = steering.SteerBook()

    def _web_dir(self) -> Path:
        if self.paths.web_dir.is_dir():
            return self.paths.web_dir
        return Path(__file__).resolve().parents[2] / "web"

    def _ledger_file_signature(self) -> tuple[int, int] | None:
        try:
            st = self.paths.ledger.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def _ledger_records(self) -> list:
        signature = self._ledger_file_signature()
        with self._ledger_lock:
            if self._ledger_runs is None or self._ledger_signature != signature:
                self._ledger_runs = list(
                    highest_valid_revisions(load_ledger(self.paths.ledger)[0]).values()
                )
                self._ledger_signature = signature
            return self._ledger_runs

    def _record(self, run_id: str):
        for record in self._ledger_records():
            if record.run_id == run_id:
                return record
        raise ApiError(404, "not_found", "unknown run")

    def _record_synced(self, run_id: str):
        """The run's ledger record. An unknown run triggers one sync first (the
        synchronizer keeps its own 2-second rate limit), so a run started since
        the dashboard last refreshed is found at once instead of answering 404."""
        try:
            return self._record(run_id)
        except ApiError as exc:
            if exc.status != 404:
                raise
        self.synchronizer.run()
        return self._record(run_id)

    def _open_runs(self) -> Mapping:
        observations = getattr(self.synchronizer, "open_runs", None)
        if isinstance(observations, Mapping):
            return observations
        return {}

    def _load_state(self):
        runs = self._ledger_records()
        projects, _ = load_projects(self.paths.projects)
        baseline = load_baseline(self.paths.baseline_pricing)
        history, _ = load_pricing_history(self.paths.pricing_history)
        return runs, projects, baseline, history

    def snapshot(self, query: SnapshotQuery) -> dict[str, object]:
        self.synchronizer.run()
        runs, projects, baseline, history = self._load_state()
        observations = getattr(self.synchronizer, "open_runs", None)
        if not isinstance(observations, Mapping):
            observations = None
        with self._tool_lock:
            calls, _ = self._tool_cache.load()
        now = self.clock()
        task_statuses = None
        if self._task_status_sources is not None:
            task_statuses = []
            for source in self._task_status_sources:
                if not isinstance(source, TaskStatusSource):
                    continue
                project = projects.get(source.project_id)
                if (project is None or not isinstance(source.project_root, str)
                        or lexical_root(project.root) != lexical_root(source.project_root)):
                    continue
                observed = read_task_status(source, now=now)
                if observed is not None:
                    task_statuses.append(observed)
        snapshot = build_snapshot(
            runs, projects, baseline, history, query,
            now=now, observations=observations,
            operator_actions=operator_log.read_actions(self.paths.state_dir),
            tool_calls=list(calls.values()),
            task_statuses=task_statuses,
        )
        if self.cost_enabled:
            snapshot["pricing"] = self._pricing_view(runs, baseline, history)
        else:
            snapshot = _strip_cost(snapshot)
        snapshot["features"] = {"cost": self.cost_enabled}
        return snapshot

    def _require_cost(self) -> None:
        if not self.cost_enabled:
            raise ApiError(404, "cost_disabled", COST_DISABLED_MESSAGE)

    def _pricing_view(self, runs, baseline, history) -> dict[str, object]:
        from dataclasses import asdict

        from .pricing import resolve_rates

        models: set[str] = {run.model for run in runs if run.model}
        models.update(baseline.keys())
        models.update(version.model for version in history)

        active: dict[str, object] = {}
        for model in sorted(models):
            rates, version_id = resolve_rates(
                model, self.clock().isoformat(), baseline, history
            )
            history_count = sum(1 for v in history if v.model == model)
            source = (
                "baseline"
                if rates is not None and version_id is None
                else ("versioned" if version_id is not None else "unset")
            )
            active[model] = {
                "rates": asdict(rates) if rates is not None else None,
                "complete": rates is not None,
                "source": source,
                "history_count": history_count,
            }
        return {
            "active": active,
            "baseline": {
                model: (asdict(rate) if rate is not None else None)
                for model, rate in baseline.items()
            },
            "history": [asdict(version) for version in history],
        }

    def projects(self) -> list[dict[str, object]]:
        projects, _ = load_projects(self.paths.projects)
        return [
            {
                "project_id": project.project_id,
                "display_name": project.display_name,
                "root": project.root,
                "state": project.state,
                "observed_at": project.observed_at,
            }
            for project in projects.values()
        ]

    def activity(self, run_id: str) -> dict[str, object]:
        from copy import deepcopy
        from datetime import timezone
        from pathlib import Path

        from . import activity as activity_mod
        from .activity import load_activity_archive
        from .pricing import resolve_rates
        from .progress import read_progress

        current = self._record_synced(run_id)
        transcript = current.transcript_artifact
        transcript_accessible = bool(transcript) and Path(transcript).is_file()

        # Cache built snapshots by transcript path while its and the usage
        # journal's (st_size, st_mtime_ns) are unchanged, so a drawer polling
        # every 2 s does not re-parse an idle run. Bounded to 16 (oldest out).
        cache = self._activity_cache

        def _cached_snapshot(path: str) -> dict[str, object]:
            transcript_path = Path(path)
            journal = transcript_path.with_suffix(".usage.jsonl")

            def _sig(p: Path):
                try:
                    st = p.stat()
                except OSError:
                    return None
                return (st.st_size, st.st_mtime_ns)

            key = str(transcript_path)
            signature = (_sig(transcript_path), _sig(journal))
            with self._activity_lock:
                cached = cache.get(key)
                if cached is not None and cached[0] == signature:
                    return cached[1]
            # Build the snapshot outside the lock so one slow parse does not
            # block other concurrent /api/activity requests.
            built = activity_mod.build_activity_snapshot(path)
            with self._activity_lock:
                cache[key] = (signature, built)
                while len(cache) > 16:
                    cache.popitem(last=False)
            return built

        # Prefer the live transcript; fall back to the sanitized archive captured
        # earlier so per-step detail survives cleanup of ephemeral workspaces.
        from_archive = False
        if transcript_accessible:
            snapshot = _cached_snapshot(transcript)
        else:
            archived = load_activity_archive(self.paths.activity_archive(run_id))
            if archived is not None:
                snapshot = archived
                from_archive = True
            else:
                snapshot = {
                    "steps": [],
                    "step_count": 0,
                    "tool_count": 0,
                    "total_seconds": 0.0,
                    "latest_started_at": None,
                    "model_seconds_total": None,
                    "tool_seconds_total": None,
                }

        baseline = load_baseline(self.paths.baseline_pricing)
        history, _ = load_pricing_history(self.paths.pricing_history)
        rate, _ = resolve_rates(current.model, current.timestamp, baseline, history)

        def _step_cost(step: dict[str, object]) -> float | None:
            if rate is None:
                return None
            sent = step.get("tokens_sent")
            recv = step.get("tokens_returned")
            if sent is None or recv is None:
                return None
            cr = step.get("cache_read") or 0
            cw = step.get("cache_write") or 0
            non_cached = max(int(sent) - int(cr), 0)
            return (
                non_cached * rate.input
                + int(recv) * rate.output
                + int(cr) * rate.cache_read
                + int(cw) * rate.cache_write
            ) / 1_000_000

        # Price each step at read time (archive is pricing-agnostic). Copy the
        # steps first: pricing mutates each dict and must not touch cached data.
        steps = deepcopy(snapshot["steps"])
        total_cost = 0.0
        priced_any = False
        for step in steps:
            if step.get("tokens_sent") is not None:
                cost = _step_cost(step)
                step["cost"] = cost
                if cost is not None:
                    total_cost += cost
                    priced_any = True
            else:
                step.setdefault("cost", None)
            step.setdefault("started_at", None)
            step.setdefault("duration_seconds", None)

        total_seconds = float(snapshot.get("total_seconds") or 0.0)
        latest_started_at = snapshot.get("latest_started_at")
        current_started_at = latest_started_at if current.status == "running" else None

        # Live progress: only for accessible, running transcripts. Reports the
        # true phase so a stuck model-call retry is visible; the phase's start
        # comes from the transcript mtime (model_call) or the latest reply.
        progress: dict[str, object] | None = None
        current_phase: str | None = None
        current_phase_since: str | None = None
        if transcript_accessible and current.status == "running":
            rp = read_progress(transcript)
            if rp is not None:
                progress = rp.as_dict()
                current_phase = progress.get("phase")
                if current_phase == "model_call":
                    try:
                        mtime = Path(transcript).stat().st_mtime
                        current_phase_since = datetime.fromtimestamp(
                            mtime, tz=timezone.utc
                        ).isoformat()
                    except OSError:
                        current_phase_since = None
                elif current_phase == "tools":
                    current_phase_since = latest_started_at
        if current_phase_since is not None:
            current_started_at = current_phase_since

        tool_steps = [s for s in steps if s["phase"] == "tool"]
        payload = {
            "run_id": run_id,
            "status": current.status,
            "model": current.model,
            "label": current.label,
            "available": bool(transcript),
            "artifact_accessible": transcript_accessible,
            "artifact_path_recorded": bool(transcript),
            "from_archive": from_archive,
            "step_count": len(steps),
            "tool_count": len(tool_steps),
            "total_seconds": total_seconds,
            "model_seconds_total": snapshot.get("model_seconds_total"),
            "tool_seconds_total": snapshot.get("tool_seconds_total"),
            "total_cost": total_cost if priced_any else None,
            "server_now": self.clock().isoformat(),
            "current_started_at": current_started_at,
            "progress": progress,
            "current_phase": current_phase,
            "current_phase_since": current_phase_since,
            # Cap the returned timeline so a giant run cannot bloat the response.
            "steps": steps[-400:],
            "diagnosis": self.run_diagnosis(run_id),
        }
        return payload if self.cost_enabled else _strip_cost(payload)

    def run_diagnosis(self, run_id: str) -> dict[str, object]:
        from .run_diagnosis import diagnose_run

        return diagnose_run(self._record_synced(run_id))

    def diagnostics(self) -> list[dict[str, object]]:
        _, ledger_diagnostics = load_ledger(self.paths.ledger)
        _, project_diagnostics = load_projects(self.paths.projects)
        combined = [*ledger_diagnostics, *project_diagnostics]
        return [
            {
                "code": diag.code,
                "message": diag.message,
                "source": diag.source,
                "run_id": diag.run_id,
                "project_id": diag.project_id,
            }
            for diag in combined
        ]

    # --- narration & intervention ---------------------------------------
    def _narration_for(self, transcript: str) -> _CachedNarration | None:
        path = Path(transcript)
        try:
            st = path.stat()
        except OSError:
            return None
        signature = (st.st_size, st.st_mtime_ns)
        key = transcript
        with self._narration_lock:
            cached = self._narration_cache.get(key)
            if cached is not None and cached[0] == signature:
                self._narration_cache.move_to_end(key)
                return cached[1]
        if transcript.endswith(".md"):
            msgs = transcript[:-3] + ".messages.jsonl"
        else:
            msgs = transcript + ".messages.jsonl"
        parsed = read_narration(path, task_text=read_task_text(msgs))
        entry = self._build_cached_narration(parsed) if parsed is not None else None
        with self._narration_lock:
            self._narration_cache[key] = (signature, entry)
            self._narration_cache.move_to_end(key)
            while len(self._narration_cache) > 8:
                self._narration_cache.popitem(last=False)
        return entry

    @staticmethod
    def _build_cached_narration(parsed: Narration) -> _CachedNarration:
        masked_events: list[dict] = []
        masked_total = 0
        for event in parsed.events:
            data, count = masked_dict(event)
            masked_events.append(data)
            masked_total += count
        brief: str | None = None
        if parsed.brief:
            brief, n = mask_secrets(parsed.brief)
            masked_total += n
        return _CachedNarration(
            parsed=parsed,
            events=tuple(masked_events),
            brief=brief,
            masked_total=masked_total,
        )

    def narration(self, run_id: str, after: int) -> dict[str, object]:
        record = self._record_synced(run_id)
        transcript = record.transcript_artifact or ""
        cached = self._narration_for(transcript) if transcript else None
        parsed = cached.parsed if cached else None

        guidance = guidance_entries(parsed) if parsed else []
        steer_file_text = (
            steering.read_steer_file(Path(transcript + ".steer"))
            if transcript
            else ""
        )
        run_open = run_id in self._open_runs()
        statuses = []
        for pending in self._steers.for_run(run_id):
            state, turn = steering.delivery_state(
                pending,
                guidance=guidance,
                steer_file_text=steer_file_text,
                run_open=run_open,
            )
            statuses.append(steering.status_dict(pending, state, turn))

        if cached is None:
            return {
                "run_id": run_id,
                "status": record.status,
                "available": False,
                "header": None,
                "brief": None,
                "events": [],
                "total": 0,
                "masked": 0,
                "truncated": False,
                "steer": statuses,
            }

        events = list(cached.events[after:])
        brief = cached.brief if after == 0 else None
        return {
            "run_id": run_id,
            "status": record.status,
            "available": True,
            "header": parsed.header.as_dict() if parsed.header else None,
            "brief": brief,
            "events": events,
            "total": len(cached.events),
            "masked": cached.masked_total,
            "truncated": parsed.truncated,
            "steer": statuses,
        }

    def steer(self, run_id: str, body: dict) -> dict:
        has_text = "text" in body
        has_kind = "kind" in body
        if has_text == has_kind:
            raise ApiError(400, "invalid_request", "provide exactly one of text or kind")
        if has_kind:
            if body["kind"] != "wrap_up":
                raise ApiError(400, "invalid_request", "unknown kind")
            text = steering.WRAP_UP_TEXT
            kind = "wrap_up"
        else:
            try:
                text = steering.validate_text(body["text"])
            except steering.SteerError as exc:
                raise ApiError(400, exc.code, exc.message) from exc
            kind = "steer"

        obs = self._open_runs().get(run_id)
        if obs is None:
            raise ApiError(409, "not_open", "the run has ended")
        if obs.liveness.code != "process_identity_valid":
            raise ApiError(
                409, "not_verified", "the run's engine process is not verified alive"
            )
        transcript = obs.start.transcript if obs.start else (obs.transcript or "")
        try:
            path = steering.steer_path_for(transcript)
        except steering.SteerError as exc:
            raise ApiError(409, exc.code, exc.message) from exc

        with self._steers.lock_for(run_id):
            cached = self._narration_for(transcript)
            events_at_send = len(cached.parsed.events) if cached is not None else 0
            steering.append_steer(path, text)
            pending = steering.PendingSteer(
                id=secrets.token_hex(8),
                run_id=run_id,
                text=text,
                kind=kind,
                chars=len(text),
                sent_at=self.clock().isoformat(),
                events_at_send=events_at_send,
            )
            self._steers.add(pending)

        operator_log.append_action(
            self.paths.state_dir,
            self.locks.write,
            ts=pending.sent_at,
            action="steer",
            run_id=run_id,
            outcome="queued",
            kind=kind,
            chars=len(text),
        )
        return {
            "run_id": run_id,
            "id": pending.id,
            "state": "queued",
            "kind": kind,
            "chars": len(text),
            "sent_at": pending.sent_at,
        }

    def cancel_steer(self, run_id: str, body: dict) -> dict:
        steer_id = body.get("id")
        if not isinstance(steer_id, str) or not steer_id:
            raise ApiError(400, "invalid_request", "id is required")
        pending = self._steers.get(run_id, steer_id)
        if pending is None:
            raise ApiError(409, "not_pending", "no such pending steer")
        obs = self._open_runs().get(run_id)
        if obs is not None and obs.start is not None:
            transcript = obs.start.transcript
        else:
            transcript = self._record(run_id).transcript_artifact or ""
        with self._steers.lock_for(run_id):
            removed = steering.cancel_steer(
                Path(transcript + ".steer"), pending.text
            )
        if removed:
            self._steers.remove(run_id, steer_id)
            operator_log.append_action(
                self.paths.state_dir,
                self.locks.write,
                ts=self.clock().isoformat(),
                action="steer_cancel",
                run_id=run_id,
                outcome="cancelled",
            )
            return {"run_id": run_id, "id": steer_id, "state": "cancelled"}
        operator_log.append_action(
            self.paths.state_dir,
            self.locks.write,
            ts=self.clock().isoformat(),
            action="steer_cancel",
            run_id=run_id,
            outcome="not_pending",
        )
        raise ApiError(409, "not_pending", "steer already consumed")

    def kill(self, run_id: str, body: dict) -> dict:
        if body.get("confirm") is not True:
            raise ApiError(400, "confirm_required", "confirmation required")
        obs = self._open_runs().get(run_id)
        if obs is None:
            raise ApiError(409, "not_open", "the run has ended")
        start = obs.start
        if start is None or start.engine_pid is None:
            raise ApiError(
                409, "no_process_identity", "no verified engine process for this run"
            )
        if start.engine_pid == os.getpid():
            raise ApiError(409, "refused_self", "refusing to terminate this process")
        try:
            result = terminate_engine(start, psutil_module=self._psutil_module)
        except NotVerified as exc:
            operator_log.append_action(
                self.paths.state_dir,
                self.locks.write,
                ts=self.clock().isoformat(),
                action="kill",
                run_id=run_id,
                outcome="not_verified",
                engine_pid=start.engine_pid,
            )
            raise ApiError(
                409,
                "not_verified",
                "the process is not this run's engine; nothing was terminated",
            ) from exc
        except KillDenied as exc:
            operator_log.append_action(
                self.paths.state_dir,
                self.locks.write,
                ts=self.clock().isoformat(),
                action="kill",
                run_id=run_id,
                outcome="access_denied",
                engine_pid=start.engine_pid,
            )
            raise ApiError(409, "access_denied", str(exc)) from exc
        operator_log.append_action(
            self.paths.state_dir,
            self.locks.write,
            ts=self.clock().isoformat(),
            action="kill",
            run_id=run_id,
            outcome=result.outcome,
            engine_pid=result.engine_pid,
            children=result.children,
            survivors=result.survivors,
        )
        return {"run_id": run_id, **result.as_dict()}

    # --- mutations -------------------------------------------------------
    def transition_project(self, project_id: str, action: str, body: dict) -> dict:
        note = body.get("note")
        state = _MUTATION_STATES[action]
        project = self.registry.transition(project_id, state, note=note)
        return {"project_id": project.project_id, "state": project.state}

    def rename_project(self, project_id: str, body: dict) -> dict:
        name = body.get("display_name")
        if not isinstance(name, str):
            raise ApiError(400, "invalid_request", "display_name is required")
        project = self.registry.rename(project_id, name, note=body.get("note"))
        return {"project_id": project.project_id, "display_name": project.display_name}

    def dismiss_run(self, run_id: str, body: dict) -> dict:
        """Operator marks a stuck open run as interrupted (terminal, absorbing).

        Only an OPEN ("running") run can be dismissed; terminal runs are left
        as-is. This lets an operator immediately clear a run that is obviously
        dead without waiting for the automatic long-silence reap.
        """
        from dataclasses import replace

        from .ledger import sync_candidate
        from .models import RunCandidate, UsageEvidence, UsageTotals

        records, _ = load_ledger(self.paths.ledger)
        current = highest_valid_revisions(records).get(run_id)
        if current is None:
            raise ApiError(404, "not_found", "unknown run")

        open_runs = getattr(self.synchronizer, "open_runs", None)
        if isinstance(open_runs, Mapping):
            observation = open_runs.get(run_id)
            liveness = getattr(observation, "liveness", None)
            if getattr(liveness, "code", None) == "process_identity_valid":
                raise ApiError(
                    409,
                    "engine_alive",
                    "the run's engine process is verified alive; stop the "
                    "process instead of dismissing it",
                )

        if current.status != "running":
            return {"run_id": run_id, "status": current.status, "changed": False}

        totals = UsageTotals(
            tokens_sent=current.tokens_sent,
            tokens_returned=current.tokens_returned,
            cache_read=current.cache_read,
            cache_write=current.cache_write,
            sent_includes_cache_read=current.sent_includes_cache_read,
            response_count=0,
            tokens_sent_unknown_responses=0,
            tokens_returned_unknown_responses=0,
            cache_read_unknown_responses=0,
            cache_write_unknown_responses=0,
        )
        candidate = RunCandidate(
            run_id=current.run_id,
            project_id=current.project_id,
            timestamp=current.timestamp,
            ended_at=self.clock().isoformat(),
            status="interrupted",
            label=current.label,
            model=current.model,
            effort=current.effort,
            tool_mode=current.tool_mode,
            write_mode=current.write_mode,
            tool_calls=current.tool_calls,
            refused=current.refused,
            usage=UsageEvidence(
                totals=totals,
                source=current.usage_source,
                artifact=current.usage_artifact,
                diagnostics=(),
            ),
            transcript_artifact=current.transcript_artifact,
            provenance=tuple(current.provenance) + ("operator_dismiss",),
            lifecycle_inferred=True,
        )
        result = sync_candidate(
            self.paths.ledger,
            candidate,
            observed_at=self.clock().isoformat(),
            locks=self.locks,
        )
        return {"run_id": run_id, "status": result.record.status, "changed": result.appended}

    def pricing_preview(self, body: dict) -> dict:
        self._require_cost()
        runs, _, baseline, history = self._load_state()
        preview = preview_pricing(body, runs, baseline, history, now=self.clock())
        return _preview_payload(preview)

    def pricing_append(self, body: dict) -> dict:
        self._require_cost()
        runs, _, baseline, _ = self._load_state()
        version, preview = append_pricing_version(
            self.paths.pricing_history,
            body,
            runs,
            baseline,
            now=self.clock(),
            locks=self.locks,
        )
        return {
            "version_id": version.version_id,
            "model": version.model,
            "preview": _preview_payload(preview),
        }


def _preview_payload(preview) -> dict:
    return {
        "model": preview.model,
        "effective_at": preview.effective_at,
        "matching_runs": preview.matching_runs,
        "affected_runs": preview.affected_runs,
        "newly_priced_runs": preview.newly_priced_runs,
        "superseded_runs": preview.superseded_runs,
        "old_known_total": preview.old_known_total,
        "projected_known_total": preview.projected_known_total,
        "delta": preview.delta,
        "warnings": list(preview.warnings),
    }


def _query_from_params(raw: str) -> SnapshotQuery:
    params = parse_qs(raw, keep_blank_values=False)
    allowed = {
        "project", "model", "task", "state", "date_from", "date_to", "include_excluded",
        "tool",
    }
    unknown = set(params) - allowed
    if unknown:
        raise ApiError(400, "invalid_query", f"unknown query keys: {sorted(unknown)}")

    tools: list[str] = []
    for value in params.get("tool", []):
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if item not in _QUERY_TOOLS:
                raise ApiError(400, "invalid_query", "invalid tool")
            if item not in tools:
                tools.append(item)

    def _date(name: str) -> date | None:
        if name not in params:
            return None
        try:
            return date.fromisoformat(params[name][0])
        except ValueError as exc:
            raise ApiError(400, "invalid_query", f"invalid {name}") from exc

    return SnapshotQuery(
        project_ids=tuple(params.get("project", [])),
        models=tuple(params.get("model", [])),
        tasks=tuple(params.get("task", [])),
        states=tuple(params.get("state", [])),
        date_from=_date("date_from"),
        date_to=_date("date_to"),
        include_excluded=params.get("include_excluded", ["false"])[0] == "true",
        tools=tuple(tools),
    )


def _safe_static_name(path: str) -> str | None:
    prefix = "/static/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix) :]
    if "/" in name or "\\" in name or ".." in name or name not in _STATIC_FILES:
        return None
    return name


def make_handler(app: DashboardApp, csrf_token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # noqa: D401 - silence logging
            return

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # A cancelled client cannot receive a response; never retry it.
                self.close_connection = True

        def _write_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, error: ApiError) -> None:
            self._write_json(
                error.status, {"error": {"code": error.code, "message": error.message}}
            )

        def _host_ok(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            host = hosts[0]
            if app.public_origin is not None:
                try:
                    return normalize_public_origin(f"https://{host}") == app.public_origin
                except ValueError:
                    return False
            expected = f"127.0.0.1:{self.server.server_address[1]}"
            return host == expected

        def _get_host_ok(self) -> bool:
            if app.public_origin is not None:
                return self._host_ok()
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            host = hosts[0]
            port = self.server.server_address[1]
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _origin_ok(self) -> bool:
            origins = self.headers.get_all("Origin", [])
            if len(origins) != 1:
                return False
            origin = origins[0]
            if app.public_origin is not None:
                try:
                    return normalize_public_origin(origin) == app.public_origin
                except ValueError:
                    return False
            expected = f"http://127.0.0.1:{self.server.server_address[1]}"
            return origin == expected

        def _read_body(self) -> dict:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip() != "application/json":
                raise ApiError(415, "unsupported_media_type", "JSON body required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError(400, "invalid_length", "invalid Content-Length") from exc
            if length < 0:
                raise ApiError(400, "invalid_length", "invalid Content-Length")
            if length > _MAX_BODY:
                raise ApiError(413, "payload_too_large", "request body too large")
            raw = self.rfile.read(length)
            try:
                value = strict_json_loads(raw)
            except ValueError as exc:
                raise ApiError(
                    400, "invalid_json", "Request body must be strict JSON"
                ) from exc
            if not isinstance(value, dict):
                raise ApiError(400, "invalid_json", "Request body must be a JSON object")
            return value

        def _serve_static(self, name: str) -> None:
            web_dir = app._web_dir()
            file_path = web_dir / name
            try:
                data = file_path.read_bytes()
            except OSError:
                self._error(ApiError(404, "not_found", "not found"))
                return
            if name == "index.html":
                data = data.replace(
                    b"__AGENT_CONTROL_CSRF__", csrf_token.encode("utf-8")
                )
            self.send_response(200)
            self.send_header("Content-Type", _STATIC_FILES[name])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if name == "index.html":
                self.send_header("Content-Security-Policy", _PAGE_CSP)
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                if not self._get_host_ok():
                    raise ApiError(400, "bad_host", "invalid Host header")
                if path in ("/", "/index.html"):
                    self._serve_static("index.html")
                    return
                static_name = _safe_static_name(path)
                if path.startswith("/static/"):
                    if static_name is None:
                        self._error(ApiError(404, "not_found", "not found"))
                        return
                    self._serve_static(static_name)
                    return
                if path == "/api/data":
                    query = _query_from_params(parsed.query)
                    self._write_json(200, app.snapshot(query))
                    return
                if path == "/api/projects":
                    self._write_json(200, app.projects())
                    return
                if path == "/api/diagnostics":
                    self._write_json(200, app.diagnostics())
                    return
                if path == "/api/activity":
                    params = parse_qs(parsed.query)
                    run_ids = params.get("run_id", [])
                    if len(run_ids) != 1 or not run_ids[0]:
                        raise ApiError(400, "invalid_query", "run_id is required")
                    self._write_json(200, app.activity(run_ids[0]))
                    return
                if path == "/api/diagnosis":
                    run_ids = parse_qs(parsed.query).get("run_id", [])
                    if len(run_ids) != 1 or not run_ids[0]:
                        raise ApiError(400, "invalid_query", "run_id is required")
                    self._write_json(200, app.run_diagnosis(run_ids[0]))
                    return
                if path == "/api/narration":
                    if self.headers.get("X-AgentControl-CSRF", "") != csrf_token:
                        raise ApiError(403, "bad_csrf", "invalid CSRF token")
                    params = parse_qs(parsed.query)
                    run_ids = params.get("run_id", [])
                    if len(run_ids) != 1 or not run_ids[0]:
                        raise ApiError(400, "invalid_query", "run_id is required")
                    afters = params.get("after", ["0"])
                    if len(afters) != 1 or not (
                        afters[0].isascii() and afters[0].isdigit()
                    ):
                        raise ApiError(400, "invalid_query", "invalid after")
                    self._write_json(
                        200, app.narration(run_ids[0], int(afters[0]))
                    )
                    return
                self._error(ApiError(404, "not_found", "not found"))
            except ApiError as error:
                self._error(error)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                if not self._host_ok():
                    raise ApiError(400, "bad_host", "invalid Host header")
                if not self._origin_ok():
                    raise ApiError(403, "bad_origin", "invalid Origin header")
                if self.headers.get("X-AgentControl-CSRF", "") != csrf_token:
                    raise ApiError(403, "bad_csrf", "invalid CSRF token")
                body = self._read_body()
                payload = self._dispatch_post(path, body)
                self._write_json(200, payload)
            except ApiError as error:
                self._error(error)
            except ValueError as error:
                self._error(ApiError(400, "invalid_request", str(error)))

        def _dispatch_post(self, path: str, body: dict) -> object:
            if path == "/api/pricing/preview":
                return app.pricing_preview(body)
            if path == "/api/pricing/history":
                return app.pricing_append(body)
            if path.startswith("/api/projects/"):
                parts = path[len("/api/projects/") :].split("/")
                if len(parts) == 2:
                    project_id, action = parts
                    if action in _MUTATION_STATES:
                        return app.transition_project(project_id, action, body)
                    if action == "rename":
                        return app.rename_project(project_id, body)
            if path.startswith("/api/runs/"):
                rest = path[len("/api/runs/") :].split("/")
                run_id = urllib.parse.unquote(rest[0])
                tail = rest[1:]
                if tail == ["dismiss"]:
                    return app.dismiss_run(run_id, body)
                if tail == ["steer"]:
                    return app.steer(run_id, body)
                if tail == ["steer", "cancel"]:
                    return app.cancel_steer(run_id, body)
                if tail == ["kill"]:
                    return app.kill(run_id, body)
            raise ApiError(404, "not_found", "not found")

        def _method_not_allowed(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802
            self._method_not_allowed()

    return Handler


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    """Threading server that refuses to share a port with another process.

    On Windows the default ``allow_reuse_address`` lets two sockets bind the
    same port via ``SO_REUSEADDR``; we disable that and set
    ``SO_EXCLUSIVEADDRUSE`` before binding so a second serve fails at bind.
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:  # noqa: D102 - stdlib hook
        if os.name == "nt":
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


def create_server(
    app: DashboardApp, *, port: int = 8787, csrf_token: str | None = None
) -> ThreadingHTTPServer:
    token = csrf_token or secrets.token_urlsafe(32)
    handler = make_handler(app, token)
    httpd = SingleInstanceHTTPServer(("127.0.0.1", port), handler)
    httpd.csrf_token = token  # type: ignore[attr-defined]
    return httpd
