#!/usr/bin/env bash
# Install the local vision LLM (Qwen3-VL-8B-Instruct, Apache-2.0) in its own venv.
# Usage: scripts/install_qwen_vl.sh [venv] [model]
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
ENV="$AP_ROOT/envs/qwen-vl"
MODEL_DIR="$AP_ROOT/models/Qwen3-VL-8B-Instruct"
PY="$ENV/bin/python"

stage_venv() {
  [ -x "$PY" ] || python3.12 -m venv "$ENV"
  "$PY" -m pip install -U pip setuptools wheel
  # Same torch as TRELLIS.2 (validated on gfx1201).
  "$PY" -m pip install torch==2.13.0 torchvision --index-url https://download.pytorch.org/whl/rocm7.2
  "$PY" -m pip install "transformers>=4.57" accelerate pillow huggingface_hub
}

stage_model() {
  # HF_HOME stays at its default here so a token in ~/.cache/huggingface still works.
  "$ENV/bin/hf" download Qwen/Qwen3-VL-8B-Instruct --local-dir "$MODEL_DIR"
}

[ $# -gt 0 ] || set -- venv model
for s in "$@"; do echo "=== stage: $s"; "stage_$s"; done
