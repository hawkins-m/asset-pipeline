#!/usr/bin/env bash
# Install the crop-refinement models (library crops, sw_library) in their own venv:
#   OneFormer Swin-L ADE20K  (shi-labs/oneformer_ade20k_swin_large, MIT)   per-pixel ADE20K labels
#   Depth Anything V2 Small  (depth-anything/Depth-Anything-V2-Small-hf, Apache-2.0)
#     Base / Large / Giant are CC-BY-NC-4.0: don't use them for project work.
#   EXPERIMENTAL, not installed by default: Mask2Former Swin-L ADE20K
#   (facebook/mask2former-swin-large-ade-semantic, HF card "other"), only for the A/B in
#   scripts/diag/crop_refine_ab.py.
# Usage: scripts/install_segdepth.sh [venv] [models] [mask2former]
set -euo pipefail
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_root.sh"
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/torch_index.sh"
# AP_TORCH=rocm7.2 (default) or a CUDA tag such as cu128 (untested): see scripts/torch_index.sh.
ENV="$AP_ROOT/envs/segdepth"
MODELS="$AP_ROOT/models"
PY="$ENV/bin/python"

stage_venv() {
  [ -x "$PY" ] || python3.12 -m venv "$ENV"
  "$PY" -m pip install -U pip setuptools wheel
  # Same torch as TRELLIS.2 and the VLM (validated on gfx1201).
  "$PY" -m pip install torch==2.13.0 torchvision --index-url "$TORCH_INDEX"
  "$PY" -m pip install "transformers>=4.57" accelerate pillow huggingface_hub scipy
}

stage_models() {        # the manifest's files, checksums and resume (config/models.toml)
  "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap" models download oneformer-ade20k depth-anything-v2-small
}

stage_mask2former() {   # experimental A/B only
  "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap" models download mask2former-ade20k
}

[ $# -gt 0 ] || set -- venv models
for s in "$@"; do echo "=== stage: $s"; "stage_$s"; done
