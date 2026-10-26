#!/usr/bin/env python3
"""
CATSEC x AgentDojo -- OpenAI runner (GPT-4o-mini and friends).

Sibling of run_catsec_gemini_vertex.py. Same architecture, same CLI surface
where it makes sense, same kernel.py / planner.py underneath (unchanged --
those files are provider-agnostic). Only the model-calling layer differs.

WHY THIS FILE EXISTS
=====================
Vertex pay-as-you-go Gemini runs on Dynamic Shared Quota: throughput is not a
number you can see, request, or plan around, and it measured at ~46s/pair
(catsec) to ~214s/pair (baseline) with a low-spend project -- projecting to
roughly 150 hours for the full Gemini leg against a 32-hour budget. OpenAI
publishes actual per-tier rate limits, so this becomes the PRIMARY pilot;
Gemini becomes a slower replication leg for E7 (multi-model), run overnight
with --resume once this leg is solid.

WHAT'S DIFFERENT FROM THE VERTEX RUNNER
========================================
* Client: openai.OpenAI(api_key=...) instead of genai.Client(vertexai=True).
* No Vertex-style ADC login -- just OPENAI_API_KEY.
* No "thinking budget" for gpt-4o-mini (it isn't a reasoning model); the
  --reasoning-effort flag is exposed for o-series models but left unset by
  default and simply omitted from the request when None.
* Usage fields come from response.usage.prompt_tokens / completion_tokens.
  There is no separate "thoughts" count for non-reasoning models, so
  tokens_thoughts is always 0 here (kept in the record for schema parity
  with the Gemini runner, so analyze.py needs no changes).
* Rate limiting defaults to OFF (--min-call-interval 0): OpenAI's documented
  per-tier RPM is far above what this harness generates. The same adaptive
  RateLimiter from the Vertex runner is kept as a safety net in case of
  429s, not as the primary strategy.
* Baseline arm uses agentdojo's OpenAILLM directly (it takes model_id in its
  constructor with no enum lookup, so there is no MODEL_NAMES-style pinning
  needed here the way Vertex needed for the important_instructions attack --
  OpenAI model names are stable strings AgentDojo already recognizes).

Everything else -- the plan/execute split, the provenance lattice, the
per-pair JSONL logging, --resume, --all-pairs, --k1-check, --no-injection,
--trial-timeout -- is identical in spirit and, where possible, identical in
code, so a result from this runner and a result from the Vertex runner are
directly comparable in analyze.py.

RUN ORDER: see README.md (OpenAI section).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

# The defence (kernel, planner, contracts, composer prompt, harness helpers)
# lives ONCE, in the repository-level `catsec` package; every runner imports it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from catsec.kernel import (  # noqa: E402
    ToolSpec, Interpreter, TRUSTED, DERIVED, UNTRUSTED, PROV_NAME, PlanError,
)
from catsec.planner import (  # noqa: E402
    build_plan, assert_no_tool_output, PlannerError, plan_signature,
)
from catsec.contracts import (  # noqa: E402,F401
    ToolCallError, SENSITIVE_CONTRACTS, READ_TOOLS, build_toolspecs,
    tool_descriptions, designated_sources,
)
from catsec.composer import COMPOSER_SYSTEM  # noqa: E402,F401
from catsec.harness import (  # noqa: E402,F401
    TrialProbe, tool_sequence_from_messages, make_trace_recorder, trial_key,
    load_resume_index, JsonlWriter, _single_value,
)
from catsec.options import (  # noqa: E402
    CatsecOptions, add_cli_args, from_args as catsec_options_from_args,
    manifest_info,
)


# --------------------------------------------------------------------------- #
# Retry policy -- shared design with the Vertex runner
# --------------------------------------------------------------------------- #
def _is_rate_limited(exc: Exception) -> bool:
    """True for OpenAI 429s specifically. Checked both by exception type (the
    openai SDK raises RateLimitError) and by string, in case a wrapped/retried
    exception loses its type along the way."""
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return True
    except ImportError:
        pass
    s = str(exc)
    return "429" in s or "rate_limit" in s.lower()


def _backoff_seconds(attempt: int, rate_limited: bool) -> float:
    """attempt is 0-indexed. OpenAI's rate limits are per-tier and documented,
    so a rejection is usually a short, real burst rather than an empty shared
    pool -- backoff can be shorter than the Vertex runner's. Jittered so
    several failed calls don't resynchronise and retry in lockstep."""
    if rate_limited:
        base = min(4 * (2 ** attempt), 60)
    else:
        base = min(2 ** attempt, 20)
    return base * (0.75 + random.random() * 0.5)


