"""
FU153 — Modal deployment of a SELF-HOSTED open model (vLLM, OpenAI-compatible) used ONLY for the
final blog content-writing pass that strips a Claude SynthID watermark. This file is STANDALONE —
it is NOT imported by the Flask app; the operator runs it once, then pastes the endpoint URL + key
into the app's Settings → "Content writer" card.

────────────────────────────────────────────────────────────────────────────────────────────────
RUNBOOK (operator does these — I cannot: they need your Modal account, payment, and login)
────────────────────────────────────────────────────────────────────────────────────────────────
  1. Create a Modal account:      https://modal.com   (free starter credits)
  2. Add a payment method:        Modal dashboard → Settings → Billing (charged only past credits)
  3. pip install modal
  4. modal token new              # opens a browser to authenticate to YOUR account
  5. Pick a long random bearer token and store it as a Modal secret:
        modal secret create writer-secret WRITER_API_KEY=<your-long-random-token>
  6. modal deploy deploy.py       # builds the image + prints the public endpoint URL
  7. In the app: Settings → "Content writer — watermark strip" card (or Railway env):
        Endpoint URL = https://<your-workspace>--geo-writer-serve.modal.run/v1   ← note the /v1
        API key      = the SAME token from step 5
        Model        = qwen-writer   (the SERVED_NAME below)
        Mode         = rewrite   (or compose)

Serverless + scale-to-zero: you pay only for GPU-seconds while a blog is actually being written
(~$0.12/blog at ~120s on an A100-80GB → roughly $35-55/mo at ~10 blogs/day). Idle = $0. The FIRST
request after a deploy downloads ~40GB of weights + loads them, so it takes several minutes; after
that the weights are cached on the volume and cold starts are much faster.

NOTE: Modal's Python API evolves. This targets a recent Modal + vLLM. If a decorator name or arg has
changed by the time you deploy, cross-check Modal's current "Run an OpenAI-compatible LLM server with
vLLM" example and adjust — the shape (vLLM image → GPU → HF cache volume → secret → web_server on
port 8000 running `vllm serve --api-key`) stays the same.
"""
import subprocess

import modal

# --- what to serve ---------------------------------------------------------------------------
# Qwen2.5-72B-Instruct in 4-bit AWQ (~40GB) → fits a single 80GB A100. The strongest open writing
# quality (closest to Claude). Pure instruct model — no "thinking mode" that would leak <think>
# traces into the blog. To go cheaper: "Qwen/Qwen2.5-32B-Instruct-AWQ" on GPU "A100-40GB".
MODEL_REPO = "Qwen/Qwen2.5-72B-Instruct-AWQ"   # what vLLM downloads + loads (4-bit, ~40GB)
SERVED_NAME = "qwen-writer"                     # the "model" id clients send → set the app's Model to THIS
GPU = "A100-80GB"                               # 80GB — fits 72B AWQ + KV cache on ONE GPU
PORT = 8000
MINUTES = 60

# The Hugging Face weights cache lives on a persistent Volume so the 40GB is downloaded ONCE.
hf_cache = modal.Volume.from_name("geo-writer-hf-cache", create_if_missing=True)
HF_CACHE_PATH = "/root/.cache/huggingface"


def _predownload_model():
    """Front-load the ~40GB weight download to a STABLE build step (into the Volume) so the serving
    container just loads from local disk (~2 min) instead of downloading 40GB on the first request —
    which is what the A100 capacity preemption kept interrupting."""
    from huggingface_hub import snapshot_download
    snapshot_download(MODEL_REPO)


vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm==0.8.5",            # supports Qwen2.5 AWQ; brings its own huggingface_hub (>=0.30)
        "transformers==4.51.3",   # PIN to what vLLM 0.8.5 was built against — a newer transformers
                                  # removed Qwen2Tokenizer.all_special_tokens_extended → load crash
    )
    # Download the weights during BUILD (once) into the Volume. Runs on stable CPU build capacity,
    # not the request-driven GPU container, so it isn't interrupted by A100 preemption.
    .run_function(_predownload_model, volumes={HF_CACHE_PATH: hf_cache}, timeout=60 * MINUTES)
)

app = modal.App("geo-writer")


@app.function(
    image=vllm_image,
    gpu=GPU,
    volumes={HF_CACHE_PATH: hf_cache},
    secrets=[modal.Secret.from_name("writer-secret")],   # provides WRITER_API_KEY in the container env
    # FU188: back to 5 min. FU164 raised this to 30 because the writer pass ran ~15 min into a blog
    # generation and blogs ran back-to-back, so the GPU had to survive the gap BETWEEN blogs. The
    # on-demand rewrite button has no such gap, and 30 minutes of idle A100 after every rewrite was
    # ~90% of the Modal bill: ~4 min of actual compute bought ~34 min of billed GPU (~$2/rewrite at
    # $2.50/hr, more once CPU+RAM are counted). A cold start costs ~$0.19 in GPU time, so you would
    # have to eat TEN of them to match ONE 30-min idle window. Rewrites clicked within ~5 min of each
    # other still find the model loaded; an isolated one pays a ~2-5 min load, which Settings →
    # "🔥 Warm up" removes from the critical path. (`modal deploy deploy.py` to apply.)
    scaledown_window=5 * MINUTES,
    timeout=30 * MINUTES,
    max_containers=1,               # one GPU is plenty for ~10 blogs/day
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=PORT, startup_timeout=20 * MINUTES)   # loads ~40GB from the Volume onto the GPU
def serve():
    """Run vLLM's own OpenAI-compatible server. It enforces the bearer token via --api-key, so the
    endpoint is authenticated with the SAME WRITER_API_KEY the app sends in its Authorization header."""
    import os

    cmd = [
        "vllm", "serve", MODEL_REPO,
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--api-key", os.environ["WRITER_API_KEY"],
        "--served-model-name", SERVED_NAME,
        "--max-model-len", "24576",     # prompt (rewrite feeds the full article) + up to ~9k output
        "--enforce-eager",              # skip CUDA-graph capture → much faster startup (fine for our low volume)
        # vLLM auto-detects AWQ 4-bit from the model config — no --quantization flag needed.
    ]
    subprocess.Popen(" ".join(cmd), shell=True)
