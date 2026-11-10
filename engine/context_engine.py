"""Request-scoped context policy glue for delegate.py: cold CLI resolution and active scope."""
import contextlib
import contextvars
import math

_INTS = {"context_window_tokens": "invalid_window_tokens",
         "context_reply_tokens": "invalid_reply_tokens",
         "context_safety_tokens": "invalid_safety_tokens"}
_SCOPE = contextvars.ContextVar("agentcontrol_context_scope", default=None)


class Refused(ValueError):
    """Fixed refusal code only; never carries argument values."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def select(args, policy_mod, *, frozen, oneshot, model_window=1_000_000,
           profile_supplier=None):
    """Validate cold flags before looking up local, optional provider evidence."""
    try:
        return _select(args, policy_mod, frozen=frozen, oneshot=oneshot,
                       model_window=model_window, profile_supplier=profile_supplier)
    except policy_mod.ContextPolicyError as e:
        raise Refused(e.args[0]) from None


def _select(args, policy_mod, *, frozen, oneshot, model_window, profile_supplier):
    err = policy_mod.ContextPolicyError
    vals = {k: getattr(args, k, None) for k in
            ("context_policy", "auto_compact", "compact_fraction", *_INTS)}
    if not frozen:  # frozen runs reject any supplied flag first, whatever its value
        for key, code in _INTS.items():
            if vals[key] is not None:
                try:
                    vals[key] = int(vals[key], 10)
                except (TypeError, ValueError):
                    raise err(code) from None
        if vals["compact_fraction"] is not None:
            try:
                vals["compact_fraction"] = float(vals["compact_fraction"])
            except (TypeError, ValueError):
                raise err("invalid_compact_fraction") from None
            if not math.isfinite(vals["compact_fraction"]):
                raise err("invalid_compact_fraction")
    sel = policy_mod.resolve_policy(vals, frozen=frozen, oneshot=oneshot,
                                    model_window=model_window)
    if sel.policy is None or profile_supplier is None:
        return sel
    profile = dict(profile_supplier())
    configured = profile["configured_operating_window"]
    profile["configured_operating_window"] = configured if configured is not None else sel.policy.window_tokens
    profile["window_provenance"] = (
        "operator_override" if vals["context_window_tokens"] is not None else
        "operating_profile" if configured is not None else "existing_configuration")
    sel = policy_mod.resolve_policy(vals, oneshot=oneshot,
                                    model_window=profile["configured_operating_window"])
    return policy_mod.Selection(sel.policy, sel.provenance, sel.explicit_fields,
                                tuple(profile.items()))


class Scope:
    """One request's policy, meter and last numeric estimate (no payload retained)."""

    def __init__(self, policy, meter):
        self.policy, self.meter, self.last_estimate = policy, meter, None

    def estimate(self, payload):
        self.last_estimate = self.meter.estimate(payload)
        return self.last_estimate


def current():
    return _SCOPE.get()


@contextlib.contextmanager
def scope(ctx):
    token = _SCOPE.set(ctx)
    try:
        yield ctx
    finally:
        _SCOPE.reset(token)


_RUNTIME = contextvars.ContextVar("agentcontrol_context_runtime", default=None)


def current_runtime():
    return _RUNTIME.get()


@contextlib.contextmanager
def runtime_scope(runtime):
    token = _RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME.reset(token)


class ContextEngineRuntimeScope(Scope):
    def __init__(self, runtime, phase):
        self.runtime, self.phase = runtime, phase
        super().__init__(runtime.policy, runtime.meter_for(phase))
        self.identity = None

    def estimate(self, payload):
        model = payload["model"]
        route = "responses" if "input" in payload else "chat"
        self.identity = model, route
        self.meter = self.runtime.meter_for(self.phase, model, route)
        return super().estimate(payload)

    def observe(self, raw_usage):
        estimate, self.last_estimate = self.last_estimate, None
        if estimate is not None:
            model, route = self.identity
            self.runtime.observe_response(
                estimate, raw_usage, phase=self.phase, model=model, route=route)


@contextlib.contextmanager
def request_scope(phase):
    runtime = current_runtime()
    if runtime is None:
        yield None
    else:
        with scope(ContextEngineRuntimeScope(runtime, phase)) as ctx:
            yield ctx


def semantic_bindings(run, *, task, steers, steer_path, policy, protocol, review,
                      correction, review_attempted):
    """Bind semantic owners, not accounting, clocks or compaction artifacts."""
    import dataclasses
    import hashlib
    import json
    import os

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    pending_steer = None
    if os.path.exists(steer_path):
        with open(steer_path, encoding="utf-8") as source:
            pending_steer = source.read().strip() or None
    return {"candidate_seq": run.candidate_seq,
            "pending_sha256": digest(run.pending),
            "evidence": dataclasses.asdict(run.evidence.snapshot(tuple(run.pending))),
            "unknown": run.outcome_unknown, "command": run._last_cmd_info,
            "task_sha256": digest(task), "steers_sha256": digest(steers),
            "pending_steer_sha256": digest(pending_steer),
            "policy_sha256": digest(policy), "skills": run.caps.loaded_skills(),
            "advertised": sorted(run.caps.advertised),
            "receipts": run.context_runtime.metadata()["receipts"],
            "artifacts": sorted(k for k in run.caps.artifacts
                                if not k.endswith((".ctx", ".ctxjson"))),
            "protocol": protocol.metadata() if protocol is not None else None,
            "anomalies": run.trajectory.to_metadata() if run.trajectory is not None else [],
            "review": review.to_metadata() if review is not None else None,
            "correction_attempted": correction, "review_attempted": review_attempted}


