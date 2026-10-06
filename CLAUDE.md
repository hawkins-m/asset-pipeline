# CLAUDE.md

**[PLAN.md](PLAN.md) is the source of truth for the design** (goal, architecture, stages,
milestones). Read it before starting work. This file only covers the machine and the
working rules. If the two disagree on design, PLAN.md wins. On machine facts, this file
reflects what was actually verified.

## Repo rules
- The repo holds only code, ComfyUI workflow JSONs, configs and docs.
- **This repo is public; projects are private.** Never commit project-specific content:
  briefs, layouts, shot lists, greyboxes, generated media, project names or brief terms.
  Keep CLAUDE.md, PLAN.md and USAGE.md generic (placeholders like `my-project`).
  - Project content lives in `$AP_ROOT/projects/<slug>/`, and each project folder is its
    own private git repo (with its own `.gitignore` for generated media).
  - Safety net: `git config core.hooksPath scripts/git-hooks`. Its pre-commit hook refuses
    staged media, project-shaped files, any project slug, and terms listed in
    `$AP_ROOT/private-terms.txt`. Don't bypass it.
  - Sample and test layouts in code must be neutral (`sw_site.starter_layout()`).
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
- Verified 2026-10-05: structured scene analysis on a test project validates first try,
  ~25 s warm (1.4k tokens in, ~560 out, ~23 tok/s).
- Stage 1 plans (verified 2026-10-05 on a test project): both scenes validated first try,
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
  - A/B on 4 test scenes, blind-judged against a reference written first
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
- SAM 3.1 finds the three views on a sheet reliably (31/32 test sheets, 2026-10-05)
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

## Hero: render matching and Hunyuan3D-2mv findings (2026-10-05, experimental)
- Render matching (`s5_hero.match_angles`, `scripts/blender_turntable.py`) is correct on
  consistent data (synthetic box: eased full turn recovered within 10 deg, front/back
  told apart) but FAILS on real Wan orbits (stall, house): the track stays within
  ~25-50 deg of the start. Two reasons: Wan's "turn" barely changes the silhouette (house
  width/height 1.08-1.24 over the orbit vs 1.15-1.40 for the mesh's real rotation), and
  the mesh's unseen sides (TRELLIS) and the orbit's (Wan) are independent inventions, so
  only the front is shared.
