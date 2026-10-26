# Gemini leg via Google AI Studio — run order

`kernel.py`, `planner.py`, `analyze.py`, `selftest.py` are **unchanged**. Only
`run_catsec_gemini.py` is new. `check_composer_prompt.py` gained one line so it
can be pointed at any runner; its old no-argument behaviour is identical.

## 0. Setup

```bash
cp .env.example .env      # then paste your AI Studio key into GEMINI_API_KEY
pip install google-genai python-dotenv
```

## 1. Offline gates — no key, no spend, ~2 seconds

Run all three before touching the API. If any fails, stop.

```bash
python selftest.py                                  # kernel + planner
python check_composer_prompt.py run_catsec_gemini.py  # overfitting guard
python selftest_gemini_wiring.py                    # provider plumbing (32 checks)
```

`selftest_gemini_wiring.py` reads `run_catsec_openai.py` from the same
directory to assert the composer prompt is byte-identical across legs. Keep
both runners together.

## 2. Smoke test — one real pair

```bash
python run_catsec_gemini.py --smoke-test-only
```

Check three things in the output before going further:

- `plan emitted:` shows real steps (not `PLAN FAILURE`)
- `tokens=…(thoughts=0)` — thoughts should be **0**. Anything else means
  `ThinkingConfig` was not accepted and the run is not comparable to the
  gpt-4o-mini and haiku legs.
- `throttle={… 'rate_limit_events': 0 …}`

## 3. Attack-strength check — no model calls at all

```bash
python run_catsec_gemini.py --attack important_instructions --print-attack-text
```

Confirm the rendered jailbreak names a Google model rather than reading
generically. A wrong name silently *weakens* the attack on the Google path and
would flatter the baseline's security number.

## 4. Gate 1 — small paired run

```bash
python run_catsec_gemini.py \
  --max-pairs 30 --pair-seed 0 \
  --plan-repair \
  --jsonl runs/gemini_gate1.jsonl \
  --out gemini_gate1.csv
```

## 5. Full suite

```bash
python run_catsec_gemini.py \
  --all-pairs --plan-repair \
  --jsonl runs/gemini_full.jsonl \
  --out catsec_results_gemini.csv \
  --resume
```

Then, as always:

```bash
python analyze.py --jsonl runs/gemini_full.jsonl
```

## Tier and quota

`--tier free` (the default) paces at 7s/call and stops cleanly after 200 model
calls. `--tier paid` turns pacing off and removes the cap. Override either
directly with `--min-call-interval` / `--max-requests`.

The full 288-trial run will **not** finish in one free-tier day. That is
expected and handled: the run stops at the budget without writing a partial
trial, and rerunning the identical command with `--resume` continues from a
clean boundary. Always take final numbers from `analyze.py` on the JSONL, never
from the per-session CSV.

Current limits for your key: https://ai.google.dev/gemini-api/docs/rate-limits

## Deduplication

Never run two processes against the same JSONL. The full banking run in the
GPT-4o-mini leg picked up 143 duplicate rows that way after an SSH disconnect
left the first process alive. Check before analysing:

```bash
wc -l runs/gemini_full.jsonl
python -c "import json;rs=[json.loads(l) for l in open('runs/gemini_full.jsonl')];print(len({r['run_id'] for r in rs}),'run_ids')"
```

Use `tmux` or `nohup` so a dropped SSH session doesn't tempt a restart.
