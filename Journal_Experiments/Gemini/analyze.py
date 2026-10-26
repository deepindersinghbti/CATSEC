#!/usr/bin/env python3
"""
CATSEC offline analysis.

Reads the per-trial JSONL written by run_catsec_gemini_vertex.py and produces
every table the paper needs. NOTHING here calls an API, so you can re-cut the
statistics as many times as you like -- including after a reviewer asks for a
different test -- without spending a rupee or re-running anything.

WHAT IT COMPUTES AND WHY (plain English)
========================================
Wilson score intervals
    A proportion like "100% secure out of 144 trials" does not mean the true
    rate is exactly 1. It means the data is consistent with a range. The
    textbook interval breaks down at 0% and 100% -- exactly where the catsec
    numbers sit -- and would give you a nonsensical width of zero. Wilson
    handles the boundary correctly. This is what replaces "100.0% +/- 0.0".

McNemar's exact test
    Both arms ran on the SAME pairs, like testing two treatments on the same
    patients. McNemar throws away the pairs where the arms agreed (they carry
    no information about which is better) and asks whether the DISAGREEMENTS
    lean one way more than coin-flipping would explain. This is the correct
    test for a paired design and is far stronger than comparing two independent
    percentages.

Paired bootstrap
    For quantities with no neat closed form (e.g. the utility DIFFERENCE
    between arms), resample the pairs with replacement a few thousand times and
    read the spread off the resamples.

Intent-to-treat vs per-protocol
    Per-protocol drops trials that errored. Intent-to-treat counts them as
    failures. Reporting both, with the gap visible, is what stops a crash-prone
    arm from looking artificially good. Clinical trials do exactly this.

CFDR (Control-Flow Divergence Rate)
    The direct empirical test of Theorem K1: the fraction of pairs where the
    executed tool sequence differed between the clean and injected runs.

Usage:
    python analyze.py --jsonl 'runs/*.jsonl'
    python analyze.py --jsonl 'runs/g3_banking_full.jsonl' --latex tables/
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import random
from collections import defaultdict


# --------------------------------------------------------------------------- #
# Statistics (stdlib only -- no scipy dependency, so this always runs)
# --------------------------------------------------------------------------- #
def wilson(k: int, n: int, z: float = 1.959963985):
    """Wilson score interval for a binomial proportion. Returns (p, lo, hi)."""
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return p, max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom)


def fmt_ci(k: int, n: int) -> str:
    p, lo, hi = wilson(k, n)
    if n == 0:
        return "n/a"
    return f"{100*p:.1f}% (95% CI {100*lo:.1f}-{100*hi:.1f}, n={n})"


def binom_two_sided_exact(b: int, c: int) -> float:
    """Two-sided exact binomial test of p=0.5 on the discordant counts.
    This is McNemar's exact test."""
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    tail = sum(math.comb(n, k) for k in range(0, lo + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


def mcnemar(pairs_a: dict, pairs_b: dict):
    """pairs_* map pair-key -> bool outcome. Returns (b, c, p_value, n_common).

    b = A succeeded, B failed.  c = A failed, B succeeded.
    """
    common = set(pairs_a) & set(pairs_b)
    b = sum(1 for k in common if pairs_a[k] and not pairs_b[k])
    c = sum(1 for k in common if not pairs_a[k] and pairs_b[k])
    return b, c, binom_two_sided_exact(b, c), len(common)


def paired_bootstrap_diff(pairs_a: dict, pairs_b: dict, iters: int = 5000,
                          seed: int = 12345):
    """95% CI on mean(A) - mean(B) over the pairs both arms completed."""
    common = sorted(set(pairs_a) & set(pairs_b))
    if not common:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(common)
    point = (sum(pairs_a[k] for k in common) - sum(pairs_b[k] for k in common)) / n
    diffs = []
    for _ in range(iters):
        s = [common[rng.randrange(n)] for _ in range(n)]
        diffs.append((sum(pairs_a[k] for k in s) - sum(pairs_b[k] for k in s)) / n)
    diffs.sort()
    return point, diffs[int(0.025 * iters)], diffs[int(0.975 * iters) - 1]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load(patterns):
    recs = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return recs


def pair_key(r) -> tuple:
    return (r.get("user_task"), r.get("injection_task"), r.get("repetition"))


def arms(recs):
    out = defaultdict(list)
    for r in recs:
        out[r.get("defense", "?")].append(r)
    return out


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def summary_table(recs, itt: bool):
    """One row per arm. itt=True counts errored trials as utility failures and
    as security-indeterminate (they are excluded from the security denominator
    because we genuinely do not know what happened)."""
    rows = []
    for arm, rs in sorted(arms(recs).items()):
        n_all = len(rs)
        ok = [r for r in rs if r.get("error") is None]
        n_err = n_all - len(ok)

        if itt:
            u_k = sum(1 for r in ok if r.get("utility"))
            u_n = n_all
        else:
            u_k = sum(1 for r in ok if r.get("utility"))
            u_n = len(ok)

        sec_rs = [r for r in ok if r.get("security_attack_succeeded") is not None]
        s_k = sum(1 for r in sec_rs if not r["security_attack_succeeded"])
        s_n = len(sec_rs)

        plan_rs = [r for r in ok if r.get("plan_valid") is not None]
        p_k = sum(1 for r in plan_rs if r["plan_valid"])
        refusing = sum(1 for r in ok if r.get("refused"))

        rows.append({
            "arm": arm, "trials": n_all, "errors": n_err,
            "utility": fmt_ci(u_k, u_n),
            "secure": fmt_ci(s_k, s_n),
            "ASR": fmt_ci(s_n - s_k, s_n),
            "plan_valid": fmt_ci(p_k, len(plan_rs)) if plan_rs else "n/a",
            "trials_with_refusal": fmt_ci(refusing, len(ok)),
        })
    return rows


def outcome_breakdown(recs):
    """R4: never let a refusal or a crash read as a security win."""
    rows = []
    for arm, rs in sorted(arms(recs).items()):
        n = len(rs)
        err = sum(1 for r in rs if r.get("error"))
        planfail = sum(1 for r in rs if r.get("error") is None
                       and r.get("plan_valid") is False)
        refused = sum(1 for r in rs if r.get("error") is None and r.get("refused"))
        ok_correct = sum(1 for r in rs if r.get("error") is None and r.get("utility"))
        ok_wrong = n - err - ok_correct
        rows.append({"arm": arm, "n": n, "completed_correctly": ok_correct,
                     "completed_incorrectly_or_incomplete": ok_wrong,
                     "of_which_refused": refused,
                     "of_which_plan_failure": planfail, "errored": err})
    return rows


def refusal_causes(recs):
    causes = defaultdict(lambda: defaultdict(int))
    for r in recs:
        for _tool, _args, reason in (r.get("refused") or []):
            reason = str(reason)
            if "extract-failed" in reason:
                bucket = "extraction failed (absent or ambiguous)"
            elif "UNTRUSTED" in reason:
                bucket = "provenance UNTRUSTED on critical arg"
            elif "DERIVED" in reason:
                bucket = "provenance DERIVED above tool maximum"
            elif "MISSING" in reason:
                bucket = "critical arg missing"
            else:
                bucket = "other"
            causes[r.get("defense", "?")][bucket] += 1
    return causes


def cfdr_table(recs):
    rows = []
    for arm, rs in sorted(arms(recs).items()):
        k1 = [r["k1"] for r in rs if r.get("k1")]
        if not k1:
            continue
        diverged = sum(1 for x in k1 if not x.get("traces_identical"))
        plan_diff = sum(1 for x in k1 if x.get("plans_identical") is False)
        rows.append({"arm": arm, "pairs_checked": len(k1),
                     "CFDR": fmt_ci(diverged, len(k1)),
                     "plan_mismatches": plan_diff})
    return rows


def cost_table(recs, price_in: float, price_out: float):
    rows = []
    for arm, rs in sorted(arms(recs).items()):
        ok = [r for r in rs if r.get("error") is None]
        if not ok:
            continue
        n = len(ok)
        def mean(k):
            return sum(float(r.get(k) or 0) for r in ok) / n
        tin, tout = mean("tokens_in"), mean("tokens_out")
        cost100 = 100 * (tin * price_in + tout * price_out) / 1e6
        rows.append({
            "arm": arm, "trials": n,
            "model_calls": f"{mean('n_model_calls'):.2f}",
            "tokens_in": f"{tin:.0f}", "tokens_out": f"{tout:.0f}",
            "thinking_tokens": f"{mean('tokens_thoughts'):.0f}",
            "latency_s": f"{mean('latency_s'):.1f}",
            "usd_per_100_tasks": f"${cost100:.3f}",
        })
    return rows


def paired_tests(recs):
    by_arm = arms(recs)
    names = sorted(by_arm)
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            for metric, getter in (
                ("utility", lambda r: bool(r.get("utility"))),
                ("secure", lambda r: not bool(r.get("security_attack_succeeded"))),
            ):
                pa = {pair_key(r): getter(r) for r in by_arm[a]
                      if r.get("error") is None
                      and (metric != "secure"
                           or r.get("security_attack_succeeded") is not None)}
                pb = {pair_key(r): getter(r) for r in by_arm[b]
                      if r.get("error") is None
                      and (metric != "secure"
                           or r.get("security_attack_succeeded") is not None)}
                bb, cc, p, ncom = mcnemar(pa, pb)
                pt, lo, hi = paired_bootstrap_diff(pa, pb)
                out.append({
                    "metric": metric, "arm_A": a, "arm_B": b, "paired_n": ncom,
                    "A_better": bb, "B_better": cc,
                    "mcnemar_p": f"{p:.3g}",
                    "diff_A_minus_B": f"{100*pt:+.1f}pp (95% CI "
                                      f"{100*lo:+.1f} to {100*hi:+.1f})",
                })
    return out


def repetition_variance(recs):
    """G6.1: does the outcome move between identical repeated runs?"""
    by = defaultdict(dict)
    for r in recs:
        if r.get("error") is not None:
            continue
        k = (r.get("defense"), r.get("user_task"), r.get("injection_task"))
        by[k][r.get("repetition")] = (bool(r.get("utility")),
                                      r.get("security_attack_succeeded"))
    multi = {k: v for k, v in by.items() if len(v) > 1}
    if not multi:
        return None
    u_stable = sum(1 for v in multi.values() if len({x[0] for x in v.values()}) == 1)
    s_stable = sum(1 for v in multi.values() if len({x[1] for x in v.values()}) == 1)
    return {"pairs_with_repeats": len(multi),
            "utility_identical_across_reps": fmt_ci(u_stable, len(multi)),
            "security_identical_across_reps": fmt_ci(s_stable, len(multi))}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def show(title, rows):
    print(f"\n=== {title} ===")
    if not rows:
        print("  (no data)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    print("  " + "  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def to_latex(rows, caption, label):
    if not rows:
        return ""
    cols = list(rows[0].keys())
    esc = lambda s: (str(s).replace("_", r"\_").replace("%", r"\%")
                     .replace("&", r"\&"))
    out = ["\\begin{table}[t]", "\\centering",
           "\\begin{tabular}{" + "l" * len(cols) + "}", "\\toprule",
           " & ".join(esc(c) for c in cols) + " \\\\", "\\midrule"]
    for r in rows:
        out.append(" & ".join(esc(r[c]) for c in cols) + " \\\\")
    out += ["\\bottomrule", "\\end{tabular}",
            f"\\caption{{{caption}}}", f"\\label{{tab:{label}}}", "\\end{table}"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", nargs="+", required=True,
                    help="One or more glob patterns of per-trial JSONL files.")
    ap.add_argument("--condition", default=None, choices=["injected", "clean"],
                    help="Restrict to injected or clean trials.")
    ap.add_argument("--attack", default=None)
    ap.add_argument("--ablation", default=None)
    ap.add_argument("--latex", default=None, help="Directory to write .tex tables.")
    ap.add_argument("--price-in", type=float, default=0.30,
                    help="USD per MILLION input tokens. VERIFY against current "
                         "Vertex pricing and record the date you checked.")
    ap.add_argument("--price-out", type=float, default=2.50,
                    help="USD per MILLION output tokens. Same caveat.")
    args = ap.parse_args()

    recs = load(args.jsonl)
    if args.condition:
        recs = [r for r in recs if r.get("condition") == args.condition]
    if args.attack:
        recs = [r for r in recs if r.get("attack") == args.attack]
    if args.ablation:
        recs = [r for r in recs if r.get("ablation") == args.ablation]

    if not recs:
        print("No records matched. Check --jsonl patterns and filters.")
        return

    print(f"Loaded {len(recs)} trial records")
    print(f"  models     : {sorted({r.get('model') for r in recs})}")
    print(f"  suites     : {sorted({r.get('suite') for r in recs})}")
    print(f"  attacks    : {sorted({str(r.get('attack')) for r in recs})}")
    print(f"  arms       : {sorted({r.get('defense') for r in recs})}")
    print(f"  conditions : {sorted({r.get('condition') for r in recs})}")
    print(f"  repetitions: {sorted({r.get('repetition') for r in recs})}")

    pp = summary_table(recs, itt=False)
    itt = summary_table(recs, itt=True)
    outcomes = outcome_breakdown(recs)
    tests = paired_tests(recs)
    cfdr = cfdr_table(recs)
    costs = cost_table(recs, args.price_in, args.price_out)

    show("MAIN RESULTS - per-protocol (errored trials dropped)", pp)
    show("MAIN RESULTS - intent-to-treat (errors = utility failure)", itt)
    show("OUTCOME BREAKDOWN (R4: refusals and crashes are not security wins)",
         outcomes)
    show("PAIRED COMPARISONS (McNemar exact + paired bootstrap)", tests)
    if cfdr:
        show("CONTROL-FLOW DIVERGENCE (E1: direct test of Theorem K1)", cfdr)
    show("COST AND LATENCY (E11)", costs)

    causes = refusal_causes(recs)
    if causes:
        print("\n=== REFUSAL CAUSES (E3) ===")
        for arm, buckets in causes.items():
            print(f"  {arm}:")
            for b, c in sorted(buckets.items(), key=lambda x: -x[1]):
                print(f"     {c:5d}  {b}")

    rv = repetition_variance(recs)
    if rv:
        print("\n=== REPETITION STABILITY (G6.1) ===")
        for k, v in rv.items():
            print(f"  {k}: {v}")

    print("\nNOTE: prices used were $%.2f/M in, $%.2f/M out. Verify these "
          "against current Vertex pricing before quoting any cost figure."
          % (args.price_in, args.price_out))

    if args.latex:
        import os
        os.makedirs(args.latex, exist_ok=True)
        blocks = {
            "main_per_protocol": (pp, "Main results (per-protocol)."),
            "main_itt": (itt, "Main results (intent-to-treat)."),
            "outcomes": (outcomes, "Outcome breakdown by arm."),
            "paired": (tests, "Paired comparisons (McNemar exact test)."),
            "cfdr": (cfdr, "Control-flow divergence rate."),
            "cost": (costs, "Cost and latency per trial."),
        }
        for name, (rows, cap) in blocks.items():
            if not rows:
                continue
            with open(os.path.join(args.latex, f"{name}.tex"), "w") as fh:
                fh.write(to_latex(rows, cap, name))
        print(f"\nLaTeX tables written to {args.latex}/")


if __name__ == "__main__":
    main()
