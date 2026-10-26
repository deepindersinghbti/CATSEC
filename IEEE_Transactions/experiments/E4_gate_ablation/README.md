# CATSEC -- IEEE Transactions experiments

    code/          kernel.py, planner.py, run_catsec_openai.py (copied UNCHANGED
                   from ../Journal_Experiments/GPT -- never edit them here)
    data/logs/     logged runs, one folder per model (read-only inputs)
    experiments/   one folder per experiment (E1, E2, ...)
    results/       outputs, mirroring experiments/
    paper/         the IEEE Transactions manuscript

## E1 -- adaptive designated-file attack (offline, no API, no GPU)
    source .venv/bin/activate
    python experiments/E1_adaptive_designated/replay_adaptive.py --mode faithful
    python experiments/E1_adaptive_designated/replay_adaptive.py --mode adaptive
Run `faithful` first. It must print 144/144 before the adaptive numbers are trusted.

## E2 -- mitigations (offline, no API, no GPU)
    python experiments/E2_mitigations/replay_mitigations.py \
        --log data/logs/<model>/<file>.jsonl --out results/E2_mitigations/<model>
Tests six settings (none, amount_critical, confirm_derived, both, refuse_failed,
confirm_refuse) against
the E1 adaptive attacks (Part A) and the original 144 pairs (Part B). Needs E1's
script in place; code/ is never edited. The SANITY line must say PASS.
refuse_failed uses experiments/E2_mitigations/strict_interpreter.py, a verified
copy of kernel.Interpreter.run plus one marked rule (it self-checks on import).

## E3 -- attack A6: planted amounts in transaction history (offline)
    python experiments/E3_history_amount/replay_history_amount.py \
        --log data/logs/<model>/<file>.jsonl --out results/E3_history_amount/<model>
Targets user_task_6 under all four E2 settings. Needs the E1 and E2 scripts in place.

## E4 -- gate-off ablation (offline)
    python experiments/E4_gate_ablation/replay_gate_ablation.py \
        --log data/logs/<model>/<file>.jsonl --out results/E4_gate_ablation/<model>
gate_on vs gate_off (runner's no_provenance) vs final (confirm_refuse), on the
original 144 pairs and the A7 untrusted-record hijack. Needs E1 and E2 in place.
