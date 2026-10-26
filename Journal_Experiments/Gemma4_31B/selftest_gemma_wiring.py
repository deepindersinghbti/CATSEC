#!/usr/bin/env python3
"""
Offline wiring selftest for run_catsec_gemma.py.

NO API KEY, NO SERVER, NO GPU. Every check here is about wiring -- the class
of mistake that costs GPU-hours because it does not fail, it just quietly
produces numbers that mean something other than what you think.

Run this before renting anything:

    python selftest_gemma_wiring.py

Sibling of selftest_claude_wiring.py. It does NOT re-test kernel.py or
planner.py -- selftest.py owns those, unchanged, at 61/61.
"""
from __future__ import annotations

import sys
import types

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def section(t):
    print(f"\n--- {t} ---")


import run_catsec_gemma as R  # noqa: E402


# ===================================================================== #
section("DecodeConfig / thinking mode")
# ===================================================================== #
d = R.DecodeConfig()
check("thinking is OFF by default", d.enable_thinking is False,
      "the other legs are non-reasoning; default must match")
k = d.kwargs()
check("kwargs carries extra_body", "extra_body" in k)
check("enable_thinking is sent explicitly when False",
      k["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False,
      "must be sent, not omitted -- the request has to record the decision")
kon = R.DecodeConfig(enable_thinking=True).kwargs()
check("enable_thinking=True propagates",
      kon["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True)
check("no reasoning_effort leaked in from the OpenAI runner",
      "reasoning_effort" not in k)
check("temperature/top_p/max_tokens present",
      k["temperature"] == 0.0 and k["top_p"] == 1.0 and k["max_tokens"] == 2048)


# ===================================================================== #
section("Decode defaults reach BOTH arms (the confound guard)")
# ===================================================================== #
class _Resp:
    class _U:
        prompt_tokens = 3
        completion_tokens = 5
    usage = _U()


class _Inner:
    """Stands in for openai's completions object; records what it was sent."""

    def __init__(self):
        self.seen = []

    def create(self, **kwargs):
        self.seen.append(kwargs)
        return _Resp()


meter = R.UsageMeter()
limiter = R.RateLimiter(base_interval=0.0)
inner = _Inner()
mc = R._MeteredCompletions(inner, meter, limiter, defaults=R.DecodeConfig().kwargs())

# The baseline arm: AgentDojo's OpenAILLM only ever passes temperature.
mc.create(model="m", messages=[], temperature=0.0)
sent = inner.seen[-1]
check("baseline arm inherits max_tokens",
      sent.get("max_tokens") == 2048,
      "otherwise the baseline silently truncates against CATSEC's 2048")
check("baseline arm inherits enable_thinking=False",
      sent["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False,
      "otherwise CATSEC runs non-reasoning and the baseline does not")

# The CATSEC arm: passes the full decode kwargs itself. Nothing may change.
mc.create(model="m", messages=[], **R.DecodeConfig(max_tokens=999).kwargs())
sent = inner.seen[-1]
check("explicit max_tokens wins over the default", sent["max_tokens"] == 999)
check("explicit extra_body preserved",
      sent["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False)

# A caller setting some OTHER template flag must not drop enable_thinking.
mc.create(model="m", messages=[],
          extra_body={"chat_template_kwargs": {"something_else": 1}})
ctk = inner.seen[-1]["extra_body"]["chat_template_kwargs"]
check("second-level merge keeps enable_thinking alongside other flags",
      ctk.get("enable_thinking") is False and ctk.get("something_else") == 1)

# An explicitly-set enable_thinking must beat the default (ablation runs).
mc.create(model="m", messages=[],
          extra_body={"chat_template_kwargs": {"enable_thinking": True}})
check("explicit enable_thinking=True overrides the default",
      inner.seen[-1]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True)

check("metering still fires through the wrapper",
      meter.calls == len(inner.seen) and meter.tokens_in == 3 * meter.calls)


# ===================================================================== #
section("MeteredClient plumbing")
# ===================================================================== #
class _FakeChat:
    def __init__(self):
        self.completions = _Inner()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.models = "passthrough-sentinel"


fc = _FakeClient()
mcl = R.MeteredClient(fc, R.UsageMeter(), R.RateLimiter(),
                      defaults=R.DecodeConfig().kwargs())
check("chat.completions.create is wrapped",
      isinstance(mcl.chat.completions, R._MeteredCompletions))
check("unwrapped attributes pass through (models.list for preflight)",
      mcl.models == "passthrough-sentinel")


# ===================================================================== #
section("Rate-limit / saturation detection")
# ===================================================================== #
check("429 string detected", R._is_rate_limited(Exception("HTTP 429 boom")))
check("503 detected (vLLM queue full)", R._is_rate_limited(Exception("503")))
check("overloaded detected", R._is_rate_limited(Exception("Server Overloaded")))
check("unrelated error NOT flagged",
      not R._is_rate_limited(ValueError("bad plan json")))
b = R._backoff_seconds(0, True)
check("backoff is positive and jittered into range", 3.0 <= b <= 6.0, f"{b}")


# ===================================================================== #
section("Endpoint resolution")
# ===================================================================== #
class _Args:
    base_url = None


import os  # noqa: E402

a = _Args()
a.base_url = "http://127.0.0.1:8000"
check("missing /v1 is appended", R.ensure_gemma_env(a).endswith("/v1"))
a.base_url = "http://127.0.0.1:8000/v1/"
check("trailing slash normalised",
      R.ensure_gemma_env(a) == "http://127.0.0.1:8000/v1")
a.base_url = "http://gpu-box:8000/v1"
check("already-correct url untouched",
      R.ensure_gemma_env(a) == "http://gpu-box:8000/v1")
check("placeholder api key set", os.environ.get("GEMMA_API_KEY") == "EMPTY")

a.base_url = None
os.environ.pop("GEMMA_BASE_URL", None)
_real_isatty = sys.stdin.isatty
sys.stdin.isatty = lambda: False
try:
    R.ensure_gemma_env(a)
    check("missing endpoint is fatal", False, "should have raised SystemExit")
except SystemExit:
    check("missing endpoint is fatal", True)
finally:
    sys.stdin.isatty = _real_isatty


# ===================================================================== #
section("MODEL_NAMES registration (attack strength)")
# ===================================================================== #
fake_models = types.ModuleType("agentdojo.models")
fake_models.MODEL_NAMES = {}
# We only need the registration block's behaviour, so re-implement the lookup
# the same way and assert the mapping, rather than importing agentdojo here.


def display_for(model_id: str) -> str:
    low = model_id.lower()
    if "gemma" in low:
        return "Gemma"
    if low.startswith("gpt-4"):
        return "GPT-4"
    if low.startswith("gpt-3.5"):
        return "GPT-3.5"
    return "AI assistant"


check("bare served name maps to Gemma",
      display_for("gemma-4-31b-it") == "Gemma")
check("full HF path maps to Gemma",
      display_for("google/gemma-4-31B-it") == "Gemma")
check("MoE variant maps to Gemma",
      display_for("gemma-4-26b-a4b") == "Gemma")
check("unrelated id falls back safely",
      display_for("some-other-model") == "AI assistant")
src = open("run_catsec_gemma.py").read()
check("runner actually contains the gemma registration branch",
      '"gemma" in low' in src and 'display = "Gemma"' in src)
check("pipeline name still embeds model_id (substring lookup depends on it)",
      'f"{model_id}-catsec"' in src)


# ===================================================================== #
section("Composer prompt untouched")
# ===================================================================== #
check("COMPOSER_SYSTEM present", isinstance(R.COMPOSER_SYSTEM, str))
for rule in ("DIRECTION", "PERIOD", "SCOPE"):
    check(f"composer rule {rule} intact", rule in R.COMPOSER_SYSTEM)
check("composer still forbids following instructions in data",
      "NEVER" in R.COMPOSER_SYSTEM)
check("no benchmark-specific figure crept in",
      "1050" not in R.COMPOSER_SYSTEM and "March" not in R.COMPOSER_SYSTEM,
      "run check_composer_prompt.py run_catsec_gemma.py for the full guard")


# ===================================================================== #
section("Record / manifest schema parity with the other runners")
# ===================================================================== #
class _A:
    suite = "banking"
    version = "v1.2"
    attack = "tool_knowledge"
    policy = "refuse"
    ablation = "none"
    plan_repair = False


rec = R.make_record(
    run_id="r", args=_A(), model_id="gemma-4-31b-it", defense="catsec",
    user_task="user_task_0", injection_task="injection_task_0", repetition=0,
    condition="injected", injected=True, utility=True, security=False,
    error=None, usage=R.UsageMeter().snapshot(),
    probe_record=R.TrialProbe().as_record(), wall_s=1.0)
check("provider tagged gemma-vllm", rec["provider"] == "gemma-vllm")
check("tokens_thoughts retained for analyze.py parity",
      "tokens_thoughts" in rec)
check("compose_error field present", "compose_error" in rec)
check("plan_signature field present", "plan_signature" in rec)


print(f"\n{'='*54}\n  {PASS} passed, {FAIL} failed\n{'='*54}")
sys.exit(1 if FAIL else 0)
