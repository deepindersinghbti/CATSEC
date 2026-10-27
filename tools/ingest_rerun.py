#!/usr/bin/env python3
"""
Ingest the clean-prompt rerun bundle and compute every number the paper reports.

    python tools/ingest_rerun.py catsec_clean_rerun_<date>.tar.gz --out DIR
    python tools/ingest_rerun.py path/to/runs_clean/            --out DIR
    python tools/ingest_rerun.py --original-only                --out DIR

Input: the tarball written by ``./run_clean_rerun.sh bundle`` (or its
extracted ``runs_clean/`` folder):

    runs_clean/PROVENANCE.txt
    runs_clean/<gpt|claude|gemini|gemma>/{smoke,clean_main,clean_benign,kfold_0..3}.{jsonl,csv,log}
                                          (+ <name>.manifest.json)

Missing models are allowed: their macros render as "--" and a warning is
printed (e.g. Gemma, which needs a rented GPU).

VERIFICATION (any failure aborts unless --force; nothing is written):
  * PROVENANCE.txt exists, names a 40-hex commit, and its protocol check says
    "PROTOCOL FREEZE: intact".
  * Every JSONL record carries catsec_run.prompt_sha256 and every hash equals
    the frozen value in PROTOCOL.md: clean_v1 planner template (smoke,
    clean_main, clean_benign) or the rendered k-fold fold-f template
    (kfold_f); repair_with_request_v1; heuristics.py; COMPOSER_SYSTEM. The
    logged options must say profile=clean and the right variant / fold.
  * Each manifest's catsec_repo_commit equals PROVENANCE's commit and is not
    "-dirty". If the commit exists locally, the frozen files must not differ
    from this checkout (warning otherwise).
  * The k-fold files contain only their own fold's tasks.

OUTPUTS (in --out):
  results_macros.tex          \\newcommand definitions, \\input by the paper.
                              \\Orig*  = the original (contaminated-prompt)
                              committed runs, always computed from
                              IEEE_Transactions/data/logs; \\Clean*, \\Benign*,
                              \\Kfold*, \\Cmp* = the rerun ("--" if absent).
  results_table_clean.tex     body rows of the main results table (clean runs)
  results_table_orig.tex      body rows of the same table (original runs)
  results_table_compare.tex   body rows of an original-vs-clean comparison table
  results_summary.md          human-readable summary incl. all warnings
  results.json                every computed number

STATISTICS (same procedures as analysis/analyze.py and verify_claims.py):
  Wilson 95% intervals at the trial level; McNemar's exact test on the
  discordant pairs, paired by (user task, injection task, repetition);
  task-clustered bootstrap (resample the 16 user tasks, 5000 iterations,
  random.Random(0) per model, percentiles 125/4874 -- exactly the procedure
  that produced the paper's clustered intervals); mean and range of the
  per-repetition rates; benign utility of both arms; pooled k-fold utility /
  ASR; tokens, calls and latency with CATSEC-vs-undefended reductions.
Per-protocol: errored trials are dropped from denominators and counted in
*Err* macros (intent-to-treat utility is in results.json and the summary).

Offline, read-only on its inputs, stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import random
import re
import shutil
import statistics as st
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis"))

from analyze import binom_two_sided_exact, wilson  # noqa: E402  (reuse analysis logic)

# --------------------------------------------------------------------------- #
# Fixed facts of the experiment
# --------------------------------------------------------------------------- #
# key -> (macro suffix, paper name, model id in clean runs, original log dir, original log file)
MODELS = {
    "gpt": ("Gpt", "GPT-4o-mini", "gpt-4o-mini", "gpt-4o-mini", "banking_final.jsonl"),
    "gemini": ("Gemini", "Gemini 2.5 Flash", "gemini-2.5-flash", "gemini-2.5-flash", "gemini_full.jsonl"),
    "claude": ("Claude", "Claude Haiku 4.5", "claude-haiku-4-5-20251001", "claude-haiku-4-5",
               "banking_claude_final.jsonl"),
    "gemma": ("Gemma", "Gemma 4 31B", "gemma-4-31b-it", "gemma-4-31b-it", "gemma_full.jsonl"),
}
ARMS = {"und": "Und", "cat": "Cat"}
ARM_NAMES = {"und": "Undefended", "cat": "CATSEC-base"}
UT = [f"user_task_{i}" for i in range(16)]
IT = [f"injection_task_{i}" for i in range(9)]
N_FOLDS = 4
FILES = ["smoke", "clean_main", "clean_benign"] + [f"kfold_{f}" for f in range(N_FOLDS)]
BOOT_ITERS = 5000
MISSING = r"\mbox{--}"
WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four"}
DEFAULT_ORIG_LOGS = REPO / "IEEE_Transactions" / "data" / "logs"
FROZEN_PATHS = ["catsec/prompts", "catsec/heuristics.py", "catsec/composer.py"]


class Abort(Exception):
    pass


# --------------------------------------------------------------------------- #
# Frozen hashes (PROTOCOL.md is the pre-registration record)
# --------------------------------------------------------------------------- #
def protocol_hashes(protocol_md: Path) -> dict:
    text = protocol_md.read_text(encoding="utf-8")
    found = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", text, re.MULTILINE))
    need = ["catsec/prompts/clean_v1.txt", "catsec/prompts/repair_with_request_v1.txt",
            "catsec/heuristics.py", "COMPOSER_SYSTEM (logged runs, unchanged)"] + \
           [f"kfold fold {f} (rendered template)" for f in range(N_FOLDS)]
    missing = [k for k in need if k not in found]
    if missing:
        raise Abort(f"PROTOCOL.md lacks frozen hashes for {missing}")
    common = {"repair_template": found["catsec/prompts/repair_with_request_v1.txt"],
              "generic_heuristics_py": found["catsec/heuristics.py"],
              "composer_system": found["COMPOSER_SYSTEM (logged runs, unchanged)"]}
    exp = {}
    for name in FILES:
        if name.startswith("kfold_"):
            f = int(name.split("_")[1])
            exp[name] = {"planner_system_template": found[f"kfold fold {f} (rendered template)"],
                         **common, "_variant": "kfold", "_fold": f}
        else:
            exp[name] = {"planner_system_template": found["catsec/prompts/clean_v1.txt"],
                         **common, "_variant": "clean", "_fold": None}
    return exp


def local_protocol_intact() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-I", str(REPO / "tools" / "protocol_hashes.py"), "--check"],
                       capture_output=True, text=True, cwd=REPO)
    return r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def safe_extract(tar_path: Path, dest: Path) -> Path:
    """Extract an untrusted tarball: regular files and directories only, no
    absolute paths, no '..', no links (tarfile 'data' filter)."""
    with tarfile.open(tar_path, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                raise Abort(f"refusing suspicious tar member {m.name!r}")
            members.append(m)
        tf.extractall(dest, members=members, filter="data")
    return find_runs_root(dest)


def find_runs_root(path: Path) -> Path:
    if (path / "PROVENANCE.txt").exists():
        return path
    if (path / "runs_clean").is_dir():
        return path / "runs_clean"
    hits = sorted(p.parent for p in path.rglob("PROVENANCE.txt"))
    if len(hits) == 1:
        return hits[0]
    cand = sorted(p for p in path.rglob("runs_clean") if p.is_dir())
    if len(cand) == 1:
        return cand[0]
    raise Abort(f"cannot find runs_clean/ (with PROVENANCE.txt) under {path}")


def load_jsonl(path: Path) -> tuple[list, int]:
    recs, bad = [], 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1  # a truncated last line after an interruption
    return recs, bad


def arm_of(rec) -> str | None:
    d = rec.get("defense")
    if d in ("no-defense", "none", None, "None"):
        return "und"
    if d == "catsec":
        return "cat"
    return None


def dedupe(recs: list) -> list:
    """--resume re-runs errored trials and appends: keep, per trial key, the
    last successful record, else the last record."""
    best = {}
    for r in recs:
        k = (r.get("defense"), r.get("attack"), r.get("user_task"), r.get("injection_task"),
             r.get("repetition"), r.get("condition"), r.get("ablation"))
        if k not in best or r.get("error") is None or best[k].get("error") is not None:
            best[k] = r
    return list(best.values())


# --------------------------------------------------------------------------- #
# Verification of the bundle
# --------------------------------------------------------------------------- #
def parse_provenance(root: Path, issues: dict) -> dict:
    p = root / "PROVENANCE.txt"
    prov = {"commit": None, "branch": None, "date": None, "freeze_intact": False, "fake": False}
    if not p.exists():
        issues["fatal"].append("PROVENANCE.txt is missing from the bundle")
        return prov
    text = p.read_text(encoding="utf-8", errors="replace")
    prov["raw"] = text
    for key in ("commit", "branch", "date"):
        m = re.search(rf"^{key}:\s*(\S+)", text, re.MULTILINE)
        prov[key] = m.group(1) if m else None
    prov["freeze_intact"] = "PROTOCOL FREEZE: intact" in text
    prov["fake"] = "FAKE" in text
    if not prov["freeze_intact"]:
        issues["fatal"].append("PROVENANCE.txt: protocol check did not report 'PROTOCOL FREEZE: intact'")
    if any(line.startswith("DRIFT") for line in text.splitlines()):
        issues["fatal"].append("PROVENANCE.txt: protocol check reports DRIFT")
    if not prov["commit"] or not re.fullmatch(r"[0-9a-f]{40}", prov["commit"]):
        issues["fatal"].append(f"PROVENANCE.txt: no valid commit hash (got {prov['commit']!r})")
    else:
        known = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", prov["commit"] + "^{commit}"],
                               capture_output=True).returncode == 0
        if not known:
            issues["warn"].append(f"commit {prov['commit'][:12]} is not in this checkout "
                                  "(git fetch origin to check it); record hashes were verified anyway")
        else:
            same = subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", prov["commit"], "HEAD",
                                   "--", *FROZEN_PATHS], capture_output=True).returncode == 0
            if not same:
                issues["warn"].append(f"frozen files differ between {prov['commit'][:12]} and this "
                                      "checkout's HEAD (record hashes decide)")
    return prov


def verify_file(model: str, name: str, recs: list, manifest: dict | None, exp: dict,
                prov: dict, issues: dict) -> None:
    where = f"{model}/{name}"
    e = exp[name]
    n_missing = n_bad = n_opt = 0
    bad_keys = Counter()
    for r in recs:
        cr = r.get("catsec_run") or {}
        h = cr.get("prompt_sha256")
        if not h:
            n_missing += 1
            continue
        bad = [k for k in ("planner_system_template", "repair_template", "generic_heuristics_py",
                           "composer_system") if h.get(k) != e[k]]
        if bad:
            n_bad += 1
            bad_keys.update(bad)
        o = cr.get("options") or {}
        if (o.get("profile") != "clean" or o.get("prompt_variant") != e["_variant"]
                or o.get("fold") != e["_fold"]):
            n_opt += 1
    if n_missing:
        issues["fatal"].append(f"{where}: {n_missing}/{len(recs)} records have no catsec_run.prompt_sha256")
    if n_bad:
        issues["fatal"].append(f"{where}: {n_bad}/{len(recs)} records have prompt hashes that differ "
                               f"from PROTOCOL.md ({dict(bad_keys)})")
    if n_opt:
        issues["fatal"].append(f"{where}: {n_opt}/{len(recs)} records were not run with "
                               f"--profile clean / variant {e['_variant']} / fold {e['_fold']}")
    if manifest is None:
        issues["warn"].append(f"{where}: no manifest.json (commit cross-check skipped)")
    else:
        c = str(manifest.get("catsec_repo_commit", ""))
        if c.endswith("-dirty"):
            issues["fatal"].append(f"{where}: run from a modified catsec/ checkout ({c})")
        elif prov.get("commit") and c != prov["commit"]:
            issues["fatal"].append(f"{where}: manifest commit {c[:12]} != PROVENANCE commit "
                                   f"{prov['commit'][:12]}")
        mh = manifest.get("prompt_sha256") or {}
        if mh.get("planner_system_template") != e["planner_system_template"]:
            issues["fatal"].append(f"{where}: manifest planner hash differs from PROTOCOL.md")
    if name.startswith("kfold_"):
        f = int(name.split("_")[1])
        sys.path.insert(0, str(REPO))
        from catsec import prompts as P
        allowed = set(P.fold_tasks(f))
        stray = {r.get("user_task") for r in recs} - allowed
        if stray:
            issues["fatal"].append(f"{where}: tasks outside fold {f}: {sorted(stray)}")
    models = {r.get("model") for r in recs}
    if models - {MODELS[model][2]}:
        issues["warn"].append(f"{where}: model id(s) {sorted(map(str, models))}, expected "
                              f"{MODELS[model][2]}")


def load_bundle(root: Path, issues: dict) -> tuple[dict, dict]:
    prov = parse_provenance(root, issues)
    exp = protocol_hashes(REPO / "PROTOCOL.md")
    data = {}
    for model in MODELS:
        d = root / model
        if not d.is_dir():
            issues["warn"].append(f"model {model}: not in the bundle -> its macros render as '--'")
            continue
        data[model] = {}
        for extra in sorted(d.glob("*.jsonl")):
            if extra.stem not in FILES and not extra.stem.endswith(".shard"):
                issues["warn"].append(f"{model}/{extra.name}: unexpected file, ignored")
        for name in FILES:
            p = d / f"{name}.jsonl"
            if not p.exists():
                issues["warn"].append(f"{model}/{name}.jsonl missing")
                continue
            recs, bad = load_jsonl(p)
            if bad:
                issues["warn"].append(f"{model}/{name}.jsonl: {bad} unparsable line(s) skipped")
            if any(r.get("FAKE_SYNTHETIC") for r in recs):
                prov["fake"] = True
            mpath = d / f"{name}.manifest.json"
            manifest = json.loads(mpath.read_text()) if mpath.exists() else None
            verify_file(model, name, recs, manifest, exp, prov, issues)
            recs = dedupe(recs)
            errs = sum(r.get("error") is not None for r in recs)
            if errs:
                issues["warn"].append(f"{model}/{name}: {errs} errored trial(s) remain after resume "
                                      "(dropped from per-protocol denominators)")
            data[model][name] = recs
    return data, prov


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def ok(r) -> bool:
    return r.get("error") is None


def wil(k: int, n: int) -> dict:
    if n == 0:
        return {"k": k, "n": n, "p": None, "lo": None, "hi": None}
    p, lo, hi = wilson(k, n)
    return {"k": k, "n": n, "p": p, "lo": lo, "hi": hi}


def cluster_boot(rng: random.Random, num: dict, den: dict) -> tuple:
    """Resample the 16 user tasks with replacement; statistic = pooled ratio.
    With complete data this is exactly verify_claims.py's procedure."""
    bs = []
    for _ in range(BOOT_ITERS):
        s = [rng.choice(UT) for _ in UT]
        d = sum(den.get(u, 0) for u in s)
        bs.append(sum(num.get(u, 0) for u in s) / d if d else float("nan"))
    bs = sorted(x for x in bs if not math.isnan(x))
    if not bs:
        return None, None
    n = len(bs)
    return bs[int(0.025 * n)], bs[int(0.975 * n) - 1]


