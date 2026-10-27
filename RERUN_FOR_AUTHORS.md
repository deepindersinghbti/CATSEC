# CATSEC clean-prompt rerun — instructions for Deepinder / Vishwanath

## Why
The planner prompt used for the submitted runs contains AgentDojo banking test tasks as worked
examples (Example 5 is `user_task_12` with the ground-truth amount 1200; also ut7, and ut2/ut15
almost verbatim), and three repair heuristics are written for user tasks 2, 12 and 15. A TDSC
reviewer who reads the released code will treat this as test-set contamination. Gemini's utility
falls from 68.8% to 62.5% without the copied ut12 plan.

This branch adds a **clean prompt** (zero overlap with every AgentDojo task — run
`make contamination`), generic repair rules, and a frozen protocol (`PROTOCOL.md`, SHA-256 hashes
checked before every run). The rerun results become the paper's main numbers; the old runs become
a short comparison. Please read `PROTOCOL.md` and add your sign-off there.

## What it costs
About **$12 in total** for GPT-4o-mini + Claude Haiku 4.5 + Gemini 2.5 Flash (please allow $25 for
retries), plus one GPU session for Gemma 4 31B (H100/H200 BF16 or 48 GB FP8, ~2–3 h including
download). Set a spend limit on each provider's billing page as a safety net.

## Steps
```bash
git fetch && git checkout revision/abdul-2026-10       # or apply the patches with `git am`
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt                        # Gemma box: also requirements-vllm.txt
make test                                              # 64 tests, offline
make protocol-check                                    # must say PROTOCOL FREEZE: intact

cp IEEE_Transactions/.env.example .env                 # then put your keys in .env
                                                       # OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY

./run_clean_rerun.sh gpt smoke                         # 2 pairs, a few cents: checks keys work
./run_clean_rerun.sh gpt full                          # then claude, gemini the same way
./run_clean_rerun.sh claude all
./run_clean_rerun.sh gemini all
# Gemma (GPU box with vLLM serving gemma-4-31b-it, see Journal_Experiments/Gemma4_31B/README_gemma.md):
./run_clean_rerun.sh gemma all

./run_clean_rerun.sh bundle                            # makes catsec_clean_rerun_<date>.tar.gz
```
Each run can be resumed by repeating the same command. Please **do not edit any file under
`catsec/prompts/`** or the heuristics — the script refuses to run if the frozen hashes change.

## What each run produces (in `runs_clean/<model>/`)
| File | What it is | Paper use |
|---|---|---|
| `clean_main.jsonl` | 144 pairs × 3 repetitions, undefended and CATSEC | new Table II (security, utility) |
| `clean_benign.jsonl` | 16 tasks, no injection, both arms | benign utility of both arms |
| `kfold_0..3.jsonl` | 4-fold variant, CATSEC arm | robustness to choice of examples |

## Send back
The `catsec_clean_rerun_<date>.tar.gz` from the `bundle` step (it includes the commit hash and the
protocol check). Abdul will run the analysis, update the paper and share the revised draft.