- Hunyuan3D-2mv turbo: `ComfyUI-app/models/checkpoints/hunyuan3d-dit-v2-mv-turbo.safetensors`
  (model file only, user's OK 2026-10-05; contains conditioner + model + vae), workflow
  `workflows/hunyuan3d_mv.json`. ~30-40 s per shape on GPU 1, untextured.
  - On the stall with frames picked by eye (front 0, left 20, back 40, right 60), the
    4-view shape was cleaner from the sides than front-only; swapping left/right gave a
    visible conflict blob, so a wrong side assignment shows. All Hunyuan shapes had a
    ground slab (floor shading in the frames) and thin stray spikes; single-image TRELLIS
    stayed more detailed (and textured).
  - License: Tencent Hunyuan Community License: not licensed in the EU, UK or South
    Korea; >1M MAU needs a separate licence.
- Running GPU experiments while the user works: wait for an idle ComfyUI queue and for
  GPU 0 to be free (VLM down, no trellis locks); TRELLIS for scratch work via the real
  `scripts/trellis` with `--out-dir` in scratch, so its GPU claim is visible to the UI.

## Cleanup (stage 6) findings (2026-10-05)
- **Snap Blender does nothing when its stdout is a regular file** (exit 0, no output, the
  script's files never written). Pipe its output (`subprocess.PIPE`) and copy it to the log.
  `tests/test_stage6.py::test_module_launches_real_blender` guards this.
- glTF splits vertices along UV seams, so an imported TRELLIS mesh is many islands;
  decimating unwelded islands shrinks them and opens cracks (bake speckles). Weld
  (`remove_doubles`, 1e-5 of the size) before decimating.
- Budgets measured in Cycles (Workbench previews ignore normal maps and mislead): a stall
  warped at 15k and was close to the original at 30k; a house was fine at 30k.
- QuadriFlow rejects TRELLIS meshes as non-manifold even after a voxel remesh plus
  non-manifold repair; it works headless on clean meshes. Kept behind `--quad`.
- TRELLIS output faces glTF +Z (Blender -Y) and is ~1 unit tall; it can come out as a flat
  card (a barrel 4 mm deep) and with proportions far from the plan (house 2.3x deeper):
  the cleanup report warns about both.

## World mode: greybox and shot passes (verified 2026-10-05)
- Headless Blender 5.2 (CPU) builds an ~80-slot / ~4.4k-piece greybox in about 4 s.
  Pieces with the same primitive share one mesh, so kit pieces stay instanceable.
- The data passes are exact with Cycles on the CPU at 1 sample, with:
  - a box filter of width 0.01;
  - `dither_intensity = 0`;
  - the Raw view transform;
  - an emission-only material override.

  The ID pass then decodes with 0 unknown pixels, and the ID and depth hit masks agree
  pixel for pixel. Any of AA, dither or a view transform blends slot colours into
  colours nobody has.
- Depth is stored as 16-bit camera Z over 10 km (15 cm steps): the sea plane reaches
  about 5 km. Sky is 0.
- About 3.5 s per shot for all four passes at 1344x768.
- A rebuild always changes the .blend's hash, even from the same layout, so every shot's
  passes show as stale afterwards. Re-render them.
- Kit modularity: colonnades snap to whole modules, and a stoa's bay angle is module /
  radius. Without that, every building had its own entablature length (no reuse).

## Concept frames (world mode) findings (2026-10-06)
- ControlNet Union Pro 2.0 (`flux1-dev-controlnet-union-pro-2.0.safetensors`) and the
  BFL `flux1-depth-dev-lora.safetensors` both load in ComfyUI 0.24 with core nodes.
  Union needs the VAE input on `ControlNetApplyAdvanced`. About 35 s per 1344x768 frame
  at 28 steps on GPU 1.
- A/B (`scripts/diag/frames_ab.py`, 3 shots: a domed rotunda at eye level, a wide coastal
  aerial, a curved colonnade beside a garden; same seed and prompt). Edge match
  (chance-corrected, see `s0_frames.edge_match`) means:

  | Variant | Match | Look |
  |---|---|---|
  | Union depth 0.8, end ≥ 0.6 (+ canny 0.4) | 0.88–0.93 | Greybox shows through: flat grey ground, plain masses, desaturated; the sea became a concrete wall |
  | Union depth 0.6/0.6 + canny 0.3/0.5 | 0.79 | Layout exact, but surfaces flat and garden turned to paving |
  | **Union depth 0.6/0.6 alone** | 0.67 | **Best**: layout and scale hold; planting, sky and light come back. The default. |
  | Union depth 0.4 (any end) | 0.50–0.72 | Style good, layout and scale drift (a rotunda shrank behind an invented arch) |
  | Depth LoRA 0.65 / 0.85 | 0.17 / 0.37 | Redesigns or relocates buildings: unusable for one consistent place |

  Canny edges from the greybox are what flatten surfaces: they ask for the greybox's
  plain planes.
- Cross-shot references (`--ref`, an approved wide frame as extra Redux image at total
  +0.08), tried on two medium shots, same seeds:
  - they carry palette, light and planting across shots (the mediums took the wide
    frame's warm backlight and planted parterres: clearly "the same city");
  - they also carry the reference's sky: a hazy, sun-filled sky turned a large dome
    into a translucent glow, and a smaller invented dome took its place.

  Keep it opt-in. Try a lower strength (≈0.04) or a reference with a clear sky before
  making it a default.
- Wide aerials read a plateau's edge (a depth step around the city) as a circular ring
  wall, and a sparse layout makes wide shots look like a scale model.
- The edge score must be chance-corrected: raw recall gave random noise 0.96. Sobel
  edges at the top 12%, dilated 2 px, cover most of a busy image.
- Shell gotcha: `pgrep -f`/`pkill -f` with a script path also match the calling
  shell's own command line (a wait loop never ends; pkill kills itself). Match on
  something the caller doesn't contain, or use PIDs.

## Unreal Engine 5.8.3 (world mode delivery) findings (2026-10-06)
- Editor: `/mnt/storage/UnrealEngine/5.8.3/Engine/Binaries/Linux/UnrealEditor(-Cmd)`
  (config `[unreal] editor_cmd`). Python 3.11.8 inside.
- UE projects live in `~/Projects/Unreal/<slug>/` (`[unreal] projects_dir`, the system
  NVMe). Backups go to `/mnt/storage/backups/unreal/<slug>/<timestamp>/`
  (`[unreal] backup_dir`, the other drive): `ap ue backup`, rsync with `--link-dest`,
  10 kept.
- **rsync needs `--checksum` for backups.** Its default size + mtime check skipped a file
  re-saved within the same second at the same size, and the new snapshot hard-linked the
  old content.
- Headless runs: `UnrealEditor-Cmd <uproject> -run=pythonscript -script="<file> <args.json>"
  -unattended -nullrhi`. No GPU is used, and an import takes about 1 min fresh and 10 s
  unchanged.
  - The commandlet exits 1 whenever anything logged an error, so judge by the script's
    JSON report.
  - `EditorAssetLibrary.load_asset` can't load engine content in the commandlet (the
    asset registry isn't scanned); `unreal.load_asset` can.
- **Axes, verified live:** Interchange imports a Blender glTF as Blender +X → UE +X,
  +Y → −Y, +Z → +Z, metres → cm. So every placement converts by C·M·C, C = diag(1,−1,1),
  with translation ×100 (`asset_pipeline/ue_coords.py`).
  - `unreal.Rotator(roll, pitch, yaw)` is positional.
  - The import self-check recomputes 155 instance world positions from the greybox
    independently: all within 1 cm.
- Interchange puts a mesh at `<dest>/<file>/StaticMeshes/<file>`, so the importer moves
  it to `/Game/AP/Library/<id>/SM_<id>`. A changed GLB replaces the mesh with
  `consolidate_assets`, which keeps references.
- InstancedStaticMeshComponents added through `SubobjectDataSubsystem.add_new_subobject`
  persist across save and reload, with label, tags, outliner folder and instances.
- **No Python API creates a Landscape from a heightmap.** Only
  `landscape_import_heightmap_from_render_target` exists, into an existing landscape's
  components. Terrain is therefore a Nanite mesh; the manifest carries the values for a
  manual Landscape-mode import.
- Shot renders need the full editor: `UnrealEditor -RenderOffscreen -graphicsadapter=0`
  (Vulkan adapter 0 = GPU 0) with a Slate tick callback. Screenshots taken while
  ShaderCompileWorkers still run show grey default materials, so wait for them to go
  idle.

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
