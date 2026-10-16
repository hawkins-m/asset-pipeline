#!/usr/bin/env bash
# Install TRELLIS.2 into its own venv on ROCm / gfx1201.
#
# Recipe: github.com/bioritmovideo/trellis2-rocm-gfx1201 (validated on R9700) and
# nvdiffrast from github.com/Painter3000/amd-nvdiffrast-rocm72-gfx1201. The CuMesh HIP
# build fixes are our own, derived from compiler errors (see stage_cumesh).
#
# Usage: scripts/install_trellis2.sh <stage>...
# Stages (in order): venv deps trellis nvdiffrast cumesh flexgemm ovoxel nvdiffrec verify
# Every stage can be re-run on its own.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/rocm_build_env.sh"

ENV="$AP_ROOT/envs/trellis2"
SRC="$AP_ROOT/src"
TRELLIS="$SRC/TRELLIS.2"
PATCHES="$SRC/trellis2-rocm-gfx1201"
PY="$ENV/bin/python"
PIP="$PY -m pip"
TORCH_INDEX=https://download.pytorch.org/whl/rocm7.2

clone() {  # clone <url> <dir> [git args...]
  local url=$1 dir=$2; shift 2
  [ -d "$dir/.git" ] || git clone "$@" "$url" "$dir"
}

stage_venv() {
  [ -x "$PY" ] || python3.12 -m venv "$ENV"
  $PIP install -U pip setuptools wheel ninja
  # Exact stack validated for nvdiffrast + TRELLIS.2 on gfx1201.
  $PIP install torch==2.13.0 torchvision --index-url "$TORCH_INDEX"
}

stage_deps() {
  $PIP install imageio imageio-ffmpeg tqdm easydict opencv-python-headless trimesh \
    transformers tensorboard pandas lpips zstandard kornia timm einops OpenEXR \
    huggingface_hub pillow plyfile
  $PIP install "git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8"
}

stage_trellis() {
  clone https://github.com/microsoft/TRELLIS.2.git "$TRELLIS" --recursive
  clone https://github.com/bioritmovideo/trellis2-rocm-gfx1201 "$PATCHES"
  cd "$TRELLIS"
  if git diff --quiet; then git apply "$PATCHES/patches/trellis2_gfx1201.diff"; fi
  git submodule update --init --recursive
}

stage_nvdiffrast() {
  local d="$SRC/amd-nvdiffrast-rocm72-gfx1201"
  local nv="$SRC/nvdiffrast-build/nvdiffrast"
  clone https://github.com/Painter3000/amd-nvdiffrast-rocm72-gfx1201 "$d"
  clone https://github.com/NVlabs/nvdiffrast.git "$nv" -b v0.4.0
  # torch_common.inl (.inl) is skipped by PyTorch's hipify, so it keeps including the
  # CUDA framework.h -> ATen/cuda -> cuda_runtime_api.h, which doesn't exist here.
  local inl="$nv/csrc/torch/torch_common.inl"
  if ! grep -q __HIP_PLATFORM_AMD__ "$inl"; then
    sed -i 's|^#include "../common/framework.h"$|#ifdef __HIP_PLATFORM_AMD__\n#include "../common/framework_hip.h"\n#else\n#include "../common/framework.h"\n#endif|' "$inl"
  fi
  PATH="$ENV/bin:$PATH" VIRTUAL_ENV="$ENV" "$PY" "$d/amd_nvdiffrast_setup.py" \
    --workdir "$SRC/nvdiffrast-build" --venv "$ENV" --rocm-path /opt/rocm \
    --arch gfx1201 --max-jobs "$MAX_JOBS" --validation quick --skip-clone
}

