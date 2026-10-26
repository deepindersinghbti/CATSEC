#!/usr/bin/env python3
"""
SHA-256 of every frozen artefact of the clean planning protocol.

    python tools/protocol_hashes.py            print the table (markdown)
    python tools/protocol_hashes.py --check    verify PROTOCOL.md's table; exit 1 on drift

PROTOCOL.md is the pre-registration record: the clean prompt, the k-fold
prompts, the repair prompt and the generic heuristic were frozen BEFORE any
test run with them. Any edit to these files changes a hash here and fails
tests/test_protocol.py, so a silent post-hoc change cannot slip in.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from catsec import composer, prompts as P  # noqa: E402


def file_sha(rel: str) -> str:
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def table() -> dict:
    rows = {
        "catsec/prompts/clean_v1.txt": file_sha("catsec/prompts/clean_v1.txt"),
        "catsec/prompts/repair_with_request_v1.txt": file_sha("catsec/prompts/repair_with_request_v1.txt"),
        "catsec/heuristics.py": file_sha("catsec/heuristics.py"),
    }
    for f in range(P.N_FOLDS):
        rows[f"kfold fold {f} (rendered template)"] = P.sha256(P.system_template("kfold", f))
    rows["catsec/prompts/original.txt (logged runs, unchanged)"] = file_sha("catsec/prompts/original.txt")
    rows["catsec/prompts/repair_original.txt (logged runs, unchanged)"] = file_sha("catsec/prompts/repair_original.txt")
    rows["COMPOSER_SYSTEM (logged runs, unchanged)"] = P.sha256(composer.COMPOSER_SYSTEM)
    return rows


def main() -> int:
    rows = table()
    if "--check" not in sys.argv:
        print("| artefact | SHA-256 |\n|---|---|")
        for k, v in rows.items():
            print(f"| `{k}` | `{v}` |")
        return 0
    text = (REPO / "PROTOCOL.md").read_text(encoding="utf-8")
    found = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", text, re.MULTILINE))
    bad = 0
    for k, v in rows.items():
        ok = found.get(k) == v
        bad += not ok
        print(f"{'ok   ' if ok else 'DRIFT'} {k}")
    print("PROTOCOL FREEZE:", "intact" if bad == 0 else f"{bad} artefact(s) changed since freezing")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
