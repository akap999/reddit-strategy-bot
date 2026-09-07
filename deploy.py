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
        Model        = the MODEL_NAME below
        Mode         = rewrite   (or compose)

Serverless + scale-to-zero: you pay only for GPU-seconds while a blog is actually being written
(~$0.02/blog at ~60s on an A10G). Idle = $0. Upgrade MODEL_NAME to a 32B for higher quality and
raise GPU accordingly.

NOTE: Modal's Python API evolves. This targets a recent Modal + vLLM. If a decorator name or arg has
changed by the time you deploy, cross-check Modal's current "Run an OpenAI-compatible LLM server with
vLLM" example and adjust — the shape (vLLM image → GPU → HF cache volume → secret → web_server on
port 8000 running `vllm serve --api-key`) stays the same.
"""
import subprocess

import modal

# --- what to serve ---------------------------------------------------------------------------
MODEL_NAME = "Qwen/Qwen3-14B-Instruct"   # upgradeable to "Qwen/Qwen3-32B" (raise GPU to A100-40GB)
GPU = "A10G"                             # 24GB — fits 14B (4-bit/AWQ or fp16-tight); A100-40GB for 32B
PORT = 8000
MINUTES = 60

# vLLM + the Hugging Face weights cache live in the image / a persistent volume so cold starts
# don't re-download the model every time.
vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm==0.6.6",                           # bump if a newer vLLM is out at deploy time
        "huggingface_hub[hf_transfer]==0.27.0",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)
hf_cache = modal.Volume.from_name("geo-writer-hf-cache", create_if_missing=True)

app = modal.App("geo-writer")


@app.function(
    image=vllm_image,
    gpu=GPU,
    volumes={"/root/.cache/huggingface": hf_cache},
    secrets=[modal.Secret.from_name("writer-secret")],   # provides WRITER_API_KEY in the container env
    scaledown_window=5 * MINUTES,   # keep warm 5 min after the last request, then scale to zero (idle = $0)
    timeout=30 * MINUTES,
    max_containers=1,               # one GPU is plenty for ~10 blogs/day
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=PORT, startup_timeout=15 * MINUTES)
def serve():
    """Run vLLM's own OpenAI-compatible server. It enforces the bearer token via --api-key, so the
    endpoint is authenticated with the SAME WRITER_API_KEY the app sends in its Authorization header."""
    import os

    cmd = [
        "vllm", "serve", MODEL_NAME,
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--api-key", os.environ["WRITER_API_KEY"],
        "--served-model-name", MODEL_NAME,
        "--max-model-len", "24576",     # prompt (rewrite feeds the full article) + up to ~9k output
    ]
    subprocess.Popen(" ".join(cmd), shell=True)
