# Vendor-neutral part of the build/run environment. Sourced by scripts/rocm_build_env.sh
# (AMD) and directly by scripts/trellis and scripts/install_trellis2.sh on CUDA (untested).
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/ap_root.sh"

export MAX_JOBS="${MAX_JOBS:-16}"

# Keep caches off the shared ~/.triton, ~/.cache (ComfyUI uses them).
export TRITON_CACHE_DIR="$AP_ROOT/cache/triton"
export HF_HOME="$AP_ROOT/hf"

# amd | nvidia | unknown. Same rule as asset_pipeline/gpu.vendor(); the nvidia branch is untested.
ap_gpu_vendor() {
  if [ -x /opt/rocm/bin/rocm-smi ] || command -v rocm-smi >/dev/null 2>&1; then echo amd
  elif command -v nvidia-smi >/dev/null 2>&1; then echo nvidia
  else echo unknown; fi
}
