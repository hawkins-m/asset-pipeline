#!/usr/bin/env bash
# Download the ComfyUI model files the demo workflows load (workflows/*.json; the list the
# check in `ap preflight` reads from ComfyUI) into a ComfyUI models directory.
#
# Usage: COMFYUI_MODELS=~/ComfyUI/models scripts/fetch_models.sh [--dry-run]
#
# Needs the `hf` CLI:  pip install -U huggingface_hub
# FLUX.1 Redux dev (black-forest-labs) is a gated repo: accept its terms on the model page and
# run `hf auth login` first. Files already present are skipped; nothing else in the models
# directory is touched. Source repos checked against the Hugging Face API on 2026-10-15.
#
# Licences: see README.md. FLUX.1 [dev], Redux and the Union ControlNet are non-commercial.
set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run|-n) DRY=1 ;;
  "") ;;
  *) sed -n 2,5p "$0"; exit 2 ;;
esac
: "${COMFYUI_MODELS:?set COMFYUI_MODELS to your ComfyUI models directory (e.g. ~/ComfyUI/models)}"
MODELS="${COMFYUI_MODELS/#\~/$HOME}"
[ -d "$MODELS" ] || { echo "not a directory: $MODELS" >&2; exit 1; }
if [ "$DRY" = 0 ] && ! command -v hf >/dev/null; then
  echo "hf not found: pip install -U huggingface_hub (and 'hf auth login' for gated repos)" >&2; exit 1
fi

# folder | file name ComfyUI loads | HF repo | path inside the repo
FILES=(
  "checkpoints|flux1-dev-fp8.safetensors|Comfy-Org/flux1-dev|flux1-dev-fp8.safetensors"
  "style_models|flux1-redux-dev.safetensors|black-forest-labs/FLUX.1-Redux-dev|flux1-redux-dev.safetensors"
  "clip_vision|sigclip_vision_patch14_384.safetensors|Comfy-Org/sigclip_vision_384|sigclip_vision_patch14_384.safetensors"
  # The repo file is diffusion_pytorch_model.safetensors; it is saved under the name the workflows use.
  "controlnet|flux1-dev-controlnet-union-pro-2.0.safetensors|Shakker-Labs/FLUX.1-dev-ControlNet-Union-Pro-2.0|diffusion_pytorch_model.safetensors"
  "checkpoints|sam3.1_multiplex_fp16.safetensors|Comfy-Org/sam3.1|checkpoints/sam3.1_multiplex_fp16.safetensors"
)

for row in "${FILES[@]}"; do
  IFS='|' read -r dir name repo src <<<"$row"
  dest="$MODELS/$dir/$name"
  if [ -e "$dest" ]; then echo "have      $dir/$name"; continue; fi
  if [ "$DRY" = 1 ]; then echo "would get $dir/$name  <- $repo:$src"; continue; fi
  echo "fetching  $dir/$name  <- $repo:$src"
  mkdir -p "$MODELS/$dir"
  tmp="$(mktemp -d "$MODELS/$dir/.fetch.XXXXXX")"
  if hf download "$repo" "$src" --local-dir "$tmp"; then
    mv "$tmp/$src" "$dest"; rm -rf "$tmp"
  else
    rm -rf "$tmp"
    echo "failed: $repo:$src (gated repo? run 'hf auth login' and accept its terms)" >&2; exit 1
  fi
done
echo "done. Check with: ap doctor"
