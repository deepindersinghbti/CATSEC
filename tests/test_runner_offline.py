"""End-to-end, offline: the GPT/OpenAI-compatible runner's CATSEC pipeline on
a real AgentDojo banking task, with a FAKE chat client (no network, no key).
Checks that the revision options reach the planner and the kernel and that
every record carries the prompt hashes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catsec import prompts as P
from catsec.options import CatsecOptions, manifest_info

REPO = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_catsec_openai_t", REPO / "Journal_Experiments/GPT/run_catsec_openai.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod          # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


PLAN = {"plan": [{"kind": "call", "var": "h", "tool": "get_most_recent_transactions",
                  "args": {"n": {"lit": 100}}}]}


class FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, model, messages, **kw):
        self.requests.append(messages)
        planning = "PLANNER" in messages[0]["content"]
        content = json.dumps(PLAN) if planning else "You spent some money."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


@pytest.mark.parametrize("profile,variant_file", [("paper", "original.txt"), ("clean", "clean_v1.txt")])
def test_pipeline_offline(profile, variant_file):
    R = _load_runner()
    from agentdojo.task_suite.load_suites import get_suite
    suite = get_suite("v1.2", "banking")
    comp = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    opts = CatsecOptions.from_profile(profile)
    probe = R.TrialProbe()
    probe.run_settings = manifest_info(opts)
    Cls = R.make_catsec_pipeline(client, "gpt-4o-mini", R.DecodeConfig(), allow_repair=True,
                                 probe=probe, opts=opts)
    from agentdojo.agent_pipeline import AgentPipeline
    pipe = AgentPipeline([Cls()])
    pipe.name = "gpt-4o-mini-catsec"
    ut = suite.user_tasks["user_task_1"]
    suite.run_task_with_pipeline(pipe, ut, None, {})
    system = comp.requests[0][0]["content"]
    assert system.startswith(P.read(variant_file).split("{tools}")[0].replace("{{", "{").replace("}}", "}")[:200])
    rec = probe.as_record()
    assert rec["plan_valid"] is True
    assert rec["control_trace"] == ["get_most_recent_transactions"]
    assert rec["catsec_run"]["prompt_sha256"]["planner_system_template"] == P.sha256(P.read(variant_file))
    assert rec["catsec_run"]["options"]["profile"] == profile
