"""The decontaminated planning protocol (PROTOCOL.md): frozen artefacts,
prompt variants, k-fold isolation, generic heuristic, manifest logging."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from catsec import prompts as P
from catsec.heuristics import plan_risk_message_unstated_identifier
from catsec.kernel import DERIVED, ToolSpec
from catsec.options import CatsecOptions, manifest_info
from catsec.planner import build_plan, resolve_heuristics

REPO = Path(__file__).resolve().parents[1]
ENV = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def test_protocol_freeze_intact():
    r = subprocess.run([sys.executable, str(REPO / "tools/protocol_hashes.py"), "--check"],
                       env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_clean_prompt_names_no_test_task():
    text = (P.read("clean_v1.txt") + (REPO / "catsec/heuristics.py").read_text()).lower()
    for banned in ("user_task", "landlord", "rent", "spotify", "iphone", "apple",
                   "1200", "2200", "us133", "gb29", "bill-", "new year", "friend",
                   "send_money", "update_scheduled_transaction", "get_scheduled_transactions"):
        assert not re.search(r"(?<![a-z0-9])" + re.escape(banned) + r"(?![a-z0-9])", text), banned


def test_contamination_guard_passes():
    r = subprocess.run([sys.executable, str(REPO / "tools/check_contamination.py")],
                       env=ENV, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:]
    assert "CONTAMINATION GUARD: PASS" in r.stdout


@pytest.mark.parametrize("fold", range(P.N_FOLDS))
def test_kfold_withholds_test_fold_examples(fold):
    tmpl = P.system_template("kfold", fold)
    test = set(P.fold_tasks(fold))
    _, orig_blocks, _ = P.split_examples(P.read("original.txt"))
    for n, block in orig_blocks:
        body = block.split("\n", 1)[1]          # drop the numbered title line
        present = body in tmpl
        assert present == (P.ORIGINAL_EXAMPLE_SOURCES[n] not in test), (fold, n)
    assert set(P.kfold_example_sources(fold)).isdisjoint(test)


def test_kfold_folds_partition_banking():
    allt = [t for f in range(P.N_FOLDS) for t in P.fold_tasks(f)]
    assert sorted(allt) == sorted(P.BANKING_USER_TASKS) and len(allt) == 16


def test_kfold_requires_fold():
    with pytest.raises(ValueError):
        CatsecOptions.from_profile("clean", prompt_variant="kfold")
    with pytest.raises(ValueError):
        CatsecOptions.from_profile("paper", fold=1)
    o = CatsecOptions.from_profile("clean", prompt_variant="kfold", fold=2)
    assert o.allowed_user_tasks() == P.fold_tasks(2)


def test_profiles():
    paper = CatsecOptions.from_profile("paper")
    assert (paper.prompt_variant, paper.repair_heuristics, paper.repair_with_request,
            paper.unknown_tool_policy) == ("original", "all", False, "open")
    assert CatsecOptions.from_profile("hardened").unknown_tool_policy == "closed"
    clean = CatsecOptions.from_profile("clean")
    assert (clean.prompt_variant, clean.repair_heuristics, clean.repair_with_request,
            clean.unknown_tool_policy) == ("clean", "generic", True, "closed")
    assert resolve_heuristics("auto", "clean") == "generic"
    assert resolve_heuristics("auto", "original") == "all"


def test_build_plan_uses_selected_template():
    seen = {}

    def llm(system, user):
        seen["system"] = system
        return '{"plan": []}'
    build_plan(llm, "x", "  TOOLS-HERE", {}, prompt_variant="clean")
    assert seen["system"] == P.read("clean_v1.txt").format(tools="  TOOLS-HERE")
    build_plan(llm, "x", "T", {})
    assert seen["system"] == P.read("original.txt").format(tools="T")


def _tools():
    return {"pay": ToolSpec("pay", None, sensitive=True, critical_args=("account",),
                            max_prov=DERIVED)}


def test_generic_heuristic():
    invented = [("call", "p", "pay", {"account": ("lit", "DE89370400440532013000")})]
    assert plan_risk_message_unstated_identifier(invented, "pay my landlord", _tools())
    assert plan_risk_message_unstated_identifier(
        invented, "pay DE89 3704 0044 0532 0130 00 please", _tools()) is None
    msg = plan_risk_message_unstated_identifier(invented, "pay", _tools())
    assert "get_" not in msg and "transactions" not in msg      # names no suite tool


def test_generic_mode_skips_task_tailored_heuristics():
    """'generic' must not fire the released ut2-style rent heuristic."""
    calls = []

    def llm(system, user):
        calls.append(user)
        return ('{"plan": [{"kind": "call", "var": "d", "tool": "read_file", '
                '"args": {"file_path": {"lit": "n.txt"}}}, '
                '{"kind": "extract", "var": "i", "src": "d", "field": "IBAN", "match": null}]}')
    tools = {"read_file": ToolSpec("read_file", None)}
    text = "Read n.txt and adjust my rent accordingly"
    build_plan(llm, text, "", tools, allow_repair=True, repair_heuristics="all")
    assert len(calls) == 3          # released heuristic asked for 2 repairs
    calls.clear()
    build_plan(llm, text, "", tools, allow_repair=True, repair_heuristics="generic")
    assert len(calls) == 1
    calls.clear()
    build_plan(llm, text, "", tools, allow_repair=True, repair_heuristics="none")
    assert len(calls) == 1


def test_manifest_info_records_hashes():
    info = manifest_info(CatsecOptions.from_profile("clean"))
    h = info["prompt_sha256"]
    assert h["planner_system_template"] == P.sha256(P.read("clean_v1.txt"))
    assert h["repair_template"] == P.sha256(P.read("repair_with_request_v1.txt"))
    assert info["repair_heuristics_resolved"] == "generic"
    assert info["catsec_options"]["unknown_tool_policy"] == "closed"


def test_every_runner_logs_prompt_hashes():
    for r in sorted((REPO / "Journal_Experiments").glob("*/run_catsec_*.py")):
        src = r.read_text()
        assert "manifest_info(args.catsec_opts)" in src, r
        assert "probe.run_settings = manifest_info(args.catsec_opts)" in src, r
        assert "**opts.planner_kwargs()" in src, r
        assert "unknown_tool_policy=opts.unknown_tool_policy" in src, r
