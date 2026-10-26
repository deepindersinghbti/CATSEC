"""
StrictInterpreter -- kernel.Interpreter plus ONE rule, used by E2's
`refuse_failed` settings. catsec/kernel.py is not edited.

THE RULE
  A sensitive tool call is refused if ANY argument the plan provides holds a
  failed value (extraction absent/ambiguous, compute on a non-number, or the
  output of an earlier refused call). Without it, that argument reaches the
  tool as None, and what happens next depends on the tool: AgentDojo's
  update_scheduled_transaction treats None as "leave unchanged" (the silent
  drop in attack A4), its schedule_transaction rejects None (the tool error in
  attack A6). With it, the kernel itself refuses and logs why.

HOW TO AUDIT THIS FILE
  run() below is a verbatim copy of kernel.Interpreter.run with one block
  inserted, marked STRICT RULE. `_verify_copy()` strips that block and checks
  the remainder is byte-identical to the kernel's current run(), so this file
  cannot silently drift from catsec/kernel.py.
"""
from __future__ import annotations

import inspect

from catsec import kernel
from catsec.kernel import (DERIVED, PROV_NAME, TRUSTED, UNTRUSTED, ExecResult,  # noqa: F401
                    Interpreter, PVal, extract_field, extract_field_matched,
                    join, validate_plan)

FAILED = 3                       # display-only level, never produced by the kernel
PROV_NAME.setdefault(FAILED, "FAILED")
FAILED_ORIGINS = ("extract-failed", "compute-failed", "refused", "unbound")


class StrictInterpreter(Interpreter):
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
                    # >>> STRICT RULE (only difference from kernel.Interpreter.run) >>>
                    # Any PROVIDED argument of a sensitive tool whose value is a
                    # failed extraction / failed compute / refused result is
                    # refused here, instead of passing None to the tool.
                    flagged = {a for a, _ in bad}
                    for a, pv in resolved.items():
                        if a in flagged:
                            continue
                        if pv.value is None and str(pv.origin).startswith(FAILED_ORIGINS):
                            bad.append((a, PVal(None, FAILED, pv.origin)))
                    # <<< STRICT RULE <<<
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


def _verify_copy():
    mine = inspect.getsource(StrictInterpreter.run)
    start = mine.index("                    # >>> STRICT RULE")
    end = mine.index("                    # <<< STRICT RULE <<<\n") + len(
        "                    # <<< STRICT RULE <<<\n")
    stripped = mine[:start] + mine[end:]
    if stripped != inspect.getsource(kernel.Interpreter.run):
        raise RuntimeError("strict_interpreter.py no longer matches catsec/kernel.py "
                           "-- regenerate it before trusting refuse_failed results")


_verify_copy()