def injected_stats(recs: list) -> dict:
    """All statistics of one model's injected run (any number of repetitions)."""
    by_arm = defaultdict(list)
    for r in recs:
        if r.get("condition", "injected") == "injected" and arm_of(r):
            by_arm[arm_of(r)].append(r)
    reps = sorted({r.get("repetition", 0) for r in recs})
    out = {"reps": reps, "arms": {}}
    keyed = {a: {(r["user_task"], r["injection_task"], r.get("repetition", 0)): r for r in rs}
             for a, rs in by_arm.items()}
    for a, rs in by_arm.items():
        okr = [r for r in rs if ok(r)]
        sec = [r for r in okr if r.get("security_attack_succeeded") is not None]
        asr_k = sum(bool(r["security_attack_succeeded"]) for r in sec)
        util_k = sum(bool(r.get("utility")) for r in okr)
        per_rep = {}
        for rep in reps:
            o = [r for r in okr if r.get("repetition", 0) == rep]
            s = [r for r in o if r.get("security_attack_succeeded") is not None]
            per_rep[rep] = {"util": sum(bool(r.get("utility")) for r in o) / len(o) if o else None,
                            "asr": sum(bool(r["security_attack_succeeded"]) for r in s) / len(s) if s else None}
        per_task_util = Counter(r["user_task"] for r in okr if r.get("utility"))
        per_task_n = Counter(r["user_task"] for r in okr)
        solved = sorted((t for t in UT if per_task_n[t] and per_task_util[t] == per_task_n[t]),
                        key=UT.index)
        solved_rep = {}
        for rep in reps:
            u = Counter(r["user_task"] for r in okr if r.get("repetition", 0) == rep and r.get("utility"))
            n = Counter(r["user_task"] for r in okr if r.get("repetition", 0) == rep)
            solved_rep[rep] = sum(1 for t in UT if n[t] and u[t] == n[t])
        # all-or-nothing within a repetition: every (task, rep) is 0/9 or 9/9
        u_tr = Counter((r["user_task"], r.get("repetition", 0)) for r in okr if r.get("utility"))
        n_tr = Counter((r["user_task"], r.get("repetition", 0)) for r in okr)
        plans = defaultdict(set)
        plans_rep = defaultdict(set)
        for r in okr:
            if r.get("plan_signature") is not None:
                plans[r["user_task"]].add(json.dumps(r["plan_signature"], sort_keys=True))
                plans_rep[(r["user_task"], r.get("repetition", 0))].add(
                    json.dumps(r["plan_signature"], sort_keys=True))
        def mean(f):
            return st.mean(float(r.get(f) or 0) for r in okr) if okr else None
        out["arms"][a] = {
            "trials": len(rs), "errors": len(rs) - len(okr),
            "util": wil(util_k, len(okr)), "util_itt": wil(util_k, len(rs)),
            "asr": wil(asr_k, len(sec)), "sec": wil(len(sec) - asr_k, len(sec)),
            "refusals": sum(len(r.get("refused") or []) for r in okr),
            "refusals_per_rep": (sum(len(r.get("refused") or []) for r in okr) / len(reps)) if reps else None,
            "per_rep": per_rep,
            "rep_util": [v["util"] for v in per_rep.values() if v["util"] is not None],
            "rep_asr": [v["asr"] for v in per_rep.values() if v["asr"] is not None],
            "tasks_solved": len(solved), "solved_list": solved,
            "tasks_solved_per_rep": solved_rep,
            "all_or_nothing": all(u_tr[k] in (0, n) for k, n in n_tr.items()),
            "plan_valid": sum(r.get("plan_valid") is True for r in okr),
            "max_plans_per_task_rep": max((len(v) for v in plans_rep.values()), default=0),
            "max_plans_per_task": max((len(v) for v in plans.values()), default=0),
            "mean": {f: mean(f) for f in ("n_model_calls", "tokens_in", "tokens_out", "latency_s")},
            "total": {f: sum(int(r.get(f) or 0) for r in okr) for f in ("n_model_calls", "tokens_in", "tokens_out")},
        }
    if "und" in keyed and "cat" in keyed:
        common = sorted(set(keyed["und"]) & set(keyed["cat"]))
        U, C = keyed["und"], keyed["cat"]
        both = [k for k in common if ok(U[k]) and ok(C[k])]
        mc = {}
        for metric, f in (("sec", lambda r: not r["security_attack_succeeded"]),
                          ("util", lambda r: bool(r.get("utility")))):
            ks = [k for k in both if metric != "sec" or (U[k].get("security_attack_succeeded") is not None
                                                          and C[k].get("security_attack_succeeded") is not None)]
            cat_better = sum(f(C[k]) and not f(U[k]) for k in ks)
            und_better = sum(f(U[k]) and not f(C[k]) for k in ks)
            mc[metric] = {"cat_better": cat_better, "und_better": und_better, "n": len(ks),
                          "p": binom_two_sided_exact(cat_better, und_better)}
        out["mcnemar"] = mc
        # task-clustered bootstrap; the ORDER of the statistics fixes the rng
        # stream (utility difference first, undefended ASR second, as in the
        # audit that produced the paper's intervals).
        rng = random.Random(0)
        dnum, dden = Counter(), Counter()
        for k in both:
            dnum[k[0]] += bool(C[k].get("utility")) - bool(U[k].get("utility"))
            dden[k[0]] += 1
        d0 = sum(dnum.values()) / sum(dden.values()) if dden else None
        lo, hi = cluster_boot(rng, dnum, dden)
        out["boot_util_diff"] = {"point": d0, "lo": lo, "hi": hi}
        for a, metric in (("und", "asr"), ("cat", "asr"), ("und", "util"), ("cat", "util")):
            num, den = Counter(), Counter()
            for r in by_arm[a]:
                if not ok(r):
                    continue
                if metric == "asr":
                    if r.get("security_attack_succeeded") is None:
                        continue
                    num[r["user_task"]] += bool(r["security_attack_succeeded"])
                else:
                    num[r["user_task"]] += bool(r.get("utility"))
                den[r["user_task"]] += 1
            lo, hi = cluster_boot(rng, num, den)
            out["arms"][a][f"boot_{metric}"] = {"lo": lo, "hi": hi}
        # repetition stability
        if len(reps) > 1:
            for a in ("und", "cat"):
                g = defaultdict(dict)
                for k, r in keyed[a].items():
                    if ok(r):
                        g[k[:2]][k[2]] = (bool(r.get("utility")), r.get("security_attack_succeeded"))
                multi = [v for v in g.values() if len(v) == len(reps)]
                out["arms"][a]["rep_stable_util"] = (sum(len({x[0] for x in v.values()}) == 1 for v in multi)
                                                     / len(multi)) if multi else None
                out["arms"][a]["rep_stable_sec"] = (sum(len({x[1] for x in v.values()}) == 1 for v in multi)
                                                    / len(multi)) if multi else None
        # cost / latency reductions
        red = {}
        ua, ca = out["arms"]["und"], out["arms"]["cat"]
        for f in ("n_model_calls", "tokens_in", "tokens_out"):
            red[f] = 1 - ca["total"][f] / ua["total"][f] if ua["total"][f] else None
        red["latency_s"] = (1 - ca["mean"]["latency_s"] / ua["mean"]["latency_s"]
                            if ua["mean"]["latency_s"] else None)
        out["reduction"] = red
    return out


