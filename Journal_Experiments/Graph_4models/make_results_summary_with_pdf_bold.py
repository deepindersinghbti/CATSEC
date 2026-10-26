#!/usr/bin/env python3
"""
Regenerate results_summary.png / results_summary.pdf (Fig. results_summary in
the CATSEC paper) directly from the per-trial JSONL logs.

Every value plotted is recomputed from the raw records; nothing is read from
the aggregate CSVs. Bars are point estimates, error bars are 95% Wilson
score intervals over n = 144 pairs per arm.

Camera-ready readability changes:
  * The figure is drawn at its PRINTED size (default 7.16 in = IEEE full text
    width, i.e. a figure* environment), so every font size below is the size
    that actually appears on the page at 100% zoom. The old 15-in-wide canvas
    was being shrunk ~2x by LaTeX, which is why the labels became unreadable.
  * Larger / bold model names, panel titles, tick numbers and legend.
  * One shared legend above the three panels so it never covers a bar.
  * Fonts embedded as TrueType (Type 42), not Type 3, for IEEE PDF eXpress.

Include it in LaTeX at its natural width, e.g.
    \\begin{figure*}\\centering
    \\includegraphics[width=\\textwidth]{results_summary.pdf}
    ...

Usage:
    python3 make_results_summary_with_pdf.py [--logdir DIR] [--out results_summary.png]
                                             [--pdf-out results_summary.pdf] [--dpi 600]
                                             [--width 7.16] [--height 2.9]
"""

import argparse
import json
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------------------
# Typography (sizes in points, as printed on the page)
# ---------------------------------------------------------------------------
FS_TITLE = 10      # panel headings
FS_XTICK = 8.5     # model names under each panel
FS_YTICK = 8.5     # 0, 20, ..., 100
FS_LEGEND = 9      # Undefended / CATSEC

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.linewidth": 0.8,
    "xtick.major.width": 0.8,
    "ytick.major.width": 0.8,
    "xtick.major.size": 3,
    "ytick.major.size": 3,
    "pdf.fonttype": 42,   # embed TrueType, avoid Type 3 fonts (IEEE PDF eXpress)
    "ps.fonttype": 42,
})

# (display name, log filename) in the order the models appear in Table I
MODELS = [
    ("gpt-4o-mini",      "banking_final.jsonl"),
    ("gemini-2.5-flash", "gemini_full.jsonl"),
    ("claude-haiku-4-5", "banking_claude_final.jsonl"),
    ("gemma-4-31b-it",   "gemma_full.jsonl"),
]

ARMS = [
    ("no-defense", "Undefended", "#B0562A"),
    ("catsec",     "CATSEC",     "#2E5E8E"),
]

METRICS = [
    ("util", "Benign utility (%)"),
    ("sec",  "Secure rate (%)"),
    ("asr",  "Attack success rate (%)"),
]

Z = 1.959963985  # two-sided 95%


def wilson(k, n, z=Z):
    """95% Wilson score interval for k successes out of n, in percent."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half) * 100.0, min(1.0, centre + half) * 100.0


def summarise(path):
    """Return {arm: {metric: (point, lo, hi)}} recomputed from raw trials."""
    rows = [json.loads(line) for line in open(path) if line.strip()]
    out = {}
    for arm, _, _ in ARMS:
        trials = [r for r in rows if r["defense"] == arm and r["error"] is None]
        n = len(trials)
        if n == 0:
            raise SystemExit(f"{path}: no completed trials for arm {arm!r}")
        u = sum(1 for r in trials if r["utility"])
        s = sum(1 for r in trials if r["security_attack_succeeded"])
        out[arm] = {
            "util": (u / n * 100.0,) + wilson(u, n),
            "sec":  ((n - s) / n * 100.0,) + wilson(n - s, n),
            "asr":  (s / n * 100.0,) + wilson(s, n),
            "n": n,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logdir", default="/home/deepinder/Category-Theory/Journal_Experiments/Graph_4models",
                    help="directory holding the four per-trial JSONL files")
    ap.add_argument("--out", default="results_summary.png",
                    help="high-DPI raster output path")
    ap.add_argument("--pdf-out", default=None,
                    help="vector PDF output path; defaults to the same basename as --out")
    ap.add_argument("--dpi", type=int, default=600,
                    help="DPI for the raster image output (default: 600)")
    ap.add_argument("--width", type=float, default=7.16,
                    help="printed figure width in inches (default 7.16 = IEEE \\textwidth)")
    ap.add_argument("--height", type=float, default=2.9,
                    help="printed figure height in inches (default 2.9)")
    args = ap.parse_args()

    pdf_out = args.pdf_out or os.path.splitext(args.out)[0] + ".pdf"

    data = {name: summarise(os.path.join(args.logdir, fn)) for name, fn in MODELS}
    names = [name for name, _ in MODELS]

    fig, axes = plt.subplots(1, 3, figsize=(args.width, args.height))
    x = np.arange(len(names))
    width = 0.38

    for ax, (key, title) in zip(axes, METRICS):
        for i, (arm, label, colour) in enumerate(ARMS):
            point = [data[m][arm][key][0] for m in names]
            lo = [max(0.0, data[m][arm][key][0] - data[m][arm][key][1]) for m in names]
            hi = [max(0.0, data[m][arm][key][2] - data[m][arm][key][0]) for m in names]
            ax.bar(x + (i - 0.5) * width, point, width, label=label, color=colour,
                   yerr=[lo, hi], capsize=2.5, error_kw=dict(lw=0.9, ecolor="#222"))
        ax.set_title(title, fontsize=FS_TITLE, fontweight="bold", pad=4)
        ax.set_xticks(x)
        ax.set_xticklabels(names, rotation=35, ha="right", rotation_mode="anchor",
                           fontsize=FS_XTICK, fontweight="bold")
        ax.set_ylim(0, 105)
        ax.set_yticks(range(0, 101, 20))
        ax.tick_params(axis="y", labelsize=FS_YTICK)
        ax.grid(axis="y", alpha=0.3, lw=0.6)
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    # One shared legend centred above all three panels, so it never hides a bar.
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(ARMS), frameon=False,
               prop={"size": FS_LEGEND, "weight": "bold"},
               bbox_to_anchor=(0.5, 1.0), handlelength=1.4, columnspacing=1.6)
    fig.tight_layout(rect=(0, 0, 1, 0.92), w_pad=1.2)

    # Save the exact same figure in two formats:
    #   1) high-DPI raster image for convenient viewing/insertion
    #   2) vector PDF for publication-quality scaling at any zoom level
    fig.savefig(args.out, dpi=args.dpi, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(pdf_out, format="pdf", bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)

    # Echo the plotted values so they can be diffed against Table I.
    print(f"wrote {args.out}")
    print(f"wrote {pdf_out}\n")
    hdr = f"{'model':17s} {'arm':11s} {'n':>4s} {'utility':>22s} {'secure':>22s} {'ASR':>22s}"
    print(hdr)
    for m in names:
        for arm, _, _ in ARMS:
            d = data[m][arm]
            cells = "".join(
                f"  {d[k][0]:5.1f} [{d[k][1]:5.1f},{d[k][2]:5.1f}]"
                for k in ("util", "sec", "asr")
            )
            print(f"{m:17s} {arm:11s} {d['n']:4d}{cells}")


if __name__ == "__main__":
    main()
