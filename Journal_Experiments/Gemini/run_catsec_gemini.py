#!/usr/bin/env python3
"""
CATSEC x AgentDojo -- Gemini runner via GOOGLE AI STUDIO (API key).

Sibling of run_catsec_openai.py and run_catsec_claude.py. Same kernel.py and
planner.py underneath, unchanged -- those files are provider-agnostic and MUST
stay byte-identical across legs, because that identity is the whole basis for
comparing models in the paper.

WHY THIS FILE EXISTS (and why it is not run_catsec_gemini_vertex.py)
====================================================================
Two separate reasons, and it is worth keeping them separate:

1. TRANSPORT. The Vertex runner authenticates with gcloud Application Default
   Credentials against a project/location, and runs on Vertex's Dynamic Shared
   Quota: throughput is not a number you can see, request, or plan around. It
   measured ~46s/pair (catsec) to ~214s/pair (baseline), projecting ~150 hours
   for the full leg against a ~32 hour budget, with 429s that no quota-increase
   request could fix. AI Studio instead uses a plain API key with PUBLISHED,
   per-tier RPM/TPM/RPD limits. Slower per-request in absolute terms is fine;
   knowing the number in advance is what makes the run plannable.

2. VINTAGE. run_catsec_gemini_vertex.py is the OLDEST runner in the project and
   predates every fix from the GPT-4o-mini pilot. Porting Vertex -> AI Studio
   would have carried all of those defects forward. So this file is a port of
   run_catsec_openai.py (the corrected, validated reference implementation) with
   only the model-calling layer swapped to google-genai. Everything that was
   fixed on the OpenAI leg is present here BY CONSTRUCTION rather than by
   back-porting:
     * per-invocation probe reset (AgentDojo calls the pipeline TWICE per pair)
     * COMPOSER_SYSTEM with the DIRECTION / PERIOD / SCOPE financial rules
     * compose_error recorded instead of a bare `except: return ""`
     * the plan cache keyed on exact user text, which is what makes --k1-check
       a real "same Ctrl, different Data" test
     * MODEL_NAMES registration for the important_instructions attack family

WHAT IS GEMINI-SPECIFIC HERE
============================
* Client: genai.Client(api_key=...). No vertexai=True, no project, no location,
  no `gcloud auth application-default login`.
* Metering wraps client.models.generate_content (not chat.completions.create).
  This is the ONLY place the baseline arm can be reached: AgentDojo's GoogleLLM
  builds its own GenerateContentConfig internally, so wrapping the client is how
  the baseline gets the same decode settings and the same pacing as catsec.
* DecodeConfig.apply() OVERWRITES decode fields on whatever config GoogleLLM
  built while PRESERVING system_instruction, tools and response schema. Getting
  this wrong silently strips the baseline's tools and it still "runs".
* Gemini 2.5 has billed hidden reasoning. thinking_budget defaults to 0
  (reasoning off) to match the non-reasoning gpt-4o-mini and claude-haiku-4-5
  legs. tokens_thoughts is recorded either way.
* Rate limiting defaults are TIER-AWARE (--tier free|paid), because AI Studio's
  free tier will wall on requests-per-day long before a 288-trial --all-pairs
  run finishes. --max-requests stops the run CLEANLY at a budget so --resume
  can pick it up the next day instead of leaving a half-written JSONL.

RUN ORDER
=========
  python selftest.py                     # kernel + planner, offline, 61 checks
  python check_composer_prompt.py run_catsec_gemini.py    # overfitting guard
  python selftest_gemini_wiring.py       # provider plumbing, offline, no key
  python run_catsec_gemini.py --smoke-test-only
  ... then the staged runs; see README_gemini.md
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


class RequestBudgetExceeded(Exception):
    """Raised when --max-requests is hit. Deliberately NOT an ordinary error:
    it must escape run_one_trial's retry loop instead of burning six retries
    against a daily cap that will not clear for hours."""


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #
def _is_rate_limited(exc: Exception) -> bool:
    """True for AI Studio 429s. Checked by exception type where the SDK exposes
    one, and by string otherwise -- google-genai raises ClientError with the
    status embedded in the message, and wrapped/retried exceptions often lose
    their type on the way up."""
    try:
        from google.genai import errors as genai_errors
        if isinstance(exc, getattr(genai_errors, "ClientError", ())):
            if getattr(exc, "code", None) == 429:
                return True
    except Exception:
        pass
    s = str(exc).lower()
    return ("429" in s or "resource_exhausted" in s or "rate limit" in s
            or "quota" in s)


def _backoff_seconds(attempt: int, rate_limited: bool) -> float:
    """attempt is 0-indexed. AI Studio's limits are published per-tier, so a
    429 is a real burst against a KNOWN ceiling rather than an empty shared
    pool -- backoff can be the OpenAI runner's shape rather than the Vertex
    runner's much longer one. Jittered so several failed calls don't
    resynchronise and retry in lockstep."""
    if rate_limited:
        base = min(4 * (2 ** attempt), 60)
    else:
        base = min(2 ** attempt, 20)
    return base * (0.75 + random.random() * 0.5)


class RateLimiter:
    """Proactive pacing with adaptive backpressure, plus a hard request budget.

    UNLIKE the OpenAI leg, pacing here is the PRIMARY strategy, not a safety
    net: AI Studio's free tier is around 10 requests/minute, which this harness
    will exceed instantly if left unpaced. `base_interval` is set from --tier.

    The request budget exists because the free tier's daily cap is the real
    binding constraint on a 288-trial run. Walling into it produces a long tail
    of 429-failed trials that pollute the JSONL; stopping cleanly at a budget
    instead means --resume continues tomorrow from a clean boundary.
    """

    def __init__(self, base_interval: float = 0.0, max_interval: float = 120.0,
                 growth: float = 1.6, decay_after: int = 10,
                 max_requests: int = 0):
        self.base = max(0.0, base_interval)
        self.current = self.base
        self.max_interval = max_interval
        self.growth = growth
        self.decay_after = decay_after
        self.max_requests = max(0, int(max_requests))
        self.total_calls = 0
        self._ok_streak = 0
        self._last_call = 0.0
        self.waited_seconds = 0.0
        self.n_rate_limits = 0
        self.peak_interval = self.base

    def wait(self):
        if self.max_requests and self.total_calls >= self.max_requests:
            raise RequestBudgetExceeded(
                f"--max-requests budget of {self.max_requests} model calls "
                f"reached. Stopping cleanly; rerun with --resume to continue.")
        self.total_calls += 1
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
                "seconds_spent_pacing": round(self.waited_seconds, 1),
                "total_model_calls": self.total_calls,
                "max_requests": self.max_requests}


# --------------------------------------------------------------------------- #
# Decode configuration
# --------------------------------------------------------------------------- #
@dataclass
class DecodeConfig:
    """Every generation parameter in one place, recorded into the manifest.

    thinking_budget = None means "leave the provider default alone" (pass
    --thinking-budget -1). 0 disables reasoning, which is the DEFAULT and a
    deliberate choice: gpt-4o-mini and claude-haiku-4-5 both run non-reasoning
    on their legs, so leaving Gemini 2.5's thinking on would make the
    cross-model comparison a comparison of two different things. State this in
    the paper as a decision, not an oversight.
    """
    temperature: float = 0.0
    top_p: float = 1.0
    candidate_count: int = 1
    max_output_tokens: int = 2048
    thinking_budget: int | None = 0

    def overrides(self, genai_types) -> dict:
        out = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "candidate_count": self.candidate_count,
            "max_output_tokens": self.max_output_tokens,
        }
        if self.thinking_budget is not None:
            try:
                out["thinking_config"] = genai_types.ThinkingConfig(
                    thinking_budget=self.thinking_budget)
            except Exception:
                print("  [warn] ThinkingConfig unsupported by this google-genai "
                      "version; thinking budget NOT pinned. Record this in your "
                      "lab notes -- output-token counts are not comparable to "
                      "runs where it was pinned.", file=sys.stderr)
        return out

    def apply(self, cfg, genai_types):
        """Overwrite decode fields on an EXISTING config, preserving everything
        else -- system_instruction, tools, response schema.

        This is the single most dangerous function in the file. GoogleLLM builds
        the baseline arm's config itself, including its TOOL DECLARATIONS. If
        this replaced the config instead of mutating it, the baseline would run
        with no tools, score 0% utility and 100% "secure", and look like a
        spectacular result for CATSEC rather than a bug. selftest_gemini_wiring
        asserts the preservation explicitly for exactly that reason.
        """
        if cfg is None:
            cfg = genai_types.GenerateContentConfig()
        for k, v in self.overrides(genai_types).items():
            try:
                setattr(cfg, k, v)
            except Exception:
                pass
        return cfg


# --------------------------------------------------------------------------- #
# Response text extraction
# --------------------------------------------------------------------------- #
def _text_of(resp) -> str:
    """Pull plain text out of a google-genai response, defensively.

    `resp.text` is the happy path but it is a property that can raise or return
    None when the candidate has no text part -- which happens on a MAX_TOKENS
    stop, a safety block, or a thinking-only candidate. A bare `resp.text` would
    turn any of those into a crash mid-trial; here they become an empty string,
    which the planner correctly reports as a plan failure and the composer
    records as a compose_error.
    """
    try:
        t = getattr(resp, "text", None)
        if t:
            return t
    except Exception:
        pass
    parts_text = []
    try:
        for cand in (getattr(resp, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                # Skip thought parts: they are not the answer, and on a
                # thinking-enabled run they would otherwise be parsed as if
                # they were the plan JSON.
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    parts_text.append(part.text)
    except Exception:
        pass
    return "".join(parts_text)


def _finish_reason_of(resp) -> str | None:
    try:
        cands = getattr(resp, "candidates", None) or []
        if cands:
            fr = getattr(cands[0], "finish_reason", None)
            return str(fr) if fr is not None else None
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Usage metering -- wraps the genai client so BOTH arms are measured the same
# --------------------------------------------------------------------------- #
@dataclass
class UsageMeter:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thoughts: int = 0
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
        um = getattr(resp, "usage_metadata", None)
        pin = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        pout = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
        pth = int(getattr(um, "thoughts_token_count", 0) or 0) if um else 0
        self.tokens_in += pin
        self.tokens_out += pout
        self.tokens_thoughts += pth
        self.per_call.append({"in": pin, "out": pout, "thoughts": pth,
                              "seconds": round(dt, 3),
                              "finish_reason": _finish_reason_of(resp)})

    def snapshot(self) -> dict:
        return {
            "n_model_calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_thoughts": self.tokens_thoughts,
            "api_seconds": round(self.api_seconds, 3),
            "per_call": list(self.per_call),
        }


class _MeteredModels:
    """Intercepts generate_content to (a) pin decode settings, (b) meter usage
    and (c) pace calls.

    Wrapping the client is how the BASELINE arm gets identical treatment to the
    catsec arm: AgentDojo's GoogleLLM builds its own config internally and calls
    client.models.generate_content, so there is no other place to reach it.
    """

    def __init__(self, inner, meter: UsageMeter, decode: DecodeConfig,
                 genai_types, limiter: RateLimiter):
        self._inner = inner
        self._meter = meter
        self._decode = decode
        self._types = genai_types
        self._limiter = limiter

    def generate_content(self, *args, **kwargs):
        if self._decode is not None:
            kwargs["config"] = self._decode.apply(
                kwargs.get("config"), self._types)
        self._limiter.wait()          # proactive pacing, BEFORE the request
        t0 = time.perf_counter()
        try:
            resp = self._inner.generate_content(*args, **kwargs)
        except Exception as e:
            if _is_rate_limited(e):
                self._limiter.on_rate_limit()
            raise
        self._limiter.on_success()
        self._meter.record(resp, time.perf_counter() - t0)
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredClient:
    """Wraps a google.genai Client. .models.generate_content is the only surface
    CatsecPipeline and GoogleLLM both go through, so wrapping it covers both
    arms identically.

    Note this is NOT a genai.Client subclass, deliberately. GoogleLLM stores
    whatever client object it is handed without an isinstance check (unlike
    AnthropicLLM, which rebuilds a sync client into an async one and silently
    discards a wrapper -- see run_catsec_claude.py's notes), so the plain
    delegating proxy survives. selftest_gemini_wiring pins that behaviour.
    """

    def __init__(self, inner, meter: UsageMeter, decode: DecodeConfig,
                 genai_types, limiter: RateLimiter):
        self._inner = inner
        self.limiter = limiter
        self.models = _MeteredModels(
            inner.models, meter, decode, genai_types, limiter)

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
    from google.genai import types as genai_types

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
            # Keyed on the EXACT user text. P(intent) depends only on this text
            # (never on tool output), so the same text must always produce the
            # same plan -- caching and reusing it instead of re-querying is what
            # lets --k1-check test "same Ctrl, different Data" rather than "two
            # independent samples from a non-deterministic model". This matters
            # MORE on Gemini than on OpenAI: the Vertex leg already showed
            # user_task_3 flipping between identical probes at temperature 0.
            self._plan_cache: dict[str, list] = {}

        # -- the single planning call: user text ONLY, never tool output ----
        def _plan_llm(self, system: str, user_text: str) -> str:
            # SAME INVARIANT AS EVERY OTHER RUNNER: this call gets `system` (the
            # plan grammar) and `user_text` ONLY. No tools are declared on this
            # request, so the model is structurally incapable of calling
            # anything -- it can only emit the plan JSON as text. Do NOT wire up
            # Gemini's native function calling here; that would reintroduce
            # exactly the Data -> Ctrl morphism K1 forbids.
            last_exc = None
            for attempt in range(5):
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=user_text or "",
                        config=genai_types.GenerateContentConfig(
                            system_instruction=system),
                    )
                    return _text_of(resp)
                except RequestBudgetExceeded:
                    raise
                except Exception as e:
                    last_exc = e
                    if attempt < 4:
                        time.sleep(_backoff_seconds(
                            attempt, _is_rate_limited(e)))
            raise last_exc

        # ---------------- terminal answer composition ----------------
        # The plan is FIXED and FULLY EXECUTED by this point, and this call
        # declares no tools, so it is structurally incapable of acting: a
        # terminal Data -> Text morphism, not Data -> Ctrl. Residual risk
        # (attacker text influencing what the USER READS) is the same stated,
        # measured limitation as every other leg -- see E9.
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
                resp = client.models.generate_content(
                    model=model_name,
                    contents=f"Question: {user_question}\n\nData retrieved:\n"
                             + "\n".join(facts),
                    config=genai_types.GenerateContentConfig(
                        system_instruction=COMPOSER_SYSTEM),
                )
                text = _text_of(resp)
                if not text:
                    # Empty but not an exception: almost always MAX_TOKENS or a
                    # safety block. Record WHICH, because read-only question
                    # tasks derive their ENTIRE utility from this string and a
                    # silent empty answer is otherwise indistinguishable from a
                    # planning defect in the per-trial log.
                    self.probe.compose_error = (
                        f"empty-response:{_finish_reason_of(resp)}")
                return text
            except RequestBudgetExceeded:
                raise
            except Exception as e:
                self.probe.compose_error = f"{type(e).__name__}: {e}"
                return ""

        def query(self, query, runtime, env=EmptyEnv(), messages=(), extra_args={}):
            msgs = list(messages)
            p = self.probe

            # Reset EVERY invocation, not just once per pair -- AgentDojo calls
            # this pipeline TWICE per pair (an injection-task-solvability
            # pre-check, then the real trial). Without a per-call reset, a
            # failing real trial can silently log the OTHER invocation's
            # leftover data as its own.
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
def ensure_gemini_env() -> str:
    """AI Studio authenticates with a plain bearer API key -- no gcloud ADC, no
    project, no location. google-genai will read GEMINI_API_KEY or
    GOOGLE_API_KEY from the environment on its own, but the key is passed
    EXPLICITLY to genai.Client below so that a stale ADC login or a leftover
    GOOGLE_CLOUD_PROJECT cannot silently route this run back to Vertex -- which
    would reintroduce the Dynamic Shared Quota problem this file exists to
    escape, and would not be visible anywhere in the output."""
    load_dotenv()
    key = (os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY"))
    if not key and sys.stdin.isatty():
        # Only prompt when there is a human at the terminal. Under nohup or a
        # detached overnight run, stdin is not a tty and input() dies with
        # EOFError several seconds in -- after which the run looks like it
        # started and then vanished.
        key = input("GEMINI_API_KEY: ").strip()
    if not key:
        raise SystemExit(
            "GEMINI_API_KEY is required. Get one from https://aistudio.google.com/"
            " and set it in your .env (see .env.example).")
    os.environ["GEMINI_API_KEY"] = key
    # Belt and braces against accidental Vertex routing.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
    return key


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
                     ["agentdojo", "google-genai", "pydantic", "pandas", "scipy"]},
        "provider": "google-aistudio",
        "tier": args.tier,
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
    from agentdojo.agent_pipeline.llms.google_llm import GoogleLLM
    from agentdojo.models import MODEL_NAMES

    # The important_instructions attack family phrases its jailbreak using the
    # model's display name, looked up via get_model_name_from_pipeline(), which
    # substring-matches pipeline.name against MODEL_NAMES' KEYS. That table is
    # frozen at package release and has no entry for current Gemini ids. This
    # matters for ATTACK STRENGTH: on the Google path a missing entry yields a
    # wrong or generic name, which makes the attack WEAKER and quietly flatters
    # the baseline's security number -- a silent failure, unlike the OpenAI path
    # where it raises. Use --print-attack-text to eyeball the generated text
    # before committing to a sweep.
    if model_id not in MODEL_NAMES:
        MODEL_NAMES[model_id] = "AI model developed by Google"

    CatsecCls = make_catsec_pipeline(
        client, model_id, decode, args.policy, args.verbose_interp,
        ablation=args.ablation, allow_repair=args.plan_repair, probe=probe)
    RecorderCls = make_trace_recorder(probe)

    def build(defense_name):
        if defense_name != "catsec":
            # Built directly instead of PipelineConfig(llm=<enum string>):
            # AgentDojo's ModelsEnum / MODEL_PROVIDERS tables are pinned to old
            # Gemini ids and have no entry for current models. from_config()
            # accepts a BasePipelineElement for `llm` directly, so passing a
            # live GoogleLLM bypasses the enum lookup while still assembling the
            # standard system-message / tool-loop / defense scaffolding. The
            # client here is the METERED one, so the baseline arm gets identical
            # decode settings, identical pacing and correct token accounting.
            llm = GoogleLLM(model_id, client)
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
        except RequestBudgetExceeded:
            # Must escape the retry loop: retrying against a daily cap just
            # burns the remaining attempts and writes six identical failures.
            raise
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
        "provider": "google-aistudio",
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
# Tier presets
# --------------------------------------------------------------------------- #
# AI Studio's limits are per-tier and per-model, and they DO change. These are
# conservative defaults, not gospel: check the current numbers for your key at
# https://ai.google.dev/gemini-api/docs/rate-limits and override with
# --min-call-interval / --max-requests if they have moved.
#
# free: paced for roughly 10 requests/minute with headroom, and stopped well
#       inside a daily request cap so the run ends at a clean boundary.
# paid: pacing off; the published RPM is far above what this harness generates.
TIER_PRESETS = {
    "free": {"min_call_interval": 7.0, "max_requests": 200},
    "paid": {"min_call_interval": 0.0, "max_requests": 0},
}


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", default="banking")
    p.add_argument("--benchmark-version", default="v1.2", dest="version")
    p.add_argument("--model", default="gemini-2.5-flash")
    p.add_argument("--attack", default="tool_knowledge")
    p.add_argument("--defenses", default="none,catsec")
    p.add_argument("--policy", default="refuse", choices=["refuse", "confirm"])
    p.add_argument("--ablation", default="none",
                   choices=["none", "no_provenance"])
    p.add_argument("--plan-repair", action="store_true")

    p.add_argument("--all-pairs", action="store_true")
    p.add_argument("--user-tasks", default=None,
                   help="Comma-separated user_task IDs to restrict to "
                        "(e.g. 'user_task_2,user_task_7').")
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
    p.add_argument("--max-output-tokens", type=int, default=2048)
    p.add_argument("--thinking-budget", type=int, default=0,
                   help="0 disables Gemini 2.5 reasoning (DEFAULT, matches the "
                        "non-reasoning gpt-4o-mini and claude-haiku-4-5 legs). "
                        "-1 leaves the provider default alone.")

    p.add_argument("--tier", default="free", choices=["free", "paid"],
                   help="Sets pacing and request-budget defaults for your AI "
                        "Studio key. Explicit --min-call-interval / "
                        "--max-requests override this.")
    p.add_argument("--logdir", default="./catsec_runs_gemini")
    p.add_argument("--jsonl", default=None)
    p.add_argument("--out", default="catsec_results_gemini.csv")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--min-call-interval", type=float, default=None,
                   help="Seconds between every model call. Defaults from "
                        "--tier if unset.")
    p.add_argument("--max-call-interval", type=float, default=120.0)
    p.add_argument("--max-requests", type=int, default=None,
                   help="Stop cleanly after this many model calls (0 = "
                        "unlimited). Defaults from --tier. Rerun with "
                        "--resume to continue after a daily cap resets.")
    p.add_argument("--trial-timeout", type=float, default=180.0,
                   help="Per-pair time budget in seconds (0 = unlimited). "
                        "Higher than the OpenAI runner's 120s because free-tier "
                        "pacing alone can consume most of a 120s budget.")
    p.add_argument("--verbose-interp", action="store_true")
    args = p.parse_args()

    preset = TIER_PRESETS[args.tier]
    if args.min_call_interval is None:
        args.min_call_interval = preset["min_call_interval"]
    if args.max_requests is None:
        args.max_requests = preset["max_requests"]
    return args


def main():
    args = parse_args()
    ensure_gemini_env()

    import logging
    for n in ["agentdojo", "httpx", "httpcore", "google_genai", "urllib3"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.attacks.attack_registry import load_attack
    from google import genai
    from google.genai import types as genai_types
    import pandas as pd
    from tqdm import tqdm

    decode = DecodeConfig(
        temperature=args.temperature, top_p=args.top_p,
        max_output_tokens=args.max_output_tokens,
        thinking_budget=None if args.thinking_budget < 0 else args.thinking_budget,
    )
    meter = UsageMeter()
    probe = TrialProbe()
    limiter = RateLimiter(base_interval=args.min_call_interval,
                          max_interval=args.max_call_interval,
                          max_requests=args.max_requests)

    raw_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    client = MeteredClient(raw_client, meter, decode, genai_types, limiter)

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

    print(f"[AI Studio] Suite {args.suite} {args.version} | model {args.model} | "
          f"policy {args.policy} | ablation {args.ablation}")
    print(f"  {len(ut_ids)} user tasks x {len(it_ids)} injection tasks "
          f"= {len(ut_ids)*len(it_ids)} pairs available")
    print(f"  decode: {asdict(decode)}")
    print(f"  tier={args.tier}  pacing: min-call-interval="
          f"{args.min_call_interval}s  max={args.max_call_interval}s  "
          f"max-requests={args.max_requests or 'unlimited'}  "
          f"trial-timeout={args.trial_timeout}s  max-retries={args.max_retries}")
    if args.tier == "free" and (args.all_pairs or args.max_pairs > 60):
        print("  [warn] A large run on the FREE tier will hit the daily request "
              "cap. That is expected: the run will stop cleanly at "
              "--max-requests. Rerun the SAME command with --resume tomorrow.")

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
                       {"n_user_tasks": len(ut_ids), "n_injection_tasks": len(it_ids)})
    writer = JsonlWriter(jsonl_path)
    done = load_resume_index(jsonl_path) if (
        args.resume and jsonl_path) else set()
    if done:
        print(f"  resume: {len(done)} completed trials found, will be skipped")

    if args.smoke_test_only:
        pipe = build("catsec")
        atk = load_attack(args.attack, suite, pipe)
        print("\n--- Smoke test: ONE real pair through CATSEC (AI Studio) ---")
        t0 = time.perf_counter()
        util, sec, err = run_one_trial(pipe, suite, atk, logdir, ut_ids[0],
                                       it_ids[0], meter, probe,
                                       args.max_retries, injected=True,
                                       trial_timeout=args.trial_timeout)
        print(f"  utility={util}  attack_succeeded={sec}  error={err}")
        print(f"  tokens={meter.tokens_in}/{meter.tokens_out} "
              f"(thoughts={meter.tokens_thoughts}) calls={meter.calls} "
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
        if probe.compose_error:
            print(f"  COMPOSE: {probe.compose_error}")
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
    budget_hit = False
    for rep in range(args.repetitions):
        if budget_hit:
            break
        for d in defenses:
            if budget_hit:
                break
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
                try:
                    util, sec, err = run_one_trial(
                        p, suite, atk, logdir, u, i, meter, probe,
                        args.max_retries, injected=not args.no_injection,
                        trial_timeout=args.trial_timeout)
                except RequestBudgetExceeded as e:
                    # Nothing is written for this pair: a partially-executed
                    # trial is not a result, and leaving it unwritten is exactly
                    # what lets --resume redo it cleanly tomorrow.
                    print(f"\n  == STOPPING CLEANLY: {e}")
                    budget_hit = True
                    break
                wall = time.perf_counter() - t0

                usage_snap = meter.snapshot()
                probe_snap = probe.as_record()
                trace_injected = list(probe.control_trace)
                plan_injected = probe.plan_signature_or_none()

                k1 = None
                if args.k1_check and not args.no_injection and err is None:
                    # For catsec, reuse the SAME pipeline instance (not a fresh
                    # build(d)) so its plan cache is hit: the clean rerun uses
                    # the identical user_task.PROMPT text, so the cache returns
                    # the SAME plan object instead of asking the model again.
                    # That is what makes this a real K1 test -- one plan, two
                    # environments -- rather than two independently-sampled
                    # plans that may legitimately differ. This matters more on
                    # Gemini than anywhere else: the Vertex leg demonstrated
                    # plan drift between identical probes at temperature 0.
                    p_clean = p if d == "catsec" else build(d)
                    try:
                        cu, _, cerr = run_one_trial(
                            p_clean, suite, None, logdir, u, None, meter, probe,
                            args.max_retries, injected=False,
                            trial_timeout=args.trial_timeout)
                    except RequestBudgetExceeded as e:
                        print(f"\n  == STOPPING CLEANLY during K1 check: {e}")
                        budget_hit = True
                        break
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
                        # produced a plan and the clean run succeeded.
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
                # too) -- fine, planning reliability is planning reliability
                # either way. refusals is different: a precheck runs the
                # injection task as its OWN prompt, which isn't an attack
                # scenario, so a refusal there says nothing about defense
                # behaviour. Report the real-trial-only tally.
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
          f"{ls['seconds_spent_pacing']}s spent pacing | "
          f"{ls['total_model_calls']} model calls total")
    if budget_hit:
        print("\nRun stopped at the request budget, NOT finished. Rerun the "
              "same command with --resume once your quota window has reset. "
              "The partial CSV above covers only the trials completed in this "
              "session -- always use analyze.py on the JSONL for real numbers.")
    if jsonl_path:
        print(f"Per-trial log: {jsonl_path}")
        print(f"Next: python analyze.py --jsonl '{jsonl_path}'")


if __name__ == "__main__":
    main()