def benign_stats(recs: list) -> dict:
    out = {}
    for a in ("und", "cat"):
        rs = [r for r in recs if arm_of(r) == a]
        okr = [r for r in rs if ok(r)]
        out[a] = {"util": wil(sum(bool(r.get("utility")) for r in okr), len(okr)),
                  "errors": len(rs) - len(okr),
                  "solved": sorted({r["user_task"] for r in okr if r.get("utility")}, key=UT.index)}
    return out


def kfold_stats(files: dict) -> dict:
    recs = [r for f in range(N_FOLDS) for r in files.get(f"kfold_{f}", []) if arm_of(r) == "cat"]
    present = [f for f in range(N_FOLDS) if f"kfold_{f}" in files]
    okr = [r for r in recs if ok(r)]
    sec = [r for r in okr if r.get("security_attack_succeeded") is not None]
    u = Counter(r["user_task"] for r in okr if r.get("utility"))
    n = Counter(r["user_task"] for r in okr)
    per_fold = {}
    for f in present:
        o = [r for r in files[f"kfold_{f}"] if arm_of(r) == "cat" and ok(r)]
        per_fold[f] = sum(bool(r.get("utility")) for r in o) / len(o) if o else None
    return {"folds_present": present, "complete": len(present) == N_FOLDS,
            "util": wil(sum(bool(r.get("utility")) for r in okr), len(okr)),
            "asr": wil(sum(bool(r["security_attack_succeeded"]) for r in sec), len(sec)),
            "tasks_solved": sum(1 for t in UT if n[t] and u[t] == n[t]),
            "errors": len(recs) - len(okr), "per_fold_util": per_fold}