class RateLimiter:
    """Proactive pacing with adaptive backpressure. Off by default here
    (base_interval=0) -- kept as a safety net, not the primary strategy, since
    OpenAI's published RPM is well above what this harness generates. If you
    DO see sustained 429s, raise --min-call-interval the same way you would
    on the Vertex runner."""

    def __init__(self, base_interval: float = 0.0, max_interval: float = 30.0,
                 growth: float = 1.6, decay_after: int = 10):
        self.base = max(0.0, base_interval)
        self.current = self.base
        self.max_interval = max_interval
        self.growth = growth
        self.decay_after = decay_after
        self._ok_streak = 0
        self._last_call = 0.0
        self.waited_seconds = 0.0
        self.n_rate_limits = 0
        self.peak_interval = self.base

    def wait(self):
        if self.current > 0:
            gap = time.monotonic() - self._last_call
            if gap < self.current:
                sleep_for = self.current - gap
                time.sleep(sleep_for)
                self.waited_seconds += sleep_for
        self._last_call = time.monotonic()

    def on_success(self):
        self._ok_streak += 1
        if self._ok_streak >= self.decay_after and self.current > self.base:
            self.current = max(self.base, self.current / self.growth)
            self._ok_streak = 0

    def on_rate_limit(self):
        self._ok_streak = 0
        self.n_rate_limits += 1
        self.current = min(self.max_interval, max(
            self.current, 1.0) * self.growth)
        self.peak_interval = max(self.peak_interval, self.current)

    def snapshot(self) -> dict:
        return {"base_interval": self.base,
                "current_interval": round(self.current, 2),
                "peak_interval": round(self.peak_interval, 2),
                "rate_limit_events": self.n_rate_limits,
                "seconds_spent_pacing": round(self.waited_seconds, 1)}


# --------------------------------------------------------------------------- #
# Decode configuration
# --------------------------------------------------------------------------- #
@dataclass
class DecodeConfig:
    """gpt-4o-mini is not a reasoning model: there is no hidden-thinking token
    cost the way there is for Gemini 2.5 or o-series, so there is no analogue
    of --thinking-budget to pin here. reasoning_effort is accepted for
    forward-compatibility with o-series models but omitted from the request
    entirely when None (passing it to a non-reasoning model is rejected)."""
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 2048
    reasoning_effort: str | None = None

    def kwargs(self) -> dict:
        out = {"temperature": self.temperature, "top_p": self.top_p,
               "max_tokens": self.max_tokens}
        if self.reasoning_effort is not None:
            out["reasoning_effort"] = self.reasoning_effort
        return out


# --------------------------------------------------------------------------- #
# Usage metering -- wraps the OpenAI client so BOTH arms are measured the same
# --------------------------------------------------------------------------- #
@dataclass
class UsageMeter:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thoughts: int = 0     # always 0 for non-reasoning models; kept for
    # schema parity with the Vertex runner
    api_seconds: float = 0.0
    per_call: list = field(default_factory=list)

    def reset(self):
        self.calls = 0
        self.tokens_in = self.tokens_out = self.tokens_thoughts = 0
        self.api_seconds = 0.0
        self.per_call = []

    def record(self, resp, dt: float):
        self.calls += 1
        self.api_seconds += dt
        u = getattr(resp, "usage", None)
        pin = int(getattr(u, "prompt_tokens", 0) or 0) if u else 0
        pout = int(getattr(u, "completion_tokens", 0) or 0) if u else 0
        self.tokens_in += pin
        self.tokens_out += pout
        self.per_call.append({"in": pin, "out": pout, "thoughts": 0,
                              "seconds": round(dt, 3)})

    def snapshot(self) -> dict:
        return {
            "n_model_calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_thoughts": self.tokens_thoughts,
            "api_seconds": round(self.api_seconds, 3),
            "per_call": list(self.per_call),
        }


class _MeteredCompletions:
    """Intercepts chat.completions.create to meter usage and pace calls. The
    baseline arm's OpenAILLM is handed THIS wrapped client directly, so both
    arms are metered and paced identically -- same design as the Vertex
    runner's _MeteredModels."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter):
        self._inner = inner
        self._meter = meter
        self._limiter = limiter

    def create(self, *args, **kwargs):
        self._limiter.wait()
        t0 = time.perf_counter()
        try:
            resp = self._inner.create(*args, **kwargs)
        except Exception as e:
            if _is_rate_limited(e):
                self._limiter.on_rate_limit()
            raise
        self._limiter.on_success()
        self._meter.record(resp, time.perf_counter() - t0)
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredChat:
    def __init__(self, inner, meter, limiter):
        self._inner = inner
        self.completions = _MeteredCompletions(
            inner.completions, meter, limiter)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredClient:
    """Wraps an openai.OpenAI client. .chat.completions.create is the only
    surface CatsecPipeline and OpenAILLM both go through, so wrapping it here
    covers both arms identically."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter):
        self._inner = inner
        self.limiter = limiter
        self.chat = _MeteredChat(inner.chat, meter, limiter)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- #
