#!/usr/bin/env python3
"""
CATSEC x AgentDojo -- Gemma 4 runner (self-hosted, OpenAI-compatible server).

Sibling of run_catsec_openai.py. Same architecture, same CLI surface, same
kernel.py / planner.py underneath (unchanged -- those files are
provider-agnostic). Only the model-calling layer differs.

WHY THIS FILE EXISTS
=====================
The open-weight leg of the model comparison. Earlier attempts went through
hosted gateways (Groq free tier hit a tokens-per-day ceiling mid-run; Vertex
MaaS inherited Dynamic Shared Quota's unobservable throughput). Renting a GPU
and serving the weights ourselves removes both problems: throughput is a
property of hardware we control, and there is no per-day token ceiling to
collide with two-thirds of the way through an arm.

Gemma 4 is Apache 2.0, so unlike every other leg the exact weights, the
serving stack and the decode settings are all reproducible by a reviewer.
That is worth saying explicitly in the paper.

WHAT'S DIFFERENT FROM THE OPENAI RUNNER
========================================
* Client: openai.OpenAI(base_url=<your vLLM endpoint>, api_key=<dummy>).
  vLLM speaks the OpenAI wire protocol, so _MeteredCompletions, MeteredClient,
  OpenAILLM and the whole probe/JSONL layer transfer unchanged.
* --base-url is REQUIRED (env: GEMMA_BASE_URL). There is no default endpoint
  to fall back to and silently hitting api.openai.com with a Gemma model id
  would be a confusing failure.
* THINKING IS OFF BY DEFAULT. Gemma 4 ships configurable thinking modes. The
  GPT-4o-mini and Claude Haiku legs both run non-reasoning, so this leg must
  too or the cross-model comparison is not like-for-like. Implemented via
  extra_body={"chat_template_kwargs": {"enable_thinking": <bool>}}, which vLLM
  forwards into the chat template. --thinking turns it on deliberately; the
  resolved value is written into the manifest either way so a run can never
  be ambiguous after the fact.
* MODEL_NAMES registration is REQUIRED here, exactly as it was for Gemini and
  for the bare gpt-4o-mini id: the important_instructions attack family looks
  the model's display name up by substring against that table, and an
  unregistered name raises ValueError outright. Served names for local models
  are arbitrary (often a full HF path with a slash), so this cannot be left
  to chance.
* Usage: vLLM populates prompt_tokens/completion_tokens like OpenAI does, so
  UsageMeter is unchanged. tokens_thoughts stays 0 for schema parity.
* Rate limiting defaults to OFF. A dedicated GPU has no RPM policy -- the
  limiter is kept only so the code path stays identical across runners, and
  because a saturated server can return 429 under vLLM's own queue limits.

Everything else -- the plan/execute split, the provenance lattice, the
per-pair JSONL logging, --resume, --all-pairs, --k1-check, --no-injection,
--trial-timeout -- is identical to the OpenAI runner, so results from the two
are directly comparable in analyze.py.

SERVER SIDE (not this file's job, but this run depends on it):
    vllm serve google/gemma-4-31b-it \
        --served-model-name gemma-4-31b-it \
        --enable-auto-tool-choice --tool-call-parser pythonic \
        --max-model-len 32768
The tool-choice flags matter for the BASELINE arm only -- that arm is
AgentDojo's stock function-calling loop, and without them every baseline
trial degrades into a no-tool-call chat and its utility number is worthless.
The CATSEC arm declares no tools at all, so it is unaffected. --preflight
checks this before you spend GPU hours finding out.

RUN ORDER: see README_gemma.md.
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

# --- AgentDojo forward-ref fix (added for the Gemma leg) ------------------
# AgentDojo's TaskResults declares a forward reference to FunctionCall that
# the package never resolves itself. Under pydantic >=2.11 this raises
# PydanticUserError on FIRST INSTANTIATION -- i.e. at the end of the first
# no-defense trial, several minutes into a run, not at import time.
# model_rebuild() resolves forward refs from the CALLING frame's namespace,
# so FunctionCall must be imported here, in this module, for it to work.
from agentdojo.functions_runtime import FunctionCall  # noqa: F401
from agentdojo.benchmark import TaskResults as _TaskResults

_TaskResults.model_rebuild()
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------- #
# Retry policy -- shared design with the Vertex runner
# --------------------------------------------------------------------------- #
def _is_rate_limited(exc: Exception) -> bool:
    """True for 429s / server-saturation. The openai SDK raises RateLimitError
    for a 429 regardless of who served it, and vLLM returns 503 when its
    request queue is full -- which is the same situation (back off and retry),
    so both are treated alike. String checks are a fallback for exceptions
    that lose their type through a retry wrapper."""
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return True
        if isinstance(exc, openai.APIStatusError) and \
                getattr(exc, "status_code", None) in (429, 503):
            return True
    except ImportError:
        pass
    s = str(exc).lower()
    return "429" in s or "503" in s or "rate_limit" in s or "overloaded" in s


def _backoff_seconds(attempt: int, rate_limited: bool) -> float:
    """attempt is 0-indexed. On a self-hosted server a rejection means the
    request queue is momentarily full, which clears in seconds -- not an
    exhausted shared quota pool, so backoff stays short. Jittered so several
    failed calls don't resynchronise and retry in lockstep."""
    if rate_limited:
        base = min(4 * (2 ** attempt), 60)
    else:
        base = min(2 ** attempt, 20)
    return base * (0.75 + random.random() * 0.5)