def load_original(logs: Path) -> dict:
    out = {}
    for model, (_m, _name, _mid, d, fn) in MODELS.items():
        recs, _ = load_jsonl(logs / d / fn)
        out[model] = recs
    return out


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def pct(x) -> str | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    s = f"{100 * x:.1f}"
    return "0.0" if s == "-0.0" else s


def spct(x) -> str | None:
    if x is None:
        return None
    s = f"{100 * x:+.1f}"
    return "+0.0" if s == "-0.0" else s


def pval(p) -> str | None:
    if p is None:
        return None
    if p >= 1e-3:
        return f"{p:#.3g}"
    mant, ex = f"{p:.1e}".split("e")
    return rf"{mant}\times10^{{{int(ex)}}}"


def f2(x) -> str | None:
    return None if x is None else f"{x:.2f}"


def thousands(n) -> str | None:
    return None if n is None else f"{n:,}"


class Macros:
    def __init__(self):
        self.defs: dict[str, str | None] = {}
        self.comments: dict[str, str] = {}

    def add(self, name: str, value, comment: str = ""):
        if not re.fullmatch(r"[A-Za-z]+", name):
            raise ValueError(f"bad macro name {name!r}")
        if name in self.defs:
            raise ValueError(f"duplicate macro {name!r}")
        self.defs[name] = None if value is None else str(value)
        if comment:
            self.comments[name] = comment

    def render(self, header: str) -> str:
        lines = [header]
        for k, v in self.defs.items():
            c = f"  % {self.comments[k]}" if k in self.comments else ""
            lines.append(rf"\newcommand{{\{k}}}{{{MISSING if v is None else v}}}{c}")
        return "\n".join(lines) + "\n"

    def get(self, name):
        v = self.defs[name]
        return MISSING if v is None else v


def g(d, *path):
    """Nested get that returns None on any missing level."""
    for p in path:
        if d is None:
            return None
        d = d.get(p) if isinstance(d, dict) else None
    return d


