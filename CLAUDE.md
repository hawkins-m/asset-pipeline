# CLAUDE.md

**[PLAN.md](PLAN.md) is the source of truth for the design** (goal, architecture, stages,
milestones). Read it before starting work. This file only covers the machine and the
working rules. If the two disagree on design, PLAN.md wins. On machine facts, this file
reflects what was actually verified.

## Repo rules
- The repo holds only code, ComfyUI workflow JSONs, configs and docs.
- Never commit venvs, model weights, caches or generated outputs (images, meshes, GLBs).
  Those live on the storage drive under `$AP_ROOT` (below).
- Commit after each working step.
- Every model gets its own isolated venv. Don't install model deps into a shared env.
- **Never use paid ComfyUI API nodes (`comfy_api_nodes`) or any other paid API without
  asking first.** That includes Gemini, Claude/Anthropic, OpenAI and hosted
  image/3D services. Ask before each new use, even when a key is configured, and that
  includes tests. Local models are fine.

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
- Each has its own venv:
  - Main (`run_comfy.sh`, port 8188): `~/Projects/AI/ComfyUI/venv`, torch 2.9.1+rocm7.2.1.
  - H3 (`run_comfy_h3.sh`, port 8189): `~/Projects/AI/ComfyUI-H3/venv`, torch 2.13.0+rocm7.2.
  - `~/pytorch_env` (torch 2.12) is a separate env, not ComfyUI's.
- **Both instances are pinned to GPU 1** (`HIP_VISIBLE_DEVICES=1` in the launchers). GPU 0
  carries the desktop and is where TRELLIS and other local 3D work run.
- **Never modify these directories or that env.** Talk to ComfyUI only over HTTP.
- Starting the main instance is OK whenever it's needed. Run
  `~/Projects/AI/ComfyUI/run_comfy.sh` in the background, log to
  `$AP_ROOT/logs/comfyui.log`, and check it with `ap comfy-check`.
- `run_comfy.sh` sets `PYTORCH_HIP_ALLOC_CONF=expandable_segments:True`, but ComfyUI's
  torch logs "expandable_segments not supported on this platform". So it's a no-op
  there, unlike the NaN problem it caused in the TRELLIS env.

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
- Pin a job to one GPU with `HIP_VISIBLE_DEVICES`. GPU 1 is ComfyUI's GPU (both instances), so check
  whether ComfyUI is running before using it.

## Vision LLM (stage 1)
- Providers per project (`ap set PROJECT --llm local|gemini|claude`). Default `local`:
  Qwen3-VL-8B-Instruct, bf16, env `$AP_ROOT/envs/qwen-vl` (torch 2.13.0+rocm7.2,
  transformers 5.18), weights `$AP_ROOT/models/Qwen3-VL-8B-Instruct`. Install with
  `scripts/install_qwen_vl.sh`.
- **Default local VLM is Qwen3-VL-32B Q5_K_M (llama.cpp, :8711); the 8B (:8710) is the
  fallback** (config `[local]`). `asset_pipeline/vlm.py` manages it: started on demand by
  stage 1, state in `$AP_ROOT/logs/vlm.json`, idle unload after 600 s for both.
- **VLM and TRELLIS never share a GPU.** `scripts/trellis` writes
  `logs/trellis-gpu<N>-<pid>.lock`, then `ap vlm down --gpu N` (waits for a request in
  flight); `vlm.up()` refuses a GPU with a live lock. Verified live 2026-10-05.
- Verified 2026-10-05: structured scene analysis on alpine-market validates first try,
  ~25 s warm (1.4k tokens in, ~560 out, ~23 tok/s).
- Stage 1 plans (verified 2026-10-05, alpine-market): both scenes validated first try,
  45–70 s warm. Without the explicit "parts are not assets" and "kit only when ≥2 pieces
  share it" rules, Qwen split stalls into poles/awnings/shingles (23 assets, 173 s) and
  gave every asset its own kit. Its `bbox_2d` boxes are rough (some tight, many offset or
  oversized); refine with SAM 3.1 before relying on them.