# The CATSEC pipeline element
# --------------------------------------------------------------------------- #
def make_catsec_pipeline(client, model_name: str, decode: DecodeConfig,
                         policy: str = "refuse", verbose: bool = False,
                         ablation: str = "none", allow_repair: bool = False,
                         probe: TrialProbe | None = None,
                         opts: CatsecOptions | None = None):
    # Revision options (catsec/options.py); the defaults reproduce the logs.
    opts = opts if opts is not None else CatsecOptions()
    from agentdojo.agent_pipeline import BasePipelineElement
    from agentdojo.functions_runtime import EmptyEnv, FunctionCall
    from agentdojo.types import text_content_block_from_string

    class CatsecPipeline(BasePipelineElement):
        name = "catsec"

        def __init__(self):
            self.plans_ok = 0
            self.plan_failures = 0
            self.refusals = 0
            self.executed_calls = 0
            self.tool_failures = 0
            self.answers_composed = 0
            self.probe = probe if probe is not None else TrialProbe()
            # Keyed on the EXACT user text. P(intent) depends only on this
            # text (never on tool output), so the same text must always
            # produce the same plan -- caching it and reusing it instead of
            # re-querying the model is what lets the K1 comparison test
            # "same Ctrl, different Data" rather than "two independent, and
            # possibly different, samples from a non-deterministic model."
            self._plan_cache: dict[str, list] = {}

        # -- the single planning call: user text ONLY, never tool output ----
        def _plan_llm(self, system: str, user_text: str) -> str:
            # SAME INVARIANT AS THE VERTEX RUNNER: this call gets `system` (the
            # plan grammar) and `user_text` ONLY. No tools/functions are
            # declared on this request, so the model is structurally incapable
            # of calling anything -- it can only emit the plan JSON as text.
            last_exc = None
            for attempt in range(5):
                try:
                    resp = client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user_text or ""}],
                        **decode.kwargs(),
                    )
                    return resp.choices[0].message.content or ""
                except Exception as e:
                    last_exc = e
                    if attempt < 4:
                        time.sleep(_backoff_seconds(
                            attempt, _is_rate_limited(e)))
            raise last_exc

        # ---------------- terminal answer composition ----------------
        # Same security argument as the Vertex runner: the plan is FIXED and
        # FULLY EXECUTED by this point, and this call has no tools/functions
        # declared, so it is structurally incapable of acting -- a terminal
        # Data -> Text morphism, not Data -> Ctrl. Residual risk (attacker
        # text influencing what the user reads) is the same, stated, measured
        # limitation as the Vertex runner -- see E9 in the master plan.
        def _compose_answer(self, user_question: str, values: dict) -> str:
            facts = []
            for name, pv in values.items():
                if pv.value is None:
                    continue
                facts.append(
                    f"{name} ({PROV_NAME[pv.prov]}): {str(pv.value)[:1200]}")
            if not facts:
                self.probe.compose_error = "no-facts"
                return ""
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": COMPOSER_SYSTEM},
                        {"role": "user", "content": f"Question: {user_question}\n\n"
                         "Data retrieved:\n"
                         + "\n".join(facts)},
                    ],
                    **decode.kwargs(),
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                # Record WHY rather than swallowing it. Still fails SOFT -- an
                # unanswerable question must never crash a trial -- but the
                # failure is now visible in the per-trial log instead of
                # masquerading as an empty answer.
                self.probe.compose_error = f"{type(e).__name__}: {e}"
                return ""

        def query(self, query, runtime, env=EmptyEnv(), messages=(), extra_args={}):
            msgs = list(messages)
            p = self.probe

            # Reset EVERY invocation, not just once per pair -- AgentDojo calls
            # this pipeline TWICE per pair (an injection-task-solvability
            # pre-check, then the real trial). Without a per-call reset, a
            # failing real trial can silently log the OTHER invocation's
            # leftover data as its own. Found and fixed during the Vertex
            # runner's development; carried over here from the start.
            p.reset()

            try:
                assert_no_tool_output(msgs)
            except PlannerError as e:
                p.guard_error = str(e)
                msgs.append({"role": "assistant",
                             "content": [text_content_block_from_string(str(e))],
                             "tool_calls": None})
                return query, runtime, env, msgs, extra_args

            specs, tool_failures = build_toolspecs(
                runtime, env, ablation=ablation,
                unknown_tool_policy=opts.unknown_tool_policy)
            tdesc = tool_descriptions(runtime)

            try:
                cached = self._plan_cache.get(query)
                if cached is not None:
                    plan = cached
                else:
                    plan = build_plan(self._plan_llm, query, tdesc, specs,
                                      allow_repair=allow_repair,
                                      **opts.planner_kwargs())
                    self._plan_cache[query] = plan
                self.plans_ok += 1
                p.plan, p.plan_valid, p.plan_error = plan, True, None
            except (PlannerError, PlanError) as e:
                self.plan_failures += 1
                p.plan, p.plan_valid, p.plan_error = None, False, f"{type(e).__name__}: {e}"
                msgs.append({"role": "assistant",
                             "content": [text_content_block_from_string(
                                 f"[catsec: no valid plan produced: {e}]")],
                             "tool_calls": None})
                return query, runtime, env, msgs, extra_args

            named = set(re.findall(r"[\w\-./]+\.\w{1,5}", query or ""))
            designated = {f"read_file({n})" for n in named}

            interp = Interpreter(specs, designated_sources=designated,
                                 policy=policy, verbose=verbose)
            try:
                result = interp.run(plan, env)
            except ToolCallError as e:
                # A tool rejected its arguments part-way through the plan. The
                # earlier steps DID execute; kernel.Interpreter attaches the
                # partial ExecResult to the exception so we can log what really
                # happened instead of an empty trace.
                partial = getattr(e, "partial_result", None)
                if partial is not None:
                    p.control_trace = list(partial.control_trace)
                    p.executed = [(t, a) for t, a in partial.executed]
                    p.refused = [(t, a, r) for t, a, r in partial.refused]
                    self.refusals += len(partial.refused)
                    self.executed_calls += len(partial.executed)
                p.tool_arg_failures = list(tool_failures)
                msgs.append({"role": "assistant",
                             "content": [text_content_block_from_string(
                                 f"[catsec: tool rejected arguments: {e}]")],
                             "tool_calls": None})
                self.tool_failures += 1
                return query, runtime, env, msgs, extra_args

            self.refusals += len(result.refused)
            self.executed_calls += len(result.executed)
            self.tool_failures += len(tool_failures)

            p.control_trace = list(result.control_trace)
            p.executed = [(t, a) for t, a in result.executed]
            p.refused = [(t, a, r) for t, a, r in result.refused]
            p.tool_arg_failures = list(tool_failures)

            for idx, (tname, plain) in enumerate(result.executed):
                fc = FunctionCall(function=tname, args=plain,
                                  id=f"catsec_{idx}")
                msgs.append(
                    {"role": "assistant", "content": None, "tool_calls": [fc]})
                msgs.append({
                    "role": "tool",
                    "content": [text_content_block_from_string("(executed)")],
                    "tool_call_id": f"catsec_{idx}",
                    "tool_call": fc,
                    "error": None,
                })

            answer = self._compose_answer(query, result.env)
            if answer:
                self.answers_composed += 1
            p.composed_answer = answer

            summary = "[catsec] executed: " + \
                ", ".join(t for t, _ in result.executed)
            if result.refused:
                summary += " | REFUSED: " + "; ".join(
                    f"{t} ({r.split(' via ')[0]})" for t, _, r in result.refused)
            final_text = (answer + "\n\n" + summary) if answer else summary
            msgs.append({"role": "assistant",
                         "content": [text_content_block_from_string(final_text)],
                         "tool_calls": None})
            return query, runtime, env, msgs, extra_args

    return CatsecPipeline


