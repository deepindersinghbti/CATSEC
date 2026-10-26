# CATSEC: Categorical Control-Flow Isolation for Prompt-Injection-Resilient LLM Agents

Code, logs and results for the paper *CATSEC: Categorical Control-Flow
Isolation for Prompt-Injection-Resilient LLM Agents* (submitted to IEEE
Transactions on Dependable and Secure Computing).

CATSEC is a defence against prompt injection for tool-using LLM agents. The planner
writes the whole plan before any tool output is seen; a small kernel executes it and
refuses any security-critical action whose argument value cannot be traced to a trusted
source. The evaluation uses the AgentDojo banking suite (144 user-task x injection-task
pairs per arm) with four planners: GPT-4o-mini, Claude Haiku 4.5, Gemini 2.5 Flash and
Gemma 4 31B.

> **Planner-prompt contamination (read first).** The planner prompt used for
> every logged run (`--prompt-variant original`, the default, kept byte-identical
> so the published numbers reproduce) contains AgentDojo banking *test* tasks as
> worked examples, and three repair heuristics written for specific test tasks.
> A decontaminated, frozen protocol (`--profile clean`, plus a 4-fold
> dev/test variant) is described in [PROTOCOL.md](PROTOCOL.md); its overlap with
> every AgentDojo task is measured by `tools/check_contamination.py`. New results
> must use it.

## Layout

```
catsec/                       the defence, ONCE (imported by every runner and replay)
  kernel.py                   provenance lattice, plan grammar, extractors, interpreter
  planner.py                  plan parsing/validation, build_plan, repair loop
  prompts/                    frozen planner/repair prompts (original, clean_v1, kfold)
  heuristics.py               generic repair heuristic of the clean protocol
  contracts.py                tool security contracts, AgentDojo tool wiring
  composer.py                 answer-composer prompt
  harness.py, options.py      per-trial probe/JSONL logging; revision options/profiles
Journal_Experiments/          the four main runs, one folder per model
  GPT/  Claude/  Gemini/  Gemma4_31B/
      run_catsec_<provider>.py      the runner used for that model (GPT runner also
                                    serves any OpenAI-compatible local endpoint)
      selftest_<provider>_wiring.py offline wiring test with fake clients
      runs/<name>.jsonl             the final log (288 trials)
      runs/<name>.manifest.json     exact command, settings and package versions
  Graph_4models/                four-model results figure and the script that draws it
IEEE_Transactions/            follow-up experiments E1-E7 (see IEEE_Transactions/README.md)
  data/logs/  experiments/  results/  paper/verify_claims.py
analysis/analyze.py           main results table, McNemar, costs, for any log
tests/                        pytest suite (unit, regression, protocol, offline end-to-end)
tools/                        contamination check, composer-prompt guard, regression
                              comparison, bug-fix impact replay, rerun cost estimate
reproduce.sh, Makefile        one-command offline reproduction
PROTOCOL.md                   pre-registered clean planning protocol (SHA-256 frozen)
LICENSE.TODO                  licence not chosen yet -- owner decision needed
```

The four logs in `IEEE_Transactions/data/logs/` and `Journal_Experiments/Graph_4models/`
are identical copies of the logs in each model's `runs/` folder.

## Requirements

Use **Python 3.12** (3.12.3 verified).

```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # direct deps, pinned
# or, for the exact verified environment:
pip install -r requirements-lock.txt     # every transitive package pinned
```

The Gemma client environment and the pinned vLLM server are in
`Journal_Experiments/Gemma4_31B/requirements*.txt`.

The figures use a Times-style serif font. On Ubuntu, `sudo apt install fonts-urw-base35`
gives Nimbus Roman, which reproduces the published figures pixel for pixel; without it
matplotlib falls back to DejaVu Serif (same data, different lettering).

## Reproducing the results

### Offline (no API keys, no GPU, no cost)

```
make test                 # pytest: unit, regression, protocol, offline end-to-end
make offline-repro        # = ./reproduce.sh : self-tests, analyze.py on the 4 logs,
                          #   E1-E6 replays for 4 planners, figures, verify_claims.py,
                          #   and a byte/pixel comparison with the committed results
make offline-repro-quick  # the same without E6 (~3 min instead of ~30)
make contamination        # prompt overlap with every AgentDojo task, all variants
make impact               # offline replay impact of the 2026-10 bug fixes
make cost                 # cost of the live reruns, from the logged token usage
```

Individual pieces: `python tests/selftest.py` (61/61),
`python tools/check_composer_prompt.py` (34/34),
`python analysis/analyze.py --jsonl Journal_Experiments/GPT/runs/banking_final.jsonl`,
`python IEEE_Transactions/paper/verify_claims.py` (asserts the paper's numbers),
experiment commands in `IEEE_Transactions/README.md`.

### Live runs (API keys or a GPU needed; hosted APIs cost money)

Each model folder has its own runner. Copy that folder's `.env.example`
(`env.example.txt` for Gemma) to `.env` and add your key. Gemma 4 31B is
self-hosted with vLLM; see `Journal_Experiments/Gemma4_31B/README_gemma.md`.
Local/open models run through any OpenAI-compatible server (Ollama, vLLM) with
the GPT runner's `--base-url`; see [docs/local_models.md](docs/local_models.md).

Revision options (all runners; defaults reproduce the logged runs):

| flag | values | meaning |
|---|---|---|
| `--profile` | `paper` (default), `hardened`, `clean` | bundles of the options below |
| `--prompt-variant` | `original`, `clean`, `kfold` (+ `--fold 0..3`) | planner prompt |
| `--repair-heuristics` | `auto`, `all`, `generic`, `none` | repair-loop heuristics |
| `--repair-with-request` | flag | repair prompt includes the user request |
| `--unknown-tool-policy` | `open`, `closed` | fail-closed gate for uncontracted tools |
| `--amount-parser` | `strict` (default), `legacy` | thousands separators / decimal commas |

Every new run writes the SHA-256 of its planner, repair and composer prompts
into the manifest and into every JSONL record.

## Notes

- `git_commit` in the run manifests refers to the private development repository
  these runs were made in, not to this repository.
- `analyze.py` prints a USD cost estimate using API prices. For the self-hosted
  Gemma run those dollar figures are not meaningful; the call, token and latency
  ratios are.
- The per-trial AgentDojo trace folders are not included; the `.jsonl` logs are the
  complete record used by every script here.
