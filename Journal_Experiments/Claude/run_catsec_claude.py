#!/usr/bin/env python3
"""
CATSEC x AgentDojo -- Anthropic runner (Claude Haiku 4.5 and friends).

Sibling of run_catsec_openai.py and run_catsec_gemini_vertex.py. Same
architecture, same CLI surface, same kernel.py / planner.py underneath
(UNCHANGED -- those files are provider-agnostic, which is what makes a
third provider a runner-only change). Only the model-calling layer differs.

WHAT'S DIFFERENT FROM THE OPENAI RUNNER
========================================
* Client: TWO clients, not one, and this is the important structural
  difference. The OpenAI runner wraps a single object because
  `chat.completions.create` is the one surface BOTH arms go through. That
  does not hold here:
    - the CATSEC arm (planner + composer) uses the SYNC client's
      `messages.create`;
    - the baseline arm goes through agentdojo's AnthropicLLM, which calls
      `client.messages.stream(...)` on an ASYNC client and awaits
      `stream.get_final_message()`.
  Both wrappers below feed the SAME UsageMeter and the SAME RateLimiter, so
  metering and pacing parity between arms is preserved -- it just takes two
  wrappers instead of one. Any per-arm token/latency comparison remains
  apples-to-apples.

* The async wrapper MUST NOT be an `anthropic.Anthropic` instance.
  AnthropicLLM.__init__ does:
        if isinstance(client, Anthropic):
            self.client = AsyncAnthropic(api_key=client.api_key, ...)
        else:
            self.client = client
  i.e. a SYNC client handed in is silently DISCARDED and rebuilt internally
  -- which would leave the baseline arm unmetered and unpaced without any
  error to notice. A duck-typed proxy (not an Anthropic subclass) survives
  the isinstance check and is kept as-is. Verified against anthropic SDK
  0.120.x.

* max_tokens parity: AnthropicLLM defaults to _MAX_TOKENS = 1024 when the
  caller omits it, while the CATSEC arm here uses --max-tokens (2048 by
  default). Left unequal that is a silent confound -- the baseline would be
  truncation-limited where CATSEC is not -- so this runner passes
  max_tokens=decode.max_tokens into AnthropicLLM explicitly.

* Thinking budget: LEFT UNSET on purpose. AnthropicLLM exposes
  thinking_budget_tokens, and setting it forces temperature=1 internally
  (see its __init__). Haiku 4.5 is therefore run NON-REASONING at
  temperature 0, matching the gpt-4o-mini leg, so the cross-model comparison
  is not confounded by one model getting hidden reasoning tokens the other
  never had. This is a deliberate methodological choice and should be stated
  as such in the write-up, not left implicit.

* Usage fields: response.usage.input_tokens / output_tokens (Anthropic)
  rather than prompt_tokens / completion_tokens (OpenAI). tokens_thoughts
  stays 0 for schema parity with both sibling runners, so analyze.py needs
  no changes.

* System prompt placement: Anthropic takes `system` as a TOP-LEVEL request
  parameter, not as a message with role="system". The planner's
  llm(system, user_text) contract is unchanged; only the marshalling differs.

* No _COT_PROMPT injection: AnthropicLLM prepends a chain-of-thought system
  preamble only when the model id contains "claude-3-sonnet" or
  "claude-3-haiku". "claude-haiku-4-5-*" matches neither, so the baseline
  arm gets no extra hidden system text. Worth re-checking if the model id
  ever changes, since it would silently alter the baseline's prompt.

Everything else -- the plan/execute split, the provenance lattice, the
per-pair JSONL logging, --resume, --all-pairs, --k1-check, --no-injection,
--trial-timeout -- is identical in code where possible, so results from this
runner are directly comparable in analyze.py.

RUN ORDER: see README.md (Anthropic section).
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
def _text_of(resp) -> str:
    """Concatenate the text blocks of an Anthropic response.

    Anthropic returns a LIST of content blocks, not a single string, and the
    list can legitimately contain non-text blocks (thinking blocks when
    extended thinking is on, tool_use blocks when tools are declared).
    Indexing content[0].text blindly is the standard way to get a confusing
    AttributeError the first time a non-text block appears, so filter by
    type and join instead."""
    parts = []
    for block in (getattr(resp, "content", None) or ()):
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _is_rate_limited(exc: Exception) -> bool:
    """True for Anthropic 429s specifically. Checked by exception type (the
    anthropic SDK raises RateLimitError) and by string, in case a wrapped or
    retried exception loses its type on the way up -- which it can, since the
    baseline arm's call is buried under asyncio.run() inside AnthropicLLM.

    APIStatusError with status 429 is also caught: overloaded_error surfaces
    that way rather than as RateLimitError, and it wants the same backoff."""
    try:
        import anthropic
        if isinstance(exc, anthropic.RateLimitError):
            return True
        if isinstance(exc, anthropic.APIStatusError) and \
                getattr(exc, "status_code", None) in (429, 529):
            return True
    except ImportError:
        pass
    s = str(exc).lower()
    return "429" in s or "529" in s or "rate_limit" in s or "overloaded" in s


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
    """Anthropic decode parameters.

    Two deliberate differences from the OpenAI runner:

    * max_tokens is REQUIRED by the Anthropic API (it is optional for
      OpenAI). It is therefore always sent, and is also pushed into
      AnthropicLLM so the baseline arm does not silently sit on its 1024
      default while the CATSEC arm gets 2048.

    * top_p defaults to None and is OMITTED when None. Anthropic's guidance
      is to tune temperature OR top_p, not both; at temperature 0 a top_p of
      1.0 is a no-op anyway, so sending it buys nothing and risks a
      needless argument rejection mid-run. Set --top-p explicitly if a run
      needs it; the manifest records whichever was actually used.

    thinking_budget is exposed but defaults to None -- see the module
    docstring for why the Claude leg is run non-reasoning."""
    temperature: float = 0.0
    top_p: float | None = None
    max_tokens: int = 2048
    thinking_budget: int | None = None

    def kwargs(self) -> dict:
        out = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.top_p is not None:
            out["top_p"] = self.top_p
        if self.thinking_budget is not None:
            # Anthropic requires temperature=1 when extended thinking is on.
            out["thinking"] = {"type": "enabled",
                               "budget_tokens": self.thinking_budget}
            out["temperature"] = 1
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
        # Anthropic names these input_tokens / output_tokens (OpenAI uses
        # prompt_tokens / completion_tokens). Cache-read/creation tokens are
        # deliberately NOT folded in here: this harness sends no cache
        # breakpoints, so they are always zero, and silently summing them
        # later would break comparability with the sibling runners' figures.
        self.calls += 1
        self.api_seconds += dt
        u = getattr(resp, "usage", None)
        pin = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        pout = int(getattr(u, "output_tokens", 0) or 0) if u else 0
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


class _MeteredMessagesSync:
    """Meters + paces the SYNC `messages.create` surface, which is the one the
    CATSEC arm (planner and composer) uses."""

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


class _MeteredStream:
    """Wraps the object yielded by `async with client.messages.stream(...)`.

    Usage is only knowable once the stream has been drained, so the meter is
    updated in get_final_message() -- the single call AnthropicLLM makes --
    rather than at __aenter__ time."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter,
                 t0: float):
        self._inner = inner
        self._meter = meter
        self._limiter = limiter
        self._t0 = t0

    async def get_final_message(self):
        try:
            msg = await self._inner.get_final_message()
        except Exception as e:
            if _is_rate_limited(e):
                self._limiter.on_rate_limit()
            raise
        self._limiter.on_success()
        self._meter.record(msg, time.perf_counter() - self._t0)
        return msg

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredStreamCtx:
    """Async context manager standing in for `client.messages.stream(...)`."""

    def __init__(self, inner_cm, meter: UsageMeter, limiter: RateLimiter):
        self._inner_cm = inner_cm
        self._meter = meter
        self._limiter = limiter
        self._t0 = 0.0

    async def __aenter__(self):
        # The limiter is synchronous and blocking. That is fine here: this
        # harness runs one trial at a time, and AnthropicLLM drives the call
        # through asyncio.run() with nothing else on the loop, so there is no
        # concurrent work to starve. Pacing stays identical to the sync arm.
        self._limiter.wait()
        self._t0 = time.perf_counter()
        try:
            stream = await self._inner_cm.__aenter__()
        except Exception as e:
            if _is_rate_limited(e):
                self._limiter.on_rate_limit()
            raise
        return _MeteredStream(stream, self._meter, self._limiter, self._t0)

    async def __aexit__(self, *exc):
        return await self._inner_cm.__aexit__(*exc)


