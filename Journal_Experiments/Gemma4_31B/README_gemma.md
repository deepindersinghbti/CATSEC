# CATSEC — Gemma 4 leg (self-hosted, rented GPU)

The open-weight leg of the model comparison. Serves Gemma 4 yourself with vLLM
and points the existing harness at it. `kernel.py` and `planner.py` are
**unchanged** — the security machinery is provider-agnostic and stays that way,
which is the whole reason a new leg costs one file instead of a rewrite.

## Files you need in the run directory

| File | Where it comes from |
|---|---|
| `run_catsec_gemma.py` | **new** (this bundle) |
| `selftest_gemma_wiring.py` | **new** (this bundle) |
| `.env.example` | **new** (this bundle) → copy to `.env` |
| `kernel.py` | your repo, unchanged |
| `planner.py` | your repo — **fix the stray trailing `l ` on the last line first** |
| `analyze.py` | your repo, unchanged |
| `selftest.py` | your repo — must be the **61/61** version, not the stale 41/41 copy |
| `check_composer_prompt.py` | your repo, unchanged (already takes a runner path arg) |

## Model choice

`google/gemma-4-31b-it` — the dense 31B instruction-tuned model. It is the
strongest reasoner in the family, and CATSEC's utility depends almost entirely
on one-shot plan quality. `gemma-4-26b-a4b` (MoE, ~4B active) is the fallback if
throughput is the binding constraint; expect weaker plans.

**Thinking mode is OFF by default and should stay off.** The GPT-4o-mini and
Claude Haiku legs are both non-reasoning. Turning it on here would confound a
model difference with an inference-budget difference. `--thinking` exists only
as a deliberate, separately-reported ablation.

## Server

```bash
vllm serve google/gemma-4-31b-it \
    --served-model-name gemma-4-31b-it \
    --enable-auto-tool-choice --tool-call-parser pythonic \
    --max-model-len 32768
```

bf16 weights are ~62 GB, so this wants a single **H100 80GB**. On 48 GB you
would need FP8/AWQ, which introduces a quantization confound you would have to
disclose in the paper — rent the bigger card instead.

The tool-choice flags matter for the **baseline arm only**. That arm is
AgentDojo's stock function-calling loop; without working tool calls every
baseline trial degrades into a no-tool chat and its utility number is
meaningless. The CATSEC arm declares no tools at all, so it is unaffected.
Confirm the parser name against your vLLM version's docs — it is the flag most
likely to have changed.

## Run order

```bash
# 0. Offline, before you rent anything. No key, no server, no GPU.
python selftest.py                                  # expect 61/61
python selftest_gemma_wiring.py                     # expect 41/41
python check_composer_prompt.py run_catsec_gemma.py # expect 30/30

# 1. On the GPU box, once vllm is up.
cp .env.example .env
python run_catsec_gemma.py --preflight

# 2. One real pair end to end. Note the wall time.
python run_catsec_gemma.py --smoke-test-only

# 3. Small paired run — sanity, not a result.
python run_catsec_gemma.py --max-pairs 30 \
    --jsonl runs/gemma_pilot.jsonl --out gemma_pilot.csv

# 4. Full suite, 288 trials. Use nohup/tmux; --resume is safe to re-invoke.
python run_catsec_gemma.py --all-pairs --plan-repair \
    --jsonl runs/gemma_full.jsonl --out catsec_results_gemma.csv --resume

python analyze.py --jsonl runs/gemma_full.jsonl
```

`--preflight` checks four things and exits: the server is reachable, the id in
`--model` is actually served, a completion works with your thinking flag, and
the server will emit a tool call. Run it every time — each of those failures
otherwise surfaces hours in, and two of them surface as bad numbers rather than
as errors.

## Two things that will bite

**Set `--trial-timeout` from your own measurement.** It defaults to 240s here
(the OpenAI runner used 120s) because a locally served 31B is slower per token.
A timeout is logged as an *error*, and errored trials drop out of the
denominator — so a too-tight timeout silently deletes real trials rather than
failing loudly. Take the smoke test's wall time and set roughly 3×.

**One run per JSONL.** The 288-trial run on the OpenAI leg picked up 143
duplicate rows when an SSH disconnect left the first process alive and a second
was started over it. Use `tmux`, and check `pgrep -f run_catsec_gemma` before
relaunching.

## What's actually different inside the runner

- Client points at your vLLM endpoint; `--base-url` / `GEMMA_BASE_URL` is
  required, with no default (an unset base URL silently hits api.openai.com).
- `DecodeConfig` drops `reasoning_effort` for `enable_thinking`, sent explicitly
  in both states via `extra_body.chat_template_kwargs`.
- `_MeteredCompletions` now injects decode **defaults**. AgentDojo's `OpenAILLM`
  only ever passes `temperature`, so without this the baseline arm would run at
  the server's default `max_tokens` and the template's default thinking mode
  while CATSEC ran at 2048 and non-reasoning — the same silent-truncation trap
  already hit on the Claude leg, plus a reasoning-budget confound on top.
  Explicit values always win, so the CATSEC arm is untouched.
- `MODEL_NAMES` registration gains a `gemma` branch. Required, not optional: the
  `important_instructions` attack looks the display name up by substring and
  raises `ValueError` on a miss.
- `_is_rate_limited` also catches 503 (vLLM's queue-full response).
- SDK-level retries disabled (`max_retries=0`) so every failure reaches the
  meter and the adaptive limiter — hidden SDK retries would be unmetered.
- `provider` is `gemma-vllm` in every record; the manifest additionally records
  `base_url` and `enable_thinking`, since an open-weight leg is only reproducible
  if the endpoint, served id and thinking state are all on the record.
