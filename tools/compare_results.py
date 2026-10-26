#!/usr/bin/env python3
"""
Regression guard: compare a fresh offline reproduction with the committed
results.

    python tools/compare_results.py OUT_DIR [--skip E6_theorem_check ...]

* Every committed file under IEEE_Transactions/results/ (E1-E6; E7 needs API
  calls and is not regenerated) must have been regenerated under
  OUT_DIR/results/ and be BYTE-IDENTICAL (sha256).
* Every committed figure PNG (paper/figs/gate_ablation.png,
  paper/figs/composer_fix.png, Graph_4models/results_summary.png) must be
  PIXEL-IDENTICAL to the regenerated one (PNG/PDF metadata may carry
  timestamps or library versions, so bytes are not compared). Pixel identity
  needs the same fonts as the original (Nimbus Roman, fonts-urw-base35);
  without them the figure is reported as DIFFERENT, not silently accepted.

Files the reproduction produces that have no committed counterpart (e.g. the
E1 gate-off adaptive replay) are listed but do not fail the check.
Exit status: 0 if everything matches, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMITTED = REPO / "IEEE_Transactions" / "results"
FIGS = {
    "gate_ablation.png": REPO / "IEEE_Transactions/paper/figs/gate_ablation.png",
    "composer_fix.png": REPO / "IEEE_Transactions/paper/figs/composer_fix.png",
    "results_summary.png": REPO / "Journal_Experiments/Graph_4models/results_summary.png",
}
NOT_REGENERATED = ("E7_composer_fix",)      # needs provider API calls


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pixels_equal(a: Path, b: Path) -> tuple[bool, str]:
    import matplotlib.image as mpimg
    import numpy as np
    x, y = mpimg.imread(a), mpimg.imread(b)
    if x.shape != y.shape:
        return False, f"shape {x.shape} vs {y.shape}"
    n = int(np.count_nonzero(np.any(x != y, axis=-1) if x.ndim == 3 else x != y))
    return n == 0, f"{n} differing pixels"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--skip", action="append", default=[],
                    help="top-level results folder to leave out (repeatable)")
    args = ap.parse_args()
    out = Path(args.out_dir)
    skip = set(NOT_REGENERATED) | set(args.skip)

    same, diff, missing = [], [], []
    for f in sorted(COMMITTED.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(COMMITTED)
        if rel.parts[0] in skip:
            continue
        g = out / "results" / rel
        if not g.exists():
            missing.append(rel)
        elif sha(f) == sha(g):
            same.append(rel)
        else:
            diff.append(rel)
    extra = sorted(g.relative_to(out / "results") for g in (out / "results").rglob("*")
                   if g.is_file() and not (COMMITTED / g.relative_to(out / "results")).exists())

    print(f"results: {len(same)} byte-identical, {len(diff)} DIFFERENT, "
          f"{len(missing)} MISSING  (skipped: {', '.join(sorted(skip))})")
    for r in diff:
        print(f"  DIFFERENT  {r}")
    for r in missing:
        print(f"  MISSING    {r}")
    for r in extra:
        print(f"  (new, no committed counterpart)  {r}")

    fig_bad = 0
    for name, committed in FIGS.items():
        g = out / "figs" / name
        if not g.exists():
            print(f"figure {name}: MISSING")
            fig_bad += 1
            continue
        ok, why = pixels_equal(committed, g)
        print(f"figure {name}: {'pixel-identical' if ok else 'DIFFERENT (' + why + ')'}")
        fig_bad += not ok

    bad = len(diff) + len(missing) + fig_bad
    print("REGRESSION GUARD:", "PASS" if bad == 0 else f"FAIL ({bad} problem(s))")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
