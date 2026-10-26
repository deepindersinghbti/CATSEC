#!/usr/bin/env python3
"""
Figure: gate ablation (E4) -- which CATSEC component stops argument hijacking?

Reads the per-trial E4 results written by
    experiments/E4_gate_ablation/replay_gate_ablation.py
from
    results/E4_gate_ablation/<model>/gate_ablation.jsonl
and writes
    paper/figs/gate_ablation.pdf   (vector, for the manuscript)
    paper/figs/gate_ablation.png   (600 DPI raster)

Panels (one group of bars per planning model, three settings each):
  (a) Utility under the benchmark attack, % of the 144 task--injection pairs
  (b) Refund-hijack attack (A7a): % of trials in which the attacker was paid

Settings: gate on (CATSEC as run), gate off (fixed plan only, i.e.
plan-then-execute), final (confirm-derived + refuse-failed).

Usage (from the project root, venv active):
  python experiments/E4_gate_ablation/plot_gate_ablation.py --check   # numbers only, no figure
  python experiments/E4_gate_ablation/plot_gate_ablation.py           # writes PDF + PNG
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results/E4_gate_ablation"
OUT_DIR = ROOT / "paper/figs"
STEM = "gate_ablation"

MODELS = [                       # (results folder, label on the axis)
    ("gpt-4o-mini", "GPT-4o-mini"),
    ("gemini-2.5-flash", "Gemini\n2.5 Flash"),
    ("claude-haiku-4-5", "Claude\nHaiku 4.5"),
    ("gemma-4-31b-it", "Gemma 4\n31B"),
]
SETTINGS = [                     # (key in the JSONL, legend label, colour, hatch)
    ("gate_on", "Gate on (CATSEC)", "#2E5F8E", ""),
    ("gate_off", "Gate off (fixed plan only)", "#B35529", "//"),
    ("final", "Final (confirm + refuse-failed)", "#5B8C5A", ".."),
]
HIJACK_ATTACK = "A7a_refund_hijack"
N_PAIRS, N_HIJACK = 144, 9


def load(model_dir: str) -> dict:
    path = RESULTS / model_dir / "gate_ablation.jsonl"
    if not path.exists():
        sys.exit(f"missing {path} -- run replay_gate_ablation.py for {model_dir} first")
    rows = [json.loads(l) for l in open(path)]
    util, n_b = defaultdict(int), defaultdict(int)
    paid, n_c = defaultdict(int), defaultdict(int)
    logged_ok = None
    for r in rows:
        s = r["setting"]
        if r["part"] == "B":
            n_b[s] += 1
            util[s] += bool(r["utility"])
        elif r["part"] == "C" and r["attack"] == HIJACK_ATTACK:
            n_c[s] += 1
            paid[s] += bool(r["paid_attacker"])
    # integrity checks: complete runs, and gate_on reproduces the logged utility
    for s, *_ in SETTINGS:
        if n_b[s] != N_PAIRS or n_c[s] != N_HIJACK:
            sys.exit(f"{model_dir}/{s}: expected {N_PAIRS} benchmark and {N_HIJACK} "
                     f"hijack trials, found {n_b[s]} and {n_c[s]}")
    logged = sum(bool(r["logged_utility"]) for r in rows
                 if r["part"] == "B" and r["setting"] == "gate_on")
    logged_ok = logged == util["gate_on"]
    if not logged_ok:
        sys.exit(f"{model_dir}: gate_on utility {util['gate_on']} != logged {logged}")
    return {"util": dict(util), "paid": dict(paid)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print the numbers, draw nothing")
    ap.add_argument("--dpi", type=int, default=600)
    ap.add_argument("--out-dir", default=str(OUT_DIR),
                    help="where to write the .pdf/.png (default: paper/figs)")
    args = ap.parse_args()

    data = {m: load(m) for m, _ in MODELS}

    print(f"{'model':18s}" + "".join(f"{'util ' + s:>16s}" for s, *_ in SETTINGS)
          + "".join(f"{'paid ' + s:>16s}" for s, *_ in SETTINGS))
    for m, _ in MODELS:
        d = data[m]
        print(f"{m:18s}"
              + "".join(f"{d['util'][s]:>12d}/{N_PAIRS}" for s, *_ in SETTINGS)
              + "".join(f"{d['paid'][s]:>14d}/{N_HIJACK}" for s, *_ in SETTINGS))
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
        "font.size": 8, "axes.titlesize": 8.5, "axes.labelsize": 8,
        "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "legend.fontsize": 7.5,
        "pdf.fonttype": 42, "ps.fonttype": 42,        # embed TrueType, no Type 3
        "axes.spines.top": False, "axes.spines.right": False,
        "hatch.linewidth": 0.6,
    })

    # IEEE double-column width is 7.16 in; this figure is meant for figure*
    fig, axes = plt.subplots(1, 2, figsize=(7.16, 2.35), constrained_layout=True)
    x = np.arange(len(MODELS))
    w = 0.26

    panels = [
        (axes[0], "util", N_PAIRS, "(a) Utility under the benchmark attack",
         "Utility (% of 144 pairs)"),
        (axes[1], "paid", N_HIJACK, "(b) Refund-hijack attack",
         "Attacker paid (% of trials)"),
    ]
    for ax, key, denom, title, ylabel in panels:
        for i, (s, label, colour, hatch) in enumerate(SETTINGS):
            vals = [100.0 * data[m][key][s] / denom for m, _ in MODELS]
            bars = ax.bar(x + (i - 1) * w, vals, w, label=label, color=colour,
                          hatch=hatch, edgecolor="white", linewidth=0.4)
            for b, v in zip(bars, vals):
                # one decimal, matching the manuscript (62.5, 68.8, ...);
                # "{:.0f}" would round 62.5 down to 62 (round-half-to-even)
                ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.1f}",
                        ha="center", va="bottom", fontsize=5.8)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, [lbl for _, lbl in MODELS])
        ax.set_ylim(0, 112)
        ax.set_yticks([0, 20, 40, 60, 80, 100])
        ax.grid(axis="y", linewidth=0.3, alpha=0.5)
        ax.set_axisbelow(True)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=3, frameon=False)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf, png = out_dir / f"{STEM}.pdf", out_dir / f"{STEM}.png"
    fig.savefig(pdf)
    fig.savefig(png, dpi=args.dpi)
    print(f"\nwrote {pdf}\nwrote {png} ({args.dpi} DPI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
