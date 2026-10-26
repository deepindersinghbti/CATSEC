"""pytest configuration: put the repository root on sys.path so the shared
`catsec` package imports without installation, and keep API keys out of
every test process (all tests are offline)."""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
for _k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(_k, None)
