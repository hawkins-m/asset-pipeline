# CLAUDE.md

**[PLAN.md](PLAN.md) is the source of truth for the design** (goal, architecture, stages,
milestones). Read it before starting work. This file only covers the machine and the
working rules. If the two disagree on design, PLAN.md wins. On machine facts, this file
reflects what was actually verified (see "Drift from PLAN.md").

## Repo rules
- The repo holds only code, ComfyUI workflow JSONs, configs and docs.
- Never commit venvs, model weights, caches or generated outputs (images, meshes, GLBs).
  Those live on the storage drive under `$AP_ROOT` (below).
- Commit after each working step.
- Every model gets its own isolated venv. Don't install model deps into a shared env.

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

### ComfyUI: do not touch
- Installs: `~/Projects/AI/ComfyUI` and `~/Projects/AI/ComfyUI-H3`.
- Its Python env is `~/pytorch_env` (torch 2.12.0+rocm7.2).
- It runs on port 8188 (GPU 0, image generation) and 8189 (GPU 1, image-to-3D).
- **Never modify these directories or that env.** Talk to ComfyUI only over HTTP.

### Storage layout (outside the repo)
`AP_ROOT=/mnt/storage/asset-pipeline` (ext4 NVMe, 1.8 TB):

| Path | Holds |
|---|---|
| `envs/<model>/` | One venv per model. |
| `src/<model>/` | Upstream and fork checkouts plus patches applied. |
| `hf/` | `HF_HOME`: Hugging Face weights and cache. |
| `outputs/` | Generated images, meshes and GLBs. |

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
- **fp32 GEMMs with more than 524,288 rows silently return corrupt results** on gfx1201
  (rocBLAS and hipBLASLt; fp16/bf16 are fine). Chunk large fp32 matmuls.
  Upstream issue: ROCm/ROCm#6595.
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
- Pin a job to one GPU with `HIP_VISIBLE_DEVICES`. GPU 1 is ComfyUI's 3D GPU, so check
  whether ComfyUI is running before using it.

## TRELLIS.2 (stage 5) status
- Install with `scripts/install_trellis2.sh venv deps trellis nvdiffrast cumesh flexgemm ovoxel nvdiffrec verify`.
  The env goes to `$AP_ROOT/envs/trellis2` and weights to `$AP_ROOT/models/TRELLIS.2-4B`.
- Run with `scripts/trellis2_image_to_glb.py` (usage is in its docstring).
- Known issue: **512 mode is verified correct. The 1024 and 1024_cascade modes still lose
  geometry.** The raw mesh has F/V ≈ 1.3 where a healthy one is ≈ 2.0, and parts of the
  surface come out hollow. The cause is upstream of CuMesh, in the high-res decode or mesh
  extraction. It is probably another >2^19 / 2^20-row truncation.

## Drift from PLAN.md
PLAN.md lists ROCm 7.2.1, PyTorch 2.9.1 and Python 3.12. What is actually installed:
ROCm 7.2.4 runtime with hipcc 7.1.1, torch 2.12 in the ComfyUI env, and system Python 3.14
with 3.12 alongside.