stage_cumesh() {
  local d="$SRC/CuMesh"
  clone https://github.com/visualbruno/CuMesh.git "$d" --recursive
  cd "$d"
  # HIP build fixes, each from a compiler error on ROCm 7.2 / clang 22. Idempotent.
  # 1. clean_up.cu: hipCUB's radix-sort decomposer returns rocprim::tuple (not
  #    cuda::std::tuple), whose converting constructor is explicit.
  if ! grep -q rocprim src/clean_up.cu; then
    python3 - src/clean_up.cu <<'EOF'
import sys; p = sys.argv[1]; s = open(p).read()
old = "    __host__ __device__ ::cuda::std::tuple<int&, int&, int&> operator()(int3& key) const\n"
new = """#ifdef __HIP_PLATFORM_AMD__
    __host__ __device__ ::rocprim::tuple<int&, int&, int&> operator()(int3& key) const
#else
    __host__ __device__ ::cuda::std::tuple<int&, int&, int&> operator()(int3& key) const
#endif
"""
assert s.count(old) == 1
s = s.replace(old, new)
old = "        return {key.x, key.y, key.z};\n"
new = """#ifdef __HIP_PLATFORM_AMD__
        return ::rocprim::tuple<int&, int&, int&>{key.x, key.y, key.z};
#else
        return {key.x, key.y, key.z};
#endif
"""
assert s.count(old) == 1
open(p, "w").write(s.replace(old, new))
EOF
  fi
  # 2. setup.py: --extended-lambda and --expt-relaxed-constexpr are nvcc-only; clang rejects
  #    them when building the cubvh extension.
  if ! grep -q 'nvcc-only options' setup.py; then
    python3 - setup.py <<'EOF'
import sys; p = sys.argv[1]; s = open(p).read()
old = """                "nvcc": ["-O3","-std=c++20"] + cc_flag + [
                    "--extended-lambda",
                    "--expt-relaxed-constexpr",
"""
new = """                "nvcc": ["-O3","-std=c++20"] + cc_flag + ([] if IS_HIP else [
                    # nvcc-only options; hipcc/clang rejects them
                    "--extended-lambda",
                    "--expt-relaxed-constexpr",
                ]) + [
"""
assert s.count(old) == 1
open(p, "w").write(s.replace(old, new))
EOF
  fi
  # ROCm 7.2 on gfx1201: hipMemcpy2D device-to-device silently copies only the first
  # 2^20 rows (returns success). CuMesh::init uses it for vertices/faces, so any mesh
  # over 1,048,576 rows lost the rest (zeros) -> missing geometry. Chunk the copies.
  if ! grep -q cumesh_memcpy2d src/io.cu; then
    python3 - src/io.cu <<'EOF'
import sys; p = sys.argv[1]; s = open(p).read()
helper = '''namespace cumesh {

// hipMemcpy2D on ROCm 7.2 (gfx1201) copies at most 2^20 rows and reports success.
static cudaError_t cumesh_memcpy2d(void* dst, size_t dpitch, const void* src, size_t spitch,
                                   size_t width, size_t height, cudaMemcpyKind kind) {
    const size_t chunk = size_t(1) << 19;
    for (size_t r = 0; r < height; r += chunk) {
        size_t h = height - r < chunk ? height - r : chunk;
        cudaError_t e = cudaMemcpy2D((char*)dst + r * dpitch, dpitch,
                                     (const char*)src + r * spitch, spitch, width, h, kind);
        if (e != cudaSuccess) return e;
    }
    return cudaSuccess;
}
'''
assert s.count("namespace cumesh {") == 1
s = s.replace("namespace cumesh {", helper, 1)
s = s.replace("CUDA_CHECK(cudaMemcpy2D(", "CUDA_CHECK(cumesh_memcpy2d(")
open(p, "w").write(s)
EOF
  fi
  local eigen=third_party/cubvh/third_party/eigen
  [ -f "$eigen/Eigen/Dense" ] || { rm -rf "$eigen"; git clone --depth 1 https://gitlab.com/libeigen/eigen.git "$eigen"; }
  PATH="$ENV/bin:$PATH" $PIP install . --no-build-isolation --no-deps -v
}

stage_flexgemm() {
  local d="$SRC/FlexGEMM"
  clone https://github.com/JeffreyXiang/FlexGEMM.git "$d" --recursive
  cd "$d"
  # --no-deps: its triton>=3.2 requirement would pull CUDA triton over triton-rocm.
  PATH="$ENV/bin:$PATH" $PIP install . --no-build-isolation --no-deps -v
  local cfg
  cfg=$("$PY" -c "import flex_gemm,os;print(os.path.dirname(flex_gemm.__file__))")/kernels/triton/spconv/config.py
  if grep -q '^allow_tf32 = True$' "$cfg"; then
    sed -i '1s/^/import torch\n/' "$cfg"
    sed -i 's/^allow_tf32 = True$/allow_tf32 = not getattr(torch.version, "hip", None)  # TF32 is NVIDIA-only/' "$cfg"
  fi
}

stage_ovoxel() {
  cd "$TRELLIS/o-voxel"
  PATH="$ENV/bin:$PATH" $PIP install . --no-build-isolation --no-deps -v
}

stage_nvdiffrec() {
  local d="$SRC/nvdiffrec"
  clone https://github.com/JeffreyXiang/nvdiffrec.git "$d" -b renderutils
  cd "$d"
  local c=nvdiffrec_render/renderutils/c_src
  if git diff --quiet; then
    sed -i "s/'-lcuda', '-lnvrtc'//g" setup.py
    sed -i 's/0xFFFFFFFF/(unsigned long long)0xFFFFFFFF/g' "$c/loss.cu"
    for f in common torch_bindings; do
      [ -f "$c/$f.cpp" ] && git mv "$c/$f.cpp" "$c/$f.cu" && sed -i "s|$f.cpp|$f.cu|" setup.py
    done
    sed -i 's|#include <ATen/cuda/CUDAContext.h>|#ifdef __HIP_PLATFORM_AMD__\n#include <ATen/hip/HIPContext.h>\n#include <ATen/hip/HIPUtils.h>\n#else\n#include <ATen/cuda/CUDAContext.h>\n#endif|' "$c/torch_bindings.cu"
    sed -i 's|#include <ATen/cuda/CUDAUtils.h>||' "$c/torch_bindings.cu"
    sed -i 's/cudaError_t/hipError_t/g; s/cudaGetLastError/hipGetLastError/g' "$c/torch_bindings.cu"
  fi
  PATH="$ENV/bin:$PATH" $PIP install . --no-build-isolation --no-deps -v
}

stage_verify() {
  $PIP list 2>/dev/null | grep -i -E '^(torch|triton|nvdiffrast|cumesh|flex.gemm|o.voxel|nvdiffrec)'
  cd "$TRELLIS"
  ATTN_BACKEND=sdpa SPARSE_ATTN_BACKEND=sdpa SPARSE_CONV_BACKEND=flex_gemm \
    "$PY" -c "import nvdiffrast.torch, cumesh, flex_gemm, o_voxel, nvdiffrec_render, trellis2; print('all imports OK')"
}

[ $# -gt 0 ] || { sed -n 2,10p "$0"; exit 1; }
for s in "$@"; do echo "=== stage: $s"; ( "stage_$s" ); done
