#!/usr/bin/env python3
"""
Build a FAKE, SYNTHETIC clean-rerun bundle for testing tools/ingest_rerun.py.

    python tests/fake_bundle.py OUT_DIR [--tar] [--models gpt,gemini,claude]

!!! The output is NOT experimental data. It relabels and perturbs the committed
ORIGINAL-prompt logs (IEEE_Transactions/data/logs) into the runs_clean/ layout
and injects the frozen clean-prompt hashes, so that the ingester's checks and
statistics can be exercised end to end. Every record carries
"FAKE_SYNTHETIC": true and PROVENANCE.txt starts with "FAKE"; the ingester
then marks all outputs FAKE and refuses to write into the paper directory.
Never cite, plot or copy anything produced from it.

Construction, per model (Gemma is left out by default to exercise the
missing-model path):
  clean_main    3 repetitions x 144 pairs x 2 arms. rep 0 = the original
                records; rep 1 flips ~10% of undefended outcomes (seeded);
                rep 2 additionally makes one solved CATSEC task fail in all
                nine contexts.
  clean_benign  one record per (task, arm), relabelled as no-injection.
  kfold_f       the CATSEC records of fold f's four tasks.
  smoke         two CATSEC records.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from catsec import prompts as P  # noqa: E402
from catsec.options import CatsecOptions, manifest_info  # noqa: E402

ORIG = {
    "gpt": ("gpt-4o-mini", "banking_final.jsonl", "gpt-4o-mini"),
    "gemini": ("gemini-2.5-flash", "gemini_full.jsonl", "gemini-2.5-flash"),
    "claude": ("claude-haiku-4-5", "banking_claude_final.jsonl", "claude-haiku-4-5-20251001"),
    "gemma": ("gemma-4-31b-it", "gemma_full.jsonl", "gemma-4-31b-it"),
}
FAKE_NOTE = "FAKE SYNTHETIC BUNDLE (tests/fake_bundle.py) -- NOT EXPERIMENTAL DATA, NEVER USE IN THE PAPER"


def _load(p: Path):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _info(variant="clean", fold=None):
    kw = {"prompt_variant": variant, "fold": fold} if variant == "kfold" else {}
    return manifest_info(CatsecOptions.from_profile("clean", **kw))


def _stamp(r, info, rep=0):
    r = copy.deepcopy(r)
    r["FAKE_SYNTHETIC"] = True
    r["repetition"] = rep
    r["run_id"] = f"FAKE-{r.get('run_id')}"
    r["catsec_run"] = {"options": info["catsec_options"], "prompt_sha256": info["prompt_sha256"]}
    return r


def _write(d: Path, name: str, recs, info):
    with (d / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    (d / f"{name}.manifest.json").write_text(json.dumps(
        {"FAKE_SYNTHETIC": True, "note": FAKE_NOTE, **info}, indent=1, default=str))
    (d / f"{name}.log").write_text(FAKE_NOTE + "\n")
    (d / f"{name}.csv").write_text("# " + FAKE_NOTE + "\n")


def build(out: Path, models=("gpt", "gemini", "claude"), seed: int = 7,
          logs: Path = REPO / "IEEE_Transactions" / "data" / "logs") -> Path:
    root = out / "runs_clean"
    root.mkdir(parents=True, exist_ok=True)
    clean = _info()
    rng = random.Random(seed)
    for m in models:
        d_orig, fn, mid = ORIG[m]
        recs = _load(logs / d_orig / fn)
        for r in recs:
            r["model"] = mid
        d = root / m
        d.mkdir(exist_ok=True)
        main = [_stamp(r, clean, 0) for r in recs]
        rep1 = []
        for r in recs:
            x = _stamp(r, clean, 1)
            if x["defense"] == "no-defense" and rng.random() < 0.10:
                x["utility"] = not x["utility"]
                x["security_attack_succeeded"] = not x["security_attack_succeeded"]
            rep1.append(x)
        solved = sorted({r["user_task"] for r in recs if r["defense"] == "catsec" and r["utility"]})
        drop = solved[0]
        rep2 = []
        for x in rep1:
            y = copy.deepcopy(x)
            y["repetition"] = 2
            if y["defense"] == "catsec" and y["user_task"] == drop:
                y["utility"] = False
            rep2.append(y)
        _write(d, "clean_main", main + rep1 + rep2, clean)
        benign = []
        for r in recs:
            if r["injection_task"] == "injection_task_0":
                x = _stamp(r, clean, 0)
                x.update(condition="clean", injected=False, injection_task=None, attack=None,
                         security_attack_succeeded=None)
                benign.append(x)
        _write(d, "clean_benign", benign, clean)
        for f in range(P.N_FOLDS):
            info = _info("kfold", f)
            ts = set(P.fold_tasks(f))
            _write(d, f"kfold_{f}", [_stamp(r, info, 0) for r in recs
                                     if r["defense"] == "catsec" and r["user_task"] in ts], info)
        _write(d, "smoke", [_stamp(r, clean, 0) for r in recs if r["defense"] == "catsec"][:2], clean)
    commit = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"]).decode().strip()
    check = subprocess.run([sys.executable, "-I", str(REPO / "tools/protocol_hashes.py"), "--check"],
                           capture_output=True, text=True).stdout
    (root / "PROVENANCE.txt").write_text(
        f"{FAKE_NOTE}\ncommit: {commit}\nbranch: FAKE-synthetic\ndate:   2099-01-01T00:00:00+00:00\n{check}")
    (out / "README_FAKE.txt").write_text(FAKE_NOTE + "\n\n" + __doc__)
    return root


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--tar", action="store_true", help="also write catsec_clean_rerun_FAKE.tar.gz")
    ap.add_argument("--models", default="gpt,gemini,claude")
    a = ap.parse_args()
    out = Path(a.out)
    root = build(out, tuple(a.models.split(",")))
    if a.tar:
        t = out / "catsec_clean_rerun_FAKE.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            tf.add(root, arcname="runs_clean")
        print("wrote", t)
    print("wrote", root, "--", FAKE_NOTE)


if __name__ == "__main__":
    main()
