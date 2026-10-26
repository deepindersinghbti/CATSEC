# Running CATSEC with local / open models (no paid API)

`Journal_Experiments/GPT/run_catsec_openai.py` accepts `--base-url` (or the
`OPENAI_BASE_URL` environment variable) and then talks to any
**OpenAI-compatible** server instead of api.openai.com; no `OPENAI_API_KEY` is
needed. Both arms go through that endpoint: the CATSEC planner/composer (plain
chat completions) and the undefended AgentDojo agent (chat completions with
tool calling -- the model and server must support OpenAI-style `tools`).

```
# Ollama (https://ollama.com), on this machine
ollama pull qwen2.5:7b-instruct-q4_K_M
cd Journal_Experiments/GPT
resman run -m 6G -g 5G -n catsec-local -- \
  python run_catsec_openai.py --base-url http://localhost:11434/v1 \
      --model qwen2.5:7b-instruct-q4_K_M --profile clean --plan-repair \
      --all-pairs --jsonl runs/qwen7b_clean.jsonl --out qwen7b_clean.csv

# vLLM, on a GPU server
vllm serve Qwen/Qwen2.5-7B-Instruct --enable-auto-tool-choice --tool-call-parser hermes
python run_catsec_openai.py --base-url http://<host>:8000/v1 --model Qwen/Qwen2.5-7B-Instruct ...
```

(Gemma 4 31B keeps its own runner, `Journal_Experiments/Gemma4_31B/run_catsec_gemma.py`,
which carries the Gemma-specific chat-template and thinking switches; it also
uses an OpenAI-compatible vLLM endpoint via `--base-url`.)

Model names not in AgentDojo's `MODEL_NAMES` are registered as "AI assistant"
for the tool_knowledge attack text (record this in the paper, as for Claude).

## What fits where

| hardware | fits | notes |
|---|---|---|
| this laptop: RTX 4050, **6 GB** VRAM, 15 GB RAM (shared -- use `resman`, `-g 5G`) | 7-8B instruct models at 4-bit (Q4_K_M, ~4.5-5 GB): Qwen2.5-7B-Instruct, Llama-3.1-8B-Instruct; 3-4B models at Q8 | set `num_ctx` >= 8192 (planner prompt ~3k tokens + tool list; undefended agent turns up to ~10k). Expect seconds per call; 288 trials ~ 1-3 h. Tool-calling quality of small models is the limiting factor for the undefended arm. |
| 24 GB GPU (RTX 4090 / A5000) | 14B at 4-8 bit, 32B at 4-bit (tight) | |
| 48 GB (L40S / A6000) | Gemma 4 31B in FP8 (~31 GB weights + KV cache) | |
| 80-141 GB (H100 / H200) | **Gemma 4 31B in BF16** (~62 GB weights), as in the logged run | the logged Gemma run used an H200; vLLM pinned in `Journal_Experiments/Gemma4_31B/requirements-vllm.txt` |

A 31B model does **not** fit the laptop's 6 GB GPU in any useful precision.
