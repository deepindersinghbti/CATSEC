"""
Frozen planner prompts and the prompt-variant protocol (see PROTOCOL.md).

Variants (selected with --prompt-variant in every runner):

  original  catsec/prompts/original.txt -- the prompt used for every logged
            run, byte-identical (SHA-256 pinned in tests). Its worked examples
            are AgentDojo banking TEST tasks (WE1~ut0, WE2=ut7 verbatim,
            WE3~ut2, WE4~ut14, WE5=ut12 verbatim incl. the ground-truth amount
            1200, WE6~ut15). Kept only so the published numbers stay
            reproducible; do not use it for new results.
  clean     catsec/prompts/clean_v1.txt -- frozen before any test run. Generic
            rules and worked examples from an unrelated application (an online
            shop's back office, with tools that exist in no AgentDojo suite).
            tools/check_contamination.py measures its overlap with every
            AgentDojo user and injection task.
  kfold     dev/test protocol over the 16 banking user tasks, in the four
            folds of KFOLD_FOLDS. To evaluate fold f the planner sees
            the clean prompt PLUS the original banking-derived worked examples
            whose source task is NOT in fold f (i.e. drawn only from the other,
            development folds). Run only fold f's tasks with prompt fold f; the
            union over the four folds covers all 16 tasks with no example ever
            derived from a task it is tested on.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent
VARIANTS = ("original", "clean", "kfold")
N_FOLDS = 4
BANKING_USER_TASKS = tuple(f"user_task_{i}" for i in range(16))

# Which AgentDojo banking user task each ORIGINAL worked example was derived
# from (review/code_reproduction_report.md, section 3.4).
ORIGINAL_EXAMPLE_SOURCES = {
    1: "user_task_0", 2: "user_task_7", 3: "user_task_2",
    4: "user_task_14", 5: "user_task_12", 6: "user_task_15",
}

_EX_RE = re.compile(r"^WORKED EXAMPLE (\d+) -- ", re.MULTILINE)
_FOOTER = "AVAILABLE TOOLS\n{tools}\n"
_CLEAN_DISCLAIMER = (
    "The worked examples below use ILLUSTRATIVE tools of an unrelated application\n"
    "(an online shop's back office). They show the plan grammar only: use ONLY the\n"
    "tools listed under AVAILABLE TOOLS at the end.\n")
_KFOLD_DISCLAIMER = (
    "Some worked examples below use ILLUSTRATIVE tools of an unrelated application\n"
    "(an online shop's back office); others use tools of this application. They\n"
    "show the plan grammar only: use ONLY the tools listed under the heading\n"
    "AVAILABLE TOOLS at the end.\n")


def read(name: str) -> str:
    """A frozen prompt file, exactly as stored (no newline translation)."""
    with open(PROMPT_DIR / name, encoding="utf-8", newline="") as fh:
        return fh.read()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_examples(template: str):
    """(header, [(number, block_text)], footer) of a prompt template."""
    starts = [m.start() for m in _EX_RE.finditer(template)]
    end = template.rindex("\nAVAILABLE TOOLS\n") + 1
    header = template[:starts[0]] if starts else template[:end]
    blocks = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        blocks.append((int(_EX_RE.match(template, s).group(1)), template[s:e]))
    return header, blocks, template[end:]


# Folds are grouped, not round-robin: tasks that share ground-truth argument
# values must sit in the same fold, otherwise an example derived from one
# carries the other's answer into its prompt. Couplings found by
# tools/check_contamination.py: ut2 & ut12 (same file, same answer 1200);
# ut3, ut4 & ut15 (same refund IBAN; ut4 & ut15 also refund 10.00). Values
# shared by >= 4 tasks (the environment's date 2022-04-01) are environment
# constants, not task labels. Each fold holds 4 tasks and withholds at least
# one original worked example.
KFOLD_FOLDS = {
    0: ("user_task_0", "user_task_2", "user_task_12", "user_task_13"),
    1: ("user_task_3", "user_task_4", "user_task_15", "user_task_5"),
    2: ("user_task_1", "user_task_7", "user_task_8", "user_task_10"),
    3: ("user_task_6", "user_task_9", "user_task_11", "user_task_14"),
}
_FOLD_OF = {t: f for f, ts in KFOLD_FOLDS.items() for t in ts}
assert sorted(_FOLD_OF) == sorted(BANKING_USER_TASKS)


def fold_of(task_id: str) -> int:
    return _FOLD_OF[task_id]


def fold_tasks(fold: int) -> list:
    return [t for t in BANKING_USER_TASKS if fold_of(t) == fold]


def _renumber(blocks):
    out = []
    for n, (_, text) in enumerate(blocks, 1):
        out.append(_EX_RE.sub(f"WORKED EXAMPLE {n} -- ", text, count=1))
    return "".join(out)


def kfold_template(fold: int) -> str:
    if fold not in range(N_FOLDS):
        raise ValueError(f"fold must be 0..{N_FOLDS - 1}, got {fold!r}")
    clean = read("clean_v1.txt")
    header, clean_blocks, footer = split_examples(clean)
    assert _CLEAN_DISCLAIMER in header
    header = header.replace(_CLEAN_DISCLAIMER, _KFOLD_DISCLAIMER)
    _, orig_blocks, _ = split_examples(read("original.txt"))
    dev = [(n, b) for n, b in orig_blocks
           if fold_of(ORIGINAL_EXAMPLE_SOURCES[n]) != fold]
    return header + _renumber(clean_blocks + dev) + footer


def kfold_example_sources(fold: int) -> list:
    """The banking tasks the fold-`fold` prompt's examples were derived from."""
    return [ORIGINAL_EXAMPLE_SOURCES[n] for n in sorted(ORIGINAL_EXAMPLE_SOURCES)
            if fold_of(ORIGINAL_EXAMPLE_SOURCES[n]) != fold]


def system_template(variant: str = "original", fold: int | None = None) -> str:
    """The planner system template (still containing the {tools} slot)."""
    if variant == "original":
        return read("original.txt")
    if variant == "clean":
        return read("clean_v1.txt")
    if variant == "kfold":
        if fold is None:
            raise ValueError("--prompt-variant kfold needs --fold 0..3")
        return kfold_template(fold)
    raise ValueError(f"unknown prompt variant {variant!r}; choose from {VARIANTS}")


def repair_template(with_request: bool = False) -> str:
    return read("repair_with_request_v1.txt" if with_request else "repair_original.txt")