class RateLimiter:
    """Proactive pacing with adaptive backpressure. Off by default here
    (base_interval=0) -- a dedicated GPU enforces no RPM policy, so there is
    nothing to pace against. Kept identical to the other runners so the code
    path does not diverge, and as a safety net if you are sharing the endpoint
    or vLLM starts shedding load."""

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
    """Gemma 4 has configurable thinking modes, so unlike gpt-4o-mini there IS
    something to pin here -- and pinning it is a comparability requirement,
    not a tuning choice: the GPT-4o-mini and Claude Haiku legs are both
    non-reasoning, so leaving Gemma to think would confound any cross-model
    difference with a difference in inference budget.

    `enable_thinking` is threaded through as extra_body.chat_template_kwargs,
    which is how vLLM passes flags into a model's chat template. It is sent
    ALWAYS, explicitly, in both states -- never omitted -- so the request
    itself records the decision rather than inheriting whatever the template's
    default happens to be in the serving stack's current version.

    NOTE: if your vLLM build rejects the kwarg (older builds error on unknown
    chat_template_kwargs), --preflight will surface it as a clear failure
    before the run rather than mid-arm."""
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 2048
    enable_thinking: bool = False

    def kwargs(self) -> dict:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
            },
        }


# --------------------------------------------------------------------------- #
# Usage metering -- wraps the OpenAI client so BOTH arms are measured the same
# --------------------------------------------------------------------------- #
@dataclass
class UsageMeter:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thoughts: int = 0     # stays 0: thinking is disabled by default,
    # and vLLM reports no separate thought-token
    # count. Kept for schema parity with the
    # other runners so analyze.py needs no change.
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
    arms are metered and paced identically.

    IT ALSO INJECTS DECODE DEFAULTS, which the OpenAI runner did not need to
    do. AgentDojo's OpenAILLM builds its own request and only ever passes
    `temperature`: it knows nothing about max_tokens and nothing about Gemma's
    chat-template kwargs. On a hosted model that was harmless. Here it is not,
    for two separate reasons:

      * enable_thinking would never reach the baseline arm, so CATSEC would run
        non-reasoning while the baseline ran at the template's default. Any
        measured difference between the arms would then be partly a difference
        in inference budget -- exactly the confound this leg exists to avoid.
      * max_tokens would sit at the server's default while CATSEC uses 2048,
        which is the same silent-truncation trap already hit on the Claude leg.

    Injection is defaults-only: an explicitly-passed value always wins, so the
    CATSEC arm (which sends the full decode kwargs) is untouched, and
    extra_body is merged key-by-key rather than replaced."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter,
                 defaults: dict | None = None):
        self._inner = inner
        self._meter = meter
        self._limiter = limiter
        self._defaults = defaults or {}

    def _apply_defaults(self, kwargs: dict) -> dict:
        for k, v in self._defaults.items():
            if k == "extra_body":
                merged = dict(v)
                merged.update(kwargs.get("extra_body") or {})
                # chat_template_kwargs needs a second-level merge, otherwise a
                # caller setting any one template flag would drop enable_thinking.
                ctk = dict((v or {}).get("chat_template_kwargs") or {})
                ctk.update(((kwargs.get("extra_body") or {})
                            .get("chat_template_kwargs")) or {})
                if ctk:
                    merged["chat_template_kwargs"] = ctk
                kwargs["extra_body"] = merged
            else:
                kwargs.setdefault(k, v)
        return kwargs

    def create(self, *args, **kwargs):
        kwargs = self._apply_defaults(kwargs)
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
    def __init__(self, inner, meter, limiter, defaults=None):
        self._inner = inner
        self.completions = _MeteredCompletions(
            inner.completions, meter, limiter, defaults)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredClient:
    """Wraps an openai.OpenAI client pointed at the vLLM endpoint.
    .chat.completions.create is the only surface CatsecPipeline and OpenAILLM
    both go through, so wrapping it here covers both arms identically."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter,
                 defaults: dict | None = None):
        self._inner = inner
        self.limiter = limiter
        self.chat = _MeteredChat(inner.chat, meter, limiter, defaults)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- #
# The CATSEC pipeline element
# --------------------------------------------------------------------------- #
def make_catsec_pipeline(client, model_name: str, decode: DecodeConfig,
                         policy: str = "refuse", verbose: bool = False,
                         ablation: str = "none", allow_repair: bool = False,
                         probe: TrialProbe | None = None):
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
                runtime, env, ablation=ablation)
            tdesc = tool_descriptions(runtime)

            try:
                cached = self._plan_cache.get(query)
                if cached is not None:
                    plan = cached
                else:
                    plan = build_plan(self._plan_llm, query, tdesc, specs,
                                      allow_repair=allow_repair)
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
def ensure_gemma_env(args) -> str:
    """Resolve the vLLM endpoint. Unlike the hosted runners there is no key to
    chase -- what must not be left implicit is the ENDPOINT, because an
    unset base_url silently falls back to api.openai.com and the failure that
    produces ("model not found") looks nothing like the real cause.

    The API key is a placeholder: vLLM ignores it unless the server was
    started with --api-key, in which case set GEMMA_API_KEY to match."""
    load_dotenv()
    base = args.base_url or os.environ.get("GEMMA_BASE_URL")
    if not base and sys.stdin.isatty():
        # Only prompt when there is a human at the terminal. Under nohup /
        # systemd / a detached overnight run, stdin is not a tty and input()
        # dies with EOFError several seconds in -- after which the run looks
        # like it started and then vanished.
        base = input("GEMMA_BASE_URL (e.g. http://127.0.0.1:8000/v1): ").strip()
    if not base:
        raise SystemExit(
            "A base URL is required: pass --base-url or set GEMMA_BASE_URL "
            "in your .env (e.g. http://127.0.0.1:8000/v1).")
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        # vLLM mounts the OpenAI-compatible routes under /v1. Forgetting it is
        # the single most common setup mistake and produces a 404 that reads
        # like the server is down.
        base += "/v1"
    os.environ.setdefault("GEMMA_API_KEY", "EMPTY")
    return base


def preflight(client, model_id: str, decode: DecodeConfig) -> None:
    """Fail fast, on the ground, before spending GPU hours.

    Checks, in order: (1) the server is reachable and serving the model id we
    are about to name -- a mismatch between --model and --served-model-name is
    silent otherwise; (2) a plain completion works with the thinking flag we
    intend to use, so a chat-template kwarg your build rejects surfaces here
    rather than on trial 1; (3) the server will actually emit a tool call,
    which the BASELINE arm depends on entirely. (3) is a warning, not a fatal
    error, so a CATSEC-only run is still possible on a server without the
    tool-choice flags -- but running the baseline arm in that state would
    produce a meaningless utility number, so it says so loudly."""
    print("  preflight: listing served models...", flush=True)
    try:
        served = [m.id for m in client.models.list().data]
    except Exception as e:
        raise SystemExit(
            f"  preflight FAILED: cannot reach the server. {type(e).__name__}: {e}\n"
            f"  Check the endpoint is up and --base-url is right.")
    print(f"  preflight: server reports {served}")
    if model_id not in served:
        raise SystemExit(
            f"  preflight FAILED: --model '{model_id}' is not served. "
            f"Available: {served}. Either pass one of those, or restart vllm "
            f"with --served-model-name {model_id}.")

    print(f"  preflight: test completion (enable_thinking="
          f"{decode.enable_thinking})...", flush=True)
    try:
        r = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "system", "content": "Reply with exactly: OK"},
                      {"role": "user", "content": "ping"}],
            **{**decode.kwargs(), "max_tokens": 16})
        print(f"  preflight: OK -- {(r.choices[0].message.content or '')[:40]!r}")
    except Exception as e:
        raise SystemExit(
            f"  preflight FAILED on a plain completion: {type(e).__name__}: {e}\n"
            f"  If this mentions chat_template_kwargs, your vLLM build does not "
            f"accept enable_thinking; upgrade vLLM, or serve a template that "
            f"takes it. Do NOT work around it by dropping the flag -- an "
            f"unpinned thinking mode makes this leg incomparable to the others.")

    print("  preflight: tool-call capability (baseline arm)...", flush=True)
    probe_tool = [{
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Get the account balance.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }}]
    try:
        r = client.chat.completions.create(
            model=model_id, tools=probe_tool, tool_choice="auto",
            messages=[{"role": "user", "content": "What is my balance?"}],
            **{**decode.kwargs(), "max_tokens": 128})
        if r.choices[0].message.tool_calls:
            print("  preflight: OK -- server emitted a tool call.")
        else:
            print("  preflight WARNING: server accepted tools but emitted NO "
                  "tool call. The baseline arm needs working function calling; "
                  "restart vllm with --enable-auto-tool-choice and the right "
                  "--tool-call-parser, or run --defenses catsec only.")
    except Exception as e:
        print(f"  preflight WARNING: tool call rejected: {type(e).__name__}: "
              f"{str(e)[:200]}\n"
              f"  Baseline-arm utility will be meaningless without this.")


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
        "provider": "gemma-vllm",
        # Recorded explicitly: an open-weight leg is only reproducible if the
        # endpoint, the served id and the thinking state are all on the record.
        "base_url": extra.get("base_url"),
        "enable_thinking": decode.enable_thinking,
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
    # frozen at package release and knows nothing about Gemma 4, so an
    # unregistered id raises ValueError outright. Same fix, same reason, as the
    # Gemini and bare-gpt-4o-mini registrations: get the ATTACK STRENGTH right.
    #
    # Served names for a self-hosted model are whatever --served-model-name
    # said, and often a full HF path with a slash, so this matters more here
    # than on any hosted leg. Our pipeline name is f"{model_id}-catsec", so the
    # id registered here IS a substring of it and the lookup succeeds.
    if model_id not in MODEL_NAMES:
        low = model_id.lower()
        if "gemma" in low:
            display = "Gemma"
        elif low.startswith("gpt-4"):
            display = "GPT-4"
        elif low.startswith("gpt-3.5"):
            display = "GPT-3.5"
        else:
            display = "AI assistant"
        MODEL_NAMES[model_id] = display

    CatsecCls = make_catsec_pipeline(
        client, model_id, decode, args.policy, args.verbose_interp,
        ablation=args.ablation, allow_repair=args.plan_repair, probe=probe)
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
        "provider": "gemma-vllm",
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
    p.add_argument("--model", default="gemma-4-31b-it",
                   help="The id the server answers to, i.e. vllm's "
                        "--served-model-name (NOT necessarily the HF path). "
                        "--preflight verifies this against /v1/models.")
    p.add_argument("--base-url", default=None,
                   help="vLLM OpenAI-compatible endpoint, e.g. "
                        "http://127.0.0.1:8000/v1. Falls back to "
                        "GEMMA_BASE_URL. Required -- there is no default.")
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
    p.add_argument("--thinking", action="store_true",
                   help="Turn Gemma 4's thinking mode ON. OFF by default and "
                        "should stay off for the paper's main comparison: the "
                        "GPT-4o-mini and Claude Haiku legs are non-reasoning, "
                        "so enabling it here confounds a model difference with "
                        "an inference-budget difference. Useful only as a "
                        "deliberate, separately-reported ablation.")
    p.add_argument("--preflight", action="store_true",
                   help="Check the server, the model id, the thinking flag and "
                        "tool-calling, then exit. Run this first, every time.")

    p.add_argument("--logdir", default="./catsec_runs_gemma")
    p.add_argument("--jsonl", default=None)
    p.add_argument("--out", default="catsec_results_gemma.csv")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--request-timeout", type=float, default=180.0,
                   help="Per-HTTP-request timeout in seconds. The SDK's own "
                        "retries are disabled so that _MeteredCompletions sees "
                        "every failure and the adaptive limiter can react -- "
                        "hidden SDK retries would be unmetered and unpaced.")
    p.add_argument("--min-call-interval", type=float, default=0.0,
                   help="Seconds between every model call. 0 by default -- a "
                        "dedicated GPU has no RPM policy to respect. Raise "
                        "only if a shared/saturated server starts returning "
                        "429s or 503s.")
    p.add_argument("--max-call-interval", type=float, default=30.0)
    p.add_argument("--trial-timeout", type=float, default=240.0,
                   help="Per-pair time budget in seconds (0 = unlimited). "
                        "Higher than the OpenAI runner's 120s: a locally "
                        "served 31b is slower per token than a hosted "
                        "gpt-4o-mini, and a timeout logs as an ERROR, which "
                        "silently deletes real trials from the denominator. "
                        "Measure your own per-pair wall time on the smoke "
                        "test and set this to roughly 3x it.")
    p.add_argument("--verbose-interp", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    base_url = ensure_gemma_env(args)

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
        max_tokens=args.max_tokens, enable_thinking=args.thinking,
    )
    meter = UsageMeter()
    probe = TrialProbe()
    limiter = RateLimiter(base_interval=args.min_call_interval,
                          max_interval=args.max_call_interval)

    raw_client = openai.OpenAI(
        base_url=base_url,
        api_key=os.environ.get("GEMMA_API_KEY", "EMPTY"),
        # A local server has no hosted-provider retry budget behind it, and a
        # 31b on one GPU can legitimately take a while on a long plan prompt.
        timeout=args.request_timeout, max_retries=0)
    # decode.kwargs() as DEFAULTS, so the baseline arm inherits max_tokens and
    # enable_thinking even though OpenAILLM never passes them. See
    # _MeteredCompletions for why this is a comparability requirement.
    client = MeteredClient(raw_client, meter, limiter,
                           defaults=decode.kwargs())

    if args.preflight:
        print(f"[Gemma] preflight against {base_url}")
        preflight(client, args.model, decode)
        print("  preflight complete.")
        return

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
    it_ids = list(suite.injection_tasks.keys())
    run_id = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H%M%SZ-") + uuid.uuid4().hex[:4]

    print(f"[Gemma] Suite {args.suite} {args.version} | model {args.model} | "
          f"policy {args.policy} | ablation {args.ablation}")
    print(f"  endpoint: {base_url} | thinking: "
          f"{'ON (ablation)' if decode.enable_thinking else 'OFF (matches other legs)'}")
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
                       {"n_user_tasks": len(ut_ids),
                        "n_injection_tasks": len(it_ids),
                        "base_url": base_url})
    writer = JsonlWriter(jsonl_path)
    done = load_resume_index(jsonl_path) if (
        args.resume and jsonl_path) else set()
    if done:
        print(f"  resume: {len(done)} completed trials found, will be skipped")

    if args.smoke_test_only:
        pipe = build("catsec")
        atk = load_attack(args.attack, suite, pipe)
        print("\n--- Smoke test: ONE real pair through CATSEC (Gemma) ---")
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
        print(f"Next: python analyze.py --jsonl '{jsonl_path}'")


if __name__ == "__main__":
    main()
