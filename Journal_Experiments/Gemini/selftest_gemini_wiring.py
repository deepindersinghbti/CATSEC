#!/usr/bin/env python3
"""
Offline wiring test for run_catsec_gemini.py.

Makes NO API calls and needs NO key -- it uses fake clients to check the
provider plumbing only. It exists because the dangerous failures in this runner
are SILENT:

  * If DecodeConfig.apply() REPLACED the baseline's config instead of mutating
    it, GoogleLLM's tool declarations would vanish. The baseline would score
    ~0% utility and ~100% "secure", which reads as a triumph for CATSEC rather
    than a bug, and nothing in the logs would say otherwise.
  * If the metered proxy did not survive GoogleLLM's constructor, the baseline
    would run unmetered and unpaced -- every number still plausible, the
    token comparison in the paper quietly wrong.
  * If _text_of() crashed on a MAX_TOKENS or safety-blocked candidate, a
    routine empty response would look like a harness failure.

Complements (does not replace) selftest.py, which covers kernel/planner.

Run:  python selftest_gemini_wiring.py
"""
from __future__ import annotations
import run_catsec_gemini as R

import sys
import types

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'  PASS' if cond else '  FAIL'}  {name}"
          + (f"  --  {detail}" if detail else ""))


# A stand-in for google.genai.types that needs no SDK installed.
class FakeThinkingConfig:
    def __init__(self, thinking_budget=None):
        self.thinking_budget = thinking_budget


class FakeGenerateContentConfig:
    def __init__(self, **kw):
        self.system_instruction = kw.get("system_instruction")
        self.tools = kw.get("tools")
        self.temperature = kw.get("temperature")
        self.top_p = kw.get("top_p")
        self.candidate_count = kw.get("candidate_count")
        self.max_output_tokens = kw.get("max_output_tokens")
        self.thinking_config = kw.get("thinking_config")


FakeTypes = types.SimpleNamespace(
    GenerateContentConfig=FakeGenerateContentConfig,
    ThinkingConfig=FakeThinkingConfig)


# --------------------------------------------------------------------------- #
print("DECODE CONFIG")
# --------------------------------------------------------------------------- #
d = R.DecodeConfig()
o = d.overrides(FakeTypes)
check("temperature pinned", o.get("temperature") == 0.0)
check("max_output_tokens pinned", o.get("max_output_tokens") == 2048)
check("thinking budget 0 by default -- the non-reasoning setting that keeps "
      "this comparable to the gpt-4o-mini and haiku legs",
      o.get("thinking_config") is not None
      and o["thinking_config"].thinking_budget == 0)

d_none = R.DecodeConfig(thinking_budget=None)
check("thinking_budget=None omits ThinkingConfig entirely (provider default)",
      "thinking_config" not in d_none.overrides(FakeTypes))


# --------------------------------------------------------------------------- #
print("\nTHE SILENT-FAILURE GUARD: apply() must PRESERVE, not replace")
# --------------------------------------------------------------------------- #
# This mirrors what GoogleLLM hands us on the baseline arm: a config it built
# itself, carrying the system message and the TOOL DECLARATIONS.
baseline_cfg = FakeGenerateContentConfig(
    system_instruction="you are a helpful agent",
    tools=["send_money", "get_balance"],
    temperature=0.9)

out = d.apply(baseline_cfg, FakeTypes)
check("TOOLS survive -- without this the baseline silently runs toolless and "
      "CATSEC looks better than it is",
      out.tools == ["send_money", "get_balance"])
check("system_instruction survives", out.system_instruction ==
      "you are a helpful agent")
check("decode fields ARE overwritten (0.9 -> 0.0), so both arms sample the "
      "same way", out.temperature == 0.0)
check("apply(None) builds a config rather than crashing",
      d.apply(None, FakeTypes).max_output_tokens == 2048)


# --------------------------------------------------------------------------- #
print("\nRESPONSE PARSING")
# --------------------------------------------------------------------------- #


def resp_with_text(t):
    return types.SimpleNamespace(text=t, candidates=[])


class ExplodingText:
    """resp.text is a PROPERTY in google-genai and can raise when there is no
    text part. A bare resp.text would kill the trial."""
    @property
    def text(self):
        raise ValueError("no text part in candidate")

    def __init__(self, parts, finish="STOP"):
        self.candidates = [types.SimpleNamespace(
            content=types.SimpleNamespace(parts=parts),
            finish_reason=finish)]


def part(text, thought=False):
    return types.SimpleNamespace(text=text, thought=thought)


check("happy path reads resp.text", R._text_of(resp_with_text("hello")) == "hello")
check("raising .text falls back to candidate parts",
      R._text_of(ExplodingText([part("plan json")])) == "plan json")
check("multiple parts are joined",
      R._text_of(ExplodingText([part("a"), part("b")])) == "ab")
check("THOUGHT parts are skipped -- otherwise reasoning text would be parsed "
      "as if it were the plan JSON",
      R._text_of(ExplodingText([part("hmm", thought=True),
                                part("plan")])) == "plan")
