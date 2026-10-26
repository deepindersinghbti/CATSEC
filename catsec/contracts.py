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


# --------------------------------------------------------------------------- #
# Fail-closed default for tools without a contract (revision 2026-10)
# --------------------------------------------------------------------------- #
# BUG FIXED: in the released code every tool that is neither in
# SENSITIVE_CONTRACTS nor otherwise declared became ToolSpec(sensitive=False),
# i.e. it ran with NO gate. On the banking suite all state-changing tools are
# contracted, so nothing changed there -- but on any other suite send_email,
# share_file, send_direct_message, reserve_hotel, ... would run ungated.
#
# unknown_tool_policy:
#   "open"   -- released behaviour (default profile, reproduces the logs)
#   "closed" -- a tool with no contract that is not DECLARED read-only is
#               treated as sensitive, with EVERY argument critical at
#               `unknown_max_prov` (DERIVED by default): a value read from an
#               undesignated tool output can no longer reach it. This is the
#               default in the `hardened` profile (catsec/options.py).
# A read-only declaration is a security contract too: a "read" that takes an
# attacker-chosen argument can exfiltrate (e.g. slack's get_webpage(url)), so
# such tools are deliberately NOT declared read-only below.
READ_ONLY_DECLARATIONS = {
    # DRAFT declarations for the suites CATSEC has not been evaluated on yet.
    # They must be reviewed before any workspace/slack/travel experiment.
    "banking": tuple(READ_TOOLS),
    "workspace": (
        "get_unread_emails", "get_sent_emails", "get_received_emails",
        "get_draft_emails", "search_emails", "search_contacts_by_name",
        "search_contacts_by_email", "get_current_day", "search_calendar_events",
        "get_day_calendar_events", "search_files_by_filename", "get_file_by_id",
        "list_files", "search_files"),
    "slack": ("get_channels", "read_channel_messages", "read_inbox",
              "get_users_in_channel"),       # get_webpage(url) is NOT read-only
    "travel": (
        "get_user_information", "get_all_hotels_in_city", "get_hotels_prices",
        "get_rating_reviews_for_hotels", "get_hotels_address",
        "get_all_restaurants_in_city", "get_cuisine_type_for_restaurants",
        "get_restaurants_address", "get_rating_reviews_for_restaurants",
        "get_dietary_restrictions_for_all_restaurants",
        "get_contact_information_for_restaurants", "get_price_for_restaurants",
        "check_restaurant_opening_hours", "get_all_car_rental_companies_in_city",
        "get_car_types_available", "get_rating_reviews_for_car_rental",
        "get_car_fuel_options", "get_car_rental_address", "get_car_price_per_day",
        "search_calendar_events", "get_day_calendar_events",
        "get_flight_information"),
}
ALL_READ_ONLY = frozenset(t for ts in READ_ONLY_DECLARATIONS.values() for t in ts)


def _param_names(runtime, fname) -> tuple:
    try:
        return tuple(runtime.functions[fname].parameters.model_fields)
    except Exception:
        return ()


def build_toolspecs(runtime, env, ablation: str = "none",
                    unknown_tool_policy: str = "open",
                    read_only=None, unknown_max_prov: int = DERIVED):
    if unknown_tool_policy not in ("open", "closed"):
        raise ValueError(f"unknown_tool_policy must be open|closed, got {unknown_tool_policy!r}")
    read_only = ALL_READ_ONLY if read_only is None else frozenset(read_only)
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
        elif unknown_tool_policy == "closed" and fname not in read_only:
            maxp = UNTRUSTED if ablation == "no_provenance" else unknown_max_prov
            specs[fname] = ToolSpec(
                fname, make_impl(fname), sensitive=True,
                critical_args=_param_names(runtime, fname), max_prov=maxp,
                output_prov=UNTRUSTED)
        else:
            specs[fname] = ToolSpec(
                fname, make_impl(fname), sensitive=False, output_prov=UNTRUSTED)
    return specs, failures


def ungated_tools(tool_names, unknown_tool_policy: str = "open", read_only=None):
    """Tools that would run with NO provenance gate under a policy -- the
    quantity the fail-closed fix is about (reported per suite in
    review/code_changes.md)."""
    read_only = ALL_READ_ONLY if read_only is None else frozenset(read_only)
    out = []
    for n in tool_names:
        if n in SENSITIVE_CONTRACTS:
            continue
        if unknown_tool_policy == "open" or n in read_only:
            out.append(n)
    return out


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