- Larger local VLMs (verified 2026-10-05): llama.cpp's prebuilt **Vulkan** build (RADV,
  `scripts/install_llamacpp_vlm.sh`, b11433) runs Qwen3-VL GGUFs on gfx1201; Vulkan0 =
  GPU 0. Serve with `scripts/llamacpp_vlm_server.sh`, backend `llm/llamacpp.py`.
  - 32B dense fits one card only at ~5 bit: Q5_K_M peaks 26.8 GB on GPU 0 at 8k ctx
    (Q8_0 34.8 GB and FP8 35.5 GB don't fit). 24 tok/s, ~110 s per scene.
  - 30B-A3B Q6_K: 27.4 GB, ~130 tok/s, but loops at greedy decoding (repeats assets
    until max_tokens) and still duplicates heavily with Qwen's recommended sampling.
  - 8B Q8_0 on llama.cpp: 65 tok/s (3x transformers) but worse plans than 8B bf16.
  - A/B on 4 alpine-market scenes, blind-judged against a reference written first
    (`$AP_ROOT/outputs/vlm_ab/`, `scripts/diag/vlm_ab*.py`): 32B Q5 greedy 18/20,
    8B bf16 13/20, 8B Q8 9.5/20, 30B-A3B <=8.5/20. LLM box hits SAM object (IoU>=0.3):
    32B 79%, 8B bf16 36%.
  - The 32B listed a dominant mountain (1500x2000x3000 m). Fixed by a "Do NOT list"
    prompt block (parts, landscape/backdrop, people) plus dropping any asset > 200 m
    without a retry (`plan.dropped`). Part violations on the 4 scenes: 10 -> 3.
- SAM 3.1 finding nothing for a noun fails the ComfyUI graph (empty batch -> IndexError
  in the image output node). `segment.detect` treats that as no detections.
- Paid backends (Gemini, Claude, Gemini images) raise `PaidAPIBlocked` unless
  `AP_ALLOW_PAID_APIS=1` is set for that run. Never set it yourself without the user's OK.
  Their tests use mocked SDKs only.

## Style anchor (stage 0) findings
- Flux Redux conditions on content as well as style. This was measured with a 3-image
  ukiyo-e anchor and the prompt "wooden barrel on white", seed 7 (`attn_bias`, total
  strength split across images):

  | Total strength | Result |
  |---|---|
  | ≤ 0.06 | Barely stylised |
  | ~0.08 | Subject kept, mildly stylised (flatter, outlined), palette not transferred |
  | ≥ 0.12 | Subject replaced by the anchor's scene |

  `multiply` mode at 0.15 also replaced the subject. Treat Redux as weak style plus
  content leakage, not a clean style transfer.
- **Anchors of single objects on white leak far less** (`scripts/diag/style_anchor_ab.py`).
  With 3 lantern-on-white anchors plus `style_text`, the barrel was kept at total 0.08
  and at 0.15, and took on the anchor set's look (flat shading, bold outlines, warm
  palette, cream ground). So stage 0 should generate isolated-object concepts by default.
  Scene anchors need ≤ 0.04 when combined with `style_text`.
- **The safe strength depends on the anchor set.** A 4-image set (barrel, planter, two
  market stalls) at 0.12 turned "wooden hand cart" into a stall-cart with an awning. At
  0.08 produce remained; at 0.05 the cart was clean and in the anchor style. Default is
  0.06. Distinctive multi-part objects (stalls) carry more content than simple ones.
- Side effect: object anchors plus text pull the anchors' cream background into
  "plain white background" prompts. Stage 2 must prompt hard for pure white, and stage 3
  cuts out regardless.

## Reference sheets (stage 2) findings
- Flux.1-dev (+ Redux anchor at the project's strength) reliably draws three copies of
  one object side by side on white, consistent in shape, colour and trim, but ignores
  "front / side / back": mostly three near-identical three-quarter views (tested
  2026-10-05 on barrel, stall, house; spelling out the rotations didn't help). The
  anchor didn't break the three-up layout.
- The explicit "LEFT/MIDDLE/RIGHT ... no ground, no shadow" wording gave cleaner white
  than the "character turnaround sheet" wording (no sand/dirt patches).
- ~55 s per 1.2 MP sheet on GPU 1. No local multi-view model is installed in ComfyUI
  (the Kontext/Qwen-Image edit nodes there are paid API nodes).

## Views and review (stages 3-4) findings
- SAM 3.1 finds the three views on a sheet reliably (31/32 alpine-market sheets, 2026-10-05)
  but its masks drop white or thin parts (snow on a shrub, the flowers in a planter). Views
  are therefore cut as full-height bands between neighbouring views, trimmed and kept on
  white; TRELLIS removes the background itself (BiRefNet).
- Pillow 12: an image made with `Image.fromarray` shares the array read-only and
  `ImageDraw.floodfill` silently does nothing on it. `.copy()` first.
- UI jobs: lanes "comfy" (GPU 1) and "gpu0"; Stop = `JobQueue.cancel` -> per-job
  cancellers (ComfyUI: `POST /queue {"delete"}` if pending, `POST /interrupt {"prompt_id"}`
  if running; TRELLIS: killpg + remove `logs/trellis-gpu<N>-<pid>.lock`). Verified live:
  another client's prompt was untouched; GPU 0 back to baseline 2 s after stopping TRELLIS.
- A scratch AP_ROOT for UI tests needs symlinks to `envs`, `models`, `tools`, `hf`, `src`,
  since `scripts/trellis` finds its env through AP_ROOT.

## Hero orbit (Wan 2.2) prototype findings (2026-10-05)
- `workflows/wan22_i2v_orbit.json`: Wan 2.2 I2V 14B fp8 high/low + lightx2v 4-step LoRAs,
  640x640, 81 frames: ~150-180 s per orbit on GPU 1 (ComfyUI swaps Flux out; the next
  Flux job reloads it).
- Orbits are visually consistent (same object, centred, same size, white kept). The
  default "slow turntable" prompt turned a stall only ~180 deg in 81 frames; "rotates
  quickly through one complete 360-degree turn in five seconds ..." gave a turn that
  returns to the start (similarity to frame 0: 0.98 at frame ~79).
- Image-only frame angles are NOT reliable: Wan eases in/out (frame 20 of 80 was ~45 deg,
  frame 40 ~90 deg), may swing back instead of completing the turn, and front/back-similar
  objects (stalls, gable houses) fool both the loop-closure and the mirror test.
- Wan's background is off-white (~250) with VAE specks on the frame edge: measure the
  background from the border and ignore a 4 px frame (`s5_hero._silhouette`).

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