class DiscardTranscript:
    """Maintenance streams must not publish summary/reasoning content."""

    def write(self, *args):
        pass

    append = write
    flush = write


class AutomaticCompaction:
    """One run's callback adapter; failed maintenance never replaces the active view."""

    def __init__(self, runtime, component, *, prepare, render, call, persist, charge,
                 can_call, sync_steers, image_type):
        self.rt, self.cc = runtime, component
        self.prepare, self.render, self.call = prepare, render, call
        self._ids = {}
        self.charge, self.sync_steers, self.image_type = charge, sync_steers, image_type
        self.compactor = component.Compactor(
            runtime.policy, measure=self.measure, render_worker=self.render_original,
            call_summary=self.summary, persist=persist, commit=self.commit,
            get_bindings=runtime.bindings, can_call=can_call)
        self.last = None

    def original_ids(self, messages):
        import copy
        messages = copy.deepcopy(messages)
        for message in messages:
            for call in message.get("tool_calls", []):
                call["id"] = self._ids.get(call["id"], call["id"])
            if message.get("role") == "tool":
                key = message.get("tool_call_id")
                message["tool_call_id"] = self._ids.get(key, key)
        return messages

    def render_original(self, messages):
        return self.render(self.original_ids(messages))

    def commit(self, bindings, candidate, artifact, *, lineage):
        # restores tool IDs only; positional lineage is forwarded unchanged
        return self.rt.commit_view(bindings, self.original_ids(candidate), artifact,
                                   lineage=lineage)

    def measure(self, packet):
        # Compactor's summary trial contains messages only; measure its actual wire form too.
        phase = "worker"
        if "model" not in packet:
            packet = self.prepare(packet["messages"], min(self.rt.policy.reply_tokens,
                                                       self.cc.MAX_REPLY_TOKENS))
            phase = "compaction"
        route = "responses" if "input" in packet else "chat"
        return self.rt.prepare_request(packet, phase=phase, route=route)

    def summary(self, request):
        before = self.rt.bindings()
        self.sync_steers()
        if self.rt.bindings() != before:
            raise ValueError("stale_bindings")
        response, error = None, None
        try:
            response = self.call(request)
        except SystemExit as exc:
            error = exc
            raise ValueError("gateway_error") from None
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.charge(response, error)
            self.sync_steers()
        if not isinstance(response, dict) or "_transient" in response:
            raise ValueError("gateway_error")
        choices = response.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        tokens = self.rt._tokens
        usage = response.get("usage")
        try:
            input_tokens = tokens.input_tokens_from_usage(usage, route=request.route)
        except ValueError:
            input_tokens = None
        # Never adopt an unknown or malformed output count as a measured zero.
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens")) \
            if isinstance(usage, dict) else None
        if type(output_tokens) is not int or output_tokens < 0:
            output_tokens = None
        return self.cc.SummaryReply(
            message.get("content"), input_tokens, output_tokens,
            request.model, request.route, request.effort,
            tool_calls=tuple(message.get("tool_calls") or ()))

    def attempt(self):
        self.sync_steers()
        data = self.rt.snapshot_data()
        # API IDs are batch-local. The pure validator requires snapshot-global IDs.
        self._ids = {}
        ids = [c["id"] for m in data["messages"] for c in m.get("tool_calls", [])]
        if len(ids) != len(set(ids)):
            for gi, group in enumerate(data["groups"]):
                local = {}
                for index in group:
                    message = data["messages"][index]
                    for ci, call in enumerate(message.get("tool_calls", [])):
                        key = f"context_group_{gi}_slot_{ci}"
                        local[call["id"]] = key
                        self._ids[key] = call["id"]
                        call["id"] = key
                    if message.get("role") == "tool":
                        message["tool_call_id"] = local.get(message["tool_call_id"],
                                                            message["tool_call_id"])
        try:
            snap = self.cc.snapshot(**data, image_type=self.image_type)
        except self.cc.CompactionError as exc:
            self.last = self.cc.Outcome(exc.code)
            return self.last
        # Trigger early when the mandatory pins plus a summary reply need headroom.
        try:
            estimate = self.measure(self.render_original(data["messages"]))
            tokens = estimate.tokens
            if type(tokens) is not int or tokens < 0:  # unknown is unmeasurable, never zero
                self.last = self.cc.Outcome("unmeasurable")
                return self.last
            early = tokens + min(self.rt.policy.reply_tokens,
                                 self.cc.MAX_REPLY_TOKENS) > self.rt.policy.input_limit_tokens
            self.last = self.compactor.attempt(snap, force=early)
            return self.last
        finally:
            self._ids = {}
