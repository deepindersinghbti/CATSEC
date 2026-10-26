#!/usr/bin/env python3
"""
CATSEC KERNEL -- a categorical control/data separation architecture.

THE CONSTRUCTION
================
Two objects, deliberately disconnected:

    Ctrl  -- the PLAN: which tools run, in what order, wired to which variables.
    Data  -- VALUES: everything a tool returns, each tagged with provenance.

The plan morphism
        P : UserIntent  ->  Ctrl
is evaluated BEFORE any element of Data exists in scope. The interpreter then
evaluates
        E : Ctrl x Data -> Data
deterministically.

THE GUARANTEE (Theorem K1, control-flow invariance)
---------------------------------------------------
There is no morphism  Data -> Ctrl  in this construction. The planner never
observes tool output; the interpreter never re-plans. Therefore for any two
data environments d, d' (e.g. one clean, one carrying an injected payload):

        Ctrl(P(intent), d)  =  Ctrl(P(intent), d')

i.e. the SEQUENCE OF TOOL CALLS IS IDENTICAL whether or not an injection is
present. Injection cannot add a call, remove a call, or reorder calls. This is
structural, not statistical: the influence channel is absent, so there is
nothing for an attacker to be clever about.

WHAT IT DOES *NOT* GUARANTEE (stated up front, honestly)
--------------------------------------------------------
Theorem K1 covers CONTROL flow. It does not make corrupted DATA correct. If the
plan legitimately says "pay the IBAN written in this file" and the adversary can
overwrite that IBAN field, the interpreter will faithfully pay the wrong party.
That residual is the obstruction identified empirically earlier in this project:
when a security-critical argument must be drawn from an adversary-writable
channel, no automatic mechanism can separate the legitimate value from the
injected one.

The kernel handles that residual EXPLICITLY rather than pretending it away, via
the provenance lattice below and a declared policy (REFUSE or CONFIRM). It never
silently proceeds.

PROVENANCE LATTICE
------------------
        TRUSTED  <  DERIVED  <  UNTRUSTED          (join = max)

    TRUSTED    literal written in the plan, or a value taken from the user's
               own instruction (out-of-band channel the adversary cannot write)
    DERIVED    obtained from a tool the user explicitly designated, via a
               STRUCTURED extractor (not free-form model interpretation)
    UNTRUSTED  everything else returned by a tool

Provenance joins on every operation: any operand at UNTRUSTED taints the result.
A security-critical argument is admissible only at or below the level its tool
declares.
"""
from __future__ import annotations
import re

from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Provenance lattice
# --------------------------------------------------------------------------- #
TRUSTED, DERIVED, UNTRUSTED = 0, 1, 2
PROV_NAME = {TRUSTED: "TRUSTED", DERIVED: "DERIVED", UNTRUSTED: "UNTRUSTED"}


@dataclass
class PVal:
    """A value together with its provenance level and an audit trail."""
    value: Any
    prov: int
    origin: str = ""

    def __repr__(self):
        return f"PVal({self.value!r}, {PROV_NAME[self.prov]}, {self.origin!r})"


def join(*ps: int) -> int:
    """Lattice join: the least upper bound. Taint dominates."""
    return max(ps) if ps else TRUSTED


# --------------------------------------------------------------------------- #
# Tool declarations
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    """Declares a tool's security contract.

    sensitive        : does invoking it cause an irreversible external effect?
    critical_args    : argument names whose provenance is load-bearing
    max_prov         : the highest provenance level admissible for those args
    output_prov      : provenance assigned to this tool's return value
    """
    name: str
    impl: Callable
    sensitive: bool = False
    critical_args: tuple = ()
    max_prov: int = TRUSTED
    output_prov: int = UNTRUSTED


# --------------------------------------------------------------------------- #
# Plan DSL -- the Ctrl object
# --------------------------------------------------------------------------- #
# A plan is a finite list of steps. Steps are total and side-effect-ordered.
# There is NO branch-on-data and NO loop-over-data instruction: that omission is
# what makes Theorem K1 hold. A plan cannot ask "what did the tool say?" and act
# differently -- only the interpreter's policy check can halt execution.
#
#   ("let",     var, literal)                         -> TRUSTED constant
#   ("call",    var, tool, {arg: ref})                -> invoke a tool
#   ("extract", var, src_var, field)                  -> structured field pull
#
# `ref` is either ("lit", value) or the name of a previously bound variable.

class PlanError(Exception):
    pass


class PolicyViolation(Exception):
    def __init__(self, tool, arg, prov, msg=""):
        self.tool, self.arg, self.prov = tool, arg, prov
        super().__init__(
            msg or f"{tool}.{arg} has provenance {PROV_NAME[prov]}")


