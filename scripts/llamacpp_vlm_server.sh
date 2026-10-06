#!/usr/bin/env bash
# Serve a Qwen3-VL GGUF with llama.cpp (Vulkan) on GPU 0, for asset_pipeline/llm/llamacpp.py.
# Usage: scripts/llamacpp_vlm_server.sh MODEL.gguf MMPROJ.gguf [PORT]
# Vulkan0 is the card carrying the desktop, i.e. HIP GPU 0 (check: llama-server --list-devices).
# Image tokens pinned to 1024: llama.cpp warns Qwen-VL grounding needs >= 1024, and it matches what the transformers server gives a 1344x768 scene (~1008).
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
exec "$AP_ROOT/envs/llamacpp/llama-server" -m "$1" --mmproj "$2" --port "${3:-8711}" \
  --host 127.0.0.1 --device "${LLAMA_DEVICE:-Vulkan0}" -ngl 999 -c "${LLAMA_CTX:-16384}" -np 1 \
  --image-min-tokens 1024 --image-max-tokens 1024 --temp 0