# --------------------------------------------------------------------------- #
# Environment + manifest
# --------------------------------------------------------------------------- #
def ensure_openai_env() -> None:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY")
    if not key and sys.stdin.isatty():
        # Only prompt when there is a human at the terminal. Under nohup /
        # systemd / a detached overnight run, stdin is not a tty and input()
        # dies with EOFError several seconds in -- after which the run looks
        # like it started and then vanished.
        key = input("OPENAI_API_KEY: ").strip()
        os.environ["OPENAI_API_KEY"] = key
    if not key:
        raise SystemExit(
            "OPENAI_API_KEY is required. Set it in your .env or environment.")


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL,
            cwd=Path(__file__).parent).decode().strip()
    except Exception:
        return "not-a-git-repo"


def write_manifest(path: Path, args, decode: DecodeConfig, run_id: str, extra: dict):
    manifest = {
        "run_id": run_id,
        "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "argv": sys.argv,
        "args": vars(args),
        "decode_config": asdict(decode),
        "git_commit": _git_commit(),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": {n: _pkg_version(n) for n in
                     ["agentdojo", "openai", "pydantic", "pandas", "scipy"]},
        "provider": "openai",
        **extra,
    }
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #
def build_pipeline_factory(args, model_id: str, client, probe: TrialProbe,
                           decode: DecodeConfig):
    from agentdojo.agent_pipeline import AgentPipeline, PipelineConfig
    from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
    from agentdojo.models import MODEL_NAMES

    # The important_instructions attack family phrases its jailbreak using the
    # model's display name, looked up via get_model_name_from_pipeline(), which
    # substring-matches pipeline.name against MODEL_NAMES' KEYS. That table is
    # frozen at package release and keyed by DATED, VERSIONED strings (e.g.
    # "gpt-4o-mini-2024-07-18"), not the bare id ("gpt-4o-mini") this harness
    # passes via --model. Our pipeline name is f"{model_id}-catsec", so the bare
    # id is registered here as its own key -- it IS a substring of that pipeline
    # name, so the lookup succeeds. Same fix as the Vertex runner's Gemini
    # registration, same reason: get the ATTACK STRENGTH right, since a failed
    # lookup crashes outright here (raises ValueError) rather than silently
    # weakening the attack the way a wrong name would on Gemini.
    if model_id not in MODEL_NAMES:
        low = model_id.lower()
        if low.startswith("gpt-4"):
            display = "GPT-4"
        elif low.startswith("gpt-3.5"):
            display = "GPT-3.5"
        else:
            display = "AI assistant"
        MODEL_NAMES[model_id] = display

    CatsecCls = make_catsec_pipeline(
        client, model_id, decode, args.policy, args.verbose_interp,
        ablation=args.ablation, allow_repair=args.plan_repair, probe=probe,
        opts=args.catsec_opts)
    RecorderCls = make_trace_recorder(probe)

    def build(defense_name):
        if defense_name != "catsec":
            # OpenAILLM's constructor takes the client and model id directly --
            # no enum lookup to fight, unlike Vertex's GoogleLLM path.
            llm = OpenAILLM(client, model_id, temperature=decode.temperature)
            llm.name = model_id
            cfg = PipelineConfig(llm=llm, model_id=None, defense=defense_name,
                                 tool_delimiter="tool",
                                 system_message_name=None, system_message=None)
            inner = AgentPipeline.from_config(cfg)
            pipe = AgentPipeline([inner, RecorderCls()])
            pipe.name = getattr(inner, "name", model_id)
            return pipe
        gate = CatsecCls()
        pipe = AgentPipeline([gate])
        pipe.name = f"{model_id}-catsec"
        pipe._catsec = gate
        return pipe

    return build