ALLOWED_STEPS = {"let", "call", "extract", "compute"}

# Arithmetic admitted by the `compute` step. Every one of these is a pure
# Data -> Data map evaluated by trusted host code: none of them can add, remove,
# or reorder a tool call, so widening this set CANNOT weaken Theorem K1. The set
# is deliberately finite and closed -- there is no eval, no expression parser,
# and no way for a plan to introduce an operation the host does not implement.
COMPUTE_OPS = ("add", "subtract", "multiply", "divide")


def validate_plan(plan, tools: dict) -> None:
    """Static check before execution: only known steps, known tools, and
    variables referenced only after binding. Rejects malformed plans outright."""
    bound = set()
    for i, step in enumerate(plan):
        if not step or step[0] not in ALLOWED_STEPS:
            raise PlanError(f"step {i}: unknown instruction {step!r}")
        kind = step[0]
        if kind == "let":
            _, var, _ = step
            bound.add(var)
        elif kind == "extract":
            # 5-tuple: (extract, var, src, field, match) -- match is None for
            # the original single-field extractors, or a keyword string when
            # locating one record among several (e.g. "find the Rent entry").
            _, var, src, _field, _match = step
            if src not in bound:
                raise PlanError(f"step {i}: extract from unbound {src!r}")
            bound.add(var)
        elif kind == "compute":
            # ("compute", var, op, left_ref, right_ref) -- pure Data->Data
            # arithmetic performed by trusted host code, never by the model.
            # This does NOT touch control flow: it cannot add, remove, or
            # reorder a tool call, so Theorem K1 is unaffected by its
            # presence. Provenance of the result is the JOIN of its operands
            # (see combine_prov) -- an amount derived from an untrusted value
            # stays untrusted, exactly as extraction already behaves.
            _, var, op, left, right = step
            if op not in COMPUTE_OPS:
                raise PlanError(f"step {i}: unknown compute op {op!r}")
            for ref in (left, right):
                if isinstance(ref, tuple) and ref and ref[0] == "lit":
                    continue
                if ref not in bound:
                    raise PlanError(
                        f"step {i}: compute references unbound {ref!r}")
            bound.add(var)
        elif kind == "call":
            _, var, tool, args = step
            if tool not in tools:
                raise PlanError(f"step {i}: unknown tool {tool!r}")
            for a, ref in args.items():
                if isinstance(ref, tuple) and ref and ref[0] == "lit":
                    continue
                if ref not in bound:
                    raise PlanError(
                        f"step {i}: arg {a} references unbound {ref!r}")
            bound.add(var)


# --------------------------------------------------------------------------- #
# Structured extractors -- the ONLY way data becomes DERIVED
# --------------------------------------------------------------------------- #
# Extraction is performed by deterministic parsers, never by asking a model to
# "read this and tell me the account number". A model asked to interpret
# adversarial text is exactly the influence channel we removed.


_EXTRACTORS = {
    # account identifier, must carry a digit (rejects words like UNDERSPECIFIED)
    # Real tool output labels this field two different ways depending on the
    # source: bank statements say "IBAN:", but transaction/schedule records
    # (get_most_recent_transactions, get_scheduled_transactions) say
    # "recipient:" instead (see selftest.py's SCHEDULED fixture). The old
    # pattern only recognized "IBAN", so any account number carried under a
    # "recipient:" label silently failed to extract -- not because the source
    # was ambiguous or tampered, but because we were searching for the wrong
    # word. Widening the accepted LABEL (not the value shape, and not the
    # ambiguity rule) fixes that: the (?i:...) scopes case-insensitivity to
    # just the label, so the captured value's case is untouched.
    "IBAN": re.compile(
        r"\b(?i:IBAN|recipient)[:\s]+([A-Z]{2}(?=[0-9A-Z]*[0-9])[0-9A-Z]{11,30})\b"),
    # monetary amount: labelled totals AND prose forms like "increased by 100.00"
    "AMOUNT": re.compile(
        r"(?:Total|Amount|increased by|decreased by|by)[:\s]+([0-9]+(?:\.[0-9]{1,2})?)",
        re.IGNORECASE),
    # street line: a line containing a street-ish word followed by a number,
    # e.g. "Dalton Street 123" or "1234 Elm Street"
    "STREET": re.compile(
        r"^\s*((?:[0-9]+\s+)?[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)*\s+"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd|Drive|Dr)"
        r"(?:\s+[0-9]+)?)\s*$",
        re.MULTILINE),
    # city line: "New York, NY 10001" -> captures "New York"
    "CITY": re.compile(r"^\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*),\s*[A-Z]{2}\s+[0-9]{5}",
                       re.MULTILINE),
}