def add_injected_macros(M: Macros, S: str, stats: dict, label: str):
    """Macros of one result set (S = Orig | Clean) for every model."""
    for model, (mm, name, *_r) in MODELS.items():
        s = stats.get(model)
        for a, aa in ARMS.items():
            A = g(s, "arms", a)
            for met, key in (("Util", "util"), ("Sec", "sec"), ("ASR", "asr")):
                w = g(A, key)
                M.add(f"{S}{met}{aa}{mm}", pct(g(w, "p")), f"{label} {name} {ARM_NAMES[a]} {key} %")
                M.add(f"{S}{met}{aa}{mm}Lo", pct(g(w, "lo")))
                M.add(f"{S}{met}{aa}{mm}Hi", pct(g(w, "hi")))
                M.add(f"{S}{met}{aa}{mm}K", g(w, "k"))
                M.add(f"{S}{met}{aa}{mm}N", g(w, "n"))
            for met, key in (("ASR", "boot_asr"), ("Util", "boot_util")):
                M.add(f"{S}Boot{met}{aa}{mm}Lo", pct(g(A, key, "lo")), "task-clustered bootstrap")
                M.add(f"{S}Boot{met}{aa}{mm}Hi", pct(g(A, key, "hi")))
            M.add(f"{S}Err{aa}{mm}", g(A, "errors"))
            M.add(f"{S}Trials{aa}{mm}", g(A, "trials"))
            for met, key in (("Util", "rep_util"), ("ASR", "rep_asr")):
                v = g(A, key)
                ok_ = bool(v)
                M.add(f"{S}{met}{aa}{mm}RepMean", pct(st.mean(v)) if ok_ else None, "mean over repetitions")
                M.add(f"{S}{met}{aa}{mm}RepMin", pct(min(v)) if ok_ else None)
                M.add(f"{S}{met}{aa}{mm}RepMax", pct(max(v)) if ok_ else None)
            M.add(f"{S}Calls{aa}{mm}", f2(g(A, "mean", "n_model_calls")), "mean per trial")
            M.add(f"{S}TokIn{aa}{mm}", thousands(g(A, "total", "tokens_in")), "total")
            M.add(f"{S}TokOut{aa}{mm}", thousands(g(A, "total", "tokens_out")), "total")
            M.add(f"{S}Lat{aa}{mm}", f2(g(A, "mean", "latency_s")), "mean s per trial")
            if a == "cat":
                M.add(f"{S}Ref{aa}{mm}", g(A, "refusals"), "refused calls, all repetitions")
                rpr = g(A, "refusals_per_rep")
                M.add(f"{S}RefPerRep{aa}{mm}", None if rpr is None else
                      (f"{rpr:.0f}" if float(rpr).is_integer() else f"{rpr:.1f}"))
                M.add(f"{S}TasksSolved{mm}", g(A, "tasks_solved"), "of 16, solved in every trial")
                tw = wil(g(A, "tasks_solved"), 16) if g(A, "tasks_solved") is not None else None
                M.add(f"{S}TasksSolved{mm}Lo", pct(g(tw, "lo")))
                M.add(f"{S}TasksSolved{mm}Hi", pct(g(tw, "hi")))
                sr = g(A, "tasks_solved_per_rep")
                M.add(f"{S}TasksSolved{mm}RepMin", min(sr.values()) if sr else None)
                M.add(f"{S}TasksSolved{mm}RepMax", max(sr.values()) if sr else None)
                M.add(f"{S}AllOrNothing{mm}", None if A is None else ("yes" if A["all_or_nothing"] else "no"))
                M.add(f"{S}PlanValid{mm}", g(A, "plan_valid"))
                M.add(f"{S}PlansPerTask{mm}", g(A, "max_plans_per_task_rep"),
                      "max distinct plans per task within a repetition")
            st_u, st_s = g(A, "rep_stable_util"), g(A, "rep_stable_sec")
            M.add(f"{S}RepStableUtil{aa}{mm}", pct(st_u), "% of pairs with identical outcome in every rep")
            M.add(f"{S}RepStableSec{aa}{mm}", pct(st_s))
        for met in ("Sec", "Util"):
            mc = g(s, "mcnemar", met.lower())
            M.add(f"{S}Mc{met}{mm}CatBetter", g(mc, "cat_better"), "McNemar n10 (CATSEC better)")
            M.add(f"{S}Mc{met}{mm}UndBetter", g(mc, "und_better"), "McNemar n01")
            M.add(f"{S}Mc{met}{mm}P", pval(g(mc, "p")), "use in math mode")
        b = g(s, "boot_util_diff")
        M.add(f"{S}DiffUtil{mm}", spct(g(b, "point")), "CATSEC minus undefended, pp (math mode)")
        M.add(f"{S}DiffUtil{mm}Lo", spct(g(b, "lo")))
        M.add(f"{S}DiffUtil{mm}Hi", spct(g(b, "hi")))
        for f, k in (("Calls", "n_model_calls"), ("TokIn", "tokens_in"), ("TokOut", "tokens_out"),
                     ("Lat", "latency_s")):
            M.add(f"{S}Red{f}{mm}", pct(g(s, "reduction", k)), "% reduction CATSEC vs undefended")
    # aggregates over the models that are present
    present = [m for m in MODELS if stats.get(m) and "cat" in stats[m]["arms"] and "und" in stats[m]["arms"]]
    cat = [stats[m]["arms"]["cat"] for m in present]
    und = [stats[m]["arms"]["und"] for m in present]
    M.add(f"{S}NModels", len(present) if present else None)
    M.add(f"{S}NModelsWord", WORDS.get(len(present)) if present else None)
    M.add(f"{S}NReps", max((len(stats[m]["reps"]) for m in present), default=None) if present else None)
    M.add(f"{S}CatTrials", sum(a["asr"]["n"] for a in cat) if cat else None, "CATSEC trials with a security verdict")
    M.add(f"{S}CatBreaches", sum(a["asr"]["k"] for a in cat) if cat else None)
    M.add(f"{S}TrialsAll", sum(a["trials"] for a in cat + und) if cat else None)
    comp = [a["asr"]["p"] for a in und if a["asr"]["p"]]
    M.add(f"{S}NCompromised", len(comp) if present else None)
    M.add(f"{S}NCompromisedWord", WORDS.get(len(comp)) if present else None)
    M.add(f"{S}ASRUndMin", pct(min(comp)) if comp else None, "over compromised undefended agents")
    M.add(f"{S}ASRUndMax", pct(max(comp)) if comp else None)
    catasr = [a["asr"]["p"] for a in cat]
    M.add(f"{S}ASRCatMax", pct(max(catasr)) if catasr else None)
    ts = [a["tasks_solved"] for a in cat]
    M.add(f"{S}TasksSolvedMin", min(ts) if ts else None)
    M.add(f"{S}TasksSolvedMax", max(ts) if ts else None)
    tw = wil(max(ts), 16) if ts else None
    M.add(f"{S}TasksSolvedMaxLo", pct(g(tw, "lo")), "Wilson CI of max tasks solved / 16")
    M.add(f"{S}TasksSolvedMaxHi", pct(g(tw, "hi")))
    best = max(present, key=lambda m: stats[m]["arms"]["cat"]["tasks_solved"]) if present else None
    bw = g(stats.get(best) if best else None, "arms", "cat", "util")
    M.add(f"{S}TasksSolvedMaxPairLo", pct(g(bw, "lo")), "pair-level Wilson CI of that planner")
    M.add(f"{S}TasksSolvedMaxPairHi", pct(g(bw, "hi")))
    refs = [a["refusals_per_rep"] for a in cat]
    fr = (lambda x: f"{x:.0f}" if float(x).is_integer() else f"{x:.1f}")
    M.add(f"{S}RefMin", fr(min(refs)) if refs else None, "refused calls per 144-pair pass")
    M.add(f"{S}RefMax", fr(max(refs)) if refs else None)
    uu = [a["util"]["p"] for a in und]
    cu = [a["util"]["p"] for a in cat]
    M.add(f"{S}UndUtilSpread", pct(max(uu) - min(uu)) if uu else None, "pp, max-min over planners")
    M.add(f"{S}CatUtilSpread", pct(max(cu) - min(cu)) if cu else None)
    M.add(f"{S}CatTasksSpread", (max(ts) - min(ts)) if ts else None)
    for aa, vals in (("Und", uu), ("Cat", cu)):
        M.add(f"{S}Util{aa}Min", pct(min(vals)) if vals else None, "range over planners")
        M.add(f"{S}Util{aa}Max", pct(max(vals)) if vals else None)
    for a, aa in ARMS.items():
        for met in ("Util", "Sec"):
            v = [stats[m]["arms"][a].get(f"rep_stable_{met.lower()}") for m in present]
            v = [x for x in v if x is not None]
            M.add(f"{S}RepStable{met}{aa}Min", pct(min(v)) if v else None,
                  "min over planners of % pairs identical in every repetition")
    for f, k in (("Calls", "n_model_calls"), ("TokIn", "tokens_in"), ("TokOut", "tokens_out"),
                 ("Lat", "latency_s")):
        v = [stats[m]["reduction"][k] for m in present if stats[m]["reduction"][k] is not None]
        M.add(f"{S}Red{f}Min", pct(min(v)) if v else None, "% reduction, range over planners")
        M.add(f"{S}Red{f}Max", pct(max(v)) if v else None)
    # the planner with the largest input-token reduction (named in Sec. VI)
    if present:
        mx = max(present, key=lambda m: stats[m]["reduction"]["tokens_in"])
        U, C = stats[mx]["arms"]["und"], stats[mx]["arms"]["cat"]
        vals = (MODELS[mx][1], thousands(U["total"]["tokens_in"]), thousands(C["total"]["tokens_in"]),
                f2(U["mean"]["latency_s"]), f2(C["mean"]["latency_s"]))
    else:
        vals = (None,) * 5
    for suf, v in zip(("Model", "InUnd", "InCat", "LatUnd", "LatCat"), vals):
        M.add(f"{S}TokRedModel{suf if suf != 'Model' else ''}", v)


