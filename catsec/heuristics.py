"""
Generic plan-level repair heuristic for the CLEAN protocol (frozen; see
PROTOCOL.md). It replaces the three task-tailored heuristics of the released
planner (the LEGACY functions in catsec/planner.py, written for three
specific benchmark tasks).

What it checks -- and why it is not tuned to any test task:
a security-critical argument (per each tool's OWN contract, `critical_args`)
bound to a LITERAL that looks like an identifier (an account-number-shaped
token) which does not occur in the user's own request. Such a literal can
only have been invented by the planner. The check reads the plan and the
user's text only, never tool output, so it adds no Data -> Ctrl path.

Unlike the released `plan_risk_message_fabricated_literal`, its corrective
message names no tool of any particular suite.
"""
from __future__ import annotations

import re

_ACCOUNT_LIKE_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{13,32}$")


def plan_risk_message_unstated_identifier(plan, user_text: str,
                                          tools: dict) -> str | None:
    text_compact = re.sub(r"\s+", "", user_text or "").upper()
    literal_of = {step[1]: step[2] for step in plan if step[0] == "let"}

    def literal_for_ref(ref):
        if isinstance(ref, tuple) and len(ref) == 2 and ref[0] == "lit":
            return ref[1]
        if isinstance(ref, str) and ref in literal_of:
            return literal_of[ref]
        return None

    for step in plan:
        if step[0] != "call":
            continue
        _, _var, tool, args = step
        spec = tools.get(tool)
        if spec is None or not getattr(spec, "critical_args", None):
            continue
        for arg_name in spec.critical_args:
            ref = args.get(arg_name)
            if ref is None:
                continue
            value = literal_for_ref(ref)
            if not isinstance(value, str):
                continue
            candidate = value.replace(" ", "").upper()
            if not _ACCOUNT_LIKE_RE.match(candidate):
                continue
            if candidate in text_compact:
                continue
            return (
                f"The '{arg_name}' argument to {tool} is the literal "
                f"{value!r}, which looks like an identifier, but it does not "
                f"appear in the user's request. Do not invent identifiers: "
                f"if the user did not state this value, retrieve it with one "
                f"of the available tools and extract it instead.")
    return None


GENERIC_HEURISTICS = (plan_risk_message_unstated_identifier,)
