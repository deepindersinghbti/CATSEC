#!/usr/bin/env python3
"""
Offline wiring test for run_catsec_claude.py.

Makes NO API calls and needs NO key -- it uses fake clients to check the
provider plumbing only. It exists because the most dangerous failure in this
runner is SILENT: if the metered proxy does not survive AnthropicLLM's
constructor, the baseline arm quietly runs unmetered and unpaced, every
number still looks plausible, and the token/cost comparison in the paper is
wrong with nothing to indicate it.

Complements (does not replace) selftest.py, which covers kernel/planner.

Run:  python selftest_claude_wiring.py
"""
from __future__ import annotations
import run_catsec_claude as R

import asyncio
import sys
import types

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'  PASS' if cond else '  FAIL'}  {name}"
          + (f"  --  {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
print("DECODE CONFIG")
# --------------------------------------------------------------------------- #
d = R.DecodeConfig()
k = d.kwargs()
check("max_tokens always sent (Anthropic requires it)", "max_tokens" in k)
check("top_p omitted when None (avoids the temperature/top_p clash)",
      "top_p" not in k)
check("temperature sent", k.get("temperature") == 0.0)
check("no 'thinking' key when budget unset -- the non-reasoning default "
      "that keeps this comparable to the gpt-4o-mini leg", "thinking" not in k)

d2 = R.DecodeConfig(thinking_budget=1024)
k2 = d2.kwargs()
check("thinking budget, when set, also forces temperature=1 as the API "
      "requires", k2.get("thinking") and k2.get("temperature") == 1)


# --------------------------------------------------------------------------- #
print("\nRESPONSE PARSING")
# --------------------------------------------------------------------------- #


def blocks(*specs):
    r = types.SimpleNamespace()
    r.content = [types.SimpleNamespace(type=t, text=x) for t, x in specs]
    return r


check("text extracted from a single text block",
      R._text_of(blocks(("text", "hello"))) == "hello")
check("multiple text blocks are joined",
      R._text_of(blocks(("text", "a"), ("text", "b"))) == "ab")
check("non-text blocks are skipped, not crashed on (the content[0].text "
      "trap)",
      R._text_of(blocks(("thinking", "secret"), ("text", "plan"))) == "plan")
check("empty content yields empty string, not an exception",
      R._text_of(types.SimpleNamespace(content=[])) == "")


# --------------------------------------------------------------------------- #
print("\nRATE-LIMIT DETECTION")
# --------------------------------------------------------------------------- #
check("429 in message detected", R._is_rate_limited(Exception("Error 429")))
check("overloaded (529) detected",
      R._is_rate_limited(Exception("overloaded_error")))
check("ordinary errors are NOT treated as rate limits",
      not R._is_rate_limited(Exception("bad request: invalid field")))


# --------------------------------------------------------------------------- #
print("\nMETERING: sync arm (planner + composer)")
# --------------------------------------------------------------------------- #


class FakeUsage:
    input_tokens = 11
    output_tokens = 7


class FakeMsg:
    usage = FakeUsage()
    content = [types.SimpleNamespace(type="text", text="ok")]


class FakeSyncMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        self.last = kw
        return FakeMsg()


class FakeSyncClient:
    def __init__(self):
        self.messages = FakeSyncMessages()


meter = R.UsageMeter()
limiter = R.RateLimiter(base_interval=0.0, max_interval=0.0)
sync_inner = FakeSyncClient()
sync = R.MeteredClient(sync_inner, meter, limiter)

sync.messages.create(model="m", system="s", messages=[], max_tokens=16)
check("sync call reaches the real client", sync_inner.messages.calls == 1)
check("sync call is metered (Anthropic input_tokens/output_tokens)",
      meter.calls == 1 and meter.tokens_in == 11 and meter.tokens_out == 7,
      f"in={meter.tokens_in} out={meter.tokens_out}")


# --------------------------------------------------------------------------- #
print("\nMETERING: async arm (baseline, via AnthropicLLM)")
# --------------------------------------------------------------------------- #


class FakeStream:
    async def get_final_message(self):
        return FakeMsg()


class FakeStreamCtx:
    async def __aenter__(self):
        return FakeStream()

    async def __aexit__(self, *a):
        return False


class FakeAsyncMessages:
    def __init__(self):
        self.calls = 0

    def stream(self, **kw):
        self.calls += 1
        self.last = kw
        return FakeStreamCtx()


class FakeAsyncClient:
    def __init__(self):
        self.messages = FakeAsyncMessages()


async_inner = FakeAsyncClient()
async_proxy = R.MeteredAsyncClient(async_inner, meter, limiter)


async def drive():
    # Mirrors exactly what agentdojo's chat_completion_request does.
    async with async_proxy.messages.stream(model="m", messages=[]) as st:
        return await st.get_final_message()


msg = asyncio.run(drive())
check("async streaming call reaches the real client",
      async_inner.messages.calls == 1)
check("async call is metered too", meter.calls == 2)
check("BOTH arms accumulate into ONE meter -- this is what keeps the "
      "per-arm token comparison honest",
      meter.tokens_in == 22 and meter.tokens_out == 14,
      f"in={meter.tokens_in} out={meter.tokens_out}")


# --------------------------------------------------------------------------- #
print("\nTHE SILENT-FAILURE GUARD: proxy must survive AnthropicLLM.__init__")
# --------------------------------------------------------------------------- #
# AnthropicLLM does:
#     if isinstance(client, Anthropic): self.client = AsyncAnthropic(...)
#     else:                             self.client = client
# A sync client is DISCARDED and rebuilt -- unmetered, unpaced, no error.
try:
    from anthropic import Anthropic, AsyncAnthropic
    real_async = AsyncAnthropic(api_key="test-not-used")
    proxy = R.MeteredAsyncClient(real_async, R.UsageMeter(), limiter)
    check("metered proxy is NOT an anthropic.Anthropic instance, so "
          "AnthropicLLM keeps it instead of rebuilding its own",
          not isinstance(proxy, Anthropic))
    check("a SYNC client WOULD be discarded (documents why we pass the "
          "async proxy)", isinstance(Anthropic(api_key="test-not-used"),
                                     Anthropic))
    check("proxy still exposes .messages.stream for agentdojo to call",
          hasattr(proxy.messages, "stream"))
    check("unknown attributes fall through to the real client",
          proxy.api_key == "test-not-used")
except ImportError:
    check("anthropic SDK present for the isinstance guard", False,
          "pip install anthropic")


# --------------------------------------------------------------------------- #
n_pass, n_all = sum(results), len(results)
print(f"\n{'=' * 62}\n{n_pass}/{n_all} checks passed\n{'=' * 62}")
sys.exit(0 if n_pass == n_all else 1)
