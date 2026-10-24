#!/usr/bin/env bash
# llama.cpp (prebuilt Vulkan build, runs on RADV; no ROCm/rocBLAS involved) plus quantized
# Qwen3-VL GGUFs, for vision LLMs too big for one 32 GB R9700 at bf16.
# The Vulkan build is vendor-neutral: it should run on NVIDIA GPUs too (untested). Nothing to change.
# Usage: scripts/install_llamacpp_vlm.sh [llamacpp] [8b] [32b] [30b-a3b]
set -euo pipefail
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_root.sh"
LLAMA_TAG="${LLAMA_TAG:-b11433}"
LLAMA_DIR="$AP_ROOT/envs/llamacpp"
GGUF="$AP_ROOT/models/gguf"

# the GGUFs come from the manifest (config/models.toml): resume, checksums, -> $GGUF/<repo name>/
AP="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap"

stage_llamacpp() {
  mkdir -p "$LLAMA_DIR"
  curl -fL "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-vulkan-x64.tar.gz" \
    | tar -xz -C "$LLAMA_DIR" --strip-components=1
  echo "$LLAMA_TAG" > "$LLAMA_DIR/VERSION"
  "$LLAMA_DIR/llama-server" --version
}

# Sizes are for one 32 GB card with the desktop on it: weights + mmproj + KV for ~16k ctx.
stage_8b() {       # control: same model as the transformers server, same engine as the big ones
  "$AP" models download vlm-8b-gguf
}
stage_32b() {      # dense 32B: Q5_K_M 23.2 GB (Q8_0 34.8 GB and FP8 35.5 GB don't fit)
  "$AP" models download vlm-32b
}
stage_30b-a3b() {  # MoE, 3B active: Q6_K 25.1 GB
  "$AP" models download vlm-30b-a3b
}

[ $# -gt 0 ] || set -- llamacpp 8b 32b 30b-a3b
for s in "$@"; do echo "=== stage: $s"; "stage_$s"; done
