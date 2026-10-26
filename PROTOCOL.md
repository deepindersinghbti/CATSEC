# CATSEC clean planning protocol (pre-registration record)

**Status:** v1 frozen on 2026-10-26 by A. M. Khan, **before any test run** with
these prompts. No model has been run with them; no banking result has been
looked at with them. Pending co-author sign-off (D. Singh, V. Bijalwan): sign
below, then treat this file and the artefacts it hashes as immutable. Any
change after the first test run must be a new version (`clean_v2`, ...) with
its own entry here, and results of every version that was run must be
reported.

## Why

The planner prompt used for every logged run (`--prompt-variant original`) has
worked examples taken from the AgentDojo banking **test** tasks (WE2 = ut7
verbatim, WE5 = ut12 verbatim including the ground-truth amount 1200, WE3 ≈ ut2,
WE6 ≈ ut15, WE1/WE4 = the plan structure of ut0/ut14), and three repair
heuristics written for user_task_2, _12 and _15. `tools/check_contamination.py`
quantifies this (2 verbatim, 2 near-verbatim, 3 ground-truth literal leaks,
5 exact plan matches).

## The protocol

1. **Planner prompt.** `catsec/prompts/clean_v1.txt`: the same plan grammar
   (FORMAT / STEP TYPES / REFERENCES), task-agnostic rules, and five worked
   examples from an unrelated application (an online shop's back office, tools
   `open_document`, `pay_invoice`, `list_orders`, `list_subscriptions`,
   `edit_subscription`, `rotate_api_key`, `set_warehouse_address`,
   `issue_refund`, none of which exists in any AgentDojo suite). They were
   written from scratch, not adapted from any AgentDojo task.
2. **Repair.** `--plan-repair` with up to 2 rounds (as in the logged runs), the
   repair prompt `catsec/prompts/repair_with_request_v1.txt` (contains the
   user's request, the validator/heuristic error and the previous output --
   never tool output), and only the generic heuristic in
   `catsec/heuristics.py` (a critical-argument literal that looks like an
   identifier but is absent from the request). No heuristic names or encodes
   a test task.
3. **Contracts / kernel.** Unchanged contracts (Table I of the paper),
   `--unknown-tool-policy closed` (fail-closed), strict amount parser.
   `--profile clean` sets 1–3.
4. **Fixed before running:** temperature 0, the logged decoding settings per
   model, AgentDojo 0.1.35 suite v1.2, attack `tool_knowledge`, all 144 pairs,
   both arms, plus a no-injection (`--no-injection`) run of both arms.
5. **No tuning after freezing.** The banking results obtained with this
   protocol are reported whatever they are. Failures may be analysed and
   discussed, but any prompt/heuristic change triggers a new version, and the
   new version must also be evaluated on the never-tuned suites (6).
6. **Never-tuned generalisation test.** workspace, slack and travel are run
   with the same frozen prompt; their contracts and read-only declarations
   (`catsec/contracts.py`, `READ_ONLY_DECLARATIONS`, currently DRAFT) must be
   reviewed and frozen in a v1.1 entry here **before** the first run on them.

### Alternative variant: 4-fold dev/test (`--prompt-variant kfold --fold f`)

For a within-banking estimate that still uses banking-style examples: the 16
banking user tasks are split into the 4 folds of `catsec.prompts.KFOLD_FOLDS`
(grouped so that tasks sharing ground-truth values -- ut2/ut12, ut3/ut4/ut15
-- sit in the same fold). To test fold *f*, the planner sees the clean
prompt **plus** the original banking worked examples whose source task is in
the other three (development) folds, and is run **only** on fold *f*'s tasks
(the runners enforce this). The union of the four test folds is the 16 tasks.
`check_contamination.py` verifies that no fold prompt shares text or a
ground-truth argument with its own test fold. Caveat: all banking tasks share
one environment, so development-fold examples still mention environment
content (e.g. a file name); report this.

## Frozen artefacts

Verified by `python tools/protocol_hashes.py --check` and
`tests/test_protocol.py`; every new run also writes these hashes into its
manifest and into every JSONL record (`catsec_run.prompt_sha256`).

| artefact | SHA-256 |
|---|---|
| `catsec/prompts/clean_v1.txt` | `36bebb09cda9f13a82d00a76413b8ccc01e163a891dc8dbb08c6699964394d24` |
| `catsec/prompts/repair_with_request_v1.txt` | `26dd128d7c92467340ddbb591c6e91ff709c6d11bf28db40696835bd726df1d7` |
| `catsec/heuristics.py` | `f8fa8afaac691f7c447288004c6aad9af5a1830f3da218f5dcbbf2551fd58dfb` |
| `kfold fold 0 (rendered template)` | `f36d279b4a65746fba256cdf7859b27d83f583c54723ec8bb0431e6a8b5b3a9d` |
| `kfold fold 1 (rendered template)` | `61a0ab805235fd48d790a3975482809a3f9666d27bb1029e8e65c42270afe010` |
| `kfold fold 2 (rendered template)` | `a7d9012c9f01bcd6778add318b42d584a1a924f48406c721ea9835e7c3fd0469` |
| `kfold fold 3 (rendered template)` | `701a421149a292787a480621b23c69f3c5dfec735ede70f17b50728a9e51432b` |
| `catsec/prompts/original.txt (logged runs, unchanged)` | `122d024001ce743b13816d42e6390bf9f7e010abe00c99a406af58e2d373fb1c` |
| `catsec/prompts/repair_original.txt (logged runs, unchanged)` | `196401083e4c9b1fc5bfd5c3ca4ca638117da10fb9ea4d3b8f0fe18f50e6a830` |
| `COMPOSER_SYSTEM (logged runs, unchanged)` | `a563eec0d6cf8b333cd3d550f7402c840b0ad9e0981eed68f7a4ecdedeb9951d` |

## Commands (live; cost money -- see review/code_changes.md for the estimate)

```
# per hosted model, from its folder (example: GPT-4o-mini)
python run_catsec_openai.py --profile clean --plan-repair --all-pairs \
    --jsonl runs/clean_v1.jsonl --out clean_v1.csv
python run_catsec_openai.py --profile clean --plan-repair --no-injection \
    --jsonl runs/clean_v1_noinj.jsonl --out clean_v1_noinj.csv
# k-fold alternative: four runs, one per fold
for f in 0 1 2 3; do python run_catsec_openai.py --profile clean \
    --prompt-variant kfold --fold $f --plan-repair --all-pairs \
    --jsonl runs/kfold$f.jsonl --out kfold$f.csv; done
```

## Sign-off

| co-author | date | signature |
|---|---|---|
| Abdul Manan Khan | 2026-10-26 | proposed |
| Deepinder Singh | | |
| Vishwanath Bijalwan | | |
