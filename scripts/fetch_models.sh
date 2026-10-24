#!/usr/bin/env bash
# Download the ComfyUI model files the workflows load into your ComfyUI's models folder.
# A thin wrapper: the list, sizes and checksums are in config/models.toml, the download is
# `ap models download --feature comfy --required` (resume, checksums, gated-repo instructions).
#
# Usage: [COMFYUI_MODELS=~/ComfyUI/models] scripts/fetch_models.sh [--dry-run]
#   COMFYUI_MODELS: the folder for this run (default: [comfyui] models_dir from `ap setup`)
#
# FLUX.1 Redux dev is gated: accept its license on the model page and set a Hugging Face token
# (`hf auth login`, or the token in `ap setup`); the download says exactly what to do.
# Licences: see README.md. FLUX.1 [dev], Redux and the Union ControlNet are non-commercial.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
args=(models download --feature comfy --required)
case "${1:-}" in
  --dry-run|-n) args+=(--dry-run) ;;
  "") ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
[ -n "${COMFYUI_MODELS:-}" ] && export AP_COMFY_MODELS="${COMFYUI_MODELS/#\~/$HOME}"
"$REPO/scripts/ap" "${args[@]}"
echo "Check with: ap doctor"
