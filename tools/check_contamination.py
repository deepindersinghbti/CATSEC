#!/usr/bin/env python3
"""
Measure how much the planner prompt's worked examples overlap with the
AgentDojo benchmark -- every user task and every injection task of every
suite (banking, workspace, slack, travel) of the installed agentdojo package.

    python tools/check_contamination.py [--suite-version v1.2] [--out report.json]
                                        [--variants original,clean,kfold]

For every worked example of every prompt variant it reports
  * TEXT overlap of the example's user request with each task text
    (user task PROMPT / injection task GOAL): containment of the example's word
    3-grams and 5-grams in the task text, and the longest common run of words;
  * PLAN overlap with each task's ground-truth function calls: same tool
    sequence (exact), difflib similarity of the sequences, shared tools;
  * LITERAL leakage: literals in the example plan/request (numbers, account
    numbers, file names, quoted strings) that equal a ground-truth ARGUMENT of
    a task, or that occur in the suite's environment / injection-vector text
    (e.g. "increased by 100.00" is file content the planner never sees).
It also searches the whole prompt (rules included) for any run of >= 8 words
copied from a task text.

Thresholds for the summary verdict (per example): VERBATIM if 5-gram
containment >= 0.8 or a common run >= 10 words; NEAR if 3-gram containment
>= 0.5 or a common run >= 6 words; LEAK if any literal equals a task's
ground-truth argument or occurs in environment text (trivial literals --
numbers below 10, the empty string, booleans -- are ignored).

kfold prompts are evaluated against their own TEST fold (no text overlap and
no ground-truth-argument leak allowed; environment mentions are reported but
not gating, because all banking tasks share one environment) and, for
information, against the development folds they were drawn from. Ground-truth
values shared by >= 4 tasks (e.g. the environment date) are treated as
environment constants, not task labels.

Exit status: 0 always for original/kfold-dev (they are expected to overlap);
1 if the CLEAN variant, or a kfold prompt against its own test fold, has any
VERBATIM/NEAR/LEAK example -- so CI catches re-contamination.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from catsec import prompts as P  # noqa: E402

SUITES = ("banking", "workspace", "slack", "travel")
# Page-size style arguments ("n": 100, "limit": 50) are not task content.
COUNT_ARGS = {"n", "limit", "count", "k", "top_k"}
MIN_STR_LEAK = 8      # shorter strings ("rent", "coffee") are common words
WORD = re.compile(r"[a-z0-9]+(?:[.,'][a-z0-9]+)*")


def words(text: str) -> list:
    return WORD.findall((text or "").lower())


def ngrams(ws, n):
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def containment(a_ws, b_ws, n):
    a = ngrams(a_ws, n)
    return (len(a & ngrams(b_ws, n)) / len(a)) if a else 0.0


def longest_run(a_ws, b_ws):
    m = difflib.SequenceMatcher(None, a_ws, b_ws, autojunk=False).find_longest_match(
        0, len(a_ws), 0, len(b_ws))
    return m.size


# --------------------------------------------------------------------------- #
# Prompt side
# --------------------------------------------------------------------------- #
_USER_RE = re.compile(r'^User: "(.*?)"\s*$', re.MULTILINE | re.DOTALL)


def parse_example(block: str) -> dict:
    title = block.splitlines()[0]
    m = re.search(r'User: "(.*?)"\n(?:\(|\{\{)', block, re.DOTALL)
    request = m.group(1).replace("\n", " ") if m else ""
    j0 = block.find('{{"plan"')
    j1 = block.find("]}}", j0) + 3
    plan_txt = block[j0:j1].replace("{{", "{").replace("}}", "}")
    try:
        plan = json.loads(plan_txt)["plan"]
    except Exception:
        plan = []
    tools, lits = [], []

    def walk_ref(v, arg=None):
        if isinstance(v, dict) and "lit" in v and arg not in COUNT_ARGS:
            lits.append(v["lit"])
    for st in plan:
        if st.get("kind") == "call":
            tools.append(st.get("tool"))
            for a, v in (st.get("args") or {}).items():
                walk_ref(v, a)
        elif st.get("kind") == "let":
            lits.append(st.get("value"))
        elif st.get("kind") == "extract" and st.get("match"):
            lits.append(st["match"])
        elif st.get("kind") == "compute":
            walk_ref(st.get("left"))
            walk_ref(st.get("right"))
    # literals stated in the request / commentary (numbers, ids, quoted text, files)
    body = block
    for tok in re.findall(r"[A-Z]{2}[0-9]{10,32}", body):
        lits.append(tok)
    for tok in re.findall(r"\b[\w\-]+\.(?:txt|pdf|csv|docx|xlsx|json)\b", body):
        lits.append(tok)
    for tok in re.findall(r"'([^'\n]{4,60})'", request):
        lits.append(tok)
    for tok in re.findall(r"\b\d+(?:\.\d+)?\b", request):
        lits.append(tok)
    return {"title": title, "request": request, "tools": tools, "literals": lits,
            "block": block}


def prompt_variants(selected):
    out = {}
    if "original" in selected:
        out["original"] = P.system_template("original")
    if "clean" in selected:
        out["clean"] = P.system_template("clean")
    if "kfold" in selected:
        for f in range(P.N_FOLDS):
            out[f"kfold{f}"] = P.system_template("kfold", f)
    return out


# --------------------------------------------------------------------------- #
# Benchmark side
# --------------------------------------------------------------------------- #
def _gt_calls(task, suite, defaults):
    try:
        env = suite.load_and_inject_default_environment(defaults)
        env = task.init_environment(env)
        return [(c.function, dict(c.args)) for c in task.ground_truth(env)]
    except Exception:
        return []


def load_benchmark(version):
    from agentdojo.task_suite.load_suites import get_suite
    tasks, env_text = [], {}
    for sname in SUITES:
        suite = get_suite(version, sname)
        defaults = suite.get_injection_vector_defaults()
        env = suite.load_and_inject_default_environment(defaults)
        try:
            env_dump = env.model_dump_json()
        except Exception:
            env_dump = str(env)
        env_text[sname] = (env_dump + " " + json.dumps(defaults)).lower()
        for tid, t in suite.user_tasks.items():
            tasks.append({"suite": sname, "id": tid, "kind": "user", "text": t.PROMPT,
                          "gt": _gt_calls(t, suite, defaults)})
        for tid, t in suite.injection_tasks.items():
            tasks.append({"suite": sname, "id": tid, "kind": "injection", "text": t.GOAL,
                          "gt": _gt_calls(t, suite, defaults)})
    for t in tasks:
        t["ws"] = words(t["text"])
    return tasks, env_text


# --------------------------------------------------------------------------- #
def _norm_lit(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if abs(v) < 10 else ("num", float(v))
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
        return None if abs(f) < 10 else ("num", f)
    except ValueError:
        return ("str", s.lower()) if len(s) >= 4 else None


ENV_CONSTANTS: set = set()     # filled in main(): GT values shared by >= 4 tasks


def _gt_values(gt):
    vals = set()
    for _fn, args in gt:
        for a, v in args.items():
            if a in COUNT_ARGS:
                continue
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                n = _norm_lit(x)
                if n and not (n[0] == "str" and len(n[1]) < MIN_STR_LEAK) \
                        and n not in ENV_CONSTANTS:
                    vals.add(n)
    return vals


def analyse_example(ex, tasks, env_text, restrict=None):
    ews = words(ex["request"])
    best = {"c3": (0.0, None), "c5": (0.0, None), "run": (0, None), "plan": (0.0, None)}
    exact_plan, leaks_gt, leaks_env = [], set(), set()
    lits = {n for n in (_norm_lit(v) for v in ex["literals"]) if n}
    for t in tasks:
        if restrict is not None and (t["suite"], t["id"]) not in restrict:
            continue
        key = f"{t['suite']}/{t['id']}"
        for name, val in (("c3", containment(ews, t["ws"], 3)),
                          ("c5", containment(ews, t["ws"], 5)),
                          ("run", longest_run(ews, t["ws"]))):
            if val > best[name][0]:
                best[name] = (val, key)
        gt_tools = [fn for fn, _ in t["gt"]]
        if gt_tools and ex["tools"]:
            r = difflib.SequenceMatcher(None, ex["tools"], gt_tools).ratio()
            if r > best["plan"][0]:
                best["plan"] = (r, key)
            if ex["tools"] == gt_tools:
                exact_plan.append(key)
        for lv in lits & _gt_values(t["gt"]):
            leaks_gt.add((lv[1], key))
        if lits:
            tw = t["text"].lower()
            for lv in lits:
                if lv[0] == "str" and len(lv[1]) >= MIN_STR_LEAK and lv[1] in tw:
                    leaks_gt.add((lv[1], key + " (task text)"))
    suites = {s for s, _ in restrict} if restrict is not None else set(SUITES)
    for lv in lits:
        if lv[0] != "str" or len(lv[1]) < MIN_STR_LEAK:
            continue
        for s in suites:
            if lv[1] in env_text[s]:
                leaks_env.add((lv[1], s))
    # numbers from the request/commentary that appear verbatim in env text
    for tok in set(re.findall(r"\b\d+\.\d+\b", ex["block"])):
        for s in suites:
            if re.search(r"(?<![\d.])" + re.escape(tok) + r"(?![\d])", env_text[s]):
                if float(tok) >= 10:
                    leaks_env.add((tok, s))
    verdict = []
    if best["c5"][0] >= 0.8 or best["run"][0] >= 10:
        verdict.append("VERBATIM")
    elif best["c3"][0] >= 0.5 or best["run"][0] >= 6:
        verdict.append("NEAR")
    if leaks_gt or leaks_env:
        verdict.append("LEAK")
    return {
        "title": ex["title"], "request": ex["request"], "tools": ex["tools"],
        "max_3gram_containment": round(best["c3"][0], 3), "best_3gram_task": best["c3"][1],
        "max_5gram_containment": round(best["c5"][0], 3), "best_5gram_task": best["c5"][1],
        "longest_common_word_run": best["run"][0], "best_run_task": best["run"][1],
        "max_plan_similarity": round(best["plan"][0], 3), "best_plan_task": best["plan"][1],
        "exact_plan_matches": exact_plan,
        "literal_leaks_ground_truth": sorted(([str(a), b] for a, b in leaks_gt)),
        "literal_leaks_environment": sorted(([str(a), b] for a, b in leaks_env)),
        "verdict": verdict or ["clean"],
    }


def whole_prompt_runs(template, tasks, restrict=None, n=8):
    pws = words(template.replace("{{", "{").replace("}}", "}"))
    grams = ngrams(pws, n)
    hits = []
    for t in tasks:
        if restrict is not None and (t["suite"], t["id"]) not in restrict:
            continue
        if grams & ngrams(t["ws"], n):
            hits.append(f"{t['suite']}/{t['id']}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-version", default="v1.2")
    ap.add_argument("--variants", default="original,clean,kfold")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    import logging
    logging.getLogger("agentdojo").setLevel(logging.ERROR)
    tasks, env_text = load_benchmark(args.suite_version)
    from collections import Counter
    cnt = Counter(v for t in tasks for v in _gt_values(t["gt"]))
    ENV_CONSTANTS.update(v for v, c in cnt.items() if c >= 4)
    variants = prompt_variants(set(args.variants.split(",")))
    report = {"agentdojo_suite_version": args.suite_version,
              "n_tasks": {s: sum(t["suite"] == s for t in tasks) for s in SUITES},
              "variants": {}}
    bad = 0
    for vname, tmpl in variants.items():
        _, blocks, _ = P.split_examples(tmpl)
        exs = [parse_example(b) for _, b in blocks]
        scopes = {"all_tasks": None}
        if vname.startswith("kfold"):
            f = int(vname[5:])
            test = {("banking", t) for t in P.fold_tasks(f)}
            dev = {("banking", t) for t in P.BANKING_USER_TASKS if P.fold_of(t) != f}
            scopes = {"test_fold": test, "dev_folds": dev}
        vrep = {"sha256": P.sha256(tmpl), "n_examples": len(exs), "scopes": {}}
        for sname, restrict in scopes.items():
            rows = [analyse_example(e, tasks, env_text, restrict) for e in exs]
            runs = whole_prompt_runs(tmpl, tasks, restrict)
            summ = {
                "examples_verbatim": sum("VERBATIM" in r["verdict"] for r in rows),
                "examples_near": sum("NEAR" in r["verdict"] for r in rows),
                "examples_literal_leak": sum("LEAK" in r["verdict"] for r in rows),
                "examples_exact_plan_match": sum(bool(r["exact_plan_matches"]) for r in rows),
                "max_3gram_containment": max((r["max_3gram_containment"] for r in rows), default=0),
                "max_5gram_containment": max((r["max_5gram_containment"] for r in rows), default=0),
                "max_common_word_run": max((r["longest_common_word_run"] for r in rows), default=0),
                "tasks_sharing_an_8gram_with_whole_prompt": runs,
            }
            vrep["scopes"][sname] = {"summary": summ, "examples": rows}
            summ["examples_ground_truth_leak"] = sum(bool(r["literal_leaks_ground_truth"]) for r in rows)
            must_be_clean = vname == "clean" or sname == "test_fold"
            # The banking environment is shared by ALL tasks, so a banking-
            # derived kfold example necessarily mentions environment content
            # (e.g. a file name); for kfold test folds only text overlap and
            # ground-truth-argument leaks are gating, env leaks are reported.
            leak = (summ["examples_literal_leak"] if vname == "clean"
                    else summ["examples_ground_truth_leak"])
            dirty = summ["examples_verbatim"] + summ["examples_near"] + leak + len(runs)
            if must_be_clean and dirty:
                bad += 1
        report["variants"][vname] = vrep

    # ---- print ----
    print(f"AgentDojo {args.suite_version}: " + ", ".join(f"{s} {n}" for s, n in report["n_tasks"].items())
          + " tasks (user + injection)\n")
    hdr = (f"{'variant':9s} {'scope':10s} {'ex':>3s} {'verb':>5s} {'near':>5s} {'leak':>5s} "
           f"{'gtlk':>5s} {'plan=':>5s} {'max3g':>6s} {'max5g':>6s} {'run':>4s} {'8g-hits':>8s}")
    print(hdr)
    for vname, v in report["variants"].items():
        for sname, sc in v["scopes"].items():
            s = sc["summary"]
            print(f"{vname:9s} {sname:10s} {v['n_examples']:>3d} {s['examples_verbatim']:>5d} "
                  f"{s['examples_near']:>5d} {s['examples_literal_leak']:>5d} "
                  f"{s['examples_ground_truth_leak']:>5d} "
                  f"{s['examples_exact_plan_match']:>5d} {s['max_3gram_containment']:>6.2f} "
                  f"{s['max_5gram_containment']:>6.2f} {s['max_common_word_run']:>4d} "
                  f"{len(s['tasks_sharing_an_8gram_with_whole_prompt']):>8d}")
    for vname in ("original", "clean"):
        if vname not in report["variants"]:
            continue
        print(f"\nper-example detail: {vname}")
        for r in report["variants"][vname]["scopes"]["all_tasks"]["examples"]:
            print(f"  {r['title'][:70]}")
            print(f"     3g={r['max_3gram_containment']:.2f} ({r['best_3gram_task']})  "
                  f"5g={r['max_5gram_containment']:.2f}  run={r['longest_common_word_run']} "
                  f"({r['best_run_task']})  plan~{r['max_plan_similarity']:.2f} ({r['best_plan_task']})"
                  f"  exact-plan={r['exact_plan_matches'] or '-'}  -> {','.join(r['verdict'])}")
            if r["literal_leaks_ground_truth"] or r["literal_leaks_environment"]:
                print(f"     literal leaks: GT={r['literal_leaks_ground_truth'][:4]} "
                      f"ENV={r['literal_leaks_environment'][:4]}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))
        print(f"\nwrote {args.out}")
    print(f"\nenvironment constants ignored as labels: {sorted(map(str, ENV_CONSTANTS))}")
    print("\nCONTAMINATION GUARD:", "PASS (clean and kfold test folds have no overlap)"
          if bad == 0 else f"FAIL ({bad} clean/test-fold scope(s) overlap the benchmark)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