def extract_field_matched(text: str, field: str, match: str):
    """Find the record containing `match` (case-insensitive substring) and
    pull a named sub-field out of it. Real tool output for record lists (e.g.
    get_scheduled_transactions) is YAML-ish:

        - amount: 1100.0
          date: '2022-04-04'
          id: 7
          recipient: US122...
          subject: Rent

    Records are blocks starting at a line beginning with "- " and running
    until the next such line (or end of text).

    AMBIGUITY IS STILL A SECURITY EVENT: if zero or more than one record
    contains the match string, this returns None rather than guessing which
    one the caller meant. An attacker cannot win by injecting a second
    similarly-labelled record -- it just makes extraction fail closed.
    """
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    blocks, current = [], []
    for line in lines:
        if line.startswith("- ") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    hits = [b for b in blocks if match.lower() in b.lower()]
    if len(hits) != 1:
        return None
    record = hits[0]
    sub_rx = {
        "ID": re.compile(r"\bid:\s*([0-9]+)"),
        "AMOUNT": re.compile(r"\bamount:\s*([0-9]+(?:\.[0-9]{1,2})?)"),
    }.get(field.upper())
    if sub_rx is not None:
        m = sub_rx.search(record)
        return m.group(1) if m else None
    # Any other field (IBAN, STREET, CITY): no YAML-style sub-key for these,
    # so fall back to the same deterministic extractor used for whole-document
    # extraction, scoped to just the matched record's text. Still fails closed
    # on ambiguity (extract_field returns None if 0 or >1 hits), still no
    # model involved -- just widening WHERE the same regex is allowed to look.
    return extract_field(record, field.upper())


def extract_field(text: str, field: str):
    """Deterministic structured extraction. Returns None if absent or ambiguous.

    AMBIGUITY IS A SECURITY EVENT: if a field appears more than once with
    different values, the source has been tampered with, and we return None
    rather than guessing. This is what stops an appended 'IBAN: <attacker>'
    from silently winning.
    """
    rx = _EXTRACTORS.get(field.upper())
    if rx is None or not isinstance(text, str):
        return None
    hits = rx.findall(text)
    if not hits:
        return None
    uniq = sorted(set(hits))
    if len(uniq) > 1:
        return None          # tampering detected -> refuse to disambiguate
    return uniq[0]


# --------------------------------------------------------------------------- #
# The interpreter -- E : Ctrl x Data -> Data
# --------------------------------------------------------------------------- #
@dataclass
class ExecResult:
    executed: list = field(default_factory=list)   # (tool, plain_args)
    refused: list = field(default_factory=list)    # (tool, plain_args, reason)
    confirmations: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    control_trace: list = field(default_factory=list)  # tool names, in order