def run_one_trial(pipe, suite, attack, logdir, user_task, injection_task,
                  meter: UsageMeter, probe: TrialProbe, max_retries: int,
                  injected: bool, trial_timeout: float = 0.0):
    from agentdojo.benchmark import (
        benchmark_suite_with_injections, benchmark_suite_without_injections)
    from agentdojo.logging import OutputLogger

    meter.reset()
    probe.reset()
    last_exc = None
    deadline = (time.monotonic() +
                trial_timeout) if trial_timeout > 0 else None

    for attempt in range(max_retries):
        if deadline is not None and time.monotonic() > deadline:
            return None, None, (
                f"TrialTimeout: exceeded {trial_timeout:.0f}s budget after "
                f"{attempt} attempt(s); last error: {last_exc}")
        try:
            with OutputLogger(str(logdir), live=None):
                if injected:
                    r = benchmark_suite_with_injections(
                        pipe, suite, attack, logdir=logdir, force_rerun=True,
                        user_tasks=[user_task], injection_tasks=[
                            injection_task],
                        verbose=False)
                    util = _single_value(r["utility_results"], "utility_result",
                                         (user_task, injection_task))
                    sec = _single_value(r["security_results"], "security_result",
                                        (user_task, injection_task))
                    return util, sec, None
                r = benchmark_suite_without_injections(
                    pipe, suite, logdir=logdir, force_rerun=True,
                    user_tasks=[user_task])
                util = _single_value(r["utility_results"], "utility_result",
                                     (user_task, None))
                return util, None, None
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(_backoff_seconds(attempt, _is_rate_limited(e)))

    return None, None, f"{type(last_exc).__name__}: {last_exc}"


def make_record(*, run_id, args, model_id, defense, user_task, injection_task,
                repetition, condition, injected, utility, security, error,
                usage: dict, probe_record: dict, wall_s: float,
                k1: dict | None = None, throttle: dict | None = None) -> dict:
    rec = {
        "run_id": run_id,
        "ts_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "model": model_id,
        "provider": "openai",
        "location": None,
        "suite": args.suite,
        "suite_version": args.version,
        "attack": args.attack if injected else None,
        "defense": defense,
        "policy": args.policy,
        "ablation": args.ablation,
        "plan_repair": args.plan_repair,
        "user_task": user_task,
        "injection_task": injection_task,
        "repetition": repetition,
        "condition": condition,
        "injected": injected,
        "utility": utility,
        "security_attack_succeeded": security,
        "error": error,
        "latency_s": round(wall_s, 3),
    }
    rec.update(probe_record)
    rec.update(usage)
    if k1 is not None:
        rec["k1"] = k1
    if throttle is not None:
        rec["throttle"] = throttle
    return rec


