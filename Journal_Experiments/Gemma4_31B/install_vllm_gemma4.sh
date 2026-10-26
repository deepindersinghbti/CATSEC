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

# 3. vLLM: PINNED stable release (requirements-vllm.txt). The logged run of
#    9 Aug 2026 used an unpinned nightly whose version was not recorded; do
#    not use nightlies for new runs. cu129 assumes CUDA 12.9 -- swap for the
#    instance's CUDA (see step 2) if different.
uv pip install -r "$(dirname "$0")/requirements-vllm.txt" \
    --extra-index-url https://download.pytorch.org/whl/cu129 \
    --index-strategy unsafe-best-match
# Record the server environment next to the run (goes into the paper's
# reproducibility appendix together with the vllm serve flags below).
mkdir -p "$(dirname "$0")/runs"
{ date -u; nvidia-smi; python3 -c "import vllm, torch, transformers; print('vllm', vllm.__version__, 'torch', torch.__version__, 'transformers', transformers.__version__)"; pip freeze; } \
    > "$(dirname "$0")/runs/vllm_server_env.txt" 2>&1 || true

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