class Interpreter:
    """Deterministic executor. Never consults a language model.

    policy:
        "refuse"  -- a critical arg above its tool's max_prov aborts that call
        "confirm" -- the same, but recorded as requiring human confirmation
                     (still not executed autonomously)
    """

    def __init__(self, tools: dict[str, ToolSpec], designated_sources: set[str] | None = None,
                 policy: str = "refuse", verbose: bool = False):
        self.tools = tools
        self.designated = designated_sources or set()
        self.policy = policy
        self.verbose = verbose

    def _log(self, *a):
        if self.verbose:
            print("   ", *a)

    def _resolve(self, ref, env) -> PVal:
        if isinstance(ref, tuple) and ref and ref[0] == "lit":
            return PVal(ref[1], TRUSTED, "plan-literal")
        pv = env.get(ref)
        if pv is None:
            return PVal(None, UNTRUSTED, f"unbound:{ref}")
        return pv

    def run(self, plan, state) -> ExecResult:
        validate_plan(plan, self.tools)
        env: dict[str, PVal] = {}
        res = ExecResult(env=env)

        for step in plan:
            kind = step[0]

            if kind == "let":
                _, var, lit = step
                env[var] = PVal(lit, TRUSTED, "plan-literal")
                self._log(f"let {var} = {lit!r} [TRUSTED]")

            elif kind == "extract":
                _, var, src, fieldname, match = step
                src_pv = self._resolve(src, env)
                if match is not None:
                    got = extract_field_matched(src_pv.value, fieldname, match)
                    tag = f"{fieldname}~{match!r}"
                else:
                    got = extract_field(src_pv.value, fieldname)
                    tag = fieldname
                if got is None:
                    # absent OR ambiguous -> hard fail, do not guess
                    env[var] = PVal(None, UNTRUSTED,
                                    f"extract-failed:{tag}@{src}")
                    self._log(
                        f"extract {var} <- {tag} FAILED (absent/ambiguous)")
                else:
                    # A structured pull from a DESIGNATED source is DERIVED;
                    # from anything else it stays UNTRUSTED.
                    lvl = DERIVED if src_pv.origin in self.designated else max(
                        src_pv.prov, UNTRUSTED)
                    if src_pv.prov == TRUSTED:
                        lvl = TRUSTED
                    env[var] = PVal(got, lvl, f"extract:{tag}@{src}")
                    self._log(f"extract {var} = {got!r} [{PROV_NAME[lvl]}]")

            elif kind == "compute":
                _, var, op, left_ref, right_ref = step
                lpv, rpv = self._resolve(
                    left_ref, env), self._resolve(right_ref, env)
                try:
                    lval, rval = float(lpv.value), float(rpv.value)
                except (TypeError, ValueError):
                    env[var] = PVal(None, UNTRUSTED, f"compute-failed:{op}")
                    self._log(
                        f"compute {var} <- {op} FAILED (non-numeric operand)")
                    continue
                if op == "add":
                    result = lval + rval
                elif op == "subtract":
                    result = lval - rval
                elif op == "multiply":
                    result = lval * rval
                else:                                    # divide
                    if rval == 0:
                        # Fail closed, exactly as for a non-numeric operand. An
                        # attacker able to influence a divisor must not be able
                        # to crash the interpreter or produce an infinity that
                        # later flows into a sensitive argument.
                        env[var] = PVal(None, UNTRUSTED,
                                        f"compute-failed:{op}")
                        self._log(
                            f"compute {var} <- {op} FAILED (division by zero)")
                        continue
                    result = lval / rval
                prov = join(lpv.prov, rpv.prov)
                env[var] = PVal(
                    result, prov, f"compute:{op}({left_ref},{right_ref})")
                self._log(
                    f"compute {var} = {lval} {op} {rval} = {result} [{PROV_NAME[prov]}]")

            elif kind == "call":
                _, var, tname, argrefs = step
                spec = self.tools[tname]
                resolved = {a: self._resolve(r, env)
                            for a, r in argrefs.items()}

                # ---- policy gate on critical arguments ----
                if spec.sensitive:
                    bad = []
                    for ca in spec.critical_args:
                        if ca not in argrefs:
                            # The plan never provided this arg at all (an
                            # OPTIONAL parameter the model chose not to set,
                            # e.g. leaving a scheduled transaction's recipient
                            # unchanged while only adjusting the amount). That
                            # is not a security event -- the underlying tool
                            # enforces its own required-arg validation
                            # separately (see ToolCallError). Only a PROVIDED
                            # value with excessive provenance is refused here.
                            continue
                        pv = resolved.get(ca)
                        if pv is None or pv.prov > spec.max_prov:
                            bad.append((ca, pv))
                    if bad:
                        plain = {k: v.value for k, v in resolved.items()}
                        reason = "; ".join(
                            f"{a}={PROV_NAME[pv.prov] if pv else 'MISSING'} "
                            f"(max {PROV_NAME[spec.max_prov]}) via {pv.origin if pv else '-'}"
                            for a, pv in bad)
                        if self.policy == "confirm":
                            res.confirmations.append((tname, plain, reason))
                        res.refused.append((tname, plain, reason))
                        env[var] = PVal(None, UNTRUSTED, "refused")
                        res.control_trace.append(f"{tname}:REFUSED")
                        self._log(f"REFUSE {tname}({plain}) -- {reason}")
                        continue

                plain = {k: v.value for k, v in resolved.items()}
                try:
                    out = spec.impl(state, **plain)
                except Exception as e:
                    # The tool rejected its arguments (or blew up). The steps
                    # BEFORE this one really did execute, and their trace is
                    # the only evidence of what the plan actually did. Without
                    # this, `res` is discarded with the stack unwind and the
                    # caller logs an EMPTY control_trace for a trial in which
                    # tools ran -- which silently corrupts the K1 comparison.
                    # Attach the partial result and mark where it stopped.
                    res.control_trace.append(f"{tname}:ERROR")
                    e.partial_result = res
                    raise
                res.executed.append((tname, plain))
                res.control_trace.append(tname)
                origin = f"{tname}({plain.get('file_path', '')})" if tname == "read_file" else tname
                env[var] = PVal(out, spec.output_prov, origin)
                self._log(
                    f"{var} = {tname}(...) [{PROV_NAME[spec.output_prov]}]")

        return res