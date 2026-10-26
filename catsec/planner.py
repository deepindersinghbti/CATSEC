#!/usr/bin/env python3
"""
CATSEC PLANNER -- the morphism  P : UserIntent -> Ctrl.

PROVENANCE OF THIS FILE (stated plainly; see review/code_changes.md)
=================================================================
The released version of this file opened with "This file is a
RECONSTRUCTION. The original `planner.py` was not among the files supplied"
-- i.e. it was rebuilt during development from the runner's call signature
and the kernel's plan grammar. The logged runs were made in a private
repository (manifest git_commit 02bd5de / d55609b / 487bef8; Gemma: none),
so it cannot be verified from this artifact that exactly this prompt
produced the logged plans; the offline replays verify the kernel, not plan
generation. New runs log the SHA-256 of every prompt in their manifest.

PROMPT VARIANTS. The released planner prompt (`--prompt-variant original`,
catsec/prompts/original.txt, byte-identical) has worked examples taken from
the AgentDojo banking TEST tasks, and three repair heuristics tailored to
user_task_2/12/15. It is kept only so the published numbers reproduce. The
`clean` and `kfold` variants (catsec/prompts/, frozen in PROTOCOL.md) and
`repair_heuristics="generic"` (catsec/heuristics.py) are the decontaminated
protocol.

THE ONE INVARIANT THIS FILE EXISTS TO PROTECT
=============================================
`build_plan` is called with the USER'S TEXT ONLY. It never receives tool
output. `assert_no_tool_output` enforces that at runtime. If tool output ever
reached the planner, a Data -> Ctrl morphism would exist and Theorem K1 would
be false. Every design choice below is downstream of that one rule.

Note on the optional repair retry (`allow_repair=True`; every logged run used
it, via --plan-repair): the released retry shows the model ITS OWN previous
output and the validator/heuristic error -- NOT the user request, contrary to
what the paper text and the old docstring said. `repair_with_request=True`
also includes the user's request (catsec/prompts/repair_with_request_v1.txt).
Neither ever shows tool output, so no Data -> Ctrl path is created.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .kernel import validate_plan, PlanError
from . import prompts as _prompts
from .heuristics import GENERIC_HEURISTICS


class PlannerError(Exception):
    """Raised when no well-formed plan can be produced. Always fails closed."""


# --------------------------------------------------------------------------- #
# Structural guard: nothing tool-derived may reach the planner
# --------------------------------------------------------------------------- #
def assert_no_tool_output(messages) -> None:
    """Fail closed if any message carries tool output or a tool call.

    This is the runtime enforcement of the paper's central structural claim.
    It is deliberately paranoid: it inspects both dict-shaped and object-shaped
    messages, because AgentDojo's message type has changed shape across
    versions and a guard that silently stops guarding is worse than no guard.
    """
    for i, m in enumerate(messages or ()):
        role = m.get("role") if isinstance(
            m, dict) else getattr(m, "role", None)
        if role == "tool":
            raise PlannerError(
                f"planner guard: message {i} is a tool result; tool output must "
                f"never reach the planner (this would create Data -> Ctrl)")
        tool_calls = (m.get("tool_calls") if isinstance(m, dict)
                      else getattr(m, "tool_calls", None))
        if tool_calls:
            raise PlannerError(
                f"planner guard: message {i} carries tool_calls; the planner "
                f"must run before any tool executes")


# --------------------------------------------------------------------------- #
# The plan grammar shown to the model
# --------------------------------------------------------------------------- #
# The released prompt, byte-identical, now stored as a frozen file
# (catsec/prompts/original.txt; SHA-256 pinned in tests/test_frozen_artifacts.py).
SYSTEM_TEMPLATE = _prompts.read("original.txt")

REPAIR_TEMPLATE = _prompts.read("repair_original.txt")
REPAIR_TEMPLATE_WITH_REQUEST = _prompts.read("repair_with_request_v1.txt")

VALID_FIELDS = {"IBAN", "AMOUNT", "STREET", "CITY", "ID"}
VALID_OPS = {"add", "subtract", "multiply", "divide"}


# --------------------------------------------------------------------------- #
# Plan-level sanity check for the recurring-payment anti-pattern
# --------------------------------------------------------------------------- #
# LEGACY (repair_heuristics='all' only; tailored to AgentDojo banking
# user_task_2, a TEST task -- not used by the clean protocol).
# THE user_task_2 PROBLEM: SYSTEM_TEMPLATE already TELLS the model to check
# get_scheduled_transactions before extracting a fresh account number for a
# recurring/existing payment (Worked Example 3) -- but the model does not
# reliably follow that instruction every time, even at temperature 0.
#
# This check does NOT touch tool output or execute anything. It inspects only
# the PLAN (a Ctrl-level object, already fully built) and the ORIGINAL user
# text (already visible to the planner) -- so it cannot create a Data->Ctrl
# path and cannot affect Theorem K1. It is a second, structural opinion on
# whether the plan actually followed the instruction it was already given,
# not a new source of information about the world.
_RECURRING_KEYWORDS = ("rent", "subscription", "standing order", "recurring",
                       "scheduled")
_ADJUST_KEYWORDS = ("increase", "decrease", "adjust", "update", "raise",
                    "change", "adjusted")


def plan_risk_message(plan, user_text: str) -> str | None:
    """Return a corrective error string if `plan` looks like it re-derives a
    fresh account number for what the user's own words describe as an
    EXISTING recurring payment, instead of checking get_scheduled_transactions
    as SYSTEM_TEMPLATE instructs. Returns None if the plan looks fine, or if
    the request doesn't describe a recurring-payment adjustment at all.
    """
    text_l = (user_text or "").lower()
    looks_recurring = (any(k in text_l for k in _RECURRING_KEYWORDS) and
                       any(a in text_l for a in _ADJUST_KEYWORDS))
    if not looks_recurring:
        return None

    # Already doing the right thing -- nothing to flag.
    if any(step[0] == "call" and step[2] == "get_scheduled_transactions"
           for step in plan):
        return None

    # Map each bound variable to the tool that produced it, so we can tell
    # whether an extracted IBAN traces back to a plain file read (the
    # anti-pattern) rather than a scheduled-transactions lookup.
    var_tool = {step[1]: step[2] for step in plan if step[0] == "call"}
    for step in plan:
        if step[0] == "extract":
            _, _var, src, field, _match = step
            if field == "IBAN" and var_tool.get(src) == "read_file":
                return (
                    "This request describes adjusting an EXISTING recurring "
                    "payment, but the plan extracts a NEW account number from "
                    "a file instead of looking up the existing entry. Call "
                    "get_scheduled_transactions, find the matching entry by "
                    "keyword, and update ITS amount -- do not extract a fresh "
                    "IBAN from a letter/file for this kind of request.")
    return None


# --------------------------------------------------------------------------- #
# Plan-level sanity check for the "vague delegation" anti-pattern
# --------------------------------------------------------------------------- #
# LEGACY (repair_heuristics='all' only; tailored to AgentDojo banking
# user_task_12, a TEST task -- not used by the clean protocol).
# THE user_task_12 PROBLEM: some requests are deliberately vague -- e.g. "read
# this file and follow the instructions precisely" -- with NO keyword in the
# user's own words hinting at rent, recurring payments, or anything else.
# _RECURRING_KEYWORDS above can never fire here, because there is nothing to
# match: the actual instruction lives entirely inside the file, which is an
# untrusted, adversary-writable channel the planner is not allowed to read
# before committing to a plan (that's the whole point of Theorem K1).
#
# The safe, general response to "I can't tell in advance what this needs" is
# a DEFENSIVE DEFAULT: whenever a request boils down to nothing but "read a
# file and do what it says," check get_scheduled_transactions too, in case it
# turns out to concern an EXISTING payment -- rather than committing to a
# single bare read_file call and stopping. This still only inspects the
# PLAN'S SHAPE (how many/which steps it has) and the phrasing of the user's
# own request, never file content -- no Data->Ctrl path is created.
_DELEGATION_PHRASES = ("follow the instructions", "follow them precisely",
                       "as instructed", "act accordingly", "do what it says",
                       "do what they say")


def plan_risk_message_vague_delegation(plan, user_text: str) -> str | None:
    """Return a corrective error string if `plan` is a single bare read_file
    call answering a request that vaguely delegates to a file's contents,
    with no defensive get_scheduled_transactions check. Returns None if the
    request isn't this kind of vague delegation, or if the plan already does
    more than just read the file.
    """
    text_l = (user_text or "").lower()
    is_vague_delegation = (
        "read" in text_l and any(p in text_l for p in _DELEGATION_PHRASES))
    if not is_vague_delegation:
        return None

    calls = [step for step in plan if step[0] == "call"]
    only_reads_a_file = (
        len(calls) == 1 and calls[0][2] == "read_file" and
        not any(step[0] in ("extract", "compute") for step in plan))
    if not only_reads_a_file:
        return None

    return (
        "This request vaguely delegates to a file's contents ('follow the "
        "instructions'), so you cannot know in advance whether it concerns a "
        "NEW payment or an EXISTING one. The plan only reads the file and "
        "stops there. As a defensive default for this kind of vague request, "
        "ALSO call get_scheduled_transactions and check for a matching entry "
        "before finalizing -- do not stop at a single read_file call.")


# --------------------------------------------------------------------------- #
# Plan-level sanity check for fabricated (hallucinated) account numbers
# --------------------------------------------------------------------------- #
# LEGACY (repair_heuristics='all' only; tailored to AgentDojo banking
# user_task_15, a TEST task; its generic replacement is
# catsec/heuristics.py:plan_risk_message_unstated_identifier).
# THE user_task_15 PROBLEM: kernel.py treats every "let" literal as TRUSTED,
# by design and correctly so -- the plan is written before any attacker-
# controlled content is ever read, so the model cannot have copied a literal
# FROM an attacker. But TRUSTED-by-construction does not mean
# TRUE-by-construction: the model can still simply invent a plausible-looking
# account number out of nothing, and the kernel has no way to tell that
# apart from a value the user actually gave it directly.
#
# The distinguishing signal is available at the PLAN level, without touching
# any tool output: a security-critical argument's literal value should
# either (a) not look like an account number at all, or (b) if it does, that
# EXACT value should appear somewhere in the user's own original request
# (e.g. "my landlord's account is US133...212" -- legitimately TRUSTED,
# stated outright) rather than being conjured with no textual basis at all
# (e.g. inventing an IBAN for "refund my friend" when the user never states
# one -- the correct source for THAT is a lookup in transaction history).
_ACCOUNT_LIKE_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{13,32}$")


def plan_risk_message_fabricated_literal(plan, user_text: str,
                                         tools: dict) -> str | None:
    """Return a corrective error string if a security-critical argument
    (per each tool's own `critical_args`, e.g. `recipient`) is bound to a
    LITERAL that looks like an account number but does not appear anywhere
    in the user's own original text -- i.e. it was invented, not stated or
    looked up. Returns None if no such fabricated value is found.
    """
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
                continue  # doesn't look like an account number -- not our concern
            if candidate in text_compact:
                continue  # the user actually stated this value themselves
            return (
                f"The '{arg_name}' argument to {tool} is the literal "
                f"{value!r}, which looks like an account number, but it does "
                f"not appear anywhere in the user's own request. Do not "
                f"INVENT account numbers. If the user did not state this "
                f"value directly, look it up instead -- e.g. via "
                f"get_most_recent_transactions (to find who sent a past "
                f"payment you're refunding) or get_scheduled_transactions -- "
                f"rather than writing a literal you were not given.")
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


def _first_json_object(text: str) -> str:
    """Return the first balanced {...} block. Models occasionally prepend a
    stray word despite instructions; recovering from that is not a security
    relaxation -- the text still came from the planner call, which never saw
    tool output."""
    text = _strip_fences(text)
    start = text.find("{")
    if start < 0:
        raise PlannerError("planner output contained no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise PlannerError("planner output had an unterminated JSON object")


def _ref_from_json(raw: Any, where: str):
    """A reference is either a literal {"lit": v} or the name of a bound var."""
    if isinstance(raw, dict):
        # {"lit": v} -> a literal written by the planner (TRUSTED by definition)
        if "lit" in raw:
            return ("lit", raw["lit"])
        # {"ref": "name"} / {"var": "name"} -> a variable reference. The model
        # invents these wrappers regularly; they are unambiguously the same
        # thing as the bare-string form, so accepting them is a parser
        # tolerance, not a security relaxation -- the reference still has to
        # resolve against a previously BOUND variable in validate_plan(), and
        # the value it resolves to keeps whatever provenance it already had.
        for key in ("ref", "var"):
            if key in raw:
                name = raw[key]
                if not isinstance(name, str) or not name:
                    raise PlannerError(
                        f"{where}: {{'{key}': ...}} must name a variable, got {name!r}")
                return name
        raise PlannerError(
            f"{where}: object reference must be {{'lit': ...}} or "
            f"{{'ref': ...}}, got {raw!r}")
    if isinstance(raw, str):
        return raw
    # Bare scalars are a very common model slip; accept them as literals rather
    # than failing the whole plan, and record nothing special -- a literal
    # written by the planner is TRUSTED by definition either way.
    if isinstance(raw, (int, float, bool)) or raw is None:
        return ("lit", raw)
    raise PlannerError(f"{where}: unusable reference {raw!r}")


def _require(step: dict, key: str, idx: int, kind: str):
    """Strict key access.

    THIS IS THE FIX for the `KeyError: 'var'` crash. Every field is fetched
    through here, so a malformed plan raises PlannerError -- which the runner
    already catches and counts as a plan failure -- instead of escaping as a
    KeyError and killing the whole harness run. A crash and a refusal are
    completely different events and must never be conflated in the results.
    """
    if key not in step:
        raise PlannerError(
            f"step {idx} ({kind}): missing required field {key!r}; got keys "
            f"{sorted(step.keys())}")
    return step[key]


def _step_from_json(step: Any, idx: int):
    """Convert one JSON step into the tuple form kernel.py executes."""
    if not isinstance(step, dict):
        raise PlannerError(
            f"step {idx}: expected an object, got {type(step).__name__}")

    kind = step.get("kind") or step.get("op") or step.get("type")
    if not isinstance(kind, str):
        raise PlannerError(
            f"step {idx}: missing 'kind'; got keys {sorted(step.keys())}")
    kind = kind.strip().lower()

    if kind == "let":
        var = _require(step, "var", idx, kind)
        value = _require(step, "value", idx, kind)
        if not isinstance(var, str) or not var:
            raise PlannerError(
                f"step {idx} (let): 'var' must be a non-empty string")
        # The model sometimes wraps a let VALUE in the reference form
        # {"lit": v}. A let value is already a literal by definition, so the
        # wrapper is redundant -- but if it is not unwrapped here the raw dict
        # gets bound to the variable and is passed to the tool verbatim
        # (observed live as recipient={'lit': 'Spotify'}, which the tool then
        # rejects as a type error). Unwrap it.
        if isinstance(value, dict) and set(value) == {"lit"}:
            value = value["lit"]
        return ("let", var, value)

    if kind == "call":
        var = _require(step, "var", idx, kind)
        tool = _require(step, "tool", idx, kind)
        args = step.get("args", {})
        if not isinstance(var, str) or not var:
            raise PlannerError(
                f"step {idx} (call): 'var' must be a non-empty string")
        if not isinstance(tool, str) or not tool:
            raise PlannerError(
                f"step {idx} (call): 'tool' must be a non-empty string")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise PlannerError(f"step {idx} (call): 'args' must be an object")
        refs = {a: _ref_from_json(r, f"step {idx} (call {tool}, arg {a})")
                for a, r in args.items()}
        return ("call", var, tool, refs)

    if kind == "extract":
        var = _require(step, "var", idx, kind)
        src = step.get("src", step.get("from"))
        field = _require(step, "field", idx, kind)
        match = step.get("match", None)
        if not isinstance(var, str) or not var:
            raise PlannerError(
                f"step {idx} (extract): 'var' must be a non-empty string")
        if not isinstance(src, str) or not src:
            raise PlannerError(
                f"step {idx} (extract): 'src' must name a bound variable")
        if not isinstance(field, str) or field.upper() not in VALID_FIELDS:
            raise PlannerError(
                f"step {idx} (extract): field must be one of {sorted(VALID_FIELDS)}, got {field!r}")
        if match is not None and not isinstance(match, str):
            raise PlannerError(
                f"step {idx} (extract): 'match' must be a string or null")
        return ("extract", var, src, field.upper(), match)

    if kind == "compute":
        var = _require(step, "var", idx, kind)
        op = _require(step, "op", idx, kind)
        left = _require(step, "left", idx, kind)
        right = _require(step, "right", idx, kind)
        if not isinstance(var, str) or not var:
            raise PlannerError(
                f"step {idx} (compute): 'var' must be a non-empty string")
        if not isinstance(op, str) or op.strip().lower() not in VALID_OPS:
            raise PlannerError(
                f"step {idx} (compute): op must be one of {sorted(VALID_OPS)}, got {op!r}")
        return ("compute", var,
                op.strip().lower(),
                _ref_from_json(left, f"step {idx} (compute left)"),
                _ref_from_json(right, f"step {idx} (compute right)"))

    raise PlannerError(f"step {idx}: unknown step kind {kind!r}")


def parse_plan(raw_text: str):
    """Text -> executable plan (list of tuples). Raises PlannerError."""
    blob = _first_json_object(raw_text)
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError as e:
        raise PlannerError(f"planner output was not valid JSON: {e}") from e

    if isinstance(obj, list):
        steps = obj                      # tolerate a bare array
    elif isinstance(obj, dict):
        steps = obj.get("plan", obj.get("steps"))
    else:
        raise PlannerError(
            f"planner output was a {type(obj).__name__}, expected an object")

    if steps is None:
        raise PlannerError(
            f"planner output had no 'plan' key; keys were {sorted(obj.keys())}")
    if not isinstance(steps, list):
        raise PlannerError(
            f"'plan' must be a list, got {type(steps).__name__}")

    return [_step_from_json(s, i) for i, s in enumerate(steps)]


# --------------------------------------------------------------------------- #
# The single planning call
# --------------------------------------------------------------------------- #
REPAIR_HEURISTICS = ("all", "generic", "none")


def resolve_heuristics(repair_heuristics: str, prompt_variant: str) -> str:
    """'auto' -> the released three heuristics for the original prompt, the
    generic one for the clean/kfold protocol."""
    if repair_heuristics == "auto":
        return "all" if prompt_variant == "original" else "generic"
    if repair_heuristics not in REPAIR_HEURISTICS:
        raise ValueError(f"repair_heuristics must be auto|all|generic|none, "
                         f"got {repair_heuristics!r}")
    return repair_heuristics


def _risk_message(plan, user_text, tools, mode):
    if mode == "all":                       # released behaviour, unchanged
        risk = plan_risk_message(plan, user_text)
        if risk is None:
            risk = plan_risk_message_vague_delegation(plan, user_text)
        if risk is None:
            risk = plan_risk_message_fabricated_literal(plan, user_text, tools)
        return risk
    if mode == "generic":
        for h in GENERIC_HEURISTICS:
            risk = h(plan, user_text, tools)
            if risk is not None:
                return risk
    return None


def build_plan(llm: Callable[[str, str], str],
               user_text: str,
               tool_descriptions: str,
               tools: dict,
               allow_repair: bool = False,
               max_repair_rounds: int = 2,
               *,
               prompt_variant: str = "original",
               fold: int | None = None,
               repair_heuristics: str = "all",
               repair_with_request: bool = False,
               system_template: str | None = None):
    """P : UserIntent -> Ctrl.

    `llm(system, user)` -> str is supplied by the runner and is the ONLY model
    call made here. It is given the plan grammar and the user's raw text, and
    nothing else. No tools are declared on it, so it is structurally incapable
    of calling anything.

    `allow_repair` enables up to `max_repair_rounds` retries, each showing the
    model its own PREVIOUS output and the error it produced -- never tool
    output, so this never creates a Data -> Ctrl path. More than one round
    matters in practice: fixing one schema violation (e.g. an unbound ref in
    arg A) can surface a DIFFERENT violation on the next call (arg B).

    Revision options (keyword-only; the defaults reproduce the logged runs):
      prompt_variant      "original" | "clean" | "kfold" (catsec/prompts)
      fold                0..3, required for "kfold"
      repair_heuristics   "all" (released, task-tailored) | "generic" |
                          "none" | "auto" (all for original, generic otherwise)
      repair_with_request include the user's request in each repair prompt
                          (what the paper describes; the released code did not)
      system_template     explicit template override (tests)
    """
    template = (system_template if system_template is not None
                else _prompts.system_template(prompt_variant, fold))
    heur = resolve_heuristics(repair_heuristics, prompt_variant)
    repair_tmpl = REPAIR_TEMPLATE_WITH_REQUEST if repair_with_request else REPAIR_TEMPLATE
    system = template.format(tools=tool_descriptions)
    raw = llm(system, user_text or "")

    last_error = None
    last_valid_plan = None
    for attempt in range(1 + (max_repair_rounds if allow_repair else 0)):
        if attempt > 0:
            fields = dict(error=str(last_error), previous=(raw or "")[:4000])
            if repair_with_request:
                fields["request"] = user_text or ""
            repair_user = repair_tmpl.format(**fields)
            raw = llm(system, repair_user)
        try:
            plan = parse_plan(raw)
            validate_plan(plan, tools)
            last_valid_plan = plan
            if allow_repair:
                # Only run this check when repair is enabled -- it exists to
                # feed a corrective retry.
                risk = _risk_message(plan, user_text, tools, heur)
                if risk is not None:
                    last_error = PlannerError(risk)
                    continue
            return plan
        except (PlannerError, PlanError) as e:
            last_error = e
    # SAFETY NET: if every repair attempt still trips a heuristic, don't fail
    # the whole trial over it -- a syntactically valid plan that still has the
    # anti-pattern is no worse than before the check existed (it will simply
    # refuse at runtime). Only raise if we never got a single valid plan.
    if last_valid_plan is not None:
        return last_valid_plan
    raise last_error


def plan_signature(plan) -> str:
    """Stable string form of a plan, for the K1 comparison and for logging."""
    return json.dumps(plan, default=str, sort_keys=False)
