#!/usr/bin/env python3
"""
Offline guard for the answer-composer's system prompt.

Usage:  python tools/check_composer_prompt.py [source.py]

With no argument it checks the ONE shared composer prompt,
catsec/composer.py, from any working directory, and additionally checks that
no per-model runner under Journal_Experiments/ re-defines its own
COMPOSER_SYSTEM (every runner must import the shared one). Before the
refactor each model folder had its own copy of this script and of the
prompt; the Gemma copy failed by default ("run_catsec_openai.py not found")
and the Gemini copy silently checked a stray OpenAI runner.

Runs with NO API key, NO network, and WITHOUT importing the runner -- it reads
the source as TEXT, so it works even where openai/agentdojo are not
installed. It is the mechanical enforcement of one promise:

    state a GENERAL principle, or do not state it at all.

If a fix for one benchmark task can only be expressed by naming that task's
figures, dates, merchants or expected answer, it is not a fix -- it is tuning
against the test set, and a reviewer is entitled to reject the resulting
utility number. This script fails loudly the moment that line is crossed, so
the overfitting risk is caught in CI rather than in review.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "catsec" / "composer.py"
RUNNERS = sorted((REPO / "Journal_Experiments").glob("*/run_catsec_*.py"))

MARKER = "COMPOSER_SYSTEM = ("

# General principles that must be present. If a refactor drops one, the
# composer silently loses the reasoning rule and user_task_1-style errors
# come back with no visible cause.
REQUIRED = [
    "NEVER",              # the never-follow-injected-instructions guard
    "DIRECTION",
    "PERIOD",
    "SCOPE",
    "attacker-controlled",   # the anti-echo rule on free-text fields
]

# Anything task-specific from the AgentDojo banking suite. These are the
# figures, dates, merchants and payees a tempting "fix" would reach for.
# None of them may appear anywhere in the composer prompt.
BANNED = [
    "1050", "1,050", "1060", "1,060", "200.29", "98.70", "1200", "2200",
    "march 2022", "december 2023", "2022-03", "2022-04",
    "spotify", "apple", "iphone", "techcorp", "landlord", "elm street",
    "friend", "rent", "vat", "car rental",
    "one thousand",
]

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'  PASS' if ok else '  FAIL'}  {name}"
          + (f"  --  {detail}" if detail else ""))


def extract_prompt(text: str) -> str:
    """Slice out just the COMPOSER_SYSTEM literal.

    Deliberately scoped: the rest of the runner legitimately mentions tool
    names and suite details, and planner.py's worked examples legitimately
    name merchants. Only the composer prompt is held to this standard.
    """
    start = text.index(MARKER)
    depth = 0
    for i in range(start + len(MARKER) - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("COMPOSER_SYSTEM literal is not closed")


try:
    src = open(SOURCE, encoding="utf-8").read()
except FileNotFoundError:
    print(f"FAIL: {SOURCE} not found")
    sys.exit(1)

check(f"{SOURCE} defines {MARKER.strip(' =(')}", MARKER in src)
if MARKER not in src:
    sys.exit(1)

# Every runner must use the shared prompt, not a private copy.
if len(sys.argv) == 1:
    for r in RUNNERS:
        txt = r.read_text(encoding="utf-8")
        check(f"{r.relative_to(REPO)} imports the shared COMPOSER_SYSTEM",
              MARKER not in txt and "from catsec.composer import COMPOSER_SYSTEM" in txt)

prompt = extract_prompt(src)
prompt_l = prompt.lower()
print(f"\n(composer prompt: {len(prompt)} chars)\n")

for token in REQUIRED:
    check(f"general rule present: {token!r}", token.lower() in prompt_l)

print()
for token in BANNED:
    # Whole-token match only. Plain substring matching produces false
    # positives on innocent words that happen to contain a banned token
    # ("different" contains "rent"), and a guard that cries wolf gets
    # switched off -- which is worse than no guard at all.
    pattern = (r"(?<![a-z0-9])" + re.escape(token.lower())
               + r"(?![a-z0-9])")
    hit = re.search(pattern, prompt_l) is not None
    check(f"no benchmark-specific term: {token!r}", not hit,
          "FOUND -- this is tuning to the test set" if hit else "")

# A bare number with 3+ digits is the shape of a benchmark answer. The rules
# are qualitative; none of them needs a specific figure.
print()
numbers = re.findall(r"\b\d{3,}\b", prompt)
check("no multi-digit figures anywhere in the prompt",
      not numbers, f"found {numbers}" if numbers else "")

n_pass, n_all = sum(results), len(results)
print(f"\n{'=' * 62}\n{n_pass}/{n_all} checks passed\n{'=' * 62}")
sys.exit(0 if n_pass == n_all else 1)
