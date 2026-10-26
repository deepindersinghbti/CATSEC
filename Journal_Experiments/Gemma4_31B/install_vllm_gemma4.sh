#!/usr/bin/env bash
# Run this AFTER `pip install -r requirements.txt`, on the Jarvis Labs
# instance itself (it needs to see the instance's actual CUDA to pick the
# right wheel index — don't run this anywhere else and copy the result over).
set -euo pipefail

# 1. uv is what the official vLLM Gemma 4 recipe uses for this install.
#    Plain pip's resolver doesn't handle --pre plus multiple
#    --extra-index-url entries the same way (it can silently pick a stale
#    version instead of erroring), so use uv here specifically, even if you
#    use pip everywhere else.
pip install -U uv

# 2. Check the instance's CUDA version BEFORE choosing the wheel index below.
nvidia-smi --query-gpu=driver_version --format=csv,noheader
nvcc --version 2>/dev/null || echo "nvcc not on PATH — check the template's CUDA docs instead"

# 3. vLLM nightly (pre-release) + a matching torch build for that CUDA.
#    cu129 below assumes CUDA 12.9 on the instance — if step 2 shows a
#    different major version, swap cu129 for e.g. cu121 / cu124 in BOTH
#    --extra-index-url lines. A mismatch here fails at import time, not at
#    install time, so it looks like everything worked until vllm serve dies.
uv pip install -U vllm --pre \
    --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
    --extra-index-url https://download.pytorch.org/whl/cu129 \
    --index-strategy unsafe-best-match

# 4. Sanity check: transformers must actually be new enough to know gemma4.
python3 -c "
import transformers
from packaging.version import Version
v = Version(transformers.__version__)
assert v >= Version('5.5.0'), f'transformers {v} is too old for Gemma 4 (need >=5.5.0)'
print('transformers', v, 'OK — gemma4 architecture recognized')
"

# 5. Gemma 4 weights are gated on Hugging Face. Accept the license at
#    https://huggingface.co/google/gemma-4-31B-it  (capital B — the repo id
#    is case-sensitive; the lowercase form 404s), then authenticate:
huggingface-cli login

echo "Done. Serve with:"
echo "  vllm serve google/gemma-4-31B-it \\"
echo "      --served-model-name gemma-4-31b-it \\"
echo "      --enable-auto-tool-choice --tool-call-parser pythonic \\"
echo "      --max-model-len 32768"
