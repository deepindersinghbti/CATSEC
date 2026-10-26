#!/usr/bin/env bash
# Offline reproduction of every committed CATSEC result. No API keys, no
# network, no GPU, no cost.
#
#   ./reproduce.sh [OUT_DIR]          (default: ./repro_out)
#   E6_RUNS=20 ./reproduce.sh         quick E6 (its outputs are then NOT compared)
#   SKIP_E6=1 ./reproduce.sh          skip E6 entirely (the slow step, ~25 min)
#   PYTHON=/path/to/python ./reproduce.sh
#
# Steps: self-tests and guards -> analyze.py on the four logs -> E1-E6 offline
# replays for the four planners -> figures -> verify_claims.py (asserts the
# paper's numbers) -> compare_results.py (byte/pixel comparison against the
# committed IEEE_Transactions/results and figures). Exit status is non-zero
# if anything fails or differs.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(mkdir -p "${1:-$REPO/repro_out}" && cd "${1:-$REPO/repro_out}" && pwd)"
PY="${PYTHON:-python}"
E6_RUNS="${E6_RUNS:-500}"
# Make sure nothing can reach a paid API even by accident.
RUN="env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u GOOGLE_API_KEY -u PYTHONPATH $PY"
mkdir -p "$OUT/logs" "$OUT/results" "$OUT/figs"
FAILS=0
step() {  # step <name> <cmd...>; logs to $OUT/logs/<name>.txt
  local name="$1"; shift
  if "$@" > "$OUT/logs/$name.txt" 2>&1; then echo "  ok    $name"
  else echo "  FAIL  $name (see $OUT/logs/$name.txt)"; FAILS=$((FAILS+1)); fi
}

echo "== self-tests and guards"
step pytest              $RUN -m pytest -q "$REPO/tests"
step selftest            $RUN "$REPO/tests/selftest.py"
step composer_check      $RUN "$REPO/tools/check_composer_prompt.py"
step contamination       $RUN "$REPO/tools/check_contamination.py" --out "$OUT/contamination.json"
step wiring_claude env -C "$REPO/Journal_Experiments/Claude"     $RUN selftest_claude_wiring.py
step wiring_gemini env -C "$REPO/Journal_Experiments/Gemini"     $RUN selftest_gemini_wiring.py
step wiring_gemma  env -C "$REPO/Journal_Experiments/Gemma4_31B" $RUN selftest_gemma_wiring.py

declare -A LOG=( [gpt-4o-mini]=banking_final [claude-haiku-4-5]=banking_claude_final
                 [gemini-2.5-flash]=gemini_full [gemma-4-31b-it]=gemma_full )
MODELS="gpt-4o-mini gemini-2.5-flash claude-haiku-4-5 gemma-4-31b-it"
IEEE="$REPO/IEEE_Transactions"

echo "== main results table (analyze.py) for the four logs"
for m in $MODELS; do
  step "analyze_$m" $RUN "$REPO/analysis/analyze.py" --jsonl "$IEEE/data/logs/$m/${LOG[$m]}.jsonl"
done

echo "== E1-E5 offline replays"
cd "$IEEE"
for m in $MODELS; do
  L="data/logs/$m/${LOG[$m]}.jsonl"; R="$OUT/results"
  step "E1_faithful_$m" $RUN experiments/E1_adaptive_designated/replay_adaptive.py --mode faithful --log "$L" --out "$R/E1_adaptive_designated/$m"
  step "E1_adaptive_$m" $RUN experiments/E1_adaptive_designated/replay_adaptive.py --mode adaptive --log "$L" --out "$R/E1_adaptive_designated/$m"
  step "E1_noprov_$m"   $RUN experiments/E1_adaptive_designated/replay_adaptive.py --mode adaptive --ablation no_provenance --log "$L" --out "$R/E1_adaptive_designated/$m"
  step "E2_$m" $RUN experiments/E2_mitigations/replay_mitigations.py --log "$L" --out "$R/E2_mitigations/$m"
  step "E3_$m" $RUN experiments/E3_history_amount/replay_history_amount.py --log "$L" --out "$R/E3_history_amount/$m"
  step "E4_$m" $RUN experiments/E4_gate_ablation/replay_gate_ablation.py --log "$L" --out "$R/E4_gate_ablation/$m"
  step "E5_$m" $RUN experiments/E5_composer_scan/scan_composer.py --log "$L" --out "$R/E5_composer_scan/$m"
done

if [ "${SKIP_E6:-0}" != "1" ]; then
  echo "== E6 property-based theorem check ($E6_RUNS runs per plan; the slow step)"
  for m in $MODELS; do
    L="data/logs/$m/${LOG[$m]}.jsonl"; R="$OUT/results/E6_theorem_check/$m"
    step "E6_fix_$m"  $RUN experiments/E6_theorem_check/fuzz_theorems.py --log "$L" --out "$R" --runs "$E6_RUNS"
    step "E6_fuzz_$m" $RUN experiments/E6_theorem_check/fuzz_theorems.py --log "$L" --out "$R" --runs "$E6_RUNS" --fuzz-designated
  done
fi

echo "== figures"
step fig_gate_ablation $RUN experiments/E4_gate_ablation/plot_gate_ablation.py --out-dir "$OUT/figs"
step fig_composer_fix  $RUN experiments/E7_composer_fix/plot_composer_fix.py --out-dir "$OUT/figs"
step fig_results_summary $RUN "$REPO/Journal_Experiments/Graph_4models/make_results_summary_with_pdf_bold.py" \
     --logdir "$REPO/Journal_Experiments/Graph_4models" --out "$OUT/figs/results_summary.png"

echo "== paper claims (asserted against the logs)"
step verify_claims $RUN "$IEEE/paper/verify_claims.py"

echo "== regression guard: compare with the committed results"
CMP_ARGS=()
if [ "${SKIP_E6:-0}" = "1" ] || [ "$E6_RUNS" != "500" ]; then CMP_ARGS+=(--skip E6_theorem_check); fi
if $RUN "$REPO/tools/compare_results.py" "$OUT" "${CMP_ARGS[@]}" | tee "$OUT/logs/compare.txt"; then :; else FAILS=$((FAILS+1)); fi

echo
if [ "$FAILS" -eq 0 ]; then echo "OFFLINE REPRODUCTION: ALL STEPS PASSED  (outputs in $OUT)"
else echo "OFFLINE REPRODUCTION: $FAILS step(s) FAILED  (outputs in $OUT)"; fi
exit "$FAILS"
