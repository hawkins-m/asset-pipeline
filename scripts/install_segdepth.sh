#!/usr/bin/env bash
# Install the crop-refinement models (library crops, sw_library) in their own venv:
#   OneFormer Swin-L ADE20K  (shi-labs/oneformer_ade20k_swin_large, MIT)   per-pixel ADE20K labels
#   Mask2Former Swin-L ADE20K (facebook/mask2former-swin-large-ade-semantic) for the A/B
#   Depth Anything V2 Small  (depth-anything/Depth-Anything-V2-Small-hf, Apache-2.0)
#     Base / Large / Giant are CC-BY-NC-4.0: don't use them for project work.
# Usage: scripts/install_segdepth.sh [venv] [models]
set -euo pipefail
AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"
ENV="$AP_ROOT/envs/segdepth"
MODELS="$AP_ROOT/models"
PY="$ENV/bin/python"

stage_venv() {
  [ -x "$PY" ] || python3.12 -m venv "$ENV"
  "$PY" -m pip install -U pip setuptools wheel
  # Same torch as TRELLIS.2 and the VLM (validated on gfx1201).
  "$PY" -m pip install torch==2.13.0 torchvision --index-url https://download.pytorch.org/whl/rocm7.2
  "$PY" -m pip install "transformers>=4.57" accelerate pillow huggingface_hub scipy
}

stage_models() {
  for repo in shi-labs/oneformer_ade20k_swin_large facebook/mask2former-swin-large-ade-semantic \
              depth-anything/Depth-Anything-V2-Small-hf; do
    "$ENV/bin/hf" download "$repo" --local-dir "$MODELS/${repo#*/}"
  done
}

[ $# -gt 0 ] || set -- venv models
for s in "$@"; do echo "=== stage: $s"; "stage_$s"; done
