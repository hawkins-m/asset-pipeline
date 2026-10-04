# Source this before building HIP/torch extensions:  source scripts/rocm_build_env.sh
#
# Why it exists (see CLAUDE.md "Machine"):
# - /opt/rocm is AMD ROCm 7.2.4 but has no hipcc; the only hipcc is Ubuntu's 7.1.1
#   (/usr/bin/hipcc, clang 21). torch then picks ROCM_HOME=/usr and would compile
#   against 7.1 headers. A shim ROCM_HOME mirrors /opt/rocm and adds bin/hipcc, and
#   HIP_CLANG_PATH points the hipcc driver at AMD's LLVM 22.
# - AMD's lld (built for Ubuntu 24.04) needs libxml2.so.2, which 26.04 dropped; the
#   24.04 libxml2 + ICU 74 are unpacked into a private compat dir.
# Both live under $AP_ROOT/tools and are created by scripts/setup_rocm_toolchain.sh.

export AP_ROOT="${AP_ROOT:-/mnt/storage/asset-pipeline}"

export ROCM_HOME="$AP_ROOT/tools/rocm-shim"
export ROCM_PATH=/opt/rocm
export HIP_PATH=/opt/rocm
export HIP_CLANG_PATH=/opt/rocm/llvm/bin
export HIP_DEVICE_LIB_PATH=/opt/rocm/amdgcn/bitcode
# Ubuntu's hipcc adds no ROCm include path, so clang falls back to /usr/include/hip (7.1).
export HIPCC_COMPILE_FLAGS_APPEND="--rocm-path=/opt/rocm -isystem /opt/rocm/include"
export LD_LIBRARY_PATH="$AP_ROOT/tools/compat-lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

export PYTORCH_ROCM_ARCH=gfx1201
export GPU_ARCHS=gfx1201
export MAX_JOBS="${MAX_JOBS:-16}"

# Keep caches off the shared ~/.triton, ~/.cache (ComfyUI uses them).
export TRITON_CACHE_DIR="$AP_ROOT/cache/triton"
export HF_HOME="$AP_ROOT/hf"