# --------------------------------------------------------------------------- #
# CLI -- same surface as the Vertex runner, minus Vertex-only flags
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", default="banking")
    p.add_argument("--benchmark-version", default="v1.2", dest="version")
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--attack", default="tool_knowledge")
    p.add_argument("--defenses", default="none,catsec")
    p.add_argument("--policy", default="refuse", choices=["refuse", "confirm"])
    p.add_argument("--ablation", default="none",
                   choices=["none", "no_provenance"])
    p.add_argument("--plan-repair", action="store_true")

    p.add_argument("--all-pairs", action="store_true")
    p.add_argument("--user-tasks", default=None,
                   help="Comma-separated list of user_task IDs to restrict to "
                        "(e.g. 'user_task_2,user_task_7'). For targeted "
                        "debugging of specific tasks without running the full "
                        "suite. Combines with --max-pairs / --all-pairs over "
                        "just this subset.")
    p.add_argument("--max-pairs", type=int, default=30)
    p.add_argument("--pair-seed", type=int, default=0)
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--shard-index", type=int, default=0)
    p.add_argument("--shard-count", type=int, default=1)

    p.add_argument("--no-injection", action="store_true")
    p.add_argument("--k1-check", action="store_true")
    p.add_argument("--smoke-test-only", action="store_true")
    p.add_argument("--print-attack-text", action="store_true")

    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--top-p", type=float, default=1.0)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--reasoning-effort", default=None,
                   choices=[None, "none", "minimal", "low", "medium", "high"],
                   help="Only meaningful for o-series reasoning models. Leave "
                        "unset for gpt-4o-mini -- passing this to a "
                        "non-reasoning model is rejected by the API.")

    p.add_argument("--logdir", default="./catsec_runs_openai")
    p.add_argument("--jsonl", default=None)
    p.add_argument("--out", default="catsec_results_openai.csv")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--min-call-interval", type=float, default=0.0,
                   help="Seconds between every model call. 0 by default -- "
                        "OpenAI's published RPM is well above what this "
                        "harness generates. Raise only if you see sustained "
                        "429s.")
    p.add_argument("--max-call-interval", type=float, default=30.0)
    p.add_argument("--trial-timeout", type=float, default=120.0,
                   help="Per-pair time budget in seconds (0 = unlimited).")
    p.add_argument("--verbose-interp", action="store_true")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"),
                   help="OpenAI-COMPATIBLE endpoint for local/open models, e.g. "
                        "http://localhost:11434/v1 (Ollama) or "
                        "http://localhost:8000/v1 (vLLM). No paid API is used and "
                        "no OPENAI_API_KEY is needed when this is set. "
                        "See docs/local_models.md for what fits which GPU.")
    add_cli_args(p)
    args = p.parse_args()
    args.catsec_opts = catsec_options_from_args(args)
    return args