def table_rows(M: Macros, S: str) -> str:
    """Body rows of the main results table, one block per planner (the column
    layout of Table II: model, configuration, utility, secure, ASR, refusals)."""
    out = []
    for i, (model, (mm, name, *_r)) in enumerate(MODELS.items()):
        if i:
            out.append(r"\midrule")
        for a, aa in ARMS.items():
            def cell(met):
                b = (rf"\{S}{met}{aa}{mm}{{}} [\{S}{met}{aa}{mm}Lo, \{S}{met}{aa}{mm}Hi]")
                return rf"\textbf{{{b}}}" if a == "cat" and met in ("Sec", "ASR") else b
            ref = rf"\{S}RefPerRep{aa}{mm}" if a == "cat" else "n/a"
            color = "catsecrow" if a == "cat" else "undefendedrow"
            out.append(rf"\rowcolor{{{color}}}")
            out.append(rf"{name} & {ARM_NAMES[a]} &" + "\n" + rf"{cell('Util')} &" + "\n"
                       + rf"{cell('Sec')} &" + "\n" + rf"{cell('ASR')} & {ref} \\")
    return "\n".join(out) + "\n"


def compare_rows() -> str:
    out = []
    for model, (mm, name, *_r) in MODELS.items():
        out.append(rf"{name} & \OrigUtilUnd{mm} & \CleanUtilUnd{mm} & \OrigUtilCat{mm} & "
                   rf"\CleanUtilCat{mm} & \OrigASRUnd{mm} & \CleanASRUnd{mm} & \OrigASRCat{mm} & "
                   rf"\CleanASRCat{mm} \\")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Markdown summary
