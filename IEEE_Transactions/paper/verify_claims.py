#!/usr/bin/env python3
"""Checks the manuscript lines marked "% VERIFY:" against all four final logs.
Offline, read-only. Run from the IEEE_Transactions project root:
    python paper/verify_claims.py
"""
import json
from collections import Counter, defaultdict

LOGS = {"gpt-4o-mini": "data/logs/gpt-4o-mini/banking_final.jsonl",
        "claude-haiku-4-5": "data/logs/claude-haiku-4-5/banking_claude_final.jsonl",
        "gemini-2.5-flash": "data/logs/gemini-2.5-flash/gemini_full.jsonl",
        "gemma-4-31b-it": "data/logs/gemma-4-31b-it/gemma_full.jsonl"}
ORDER = lambda t: int(t.split("_")[-1])

for name, path in LOGS.items():
    rows = [json.loads(l) for l in open(path)]
    c = [r for r in rows if r["defense"] == "catsec"]
    print(f"\n=== {name}  (model field: {sorted({r['model'] for r in rows})})")
    print("plan_valid:", dict(Counter(r["plan_valid"] for r in c)),
          "| plan_repair:", dict(Counter(str(r["plan_repair"]) for r in c)))
    ref = Counter(r["user_task"] for r in c if r["refused"])
    print("refused trials by task:", {t: ref[t] for t in sorted(ref, key=ORDER)},
          "| refused calls total:", sum(len(r["refused"]) for r in c))
    util = defaultdict(int)
    for r in c:
        util[r["user_task"]] += bool(r["utility"])
    print("tasks with utility < 9/9:", {t: util[t] for t in sorted(util, key=ORDER) if util[t] < 9})
    for t in ("user_task_10", "user_task_14"):
        x = [r for r in c if r["user_task"] == t]
        print(f"{t}: traces={dict(Counter(tuple(r['control_trace']) for r in x))} utility={util[t]}/9")
