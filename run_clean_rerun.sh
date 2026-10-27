#!/usr/bin/env bash
# Clean-prompt rerun for the TDSC revision (see RERUN_FOR_AUTHORS.md and PROTOCOL.md).
#
#   ./run_clean_rerun.sh <gpt|claude|gemini|gemma> [smoke|full|all]
#
#   smoke  2 pairs, CATSEC arm only: checks keys/wiring (cents)
#   full   the confirmatory runs: 144 pairs x 3 repetitions, both arms;
#          benign (no-injection) runs, both arms; 4-fold variant, CATSEC arm
#   all    smoke then full (default)
#
# Every run is resumable: re-run the same command after an interruption.
# Results land in runs_clean/<model>/ ; finish with ./run_clean_rerun.sh bundle
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python}"
MODEL="${1:?usage: $0 <gpt|claude|gemini|gemma|bundle> [smoke|full|all]}"
STAGE="${2:-all}"

if [[ "$MODEL" == bundle ]]; then
  cd "$ROOT"
  out="catsec_clean_rerun_$(date +%Y%m%d).tar.gz"
  {
    echo "commit: $(git rev-parse HEAD)"
    echo "branch: $(git rev-parse --abbrev-ref HEAD)"
    echo "date:   $(date -Is)"
    env -u PYTHONPATH "$PYTHON" tools/protocol_hashes.py --check
  } > runs_clean/PROVENANCE.txt 2>&1
  tar czf "$out" runs_clean
  echo "Send this file back: $ROOT/$out"
  exit 0
fi

case "$MODEL" in
  gpt)    DIR=GPT;        RUN=run_catsec_openai.py; EXTRA=(--model gpt-4o-mini) ;;
  claude) DIR=Claude;     RUN=run_catsec_claude.py; EXTRA=(--model claude-haiku-4-5-20251001 --min-call-interval 1.5 --trial-timeout 300) ;;
  gemini) DIR=Gemini;     RUN=run_catsec_gemini.py; EXTRA=(--model gemini-2.5-flash --tier paid) ;;
  gemma)  DIR=Gemma4_31B; RUN=run_catsec_gemma.py;  EXTRA=(--model gemma-4-31b-it --trial-timeout 240) ;;
  *) echo "unknown model $MODEL"; exit 2 ;;
esac

# Refuse to run if the frozen protocol files were changed.
(cd "$ROOT" && env -u PYTHONPATH "$PYTHON" tools/protocol_hashes.py --check) || {
  echo "PROTOCOL FREEZE BROKEN - do not run; contact Abdul."; exit 3; }

OUT="$ROOT/runs_clean/$MODEL"
mkdir -p "$OUT"
cd "$ROOT/Journal_Experiments/$DIR"

COMMON=(--suite banking --attack tool_knowledge --plan-repair "${EXTRA[@]}")
run() {  # name, args...
  local name="$1"; shift
  echo ">>> [$MODEL] $name"
  env -u PYTHONPATH "$PYTHON" "$RUN" "${COMMON[@]}" "$@" \
      --jsonl "$OUT/$name.jsonl" --out "$OUT/$name.csv" --resume 2>&1 | tee -a "$OUT/$name.log"
}

if [[ "$STAGE" == smoke || "$STAGE" == all ]]; then
  run smoke --profile clean --defenses catsec --max-pairs 2
fi

if [[ "$STAGE" == full || "$STAGE" == all ]]; then
  # 1. Main result: clean prompt, all 144 pairs, both arms, 3 repetitions
  run clean_main   --profile clean --all-pairs --defenses none,catsec --repetitions 3
  # 2. Benign utility (no injection), both arms
  run clean_benign --profile clean --no-injection --defenses none,catsec
  # 3. 4-fold variant (banking-derived examples only from held-out folds), CATSEC arm
  for f in 0 1 2 3; do
    run "kfold_$f" --profile clean --prompt-variant kfold --fold "$f" --all-pairs --defenses catsec
  done
fi
echo "Done: $OUT"
