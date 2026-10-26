"""
Provider-independent harness helpers shared by every runner: the per-trial
probe, the trace recorder, JSONL logging and resume. Moved here verbatim from
the four runners, where they were AST-identical copies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .planner import plan_signature


# --------------------------------------------------------------------------- #
# Uniform per-trial probe -- IDENTICAL shape to the Vertex runner
# --------------------------------------------------------------------------- #
@dataclass
class TrialProbe:
    plan: list | None = None
    plan_valid: bool | None = None
    plan_error: str | None = None
    control_trace: list = field(default_factory=list)
    executed: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    tool_arg_failures: list = field(default_factory=list)
    composed_answer: str = ""
    guard_error: str | None = None
    # WHY _compose_answer produced nothing, when it produced nothing. The old
    # bare `except Exception: return ""` made a transient API failure look
    # identical to "the retrieved data does not answer the question". That
    # mattered more than it first appeared: read-only question tasks derive
    # their ENTIRE utility from this string, so a silent compose failure was
    # indistinguishable from a planning defect in the per-trial log.
    compose_error: str | None = None
    # Revision: the prompt hashes / options of the run (manifest_info), copied
    # into EVERY record so a single trial can be tied to its exact prompt.
    # Survives reset(); set once by the runner.
    run_settings: dict | None = None

    def reset(self):
        keep = self.run_settings
        self.__init__()
        self.run_settings = keep

    def plan_signature_or_none(self):
        return plan_signature(self.plan) if self.plan is not None else None

    def as_record(self) -> dict:
        return {
            "plan": self.plan,
            "plan_signature": self.plan_signature_or_none(),
            "plan_valid": self.plan_valid,
            "plan_error": self.plan_error,
            "control_trace": list(self.control_trace),
            "executed": [[t, a] for t, a in self.executed],
            "refused": [[t, a, r] for t, a, r in self.refused],
            "tool_arg_failures": [[t, a, e] for t, a, e in self.tool_arg_failures],
            "composed_answer": self.composed_answer,
            "guard_error": self.guard_error,
            "compose_error": self.compose_error,
            **({"catsec_run": {"options": self.run_settings.get("catsec_options"),
                               "prompt_sha256": self.run_settings.get("prompt_sha256")}}
               if self.run_settings else {}),
        }


def tool_sequence_from_messages(messages) -> list:
    seq = []
    for m in messages or ():
        tcs = (m.get("tool_calls") if isinstance(m, dict)
               else getattr(m, "tool_calls", None))
        for tc in (tcs or ()):
            fn = (tc.get("function") if isinstance(tc, dict)
                  else getattr(tc, "function", None))
            seq.append(str(fn))
    return seq


def make_trace_recorder(probe: TrialProbe):
    from agentdojo.agent_pipeline import BasePipelineElement
    from agentdojo.functions_runtime import EmptyEnv

    class TraceRecorder(BasePipelineElement):
        name = "trace_recorder"

        def query(self, query, runtime, env=EmptyEnv(), messages=(), extra_args={}):
            # Full unconditional overwrite every call -- no staleness risk,
            # unlike CatsecPipeline's conditionally-set fields (see kernel
            # docstring above). Confirmed safe on the Vertex runner; carried
            # over unchanged.
            probe.control_trace = tool_sequence_from_messages(messages)
            return query, runtime, env, messages, extra_args

    return TraceRecorder


# --------------------------------------------------------------------------- #
# JSONL logging + resume -- identical to the Vertex runner
# --------------------------------------------------------------------------- #
def trial_key(rec: dict) -> str:
    return "|".join(str(rec.get(k)) for k in
                    ("defense", "attack", "user_task", "injection_task",
                     "repetition", "condition", "ablation"))


def load_resume_index(path: Path) -> set:
    done = set()
    if not path.exists():
        return done
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("error") is None:
                done.add(trial_key(rec))
    return done


class JsonlWriter:
    def __init__(self, path: Path | None):
        self.path = path
        self._fh = path.open("a") if path else None

    def write(self, rec: dict):
        if not self._fh:
            return
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


# --------------------------------------------------------------------------- #
# One trial -- identical logic to the Vertex runner
# --------------------------------------------------------------------------- #
def _single_value(d: dict, what: str, pair):
    if len(d) != 1:
        raise RuntimeError(
            f"expected exactly 1 {what} for pair {pair}, got {len(d)}: "
            f"{list(d.keys())}. Results are colliding -- see G0.1.")
    return bool(next(iter(d.values())))
