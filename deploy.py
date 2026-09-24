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
# Qwen3-32B at bf16 (~64GB) on a single 80GB H100 — FULL PRECISION, no quantization.
# Replaces Qwen2.5-72B-Instruct-AWQ, which was a 72B squeezed to 4 bits (~40GB, about a 40B model's
# memory). Quantization damages INSTRUCTION-FOLLOWING far more than fluency, and that was the
# measured failure: the prose read fine, but the model could not hold ~30 constraints or act on a
# correction — "ensure" survived a retry, a second retry AND a surgical "make the smallest possible
# edit, change nothing else" pass, three times over, and "boasts"/"comprehensive" kept reappearing.
# The bet is that a NEWER generation at FULL precision beats an older one at 4-bit; 32B is fewer
# parameters than 72B, so it IS a bet. Ladder if it disappoints: Qwen2.5-72B at FP8 (isolates
# quantization alone, but ~72GB is tight on an 80GB card), then Qwen3-235B-A22B on a larger box.
# Qwen3 has a hybrid thinking mode — disabled below. Its leak would be a literal <think> tag, not
# gpt-oss's unlabelled channel that is documented to merge into message.content (vLLM #32125).
MODEL_REPO = "Qwen/Qwen3-32B"                   # what vLLM downloads + loads (bf16, ~64GB)
SERVED_NAME = "qwen-writer"                     # UNCHANGED: keeps the app, DB and Settings untouched
GPU = "H100"                                    # 80GB — 64GB of weights leaves real KV-cache headroom
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


# FU285f — a CUDA *devel* base, not debian_slim. vLLM 0.30 JIT-COMPILES kernels at startup
# (`enable_jit_warmup=True`, `enable_flashinfer_autotune=True` in its KernelConfig), and every one
# of those needs nvcc. On a pip-only slim image the engine dies with
#     RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist
# It was fixed twice one caller at a time — the fp8 KV cache, then FlashInfer's sampler — and a
# third caller appeared each time. The class fix is to ship a compiler; `-devel` carries nvcc where
# `-runtime` and debian_slim do not. Bigger image, slower first build, no more of these.
vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.11")
    .pip_install(
        "vllm>=0.10.1",           # Qwen3 support; 0.8.5 predates it
        # The transformers==4.51.3 pin is GONE. It existed only because a newer transformers removed
        # Qwen2Tokenizer.all_special_tokens_extended and crashed the Qwen2.5 load — it does not apply
        # to Qwen3 and would hold vLLM back.
    )
    # FU285e — vLLM 0.30 picks FlashInfer for SAMPLING by default and JIT-COMPILES that kernel on
    # first use, which needs nvcc. This image is debian_slim + pip vllm and carries no CUDA toolkit,
    # so the engine died during its warmup profile:
    #     flashinfer_sample -> gen_sampling_module().build_and_load() -> get_cuda_path()
    #     RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist
    # The native sampler needs no compiler and is fine at this volume. (Attention is unaffected —
    # the log shows vLLM selecting FLASH_ATTN, not FlashInfer, for that.)
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0"})
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
        # MEMORY, learned the hard way. Qwen3-32B in bf16 is 61 GiB of weights on an 80 GiB card,
        # which leaves far less headroom than the 40 GiB AWQ build did. The first attempt raised
        # max-model-len to 32768 AND dropped --enforce-eager, and the engine refused to start:
        #     Available KV cache memory: 5.73 GiB  ->  _check_enough_kv_cache_memory ValueError
        # Both changes spent memory that is no longer there. Three levers, all restored/added:
        "--max-model-len", "24576",     # back to the known-good length: prompt + article + ~9k out
        "--enforce-eager",              # NO CUDA-graph capture. It costs GPU memory AND startup
                                        # time; the original config had it for exactly this reason.
        "--gpu-memory-utilization", "0.95",   # 0.92 default; vLLM itself suggests ~0.95 here
        # NOT --kv-cache-dtype fp8. It was tried and the engine died with
        #     RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda'
        # because an fp8 KV cache JIT-compiles a kernel and this image (debian_slim + pip vllm)
        # carries no CUDA toolkit. It is also unnecessary: at 0.95 utilisation the budget is
        # 76 GiB usable - 61 GiB weights - ~3 GiB workspace = ~12 GiB for KV, and 24576 tokens
        # need ~6 GiB (vLLM reported 8 GiB for 32768). The original 5.73 GiB shortfall was caused
        # by CUDA-graph capture plus 0.92 utilisation, and both are already fixed above.
        # NOTE: `--chat-template-kwargs` is NOT a vllm serve flag in this build — it was tried and
        # the container refused to start ("unrecognized arguments"). Thinking is turned off PER
        # REQUEST in WriterClient.call_text instead. That also avoids this list's shell-quoting
        # problem: the argv is joined and run through a shell, so embedded JSON loses its quotes.
        # bf16 weights need no --quantization flag.
    ]
    subprocess.Popen(" ".join(cmd), shell=True)
