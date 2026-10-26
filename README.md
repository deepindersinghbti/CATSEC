# CATSEC: a categorical verification layer for LLM agents

Code, logs and results for the paper *Categorical Verification Layer for Agentic AI:
A Functional Calculus for Type-Safe Workflows* (submitted to IEEE Transactions on
Dependable and Secure Computing).

CATSEC is a defence against prompt injection for tool-using LLM agents. The planner
writes the whole plan before any tool output is seen; a small kernel executes it and
refuses any security-critical action whose argument value cannot be traced to a trusted
source. The evaluation uses the AgentDojo banking suite (144 user-task x injection-task
pairs per arm) with four planners: GPT-4o-mini, Claude Haiku 4.5, Gemini 2.5 Flash and
Gemma 4 31B.

## Layout

```
Journal_Experiments/          the four main runs, one folder per model, kept as they ran
  GPT/  Claude/  Gemini/  Gemma4_31B/
      kernel.py, planner.py         the defence (identical in every folder)
      run_catsec_<provider>.py      the runner used for that model
      selftest*.py, check_composer_prompt.py, analyze.py
      runs/<name>.jsonl             the final log (288 trials)
      runs/<name>.manifest.json     exact command, settings and package versions
  Graph_4models/                four-model results figure and the script that draws it
IEEE_Transactions/            follow-up experiments E1-E7 (see IEEE_Transactions/README.md)
  code/  data/logs/  experiments/  results/  paper/
```

`kernel.py` and `planner.py` are byte-identical in all four model folders and in
`IEEE_Transactions/code/` (SHA-256 prefixes `655f960c703c` and `9a5abd951997`).
The four logs in `IEEE_Transactions/data/logs/` and `Journal_Experiments/Graph_4models/`
are identical copies of the logs in each model's `runs/` folder.

## Requirements

Use **Python 3.12**. Under Python 3.10, AgentDojo 0.1.35 raises
`PydanticUserError: TaskResults is not fully defined` during a run.

```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins the versions recorded in the run manifests.

The figures use a Times-style serif font. On Ubuntu, `sudo apt install fonts-urw-base35`
gives Nimbus Roman, which reproduces the published figures pixel for pixel; without it
matplotlib falls back to DejaVu Serif (same data, different lettering).

## Reproducing the results

### Offline (no API keys, no GPU)

```
# self-tests, from inside a model folder
cd Journal_Experiments/GPT
python selftest.py                                  # 61/61
python check_composer_prompt.py                     # 30/30

# main results table for one model (example: GPT-4o-mini)
python ../Claude/analyze.py --jsonl runs/banking_final.jsonl

# four-model figure
cd ../..
python Journal_Experiments/Graph_4models/make_results_summary_with_pdf_bold.py \
    --logdir Journal_Experiments/Graph_4models --out results_summary.png
```

Experiments E1-E6 replay the logged plans offline; commands are in
`IEEE_Transactions/README.md`.

### Live runs (API keys or a GPU needed, and they cost money)

Each model folder has its own runner. Copy that folder's `.env.example`
(`env.example.txt` for Gemma) to `.env` and add your key. Gemma 4 31B is
self-hosted with vLLM; see `Journal_Experiments/Gemma4_31B/README_gemma.md`.
E7 calls the provider APIs for the answer composer only; its results cover
GPT-4o-mini, Claude Haiku 4.5 and Gemini 2.5 Flash.

## Notes

- `git_commit` in the run manifests refers to the private development repository
  these runs were made in, not to this repository.
- `analyze.py` prints a USD cost estimate using API prices. For the self-hosted
  Gemma run those dollar figures are not meaningful; the call, token and latency
  ratios are.
- The per-trial AgentDojo trace folders are not included; the `.jsonl` logs are the
  complete record used by every script here.