class _MeteredMessagesAsync:
    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter):
        self._inner = inner
        self._meter = meter
        self._limiter = limiter

    def stream(self, *args, **kwargs):
        return _MeteredStreamCtx(self._inner.stream(*args, **kwargs),
                                 self._meter, self._limiter)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredAsyncClient:
    """Duck-typed stand-in for AsyncAnthropic, handed to AnthropicLLM.

    CRITICAL: this must NOT subclass anthropic.Anthropic. AnthropicLLM's
    constructor rebuilds a fresh internal client from any `Anthropic`
    instance it is given, which would throw this wrapper away and leave the
    baseline arm unmetered and unpaced -- with no error raised. A plain
    proxy fails that isinstance check and is kept verbatim.

    Only `.messages.stream` is on AnthropicLLM's path; everything else falls
    through to the real client."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter):
        self._inner = inner
        self.limiter = limiter
        self.messages = _MeteredMessagesAsync(inner.messages, meter, limiter)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredClient:
    """Sync client wrapper for the CATSEC arm. Shares the meter and limiter
    with MeteredAsyncClient so both arms are measured and paced together."""

    def __init__(self, inner, meter: UsageMeter, limiter: RateLimiter):
        self._inner = inner
        self.limiter = limiter
        self.messages = _MeteredMessagesSync(inner.messages, meter, limiter)

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
                    # `system` is a TOP-LEVEL parameter for Anthropic, not a
                    # message with role="system". No tools are declared on
                    # this request, so the model remains structurally
                    # incapable of calling anything -- the invariant is
                    # unchanged from the OpenAI runner, only the marshalling
                    # differs.
                    resp = client.messages.create(
                        model=model_name,
                        system=system,
                        messages=[{"role": "user",
                                   "content": user_text or ""}],
                        **decode.kwargs(),
                    )
                    return _text_of(resp)
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
                resp = client.messages.create(
                    model=model_name,
                    system=COMPOSER_SYSTEM,
                    messages=[
                        {"role": "user", "content": f"Question: {user_question}\n\n"
                         "Data retrieved:\n"
                         + "\n".join(facts)},
                    ],
                    **decode.kwargs(),
                )
                return _text_of(resp)
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
def ensure_anthropic_env() -> None:
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and sys.stdin.isatty():
        # Only prompt when there is a human at the terminal. Under nohup /
        # systemd / a detached overnight run, stdin is not a tty and input()
        # dies with EOFError several seconds in -- after which the run looks
        # like it started and then vanished.
        key = input("ANTHROPIC_API_KEY: ").strip()
        os.environ["ANTHROPIC_API_KEY"] = key
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is required. Set it in your .env or "
            "environment.")


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
                     ["agentdojo", "anthropic", "pydantic", "pandas", "scipy"]},
        "provider": "anthropic",
        **extra,
    }
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #
def build_pipeline_factory(args, model_id: str, client, probe: TrialProbe,
                           decode: DecodeConfig, async_client=None):
    from agentdojo.agent_pipeline import AgentPipeline, PipelineConfig
    from agentdojo.agent_pipeline.llms.anthropic_llm import AnthropicLLM
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
            # AnthropicLLM takes the client and model id directly. Two
            # things matter here:
            #  * `async_client` (the duck-typed proxy), NOT the sync client:
            #    a sync client would be discarded and rebuilt internally,
            #    silently unmetering and unpacing this arm.
            #  * max_tokens is passed explicitly -- AnthropicLLM otherwise
            #    defaults to 1024 while the CATSEC arm runs at --max-tokens,
            #    which would be a truncation confound between arms.
            llm = AnthropicLLM(async_client, model_id,
                               temperature=decode.temperature,
                               max_tokens=decode.max_tokens)
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
        "provider": "anthropic",
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
    p.add_argument("--model", default="claude-haiku-4-5-20251001")
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
    p.add_argument("--top-p", type=float, default=None,
                   help="Omitted from the request when unset. Anthropic's "
                        "guidance is to tune temperature OR top_p, not both; "
                        "at temperature 0 a top_p of 1.0 is a no-op.")
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--thinking-budget", type=int, default=None,
                   help="Extended-thinking budget in tokens. LEAVE UNSET for "
                        "the cross-model comparison: enabling it forces "
                        "temperature=1 and gives Claude hidden reasoning "
                        "tokens the gpt-4o-mini leg never had, confounding "
                        "the comparison.")

    p.add_argument("--logdir", default="./catsec_runs_claude")
    p.add_argument("--jsonl", default=None)
    p.add_argument("--out", default="catsec_results_claude.csv")
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
    add_cli_args(p)
    args = p.parse_args()
    args.catsec_opts = catsec_options_from_args(args)
    return args


def main():
    args = parse_args()
    ensure_anthropic_env()

    import logging
    for n in ["agentdojo", "httpx", "httpcore", "anthropic", "urllib3"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.attacks.attack_registry import load_attack
    import anthropic
    import pandas as pd
    from tqdm import tqdm

    decode = DecodeConfig(
        temperature=args.temperature, top_p=args.top_p,
        max_tokens=args.max_tokens, thinking_budget=args.thinking_budget,
    )
    meter = UsageMeter()
    probe = TrialProbe()
    probe.run_settings = manifest_info(args.catsec_opts)
    limiter = RateLimiter(base_interval=args.min_call_interval,
                          max_interval=args.max_call_interval)

    # TWO clients, ONE meter, ONE limiter -- see the module docstring.
    # The sync client serves the CATSEC arm; the async proxy serves the
    # baseline arm through AnthropicLLM. Sharing meter+limiter is what keeps
    # the two arms comparable despite the different call surfaces.
    _api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = MeteredClient(anthropic.Anthropic(api_key=_api_key),
                           meter, limiter)
    async_client = MeteredAsyncClient(
        anthropic.AsyncAnthropic(api_key=_api_key), meter, limiter)

    logdir = Path(args.logdir).resolve()
    logdir.mkdir(parents=True, exist_ok=True)
    suite = get_suite(args.version, args.suite)
    build = build_pipeline_factory(args, args.model, client, probe, decode,
                                   async_client=async_client)

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

    print(f"[Anthropic] Suite {args.suite} {args.version} | model {args.model} | "
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
                        "n_user_tasks": len(ut_ids), "n_injection_tasks": len(it_ids)})
    writer = JsonlWriter(jsonl_path)
    done = load_resume_index(jsonl_path) if (
        args.resume and jsonl_path) else set()
    if done:
        print(f"  resume: {len(done)} completed trials found, will be skipped")

    if args.smoke_test_only:
        pipe = build("catsec")
        atk = load_attack(args.attack, suite, pipe)
        print("\n--- Smoke test: ONE real pair through CATSEC (Anthropic) ---")
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
