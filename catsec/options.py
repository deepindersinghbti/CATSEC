"""
Runner options added in the 2026-10 revision, shared by all four runners.

Every default reproduces the logged runs exactly. Profiles:

  paper     (default) the released behaviour: original prompt, the three
            released repair heuristics, repair prompt without the request,
            uncontracted tools ungated ("open").
  hardened  fail-closed: a tool with no contract that is not declared
            read-only is gated (catsec/contracts.py).
  clean     the decontaminated planning protocol of PROTOCOL.md: clean
            prompt, generic heuristic, repair prompt with the request, and the
            hardened fail-closed default.

Explicit flags override the profile. The amount parser is a kernel-level
switch (catsec.kernel.AMOUNT_PARSER); "strict" is the fixed default and was
verified not to change any committed replay.
"""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import composer, kernel
from . import prompts as P
from .planner import resolve_heuristics

REPO = Path(__file__).resolve().parents[1]

PROFILES = {
    "paper": dict(prompt_variant="original", repair_heuristics="all",
                  repair_with_request=False, unknown_tool_policy="open"),
    "hardened": dict(prompt_variant="original", repair_heuristics="all",
                     repair_with_request=False, unknown_tool_policy="closed"),
    "clean": dict(prompt_variant="clean", repair_heuristics="generic",
                  repair_with_request=True, unknown_tool_policy="closed"),
}


@dataclass
class CatsecOptions:
    profile: str = "paper"
    prompt_variant: str = "original"
    fold: int | None = None
    repair_heuristics: str = "all"
    repair_with_request: bool = False
    unknown_tool_policy: str = "open"
    amount_parser: str = "strict"

    @classmethod
    def from_profile(cls, profile: str = "paper", **overrides):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; choose from {sorted(PROFILES)}")
        kw = dict(PROFILES[profile])
        kw.update({k: v for k, v in overrides.items() if v is not None})
        opts = cls(profile=profile, **kw)
        opts.validate()
        return opts

    def validate(self):
        if self.prompt_variant not in P.VARIANTS:
            raise ValueError(f"prompt_variant must be one of {P.VARIANTS}")
        if (self.prompt_variant == "kfold") != (self.fold is not None):
            raise ValueError("--fold 0..3 is required with, and only with, --prompt-variant kfold")
        resolve_heuristics(self.repair_heuristics, self.prompt_variant)
        if self.unknown_tool_policy not in ("open", "closed"):
            raise ValueError("unknown_tool_policy must be open|closed")
        if self.amount_parser not in ("strict", "legacy"):
            raise ValueError("amount_parser must be strict|legacy")

    def apply_kernel(self):
        kernel.AMOUNT_PARSER = self.amount_parser

    def planner_kwargs(self) -> dict:
        return dict(prompt_variant=self.prompt_variant, fold=self.fold,
                    repair_heuristics=self.repair_heuristics,
                    repair_with_request=self.repair_with_request)

    def allowed_user_tasks(self):
        """kfold: only the test fold's tasks may be run with fold f's prompt."""
        return P.fold_tasks(self.fold) if self.prompt_variant == "kfold" else None


def add_cli_args(p):
    g = p.add_argument_group("CATSEC revision options (defaults reproduce the logged runs)")
    g.add_argument("--profile", default="paper", choices=sorted(PROFILES))
    g.add_argument("--prompt-variant", default=None, choices=list(P.VARIANTS),
                   help="planner prompt: original (logged runs; contains test "
                        "tasks), clean (PROTOCOL.md), kfold (needs --fold)")
    g.add_argument("--fold", type=int, default=None, choices=range(P.N_FOLDS))
    g.add_argument("--repair-heuristics", default=None,
                   choices=["auto", "all", "generic", "none"])
    g.add_argument("--repair-with-request", default=None, action="store_true",
                   help="include the user request in each plan-repair prompt")
    g.add_argument("--unknown-tool-policy", default=None, choices=["open", "closed"],
                   help="closed = gate every uncontracted, non-read-only tool")
    g.add_argument("--amount-parser", default=None, choices=["strict", "legacy"])
    return p


def from_args(args) -> CatsecOptions:
    opts = CatsecOptions.from_profile(
        getattr(args, "profile", "paper") or "paper",
        prompt_variant=getattr(args, "prompt_variant", None),
        fold=getattr(args, "fold", None),
        repair_heuristics=getattr(args, "repair_heuristics", None),
        repair_with_request=getattr(args, "repair_with_request", None),
        unknown_tool_policy=getattr(args, "unknown_tool_policy", None),
        amount_parser=getattr(args, "amount_parser", None))
    opts.apply_kernel()
    return opts


def _repo_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO,
                                      stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.call(["git", "diff", "--quiet", "HEAD", "--", "catsec"],
                                cwd=REPO, stderr=subprocess.DEVNULL) != 0
        return out + ("-dirty" if dirty else "")
    except Exception:
        return "not-a-git-repo"


def manifest_info(opts: CatsecOptions) -> dict:
    """What every manifest (and every record) must carry so a run can be tied
    to the exact prompts and policies that produced it."""
    tmpl = P.system_template(opts.prompt_variant, opts.fold)
    heur_src = (REPO / "catsec" / "heuristics.py").read_bytes()
    import hashlib
    return {
        "catsec_options": asdict(opts),
        "catsec_repo_commit": _repo_commit(),
        "prompt_sha256": {
            "planner_system_template": P.sha256(tmpl),
            "repair_template": P.sha256(P.repair_template(opts.repair_with_request)),
            "composer_system": P.sha256(composer.COMPOSER_SYSTEM),
            "generic_heuristics_py": hashlib.sha256(heur_src).hexdigest(),
        },
        "repair_heuristics_resolved": resolve_heuristics(opts.repair_heuristics,
                                                         opts.prompt_variant),
    }
