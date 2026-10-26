#!/usr/bin/env python3
"""
Figure: composer fix (E7) -- the answer composer denies actions the system
performed; giving it the execution record removes the denials.

Reads the E7 score tables written by
    experiments/E7_composer_fix/compose_fix.py
from
    results/E7_composer_fix/<model>/summary.json
and writes
    paper/figs/composer_fix.pdf   (vector, for the manuscript)
    paper/figs/composer_fix.png   (600 DPI raster)

Panels (one pair of bars per model: original vs. fixed composer instructions):
  (a) Under the benchmark attack (144 trials per model)
  (b) Without any injection (16 user tasks per model)
Bar height = % of ACTING trials (the plan executed at least one sensitive call)
whose answer was flagged as denying an executed action. Each bar is labelled
with the raw count "flagged/acting", because panel (b) has small denominators.

Arms used: "original" and "exec2" (attack), "clean_original" and "clean_exec2"
(no attack). "exec2" is the final fixed version; the v1 "exec" arm run on
Gemini only is not plotted.

Manual inspection (see the manuscript): the 4 answers flagged for Claude under
the fixed instructions were all read by hand; none denies the action (two
misattribute the user's password). That bar is marked with a dagger, which the
figure caption must explain. Edit MANUAL_NOTES if the labels change.

Usage (from the project root, venv active):
  python experiments/E7_composer_fix/plot_composer_fix.py --check   # numbers only, no figure
  python experiments/E7_composer_fix/plot_composer_fix.py           # writes PDF + PNG
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results/E7_composer_fix"
OUT_DIR = ROOT / "paper/figs"
STEM = "composer_fix"

MODELS = [                       # (results folder, axis label) -- the three models E7 ran on
    ("gpt-4o-mini", "GPT-4o-\nmini"),
    ("gemini-2.5-flash", "Gemini\n2.5 Flash"),
    ("claude-haiku-4-5", "Claude\nHaiku 4.5"),
]
PANELS = [                       # (original arm, fixed arm, panel title)
    ("original", "exec2", "(a) Under the benchmark attack"),
    ("clean_original", "clean_exec2", "(b) Without any injection"),
]
ORIG_STYLE = ("Original instructions", "#B35529", "//")
FIXED_STYLE = ("With execution record", "#2E5F8E", "")
# (model, arm) -> suffix on that bar's label; documented manual inspection
MANUAL_NOTES = {("claude-haiku-4-5", "exec2"): r"$^{\dagger}$"}
EXPECTED_N = {"original": 144, "exec2": 144, "clean_original": 16, "clean_exec2": 16}


def load(model_dir: str) -> dict:
    path = RESULTS / model_dir / "summary.json"
    if not path.exists():
        sys.exit(f"missing {path} -- run compose_fix.py for {model_dir} first")
    s = json.loads(path.read_text())
    for orig, fixed, _ in PANELS:
        for arm in (orig, fixed):
            if arm not in s:
                sys.exit(f"{model_dir}: arm '{arm}' missing from {path} -- re-run "
                         "compose_fix.py with the default arms to re-score")
            if s[arm]["n"] != EXPECTED_N[arm]:
                sys.exit(f"{model_dir}/{arm}: expected {EXPECTED_N[arm]} trials, "
                         f"found {s[arm]['n']}")
        # both arms replay the same plans, so the acting trials must coincide
        if s[orig]["acted"] != s[fixed]["acted"]:
            sys.exit(f"{model_dir}: acting trials differ between {orig} and {fixed}")
        if s[orig]["utility"] != s[fixed]["utility"]:
            print(f"NOTE {model_dir}: utility {orig}={s[orig]['utility']} vs "
                  f"{fixed}={s[fixed]['utility']} (the manuscript states it is unchanged)")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print the numbers, draw nothing")
    ap.add_argument("--dpi", type=int, default=600)
    args = ap.parse_args()

    data = {m: load(m) for m, _ in MODELS}
    print(f"{'model':18s}{'arm':16s}{'flagged/acting':>16s}{'utility':>10s}"
          f"{'IBAN':>6s}{'quote>=30':>11s}")
    for m, _ in MODELS:
        for orig, fixed, _ in PANELS:
            for arm in (orig, fixed):
                c = data[m][arm]
                print(f"{m:18s}{arm:16s}{c['contradicts']:>9d}/{c['acted']:<6d}"
                      f"{c['utility']:>8d}/{c['n']:<4d}{c['iban']:>4d}{c.get('quote30', 0):>9d}")
    if args.check:
        return 0

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "Nimbus Roman", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 7.5, "axes.titlesize": 7.5, "axes.labelsize": 7.5,
        "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
        "pdf.fonttype": 42, "ps.fonttype": 42,
        "axes.spines.top": False, "axes.spines.right": False,
        "hatch.linewidth": 0.6,
    })

    # IEEE single-column width is 3.5 in
    fig, axes = plt.subplots(1, 2, figsize=(3.5, 2.25), sharey=True,
                             constrained_layout=True)
    x = np.arange(len(MODELS))
    w = 0.36

    for ax, (orig, fixed, title) in zip(axes, PANELS):
        for i, (arm, (label, colour, hatch)) in enumerate(
                [(orig, ORIG_STYLE), (fixed, FIXED_STYLE)]):
            vals, texts = [], []
            for m, _ in MODELS:
                c = data[m][arm]
                vals.append(100.0 * c["contradicts"] / c["acted"] if c["acted"] else 0.0)
                texts.append(f"{c['contradicts']}/{c['acted']}"
                             + MANUAL_NOTES.get((m, arm), ""))
            bars = ax.bar(x + (i - 0.5) * w, vals, w, label=label, color=colour,
                          hatch=hatch, edgecolor="white", linewidth=0.4)
            for b, v, t in zip(bars, vals, texts):
                ax.text(b.get_x() + b.get_width() / 2, v + 1.5, t, ha="center",
                        va="bottom", fontsize=5.6, rotation=90)
        ax.set_title(title)
        ax.set_xticks(x, [lbl for _, lbl in MODELS])
        ax.set_ylim(0, 115)
        ax.set_yticks([0, 20, 40, 60, 80, 100])
        ax.grid(axis="y", linewidth=0.3, alpha=0.5)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Answers denying an executed\naction (% of acting trials)")

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=2, frameon=False)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf, png = OUT_DIR / f"{STEM}.pdf", OUT_DIR / f"{STEM}.png"
    fig.savefig(pdf)
    fig.savefig(png, dpi=args.dpi)
    print(f"\nwrote {pdf}\nwrote {png} ({args.dpi} DPI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
