# Source this: picks the PyTorch wheel index from AP_TORCH.
#   AP_TORCH=rocm7.2 (default, the tested AMD path) | cu12x (e.g. cu128, cu126: NVIDIA, UNTESTED)
# Sets TORCH_INDEX (wheel index URL) and TORCH_KIND (rocm | cuda).
AP_TORCH="${AP_TORCH:-rocm7.2}"
case "$AP_TORCH" in
  rocm7.2) TORCH_KIND=rocm ;;
  cu12[0-9]|cu12[0-9][0-9]) TORCH_KIND=cuda ;;   # untested
  *) echo "AP_TORCH must be rocm7.2 or a CUDA tag like cu128 / cu126 (got: $AP_TORCH)" >&2; exit 1 ;;
esac
TORCH_INDEX="https://download.pytorch.org/whl/$AP_TORCH"
if [ "$TORCH_KIND" = cuda ]; then
  echo "WARNING: CUDA path ($AP_TORCH): untested. Only the ROCm path has been run." >&2
fi
