#!/usr/bin/env bash
# llama.cpp (prebuilt Vulkan build, runs on RADV; no ROCm/rocBLAS involved) plus quantized
# Qwen3-VL GGUFs, for vision LLMs too big for one 32 GB R9700 at bf16.
# Usage: scripts/install_llamacpp_vlm.sh [llamacpp] [8b] [32b] [30b-a3b]
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
LLAMA_TAG="${LLAMA_TAG:-b11433}"
LLAMA_DIR="$AP_ROOT/envs/llamacpp"
GGUF="$AP_ROOT/models/gguf"
HF="${HF_ENDPOINT:-https://huggingface.co}"

fetch() {  # repo file -> $GGUF/<repo name>/<file>, resumable
  local dst="$GGUF/${1#*/}/$2"
  mkdir -p "$(dirname "$dst")"
  curl -fL --retry 5 -C - -o "$dst" "$HF/$1/resolve/main/$2"
}

stage_llamacpp() {
  mkdir -p "$LLAMA_DIR"
  curl -fL "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-vulkan-x64.tar.gz" \
    | tar -xz -C "$LLAMA_DIR" --strip-components=1
  echo "$LLAMA_TAG" > "$LLAMA_DIR/VERSION"
  "$LLAMA_DIR/llama-server" --version
}

# Sizes are for one 32 GB card with the desktop on it: weights + mmproj + KV for ~16k ctx.
stage_8b() {       # control: same model as the transformers server, same engine as the big ones
  fetch Qwen/Qwen3-VL-8B-Instruct-GGUF Qwen3VL-8B-Instruct-Q8_0.gguf
  fetch Qwen/Qwen3-VL-8B-Instruct-GGUF mmproj-Qwen3VL-8B-Instruct-F16.gguf
}
stage_32b() {      # dense 32B: Q5_K_M 23.2 GB (Q8_0 34.8 GB and FP8 35.5 GB don't fit)
  fetch unsloth/Qwen3-VL-32B-Instruct-GGUF Qwen3-VL-32B-Instruct-Q5_K_M.gguf
  fetch unsloth/Qwen3-VL-32B-Instruct-GGUF mmproj-F16.gguf
}
stage_30b-a3b() {  # MoE, 3B active: Q6_K 25.1 GB
  fetch unsloth/Qwen3-VL-30B-A3B-Instruct-GGUF Qwen3-VL-30B-A3B-Instruct-Q6_K.gguf
  fetch unsloth/Qwen3-VL-30B-A3B-Instruct-GGUF mmproj-F16.gguf
}

[ $# -gt 0 ] || set -- llamacpp 8b 32b 30b-a3b
for s in "$@"; do echo "=== stage: $s"; "stage_$s"; done
