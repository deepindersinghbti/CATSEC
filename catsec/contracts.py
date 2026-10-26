"""
Tool security contracts and AgentDojo tool wiring, shared by every runner.

Moved here verbatim from the four per-model runners (run_catsec_openai.py,
run_catsec_claude.py, run_catsec_gemini.py, run_catsec_gemma.py), where these
definitions were AST-identical copies. The replay experiments (E1-E7) import
them from here too, so the runners and the replays cannot drift apart.
"""
from __future__ import annotations

import re

from .kernel import ToolSpec, TRUSTED, DERIVED, UNTRUSTED


class ToolCallError(Exception):
    """Raised when a tool rejects its arguments. Never swallowed."""


SENSITIVE_CONTRACTS = {
    "send_money":                   (("recipient",), DERIVED),
    "schedule_transaction":         (("recipient",), DERIVED),
    "update_scheduled_transaction": (("recipient",), DERIVED),
    "update_password":              (("password",),  TRUSTED),
    "update_user_info":             (("street", "city"), DERIVED),
}


READ_TOOLS = [
    "get_iban", "get_balance", "get_most_recent_transactions",
    "get_scheduled_transactions", "read_file", "get_user_info",
]


def build_toolspecs(runtime, env, ablation: str = "none"):
    from agentdojo.agent_pipeline.agent_pipeline import tool_result_to_str
    specs = {}
    failures = []

    def make_impl(fname):
        def impl(state, **kwargs):
            result, error = runtime.run_function(env, fname, kwargs)
            if not error:
                try:
                    result = tool_result_to_str(result)
                except Exception:
                    pass
            if error:
                failures.append((fname, dict(kwargs), str(error)))
                raise ToolCallError(f"{fname}: {error}")
            return result
        return impl

    for fname in list(getattr(runtime, "functions", {}) or {}):
        crit, maxp = SENSITIVE_CONTRACTS.get(fname, (None, None))
        if crit is not None:
            if ablation == "no_provenance":
                maxp = UNTRUSTED
            specs[fname] = ToolSpec(
                fname, make_impl(fname), sensitive=True,
                critical_args=crit, max_prov=maxp, output_prov=UNTRUSTED)
        else:
            specs[fname] = ToolSpec(
                fname, make_impl(fname), sensitive=False, output_prov=UNTRUSTED)
    return specs, failures


def tool_descriptions(runtime) -> str:
    lines = []
    for fname, f in (getattr(runtime, "functions", {}) or {}).items():
        try:
            mf = f.parameters.model_fields
            req = [k for k, v in mf.items() if v.is_required()]
            opt = [k for k, v in mf.items() if not v.is_required()]
        except Exception:
            req, opt = [], []
        parts = ", ".join(req)
        if opt:
            parts += (", " if parts else "") + ", ".join(f"[{o}]" for o in opt)
        lines.append(f"  {fname}({parts})"
                     + (f"   REQUIRED: {', '.join(req)}" if req else ""))
    return "\n".join(lines)


def designated_sources(query: str) -> set:
    """The read_file sources a user request designates by naming a file.

    Same lexical rule the runners and replays have always used (kept
    byte-for-byte so logged runs replay identically): any token that looks
    like ``name.ext`` in the USER'S OWN text designates ``read_file(name.ext)``.
    Known limitation (paper, review): it also matches numbers such as "12.00"
    or "19.5"; those never name a file that a plan reads.
    """
    named = set(re.findall(r"[\w\-./]+\.\w{1,5}", query or ""))
    return {f"read_file({n})" for n in named}