# --------------------------------------------------------------------------- #
def md_summary(M: Macros, prov, issues, orig, clean, benign, kfold, fake, source) -> str:
    def v(name):  # plain text for markdown
        x = M.defs[name]
        return "--" if x is None else re.sub(r"\\times10\^\{(-?\d+)\}", r"e\1", x)
    L = []
    if fake:
        L += ["# !!! FAKE SYNTHETIC BUNDLE -- TEST DATA ONLY, NEVER USE IN THE PAPER !!!", ""]
    L += ["# CATSEC clean-prompt rerun: ingest summary", "",
          f"- Generated: {_dt.datetime.now().isoformat(timespec='seconds')} by `tools/ingest_rerun.py`",
          f"- Source: `{source}`",
          f"- PROVENANCE commit: `{prov.get('commit')}`  branch: `{prov.get('branch')}`  date: `{prov.get('date')}`",
          f"- Protocol freeze in bundle: {'intact' if prov.get('freeze_intact') else 'NOT INTACT / not checked'}",
          "", "## Verification", ""]
    L += [f"- FATAL: {x}" for x in issues["fatal"]] or ["- no fatal problems"]
    L += [f"- warning: {x}" for x in issues["warn"]]
    L += ["", "## Main results (clean prompt; pooled over repetitions, Wilson 95% CI; "
          "task-clustered bootstrap CI in braces)", "",
          "| Planner | Arm | Utility % | ASR % | ASR mean (range) over reps | Refusals/pass | Errors |",
          "|---|---|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        for a, aa in ARMS.items():
            L.append(f"| {name} | {ARM_NAMES[a]} | {v(f'CleanUtil{aa}{mm}')} [{v(f'CleanUtil{aa}{mm}Lo')}, "
                     f"{v(f'CleanUtil{aa}{mm}Hi')}] {{{v(f'CleanBootUtil{aa}{mm}Lo')}, {v(f'CleanBootUtil{aa}{mm}Hi')}}} "
                     f"| {v(f'CleanASR{aa}{mm}')} [{v(f'CleanASR{aa}{mm}Lo')}, {v(f'CleanASR{aa}{mm}Hi')}] "
                     f"{{{v(f'CleanBootASR{aa}{mm}Lo')}, {v(f'CleanBootASR{aa}{mm}Hi')}}} "
                     f"| {v(f'CleanASR{aa}{mm}RepMean')} ({v(f'CleanASR{aa}{mm}RepMin')}-{v(f'CleanASR{aa}{mm}RepMax')}) "
                     f"| {v(f'CleanRefPerRep{aa}{mm}') if a == 'cat' else 'n/a'} | {v(f'CleanErr{aa}{mm}')} |")
    L += ["", "## Paired tests and clustered utility difference (clean)", "",
          "| Planner | McNemar security (n10, n01, p) | McNemar utility (n10, n01, p) | Utility diff pp [cluster CI] | Tasks solved /16 (per-rep range) | All-or-nothing |",
          "|---|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        L.append(f"| {name} | ({v(f'CleanMcSec{mm}CatBetter')}, {v(f'CleanMcSec{mm}UndBetter')}, {v(f'CleanMcSec{mm}P')}) "
                 f"| ({v(f'CleanMcUtil{mm}CatBetter')}, {v(f'CleanMcUtil{mm}UndBetter')}, {v(f'CleanMcUtil{mm}P')}) "
                 f"| {v(f'CleanDiffUtil{mm}')} [{v(f'CleanDiffUtil{mm}Lo')}, {v(f'CleanDiffUtil{mm}Hi')}] "
                 f"| {v(f'CleanTasksSolved{mm}')} ({v(f'CleanTasksSolved{mm}RepMin')}-{v(f'CleanTasksSolved{mm}RepMax')}) "
                 f"| {v(f'CleanAllOrNothing{mm}')} |")
    L += ["", "## Benign (no injection) utility, both arms", "",
          "| Planner | Undefended | CATSEC-base |", "|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        L.append(f"| {name} | {v(f'BenignUtilUnd{mm}')} [{v(f'BenignUtilUnd{mm}Lo')}, {v(f'BenignUtilUnd{mm}Hi')}] "
                 f"({v(f'BenignUtilUnd{mm}K')}/{v(f'BenignUtilUnd{mm}N')}) | {v(f'BenignUtilCat{mm}')} "
                 f"[{v(f'BenignUtilCat{mm}Lo')}, {v(f'BenignUtilCat{mm}Hi')}] ({v(f'BenignUtilCat{mm}K')}/{v(f'BenignUtilCat{mm}N')}) |")
    L += ["", "## 4-fold variant (CATSEC arm, pooled over the four test folds)", "",
          "| Planner | Utility % | ASR % | Tasks solved | Folds present |", "|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        k = kfold.get(model)
        L.append(f"| {name} | {v(f'KfoldUtilCat{mm}')} [{v(f'KfoldUtilCat{mm}Lo')}, {v(f'KfoldUtilCat{mm}Hi')}] "
                 f"| {v(f'KfoldASRCat{mm}')} | {v(f'KfoldTasksSolved{mm}')} | "
                 f"{k['folds_present'] if k else '--'} |")
    L += ["", "## Usage (clean)", "",
          "| Planner | Calls/trial und -> cat | Input tokens und -> cat | Latency s und -> cat | Reductions calls/in/out/latency % |",
          "|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        L.append(f"| {name} | {v(f'CleanCallsUnd{mm}')} -> {v(f'CleanCallsCat{mm}')} | {v(f'CleanTokInUnd{mm}')} -> "
                 f"{v(f'CleanTokInCat{mm}')} | {v(f'CleanLatUnd{mm}')} -> {v(f'CleanLatCat{mm}')} | "
                 f"{v(f'CleanRedCalls{mm}')}/{v(f'CleanRedTokIn{mm}')}/{v(f'CleanRedTokOut{mm}')}/{v(f'CleanRedLat{mm}')} |")
    L += ["", "## Repetition stability (clean): % of pairs with identical outcome in all repetitions", "",
          "| Planner | Und utility | Und security | CATSEC utility | CATSEC security |", "|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        L.append(f"| {name} | {v(f'CleanRepStableUtilUnd{mm}')} | {v(f'CleanRepStableSecUnd{mm}')} | "
                 f"{v(f'CleanRepStableUtilCat{mm}')} | {v(f'CleanRepStableSecCat{mm}')} |")
    L += ["", "## Original (contaminated-prompt) runs vs clean runs", "",
          "| Planner | Und util orig -> clean | CATSEC util orig -> clean (delta pp) | Und ASR orig -> clean | CATSEC ASR orig -> clean | CATSEC tasks gained / lost |",
          "|---|---|---|---|---|---|"]
    for model, (mm, name, *_r) in MODELS.items():
        o = set(g(orig.get(model), "arms", "cat", "solved_list") or [])
        c = g(clean.get(model), "arms", "cat", "solved_list")
        gl = "--" if c is None else (f"+{sorted(set(c) - o, key=UT.index)} / -{sorted(o - set(c), key=UT.index)}")
        L.append(f"| {name} | {v(f'OrigUtilUnd{mm}')} -> {v(f'CleanUtilUnd{mm}')} | {v(f'OrigUtilCat{mm}')} -> "
                 f"{v(f'CleanUtilCat{mm}')} ({v(f'CmpDiffUtilCat{mm}')}) | {v(f'OrigASRUnd{mm}')} -> {v(f'CleanASRUnd{mm}')} | "
                 f"{v(f'OrigASRCat{mm}')} -> {v(f'CleanASRCat{mm}')} | {gl} |")
    L += ["", f"Original-prompt Gemini without ut12: {v('OrigUtilCatGeminiNoUtTwelve')}% "
          f"({v('OrigUtilCatGeminiNoUtTwelveK')}/{v('OrigUtilCatGeminiN')}).", "",
          "Macros: see results_macros.tex (\\Orig* = committed runs, \\Clean*/\\Benign*/\\Kfold*/\\Cmp* = rerun; "
          "'\\mbox{--}' = not available).", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", nargs="?", help="catsec_clean_rerun_<date>.tar.gz or an extracted dir")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--orig-logs", default=str(DEFAULT_ORIG_LOGS),
                    help="committed original runs (IEEE_Transactions/data/logs)")
    ap.add_argument("--original-only", action="store_true",
                    help="no bundle: \\Orig* macros from the committed runs, the rest '--'")
    ap.add_argument("--force", action="store_true",
                    help="write outputs despite FATAL verification problems (marked in every file)")
    args = ap.parse_args(argv)
    if not args.original_only and not args.bundle:
        ap.error("give a bundle, or --original-only")

    issues = {"fatal": [], "warn": []}
    out = Path(args.out).resolve()
    tmp = None
    prov: dict = {}
    data: dict = {}
    try:
        good, line = local_protocol_intact()
        if not good:
            issues["fatal"].append(f"this checkout's frozen protocol is broken: {line}")
        source = "(none: --original-only)"
        if not args.original_only:
            b = Path(args.bundle).resolve()
            source = str(b)
            if b.is_file():
                tmp = Path(tempfile.mkdtemp(prefix="catsec_ingest_"))
                root = safe_extract(b, tmp)
            elif b.is_dir():
                root = find_runs_root(b)
            else:
                raise Abort(f"no such bundle: {b}")
            data, prov = load_bundle(root, issues)
            if not data:
                issues["fatal"].append("the bundle contains no model folders")
        fake = bool(prov.get("fake"))
        if fake and (any(out.glob("bare_jrnl*.tex")) or out.name == "paper"):
            raise Abort("FAKE synthetic bundle: refusing to write into the paper directory")
        if issues["fatal"] and not args.force:
            for x in issues["fatal"]:
                print("FATAL:", x)
            for x in issues["warn"]:
                print("warning:", x)
            print("INGEST ABORTED: nothing written (use --force only to inspect a broken bundle)")
            return 2

        orig_recs = load_original(Path(args.orig_logs))
        orig = {m: injected_stats(r) for m, r in orig_recs.items()}
        clean = {m: injected_stats(f["clean_main"]) for m, f in data.items() if f.get("clean_main")}
        benign = {m: benign_stats(f["clean_benign"]) for m, f in data.items() if f.get("clean_benign")}
        kfold = {m: kfold_stats(f) for m, f in data.items() if any(k.startswith("kfold_") for k in f)}
        for m, k in kfold.items():
            if not k["complete"]:
                issues["warn"].append(f"{m}: k-fold incomplete (folds {k['folds_present']}); pooled "
                                      "k-fold macros cover only these folds")
        for m in MODELS:
            if m in data and m not in clean:
                issues["warn"].append(f"{m}: no clean_main results -> Clean macros '--'")
        for m, s in clean.items():
            for a in ("und", "cat"):
                n = g(s, "arms", a, "trials")
                if n is not None and n != 144 * len(s["reps"]):
                    issues["warn"].append(f"{m} clean_main {a}: {n} trials, expected 144 x {len(s['reps'])}")
            if len(s["reps"]) != 3:
                issues["warn"].append(f"{m} clean_main: {len(s['reps'])} repetition(s), protocol says 3")

        M = Macros()
        add_injected_macros(M, "Orig", orig, "original prompt")
        g_rec = [r for r in orig_recs["gemini"] if arm_of(r) == "cat"]
        k12 = sum(bool(r["utility"]) for r in g_rec if r["user_task"] != "user_task_12")
        M.add("OrigUtilCatGeminiNoUtTwelve", pct(k12 / len(g_rec)), "Gemini CATSEC utility without ut12, /144")
        M.add("OrigUtilCatGeminiNoUtTwelveK", k12)
        add_injected_macros(M, "Clean", clean, "clean prompt")
        for model, (mm, name, *_r) in MODELS.items():
            for a, aa in ARMS.items():
                w = g(benign.get(model), a, "util")
                M.add(f"BenignUtil{aa}{mm}", pct(g(w, "p")), f"benign (no injection) {name} {ARM_NAMES[a]}")
                M.add(f"BenignUtil{aa}{mm}Lo", pct(g(w, "lo")))
                M.add(f"BenignUtil{aa}{mm}Hi", pct(g(w, "hi")))
                M.add(f"BenignUtil{aa}{mm}K", g(w, "k"))
                M.add(f"BenignUtil{aa}{mm}N", g(w, "n"))
            k = kfold.get(model)
            for met in ("Util", "ASR"):
                w = g(k, met.lower())
                M.add(f"Kfold{met}Cat{mm}", pct(g(w, "p")), f"4-fold {name} CATSEC {met}")
                M.add(f"Kfold{met}Cat{mm}Lo", pct(g(w, "lo")))
                M.add(f"Kfold{met}Cat{mm}Hi", pct(g(w, "hi")))
                M.add(f"Kfold{met}Cat{mm}K", g(w, "k"))
                M.add(f"Kfold{met}Cat{mm}N", g(w, "n"))
            M.add(f"KfoldTasksSolved{mm}", g(k, "tasks_solved"))
            for a, aa in ARMS.items():
                for met, key in (("Util", "util"), ("ASR", "asr")):
                    o, c = g(orig.get(model), "arms", a, key, "p"), g(clean.get(model), "arms", a, key, "p")
                    M.add(f"CmpDiff{met}{aa}{mm}", spct(c - o) if None not in (o, c) else None,
                          "clean minus original, pp")
        bu = [g(b, a, "util", "p") for b in benign.values() for a in ("und", "cat")]
        bu_c = [g(b, "cat", "util", "p") for b in benign.values()]
        bu_u = [g(b, "und", "util", "p") for b in benign.values()]
        M.add("BenignUtilCatMin", pct(min(bu_c)) if bu_c else None)
        M.add("BenignUtilCatMax", pct(max(bu_c)) if bu_c else None)
        M.add("BenignUtilUndMin", pct(min(bu_u)) if bu_u else None)
        M.add("BenignUtilUndMax", pct(max(bu_u)) if bu_u else None)
        ku = [k["util"]["p"] for k in kfold.values() if k["util"]["p"] is not None]
        ka = [k["asr"]["k"] for k in kfold.values()]
        M.add("KfoldUtilCatMin", pct(min(ku)) if ku else None)
        M.add("KfoldUtilCatMax", pct(max(ku)) if ku else None)
        M.add("KfoldBreaches", sum(ka) if ka else None)
        M.add("KfoldTrials", sum(k["asr"]["n"] for k in kfold.values()) if kfold else None)
        M.add("CleanCommit", prov["commit"][:7] if prov.get("commit") else None)
        M.add("CleanRunDate", (prov.get("date") or "")[:10] or None)
        M.add("CleanDataFake", "FAKE" if fake else "real")
        del bu

        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        head = ["% Generated by code/CATSEC/tools/ingest_rerun.py -- DO NOT EDIT BY HAND.",
                f"% {stamp}; source: {source}",
                f"% PROVENANCE commit: {prov.get('commit')}  run date: {prov.get('date')}",
                r"% \Orig* = original-prompt committed runs (IEEE_Transactions/data/logs);",
                r"% \Clean*, \Benign*, \Kfold*, \Cmp* = clean-prompt rerun; \mbox{--} = not available."]
        if fake:
            head = ["% " + "!" * 70, "% FAKE SYNTHETIC DATA -- TEST OF THE INGESTER ONLY -- NEVER USE IN THE PAPER",
                    "% " + "!" * 70] + head
        if issues["fatal"]:
            head.append("% WRITTEN WITH --force DESPITE FATAL VERIFICATION PROBLEMS:")
            head += [f"%   {x}" for x in issues["fatal"]]
        for w in issues["warn"]:
            head.append(f"% warning: {w}")
        out.mkdir(parents=True, exist_ok=True)
        (out / "results_macros.tex").write_text(M.render("\n".join(head)), encoding="utf-8")
        tag = "% FAKE SYNTHETIC DATA -- NOT FOR THE PAPER\n" if fake else ""
        (out / "results_table_clean.tex").write_text(
            tag + "% body rows of the main results table, clean-prompt runs (needs results_macros.tex)\n"
            + table_rows(M, "Clean"), encoding="utf-8")
        (out / "results_table_orig.tex").write_text(
            "% body rows of the main results table, original-prompt runs (needs results_macros.tex)\n"
            + table_rows(M, "Orig"), encoding="utf-8")
        (out / "results_table_compare.tex").write_text(
            tag + "% columns: planner & und util orig & clean & CATSEC util orig & clean & und ASR orig & "
            "clean & CATSEC ASR orig & clean\n" + compare_rows(), encoding="utf-8")
        (out / "results_summary.md").write_text(
            md_summary(M, prov, issues, orig, clean, benign, kfold, fake, source), encoding="utf-8")
        (out / "results.json").write_text(json.dumps(
            {"fake": fake, "provenance": {k: v for k, v in prov.items() if k != "raw"}, "issues": issues,
             "original": orig, "clean": clean, "benign": benign, "kfold": kfold,
             "macros": M.defs}, indent=1, default=str), encoding="utf-8")
        for x in issues["warn"]:
            print("warning:", x)
        n_missing = sum(v is None for v in M.defs.values())
        print(f"{'FAKE ' if fake else ''}INGEST OK: {len(M.defs)} macros ({n_missing} '--'), "
              f"models with clean results: {sorted(clean) or 'none'} -> {out}")
        return 0
    except Abort as e:
        print("INGEST ABORTED:", e)
        return 2
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
