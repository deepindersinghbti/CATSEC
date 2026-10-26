"""Run the original script-style self-tests under pytest (CI entry point).

Each script prints PASS/FAIL lines and exits non-zero on any failure; they
need no API key, no network and (except the wiring tests) no agentdojo.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENV = {k: v for k, v in os.environ.items()
       if k not in ("PYTHONPATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                    "GEMINI_API_KEY", "GOOGLE_API_KEY")}

SCRIPTS = [
    (REPO, "tests/selftest.py"),
    (REPO, "tools/check_composer_prompt.py"),
    (REPO / "Journal_Experiments/Claude", "selftest_claude_wiring.py"),
    (REPO / "Journal_Experiments/Gemini", "selftest_gemini_wiring.py"),
    (REPO / "Journal_Experiments/Gemma4_31B", "selftest_gemma_wiring.py"),
]


@pytest.mark.parametrize("cwd,script", SCRIPTS, ids=[s for _, s in SCRIPTS])
def test_script(cwd, script):
    r = subprocess.run([sys.executable, script], cwd=cwd, env=ENV,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