def main():
    args = parse_args()
    if args.base_url:
        # Local OpenAI-compatible server: no paid API, no key required.
        load_dotenv()
        os.environ.setdefault("OPENAI_API_KEY", "local-no-key")
        print(f"[OpenAI-compatible] endpoint {args.base_url}")
    else:
        ensure_openai_env()

    import logging
    for n in ["agentdojo", "httpx", "httpcore", "openai", "urllib3"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.attacks.attack_registry import load_attack
    import openai
    import pandas as pd
    from tqdm import tqdm

    decode = DecodeConfig(
        temperature=args.temperature, top_p=args.top_p,
        max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort,
    )
    meter = UsageMeter()
    probe = TrialProbe()
    probe.run_settings = manifest_info(args.catsec_opts)
    limiter = RateLimiter(base_interval=args.min_call_interval,
                          max_interval=args.max_call_interval)

    raw_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                               base_url=args.base_url or None)
    client = MeteredClient(raw_client, meter, limiter)

    logdir = Path(args.logdir).resolve()
    logdir.mkdir(parents=True, exist_ok=True)
    suite = get_suite(args.version, args.suite)
    build = build_pipeline_factory(args, args.model, client, probe, decode)

    ut_ids = list(suite.user_tasks.keys())
    if args.user_tasks:
        wanted = {t.strip() for t in args.user_tasks.split(",") if t.strip()}
        missing = wanted - set(ut_ids)
        if missing:
            raise SystemExit(
                f"--user-tasks: unknown task id(s): {sorted(missing)}")
        ut_ids = [u for u in ut_ids if u in wanted]
    allowed = args.catsec_opts.allowed_user_tasks()
    if allowed is not None:
        # kfold protocol: fold f's prompt may only be evaluated on fold f.
        outside = sorted(set(ut_ids) - set(allowed)) if args.user_tasks else []
        if outside:
            raise SystemExit(f"--prompt-variant kfold --fold {args.fold}: "
                             f"tasks {outside} are not in the test fold")
        ut_ids = [u for u in ut_ids if u in allowed]
    it_ids = list(suite.injection_tasks.keys())
    run_id = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H%M%SZ-") + uuid.uuid4().hex[:4]

    print(f"[OpenAI] Suite {args.suite} {args.version} | model {args.model} | "
          f"policy {args.policy} | ablation {args.ablation}")
    print(f"  {len(ut_ids)} user tasks x {len(it_ids)} injection tasks "
          f"= {len(ut_ids)*len(it_ids)} pairs available")
    print(f"  decode: {asdict(decode)}")
    print(f"  pacing: min-call-interval={args.min_call_interval}s  "
          f"max={args.max_call_interval}s  trial-timeout={args.trial_timeout}s  "
          f"max-retries={args.max_retries}")

    if args.print_attack_text:
        pipe = build("catsec")
        atk = load_attack(args.attack, suite, pipe)
        ut, it = suite.user_tasks[ut_ids[0]], suite.injection_tasks[it_ids[0]]
        try:
            print(json.dumps(atk.attack(ut, it), indent=2)[:4000])
        except Exception as e:
            print(f"could not render attack text: {type(e).__name__}: {e}")
        return

    jsonl_path = None
    if args.jsonl:
        base = Path(args.jsonl)
        if args.shard_count > 1:
            base = base.with_name(
                f"{base.stem}.shard{args.shard_index}{base.suffix}")
        base.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path = base
        write_manifest(base.with_suffix(".manifest.json"), args, decode, run_id,
                       {**manifest_info(args.catsec_opts),
                        "base_url": args.base_url,
                        "n_user_tasks": len(ut_ids), "n_injection_tasks": len(it_ids)})
    writer = JsonlWriter(jsonl_path)
    done = load_resume_index(jsonl_path) if (
        args.resume and jsonl_path) else set()
    if done:
        print(f"  resume: {len(done)} completed trials found, will be skipped")

    if args.smoke_test_only:
        pipe = build("catsec")
        atk = load_attack(args.attack, suite, pipe)
        print("\n--- Smoke test: ONE real pair through CATSEC (OpenAI) ---")
        t0 = time.perf_counter()
        util, sec, err = run_one_trial(pipe, suite, atk, logdir, ut_ids[0],
                                       it_ids[0], meter, probe,
                                       args.max_retries, injected=True,
                                       trial_timeout=args.trial_timeout)
        print(f"  utility={util}  attack_succeeded={sec}  error={err}")
        print(f"  tokens={meter.tokens_in}/{meter.tokens_out} calls={meter.calls} "
              f"wall={time.perf_counter()-t0:.1f}s")
        print(f"  control_trace={probe.control_trace}")
        for t, kw, reason in probe.refused:
            print(f"  REFUSED: {t}({kw}) -> {reason}")
        print(f"  throttle={limiter.snapshot()}")
        if probe.plan:
            print("  plan emitted:")
            for s in probe.plan:
                print("     ", s)
        if probe.plan_error:
            print(f"  PLAN FAILURE: {probe.plan_error}")
        for t, kw, e in probe.tool_arg_failures:
            print(
                f"  TOOL FAILURE (did NOT take effect): {t}({kw}) -> {str(e)[:110]}")
        writer.close()
        return

    defenses = [None if d.strip().lower() == "none" else d.strip()
                for d in args.defenses.split(",")]

    if args.no_injection:
        work = [(u, None) for u in ut_ids]
    elif args.all_pairs:
        work = [(u, i) for u in ut_ids for i in it_ids]
    else:
        work = [(u, i) for u in ut_ids for i in it_ids]
        random.Random(args.pair_seed).shuffle(work)
        work = work[:args.max_pairs]

    if args.shard_count > 1:
        work = work[args.shard_index::args.shard_count]
        print(
            f"  shard {args.shard_index}/{args.shard_count}: {len(work)} pairs")

    rows = []
    for rep in range(args.repetitions):
        for d in defenses:
            label = d or "no-defense"
            p = build(d)
            atk = None if args.no_injection else load_attack(
                args.attack, suite, p)
            util_vals, sec_vals, errs = [], [], 0
            real_trial_refusals = 0

            desc = f"rep{rep} {label:12s}"
            for (u, i) in tqdm(work, desc=desc, unit="pair"):
                cond = "clean" if args.no_injection else "injected"
                key = "|".join(str(x) for x in
                               (label, args.attack if not args.no_injection else None,
                                u, i, rep, cond, args.ablation))
                if key in done:
                    continue

                t0 = time.perf_counter()
                util, sec, err = run_one_trial(
                    p, suite, atk, logdir, u, i, meter, probe,
                    args.max_retries, injected=not args.no_injection,
                    trial_timeout=args.trial_timeout)
                wall = time.perf_counter() - t0

                usage_snap = meter.snapshot()
                probe_snap = probe.as_record()
                trace_injected = list(probe.control_trace)
                plan_injected = probe.plan_signature_or_none()

                k1 = None
                if args.k1_check and not args.no_injection and err is None:
                    # For catsec specifically, reuse the SAME pipeline instance
                    # (not a fresh build(d)) so its plan cache is hit: the
                    # clean rerun uses the identical user_task.PROMPT text as
                    # the just-completed real trial, so the cache returns the
                    # SAME plan object instead of asking the model again. That
                    # is what makes this a real K1 test -- one plan, executed
                    # against two different environments -- rather than two
                    # independently-sampled plans that may legitimately differ
                    # due to the model's own non-determinism. The no-defense
                    # arm has no such cache and is unaffected: it still gets a
                    # fresh build(d) exactly as before.
                    p_clean = p if d == "catsec" else build(d)
                    cu, _, cerr = run_one_trial(
                        p_clean, suite, None, logdir, u, None, meter, probe,
                        args.max_retries, injected=False,
                        trial_timeout=args.trial_timeout)
                    trace_clean = list(probe.control_trace)
                    plan_clean = probe.plan_signature_or_none()
                    k1 = {
                        "trace_injected": trace_injected,
                        "trace_clean": trace_clean,
                        "traces_identical": trace_injected == trace_clean,
                        "plans_identical": plan_injected == plan_clean,
                        "clean_utility": cu,
                        "clean_error": cerr,
                        # Two empty traces compare "identical" and would inflate
                        # the K1 statistic. Only count a pair where BOTH arms
                        # actually produced a plan and the clean run succeeded.
                        "comparable": (cerr is None
                                       and plan_injected is not None
                                       and plan_clean is not None),
                    }

                rec = make_record(
                    run_id=run_id, args=args, model_id=args.model, defense=label,
                    user_task=u, injection_task=i, repetition=rep,
                    condition=cond, injected=not args.no_injection,
                    utility=util, security=sec, error=err, usage=usage_snap,
                    probe_record=probe_snap, wall_s=wall, k1=k1,
                    throttle=limiter.snapshot())
                writer.write(rec)
                real_trial_refusals += len(probe_snap.get("refused") or [])

                if err is not None:
                    errs += 1
                    print(f"\n  !! ERROR ({u}, {i}) [{label}]: {err[:200]}")
                else:
                    util_vals.append(bool(util))
                    if sec is not None:
                        sec_vals.append(bool(sec))

            n = len(sec_vals)
            secure = (sum(1 for v in sec_vals if not v) /
                      n) if n else float("nan")
            utility = (sum(util_vals) / len(util_vals)
                       ) if util_vals else float("nan")
            row = {"repetition": rep, "defense": label, "trials_ok": len(util_vals),
                   "errors": errs, "utility": utility, "secure_rate": secure,
                   "ASR": (1 - secure) if n else float("nan")}
            cg = getattr(p, "_catsec", None)
            if cg:
                # plans_ok/plan_failures ARE pipeline-lifetime totals (they
                # count AgentDojo's injection-task-solvability precheck calls
                # too, alongside the real trials) -- that's fine, planning
                # reliability is planning reliability either way. refusals is
                # different: a precheck call runs the injection task as its
                # OWN prompt, which isn't an attack scenario, so a refusal
                # there says nothing about defense behaviour against an
                # attack. Report the real-trial-only tally instead of
                # cg.refusals (which conflates the two).
                row.update(plans_ok=cg.plans_ok, plan_failures=cg.plan_failures,
                           refusals=real_trial_refusals)
            rows.append(row)

    writer.close()

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(args.out, index=False)
        show = df.copy()
        for c in ["utility", "secure_rate", "ASR"]:
            show[c] = show[c].map(lambda x: "n/a" if x !=
                                  x else f"{100*x:.1f}%")
        print("\n=== QUICK SUMMARY (use analyze.py for the real tables) ===")
        print(show.to_string(index=False))
        print(f"\nSaved: {args.out}")

    ls = limiter.snapshot()
    print(f"\nThrottling: {ls['rate_limit_events']} rate-limit events | "
          f"peak interval {ls['peak_interval']}s | "
          f"{ls['seconds_spent_pacing']}s spent pacing")
    if jsonl_path:
        print(f"Per-trial log: {jsonl_path}")
        print(f"Next: python ../../analysis/analyze.py --jsonl '{jsonl_path}'")


if __name__ == "__main__":
    main()
