# Machine and gpu

Machine, storage layout, building HIP/torch extensions, ROCm/gfx1201 pitfalls and TRELLIS.2 install status. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## Machine (verified 2026-10-04)
- Ubuntu 26.04, Ryzen 9 9950X3D (32 threads), 64 GB RAM.
- 2× AMD Radeon AI PRO R9700, 32 GB each, RDNA4 **gfx1201**.
- ROCm runtime **7.2.4** at `/opt/rocm` (AMD packages).
- `hipcc` is Ubuntu's **7.1.1** package (`/usr/bin/hipcc`, Ubuntu clang 21). There is no
  AMD `hipcc` in `/opt/rocm/bin`. AMD's ROCm LLVM 22 lives at `/opt/rocm/llvm`.
- Default `python3` is **3.14**. Create venvs with `python3.12` explicitly.
- No sudo for the agent. No `uv`, `git-lfs` or system `ninja`; install them into the
  venv with pip where needed.
- Blender is the snap (`/snap/bin/blender`). Blender MCP is connected to Claude Code.

### Storage layout (outside the repo)
`AP_ROOT=/mnt/storage/asset-pipeline` (ext4 NVMe, 1.8 TB):

| Path | Holds |
|---|---|
| `envs/<model>/` | One venv per model. |
| `src/<model>/` | Upstream and fork checkouts plus patches applied. |
| `models/` | Model weights downloaded to a local dir (e.g. `TRELLIS.2-4B/`). |
| `hf/` | `HF_HOME`: Hugging Face weights and cache. |
| `inputs/` | Images to feed the pipeline (`trellis NAME` looks here). |
| `outputs/` | Generated images, meshes and GLBs. |
| `logs/` | Install and run logs. |

## Building HIP / torch extensions
- First run `scripts/setup_rocm_toolchain.sh` (one time, no sudo).
- Then `source scripts/rocm_build_env.sh` in every shell that builds extensions, and
  activate the target venv.
- Why: out of the box, torch would compile with Ubuntu's HIP 7.1 headers and clang 21
  against a ROCm 7.2 runtime. The script switches to AMD LLVM 22 and the 7.2.4 headers,
  sets the gfx1201 arch, and moves Triton and HF caches off the dirs ComfyUI shares.
  Comments in the script explain the details.

## ROCm / gfx1201 pitfalls
- Build HIP extensions with `PYTORCH_ROCM_ARCH=gfx1201` (and `GPU_ARCHS=gfx1201`).
  `rocm_build_env.sh` sets both.
- Most 3D-gen repos assume CUDA. Use ROCm forks and patch sets.
- No flash-attn. Use sdpa, or the Triton backend.
- **Tall GEMMs silently return corrupt results** on gfx1201 (rocBLAS and hipBLASLt alike).
  The row limit depends on dtype and output width N:
  - fp32: above 2^19 rows.
  - fp16/bf16 with small N (e.g. 8): above 2^20 rows.
  - fp16/bf16 with N=64: above 2^22 rows.
  - With a bias term, even rows below the limit come out wrong.
  - Sweep: `scripts/diag/gemm_rows_sweep.py`. Upstream issue: ROCm/ROCm#6595 (the fp32 case).
  - Fix: call `scripts/gfx1201_guard.py`'s `install()` in any torch process. It chunks
    `F.linear` (all `nn.Linear`) to 2^18 rows. It was the cause of the TRELLIS.2 1024-mode
    holes: the decoder's `to_subdiv` is an fp16 128→8 linear over 1.6M rows.
  - The guard does not cover raw `torch.mm/bmm/matmul`. Chunk those yourself when M may
    exceed 2^18.
- **`hipMemcpy2D` device-to-device copies only the first 2^20 rows** and still returns
  success; the remaining rows stay unwritten. Chunk 2D copies.
  - Repro: `scripts/diag/hip_memcpy2d_repro.hip`.
  - This is what silently cut TRELLIS.2 meshes off at 1,048,576 vertices/faces inside
    CuMesh. `install_trellis2.sh` patches CuMesh for it.
- **Never set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** on this stack. It
  produces silent NaNs.
- Silent truncation leaves no NaNs and no errors behind. To check a generated asset,
  look at it from several angles (`scripts/blender_check_glb.py`) and compare against a
  CPU reference (`scripts/diag/`).
- **Before any Unreal render, check VRAM on both GPUs** (`rocm-smi --showmeminfo vram`; card N = GPU N =
  Vulkan adapter N). If another process (the user's Blender, ComfyUI, the VLM) holds more than a
  few GB on the target GPU, wait or use the other one, and say which GPU was used.
  `ue.pick_gpu` (4 GB limit) does this for every `ue._render`; the desktop holds ~2 GB on GPU 0.
  The 2026-10-14 `VK_ERROR_DEVICE_LOST` crashes came from a Blender session on the same GPU.
- Pin a job to one GPU with `HIP_VISIBLE_DEVICES`. GPU 1 is ComfyUI's GPU (both instances), so check
  whether ComfyUI is running before using it.

## TRELLIS.2 (stage 5) status
- Install with `scripts/install_trellis2.sh venv deps trellis nvdiffrast cumesh flexgemm ovoxel nvdiffrec verify`.
  The env goes to `$AP_ROOT/envs/trellis2` and weights to `$AP_ROOT/models/TRELLIS.2-4B`.
- Run with `trellis IMAGE` (`scripts/trellis`, symlinked into `~/bin`). See USAGE.md.
- Verified modes: `1024_cascade` (the default) and `512`, each on two test images (the
  turret and crown examples). A healthy raw mesh has F/V ≈ 2.0, recorded in each run's
  JSON as `raw_faces / raw_vertices`.
- `1536_cascade` runs out of GPU memory in CuMesh `fill_holes` → `get_edges`. That is a
  genuine OOM error, not silent corruption. Not investigated yet.
- `1024` (non-cascade) has not been re-verified since the GEMM guard went in.
- CuMesh's HIP fixes are our own since 2026-10-15 (rocprim tuple in the radix-sort decomposer,
  nvcc-only flags dropped for cubvh; `stage_cumesh`). The installed build is that one; the
  previous build and source are in `$AP_ROOT/backups/cumesh-egore-2026-10-15` and
  `$AP_ROOT/src/CuMesh-egore-old`. On the turret (seed 42, 1024_cascade) the raw mesh is
  identical; final meshes differ by ~0.1% of faces, the same as two runs of one build
  (1,281 faces between repeats): GPU nondeterminism.