check("no text anywhere yields '' rather than an exception",
      R._text_of(ExplodingText([])) == "")
check("finish_reason surfaced for the compose_error message",
      R._finish_reason_of(ExplodingText([], finish="MAX_TOKENS"))
      == "MAX_TOKENS")


# --------------------------------------------------------------------------- #
print("\nRATE-LIMIT DETECTION")
# --------------------------------------------------------------------------- #
check("429 detected", R._is_rate_limited(Exception("Error 429")))
check("RESOURCE_EXHAUSTED detected",
      R._is_rate_limited(Exception("RESOURCE_EXHAUSTED: quota")))
check("ordinary errors are NOT treated as rate limits",
      not R._is_rate_limited(Exception("bad request: invalid field")))


# --------------------------------------------------------------------------- #
print("\nMETERING (both arms go through client.models.generate_content)")
# --------------------------------------------------------------------------- #


class FakeUsage:
    prompt_token_count = 11
    candidates_token_count = 7
    thoughts_token_count = 3


class FakeResp:
    usage_metadata = FakeUsage()
    text = "ok"
    candidates = []


class FakeModels:
    def __init__(self):
        self.calls = 0
        self.last = None

    def generate_content(self, **kw):
        self.calls += 1
        self.last = kw
        return FakeResp()


class FakeClient:
    def __init__(self):
        self.models = FakeModels()
        self.api_key = "test-not-used"


meter = R.UsageMeter()
limiter = R.RateLimiter(base_interval=0.0, max_interval=0.0)
inner = FakeClient()
client = R.MeteredClient(inner, meter, d, FakeTypes, limiter)

client.models.generate_content(model="m", contents="hi", config=None)
check("call reaches the real client", inner.models.calls == 1)
check("call is metered (Gemini usage_metadata field names)",
      meter.calls == 1 and meter.tokens_in == 11 and meter.tokens_out == 7,
      f"in={meter.tokens_in} out={meter.tokens_out}")
check("hidden reasoning tokens counted separately -- they are BILLED and would "
      "otherwise be invisible", meter.tokens_thoughts == 3)
check("decode settings injected even when the caller passed config=None",
      inner.models.last["config"].temperature == 0.0)

# The baseline arm's path: GoogleLLM passes its OWN config in.
client.models.generate_content(
    model="m", contents="hi",
    config=FakeGenerateContentConfig(tools=["get_balance"], temperature=0.9))
check("baseline arm accumulates into the SAME meter -- this is what keeps the "
      "per-arm token comparison honest", meter.calls == 2)
check("baseline arm keeps its tools AND gets our decode settings",
      inner.models.last["config"].tools == ["get_balance"]
      and inner.models.last["config"].temperature == 0.0)
check("unknown attributes fall through to the real client",
      client.api_key == "test-not-used")


# --------------------------------------------------------------------------- #
print("\nREQUEST BUDGET (the free-tier daily cap)")
# --------------------------------------------------------------------------- #
lim = R.RateLimiter(base_interval=0.0, max_requests=2)
lim.wait()
lim.wait()
try:
    lim.wait()
    check("budget raises once exhausted", False, "no exception")
except R.RequestBudgetExceeded:
    check("budget raises once exhausted", True)
check("RequestBudgetExceeded is NOT a rate limit -- it must escape the retry "
      "loop instead of burning six attempts against a daily cap",
      not R._is_rate_limited(R.RequestBudgetExceeded("budget")))
check("unlimited budget (0) never raises",
      [R.RateLimiter(base_interval=0.0, max_requests=0).wait()
       for _ in range(5)] is not None)


# --------------------------------------------------------------------------- #
print("\nTIER PRESETS")
# --------------------------------------------------------------------------- #
check("free tier paces calls by default", R.TIER_PRESETS["free"]
      ["min_call_interval"] > 0)
check("free tier caps requests by default",
      R.TIER_PRESETS["free"]["max_requests"] > 0)
check("paid tier leaves pacing off",
      R.TIER_PRESETS["paid"]["min_call_interval"] == 0.0)


# --------------------------------------------------------------------------- #
print("\nCROSS-RUNNER IDENTITY")
# --------------------------------------------------------------------------- #
# The composer prompt must be byte-identical to the OpenAI leg's. A cross-model
# comparison where one model got a different composer prompt is not a
# cross-model comparison. Since the refactor every runner imports the ONE
# shared literal from catsec/composer.py, so identity is checked by object.
import catsec.composer  # noqa: E402
check("COMPOSER_SYSTEM is the shared catsec.composer prompt (same as the OpenAI leg)",
      R.COMPOSER_SYSTEM is catsec.composer.COMPOSER_SYSTEM)

check("SENSITIVE_CONTRACTS unchanged (send_money still needs DERIVED)",
      R.SENSITIVE_CONTRACTS["send_money"][1] == R.DERIVED)


# --------------------------------------------------------------------------- #
n_pass, n_all = sum(results), len(results)
print(f"\n{'=' * 62}\n{n_pass}/{n_all} checks passed\n{'=' * 62}")
sys.exit(0 if n_pass == n_all else 1)
