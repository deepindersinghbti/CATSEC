#!/usr/bin/env python3
"""
Offline cost estimate for the live reruns, computed from the logged usage.

    python tools/estimate_rerun_cost.py [--json out.json]

For each planner it reads the committed log (144 pairs x 2 arms) and splits the
model calls into (a) the undefended agent, (b) CATSEC planning calls (the plan
is cached per request text, so planning happens once per distinct request:
16 user tasks + the 9 injection goals AgentDojo's pre-check feeds the
pipeline, plus repair rounds) and (c) CATSEC composer calls (real trials and
pre-checks).
It then prices the reruns at the list prices below (USD per million tokens,
checked 2026-10-26 on the providers' pricing pages; standard tier, no batch,
no caching) and scales to the other AgentDojo suites by their pair counts.

The clean-prompt planner prompt is SHORTER than the original one (7.4k vs
10.2k characters before the tool list), so using the logged planner tokens is
slightly conservative. The undefended arm does not depend on the planner
prompt at all; it only needs rerunning for repetitions / benign utility.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS = REPO / "IEEE_Transactions/data/logs"
MODELS = {
    "gpt-4o-mini": ("banking_final.jsonl", 0.15, 0.60),
    "gemini-2.5-flash": ("gemini_full.jsonl", 0.30, 2.50),
    "claude-haiku-4-5": ("banking_claude_final.jsonl", 1.00, 5.00),
    "gemma-4-31b-it": ("gemma_full.jsonl", None, None),   # self-hosted (vLLM)
}
# Gemma 4 31B on one rented 80-141 GB GPU (H100/H200). Throughput from the log.
GPU_USD_PER_HOUR = (2.0, 4.0)        # assumption: typical rental range, not a quote


PLANNER_MIN_IN = 2000   # planner calls carry the ~2.5-3k-token plan grammar;
                        # composer calls are a few hundred tokens


def split_calls(rows):
    """CATSEC calls by kind. Note: AgentDojo runs every pair's INJECTION task
    through the pipeline once as a solvability pre-check, so the CATSEC arm
    also plans the 9 injection goals (cached like user requests) and composes
    an answer for each pre-check -- these calls are in the logged meter and
    will recur in any rerun."""
    cat = [r for r in rows if r["defense"] == "catsec"]
    und = [r for r in rows if r["defense"] == "no-defense"]
    plan = dict(calls=0, tin=0, tout=0)
    comp = dict(calls=0, tin=0, tout=0)
    for r in cat:
        for c in r.get("per_call") or []:
            d = plan if c["in"] >= PLANNER_MIN_IN else comp
            d["calls"] += 1; d["tin"] += c["in"]; d["tout"] += c["out"]
    undef = dict(calls=sum(r["n_model_calls"] for r in und),
                 tin=sum(r["tokens_in"] for r in und), tout=sum(r["tokens_out"] for r in und))
    total_cat = dict(calls=sum(r["n_model_calls"] for r in cat),
                     tin=sum(r["tokens_in"] for r in cat), tout=sum(r["tokens_out"] for r in cat))
    lat = dict(undef=sum(r["latency_s"] for r in und), catsec=sum(r["latency_s"] for r in cat))
    return undef, plan, comp, total_cat, lat


def usd(t, pin, pout):
    return (t["tin"] * pin + t["tout"] * pout) / 1e6


def suite_pairs(version="v1.2"):
    from agentdojo.task_suite.load_suites import get_suite
    out = {}
    for s in ("banking", "workspace", "slack", "travel"):
        su = get_suite(version, s)
        out[s] = (len(su.user_tasks), len(su.injection_tasks))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    rep = {"models": {}}
    print(f"{'model':18s} {'arm/part':22s} {'calls':>6s} {'tok in':>10s} {'tok out':>9s} {'USD':>8s}")
    for m, (fn, pin, pout) in MODELS.items():
        rows = [json.loads(l) for l in open(LOGS / m / fn)]
        undef, plan, comp, cat, lat = split_calls(rows)
        d = {"undefended_144": undef, "catsec_planning": plan, "catsec_composer": comp,
             "catsec_total_144": cat, "latency_s_total": lat}
        for k, t in [("undefended (144)", undef), ("CATSEC planner+repair", plan),
                     ("CATSEC composer", comp), ("CATSEC total (144)", cat)]:
            cost = usd(t, pin, pout) if pin is not None else None
            print(f"{m:18s} {k:22s} {t['calls']:>6d} {t['tin']:>10,d} {t['tout']:>9,d} "
                  f"{('%8.2f' % cost) if cost is not None else '     GPU'}")
        if pin is not None:
            d["usd"] = {"undefended_144": usd(undef, pin, pout), "catsec_144": usd(cat, pin, pout),
                        "price_in_per_M": pin, "price_out_per_M": pout}
        else:
            hours = (lat["undef"] + lat["catsec"]) / 3600
            d["gpu_hours_logged_both_arms"] = hours
            print(f"{'':18s} logged wall time both arms: {hours:.2f} h "
                  f"-> ${hours * GPU_USD_PER_HOUR[0]:.0f}-{hours * GPU_USD_PER_HOUR[1]:.0f} at "
                  f"${GPU_USD_PER_HOUR[0]:.0f}-{GPU_USD_PER_HOUR[1]:.0f}/GPU-h (+ setup/download time)")
        rep["models"][m] = d
    try:
        rep["suite_pairs"] = suite_pairs()
    except Exception as e:     # agentdojo missing
        rep["suite_pairs"] = {"error": str(e)}
    print("\nAgentDojo v1.2 pairs (user x injection):",
          {s: f"{u}x{i}={u * i}" for s, (u, i) in rep["suite_pairs"].items()})
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
