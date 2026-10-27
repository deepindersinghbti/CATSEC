"""tools/ingest_rerun.py on a FAKE synthetic bundle (tests/fake_bundle.py) and
on the committed original runs. Offline; no API calls."""
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import fake_bundle  # noqa: E402
import ingest_rerun  # noqa: E402


def macros(out: Path) -> dict:
    text = (out / "results_macros.tex").read_text()
    return dict(re.findall(r"^\\newcommand\{\\([A-Za-z]+)\}\{(.*?)\}(?:  %.*)?$", text, re.MULTILINE))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return fake_bundle.build(tmp_path_factory.mktemp("fake"))  # gpt, gemini, claude; no gemma


@pytest.fixture(scope="module")
def ingested(bundle, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    assert ingest_rerun.main([str(bundle), "--out", str(out)]) == 0
    return out


def test_original_only_reproduces_paper_numbers(tmp_path):
    assert ingest_rerun.main(["--original-only", "--out", str(tmp_path)]) == 0
    m = macros(tmp_path)
    # Table II, McNemar, clustered bootstrap, usage -- as printed in the paper
    assert (m["OrigUtilUndGpt"], m["OrigUtilUndGptLo"], m["OrigUtilUndGptHi"]) == ("43.1", "35.3", "51.2")
    assert (m["OrigASRUndGemini"], m["OrigASRUndGeminiLo"], m["OrigASRUndGeminiHi"]) == ("35.4", "28.1", "43.5")
    assert (m["OrigUtilCatGemma"], m["OrigSecCatGemmaLo"], m["OrigRefPerRepCatGemma"]) == ("62.5", "97.4", "45")
    assert (m["OrigMcSecGeminiCatBetter"], m["OrigMcSecGeminiP"]) == ("51", r"8.9\times10^{-16}")
    assert (m["OrigMcUtilClaudeCatBetter"], m["OrigMcUtilClaudeUndBetter"], m["OrigMcUtilClaudeP"]) == ("17", "10", "0.248")
    assert m["OrigMcSecClaudeP"] == "1.00"
    assert (m["OrigDiffUtilClaude"], m["OrigDiffUtilClaudeLo"], m["OrigDiffUtilClaudeHi"]) == ("+4.9", "-13.9", "+25.0")
    assert (m["OrigBootASRUndGptLo"], m["OrigBootASRUndGptHi"]) == ("8.3", "46.5")
    assert (m["OrigBootASRUndGemmaLo"], m["OrigBootASRUndGemmaHi"]) == ("8.3", "40.3")
    assert (m["OrigASRUndMin"], m["OrigASRUndMax"], m["OrigCatTrials"], m["OrigCatBreaches"]) == ("23.6", "35.4", "576", "0")
    assert (m["OrigTasksSolvedMax"], m["OrigTasksSolvedMaxLo"], m["OrigTasksSolvedMaxHi"]) == ("11", "44.4", "85.8")
    assert (m["OrigRedTokInMin"], m["OrigRedTokInMax"], m["OrigRedLatMin"], m["OrigRedLatMax"]) == ("65.9", "86.1", "59.7", "76.7")
    assert (m["OrigTokRedModelInUnd"], m["OrigTokRedModelLatCat"]) == ("1,716,713", "5.03")
    assert m["OrigUtilCatGeminiNoUtTwelve"] == "62.5" and m["OrigUndUtilSpread"] == "44.4"
    assert m["CleanUtilCatGpt"] == r"\mbox{--}"


def test_fake_bundle_end_to_end(ingested):
    m = macros(ingested)
    assert m["CleanDataFake"] == "FAKE"
    assert "FAKE SYNTHETIC DATA" in (ingested / "results_macros.tex").read_text()
    assert "FAKE SYNTHETIC BUNDLE" in (ingested / "results_summary.md").read_text()
    # missing model -> "--" everywhere
    for k in ("CleanUtilCatGemma", "CleanMcSecGemmaP", "BenignUtilUndGemma", "KfoldUtilCatGemma"):
        assert m[k] == r"\mbox{--}", k
    # 3 reps x 144 pairs; rep 0 is the original data, so the max over reps of
    # CATSEC utility is the original value and rep 2 drops one task (9 pairs)
    assert m["CleanNReps"] == "3" and m["CleanTrialsCatGpt"] == "432"
    assert m["CleanUtilCatGptRepMax"] == m["OrigUtilCatGpt"] == "68.8"
    assert m["CleanUtilCatGptRepMin"] == "62.5"
    assert (m["CleanTasksSolvedGptRepMin"], m["CleanTasksSolvedGptRepMax"]) == ("10", "11")
    assert m["CleanAllOrNothingGpt"] == "yes"
    assert m["CleanASRCatGpt"] == "0.0" and m["CleanCatBreaches"] == "0" and m["CleanCatTrials"] == str(3 * 432)
    assert m["CleanSecCatGptLo"] == "99.1"          # Wilson lower bound for 432/432
    assert m["BenignUtilCatGptN"] == "16" and m["BenignUtilUndClaudeN"] == "16"
    assert (m["KfoldUtilCatGptN"], m["KfoldUtilCatGpt"]) == ("144", "68.8")
    assert m["CmpDiffUtilCatGpt"] == "-2.1"
    assert re.fullmatch(r"[+-]\d+\.\d", m["CleanDiffUtilGemini"])
    js = json.loads((ingested / "results.json").read_text())
    assert js["fake"] is True and not js["issues"]["fatal"]
    assert any("gemma" in w for w in js["issues"]["warn"])
    for f in ("results_table_clean.tex", "results_table_orig.tex", "results_table_compare.tex"):
        assert (ingested / f).stat().st_size > 0
    assert r"\CleanUtilCatGpt{}" in (ingested / "results_table_clean.tex").read_text()


def test_tarball_input(bundle, tmp_path):
    tgz = tmp_path / "catsec_clean_rerun_FAKE.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(bundle, arcname="runs_clean")
    out = tmp_path / "o"
    assert ingest_rerun.main([str(tgz), "--out", str(out)]) == 0
    assert macros(out)["CleanUtilUndGpt"] != r"\mbox{--}"


def _copy(bundle, tmp_path):
    dst = tmp_path / "runs_clean"
    shutil.copytree(bundle, dst)
    return dst


def test_wrong_prompt_hash_aborts(bundle, tmp_path):
    b = _copy(bundle, tmp_path)
    p = b / "gpt" / "clean_main.jsonl"
    lines = p.read_text().splitlines()
    r = json.loads(lines[5])
    r["catsec_run"]["prompt_sha256"]["planner_system_template"] = "0" * 64   # e.g. original prompt
    lines[5] = json.dumps(r)
    p.write_text("\n".join(lines) + "\n")
    out = tmp_path / "o"
    assert ingest_rerun.main([str(b), "--out", str(out)]) == 2
    assert not (out / "results_macros.tex").exists()


def test_missing_hash_and_broken_freeze_abort(bundle, tmp_path):
    b = _copy(bundle, tmp_path)
    prov = b / "PROVENANCE.txt"
    prov.write_text(prov.read_text().replace("PROTOCOL FREEZE: intact", "PROTOCOL FREEZE: 1 artefact(s) changed"))
    assert ingest_rerun.main([str(b), "--out", str(tmp_path / "o")]) == 2
    b2 = _copy(bundle, tmp_path / "x")
    p = b2 / "claude" / "kfold_0.jsonl"
    r = [json.loads(x) for x in p.read_text().splitlines()]
    del r[0]["catsec_run"]
    p.write_text("\n".join(json.dumps(x) for x in r) + "\n")
    assert ingest_rerun.main([str(b2), "--out", str(tmp_path / "o2")]) == 2


def test_kfold_task_outside_fold_aborts(bundle, tmp_path):
    b = _copy(bundle, tmp_path)
    p = b / "gemini" / "kfold_1.jsonl"
    r = [json.loads(x) for x in p.read_text().splitlines()]
    r[0]["user_task"] = "user_task_0"       # fold 0 task run with the fold-1 prompt
    p.write_text("\n".join(json.dumps(x) for x in r) + "\n")
    assert ingest_rerun.main([str(b), "--out", str(tmp_path / "o")]) == 2


def test_fake_bundle_never_written_into_paper_dir(bundle, tmp_path):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "bare_jrnl_new_sample4.tex").write_text("% paper")
    assert ingest_rerun.main([str(bundle), "--out", str(paper)]) == 2
    assert not (paper / "results_macros.tex").exists()


def test_unsafe_tar_member_refused(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "p.txt"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(payload, arcname="../escape.txt")
    assert ingest_rerun.main([str(evil), "--out", str(tmp_path / "o")]) == 2
    assert not (tmp_path.parent / "escape.txt").exists()
