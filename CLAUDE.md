# CLAUDE.md

The tool is called **Terraformer Pipeline** in the UI and docs; the repo stays
`asset-pipeline` and the command stays `ap`.

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
- Export and UE backup dialogs (2026-10-09) count progress from logs: export from the
  `EXPORT_TOTAL n` line in `export/blender.log` plus GLBs written, backup from rsync's
  `--info=progress2 --no-inc-recursive` (`to-chk=left/total`) in `export/ue/backup.log`.
  Until then `blender.run_logged` never flushed per line (`log.write(line) or
  log.flush()`: write returns a count), so logs reached disk in 8 KB chunks.
- glTF export walks the whole scene even with `use_selection`: 3.5 s per mesh on an
  80k-object city greybox. Exporting each mesh from an empty scene of its own
  (`temp_override(scene=...)`, `use_active_scene=True`) takes 0.09 s, byte-identical.

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
- Material wording (A/B 2026-10-06, 2 medium shots x 3 seeds, wording only):
  "board-formed concrete" draws wood: slatted screens, timber fins, plank-streaked
  walls. "Honed pale stone/travertine" gives banded, streaky surfaces. Precise wording
  per material (`SiteLayout.materials`), e.g. "polished white Carrara marble, smooth
  seamless stone with fine soft grey veining, crisp sharp machined edges", gave veined
  marble and clean ashlar. The style text must drop the vague words too: it is appended
  to every prompt. Cost: the frames come out whiter and more sterile (less warm brick).
- A material's `ref` is Redux masked to its slots (id pass; `ConditioningSetMask` +
  `ConditioningCombine`). Each masked cond is one more model evaluation per step; the
  only run so far (one ref) took 91 s including the model load. Not yet A/B'd: no
  moodboard images.
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
  `consolidate_assets`, which keeps references. In the commandlet the old asset stays at
  the target path after consolidating, so the importer deletes it before the rename, and
  the rename leaves redirectors in `_import` that are deleted directly (UE 5.8's Python
  AssetTools has no `fixup_referencers`). Verified 2026-10-06 on a full relayout.
- A wholesale relayout needs `ap ue import --reset-layout --prune` (after `ap ue backup`):
  otherwise actors whose ids the new layout reuses keep their old UE transform (UE is the
  layout source of truth) and the old layout's actors stay.
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

## City mode (verified 2026-10-06)
- `SiteLayout.city` (`asset_pipeline/city.py`): civic nodes, terrain-steered avenues,
  organic blocks, instanced massing. `ap site init --city` (neutral starter city) and
  `ap site plan PROJECT` (plan image + area report, no Blender, ~1 s).
- Measured at 5 x 3 km on 8 km terrain (6 nodes): ~7.6k buildings, ~10k trees, 468
  slots, 19.8k pieces. Greybox build 11 s; 17 shots' passes 155 s (~9 s each, CPU);
  export 208 s (251 GLBs); UE import 1-1.5 min fresh, 8 s unchanged.
- Housing is a scaled unit box (`scale` on the piece): one mesh for every building, so
  Blender, the export and UE instancing stay cheap. Draped pieces (ribbons, lawns) are
  unique, so the build merges them into one mesh per slot: 496 -> 186 GLBs on the
  starter city, export 314 s -> 115 s.
- Slot ids made file-safe must stay unique: tiles are `x<i>y<j>` (`-3_-1` and `3_-1`
  collided as asset ids).
- Density: `1 - prod(1 - exp(-(d / (falloff * weight))^2))` over the nodes, plus a
  waterfront bonus and noise, minus steep ground. With a 900 m falloff the whole city
  was dense; 650 m gives cores, terraces and a fraying low-rise edge. Classes: >= 0.6
  perimeter courtyard blocks, >= 0.4 terraces, below detached houses.
- Spiral avenues need a cap on their total turn (75 deg), and arterials must aim at the
  bearing to their target: otherwise both coil or loop near a node's pad.
- Auto shots: wides look down ~13-18 deg (lower ones showed the city as a sliver on a
  coastal plain); the aerial shows the radial plan; the tight steps back by the
  monument's longest side.

- First city frames (Union depth 0.6/0.6, 2 per shot, 2026-10-06): the wides read as a
  large coastal city. Eye-level node mediums follow the layout (edge match 0.45-0.78) and
  the precise material words hold (marble, no wood). Views from far above follow it
  poorly: raised node mediums 0.00-0.38 and the aerial 0.06-0.12. Flux keeps the
  "radial city" idea but invents its own plan and megastructures (oval stadia, curved
  slabs), because a near-top-down depth pass is almost flat. Per-view control settings: see "Concept frames: views from above".

## Concept frames: views from above (2026-10-09)
- A shot's view comes from its camera (`s0_frames.view_of`): aerial if it looks down more
  than 35 deg, raised if a medium or tight looks down more than 3 deg, else level. Wides
  at 13-18 deg down hold their layout (0.55-0.58) and stay level.
- Why raised views drift: the plain depth image is inverse depth over the whole view, so
  it's mostly the ground's near-to-far ramp, and 10-30 m of buildings are faint bumps on
  it (on the aerial the radial plan is almost invisible). `sw_shots.depth_relief`
  removes the ramp with a masked blur and gives half the range to the local relief. A
  plane's inverse depth is linear in the image, so the blur keeps the ground flat grey;
  the blur's border must be extended linearly (odd reflection), otherwise the frame's edge
  turns into a false relief band that eats the scale.
- A/B (`scripts/diag/frames_ab.py --set above|above2`, 3 raised mediums + the aerial,
  2 seeds, wording only), mean edge match on the raised shots:

  | Variant | Raised | Aerial | Look |
  |---|---|---|---|
  | default depth 0.6/0.6 | 0.16 | 0.08 | Invents its own plan (an avenue with lettering) |
  | plain depth 0.8/0.8 | 0.45 | 0.10 | Pine canopies became lawns; a plaza flooded |
  | depth 0.6/0.6 + canny 0.3/0.4 | 0.49 | 0.11 | Good layout on one shot, surfaces flatter |
  | relief 0.6/0.6 | 0.22 | 0.13 | Aerial keeps colour and a city look |
  | **relief 0.75/0.75** | **0.49** | 0.19 | **Raised default**: layout held, real buildings; aerial a white scale model |
  | relief 0.75/0.75 + canny 0.3 | 0.71 | 0.18 | The greybox shows through (white boxes, grey plaza) |
  | relief 0.75 to 0.5 | 0.35 | 0.15 | Drifts; aerial still white |

  Defaults (`schema.DEFAULT_VIEWS`): raised = relief 0.75/0.75, aerial = relief 0.6/0.6.
  The harbour raised shot still drifts at the default (0.10/0.31); the aerial is unsolved.
- Masked references cost one model evaluation per step each, whatever their size: 34 s a
  frame with none, ~76 s with one, ~116 s with two. They're skipped below 5% of the frame
  (`FrameSettings.min_ref_coverage`).

## Typology catalog (city mode) findings (2026-10-07)
- Shares must be measured by footprint, not by building count: a row of narrow
  townhouses counted as 56% of "buildings" while covering 22% of the ground.
- With only the district mixes, 2-3 types took 40-60% of a district. Two fixes brought it
  near the 22%/40% caps:
  - weights back off (x0.15) once a type nears its cap, by footprint;
  - the fill around a single civic building is a weighted pick, not the strongest type.

  Diversity per district then went from 0.7-1.5 to 1.5-1.9 (Shannon).
- A 5 x 3 km city with 33 typologies: ~9.4k buildings, plan 2.5 s, 785 slots, greybox
  build 30 s.
- A typology with one roof and one facade can only vary by storeys. At its minimum storey
  count it must step up, or identical runs of 6-9 appear.
- Prompts are not regional: a landmark's material words (a warm stone colour) turned the
  whole raised frame that colour, not just the landmark. Only the masked Redux reference
  stays local. Prefer the masked ref plus neutral district words for colour that must stay
  on one building.
- Eye-level from ~120 m, a lagoon in front of a landmark is a sliver in the depth pass;
  Flux then invents a rectangular pool. Raised shots show it.
- `pkill -f` / `pgrep -f` on a command line match the calling shell (see above): stop
  servers by PID (`ss -ltnp`).
- UE 5.8 Python: `MaterialExpressionSaturate`, `Constant3Vector` (`constant`) and
  `LinearInterpolate` ("A", "B", "Alpha") build in the commandlet with `-nullrhi`;
  `get_material_instance_*_parameter_value` reads the instance's current value, which is
  how a value tuned in the editor is detected (metadata tag holds the pipeline's last
  write).

- Height variation (2026-10-08): with storeys 4-10 and +-2 jitter, a perimeter block's
  height spread (std / mean) lands around 0.21-0.30, even though it visibly runs from 4
  to 10 storeys. A 0.3 minimum flags about a third of blocks; corner emphasis (+1-2
  storeys) barely moves it. The district height field doesn't help within a block: it's
  smooth over ~700 m. Treat ~0.2 as "varied" for mid-rise blocks.
- Core blocks around a civic node are wedges (35-70% of their convex hull): a single
  building centred on the bounding box falls outside. Centre it at the block's most
  interior point (`polylabel`) and check type sizes against the inscribed circle.
- Plot merging: STRtree neighbours include blocks across avenues (19-24 m); only join
  blocks within street_w + 1 of each other. A local street is one long cut line across
  several blocks: clip it to the merged outline rather than testing its midpoint.

- Frame review round 2 (2026-10-08):
  - Style drift (frames reading as suburban California, North African or Middle Eastern)
    came from the words: "warm ochre / pale rose render", terracotta pitched roofs as the
    default roof, "citrus", "canvas awnings", and lollipop street trees (read as palms).
    Flux has no negative prompt here, so name what you want instead: travertine, marble,
    flat roofs with crisp cornices, umbrella pines, cypresses, olives.
  - A hill node levelled as one 220 m pad (the default pad) reads as a flat ring city;
    level only the summit plaza and let the slope stay.
  - Contour strips on a hill come out a few degrees off the local slope (the cut follows
    the parent block's slope). Terraces must follow the strip's own axis, or a 40 m
    terrace won't fit a 15 m strip, and the block stays empty.
  - A street cut can cross a block in several pieces: give each piece its own id.
  - **Arched walls were solid until 2026-10-08.** A notched outline (arches cut from the
    bottom edge) made by `bm.faces.new` + triangulate came out with overlapping triangles
    over the openings: the front faces covered twice the wall's own area, and every arch
    (rotunda sides, gates, aqueducts, arcades) was a solid wall in all passes. Build such
    walls from convex pieces (piers plus a comb of trapezoids over each arch);
    `tests/test_world.py::test_arched_walls_have_open_arches_in_blender` checks the area.
  - Depth only shows detail near the camera: a 6 m arcade walk at 35 m is under 2% of the
    shot's inverse-depth range (invisible), and canny needs an 8% relative depth step. An
    arcade reads at 5-20 m: look along it, not across a court at it.
  - Anchor A/B (6 moodboard scene images, total 0.05, plus two masked references): about
    310 s per frame vs 117 s without the anchor (vs ~48 s with no references at all).
    The anchor added ornament and palms and lowered the edge match (0.15 vs 0.40, same
    seed): left off.
  - Palms came from the words, not the trees: "sunlit coast" with sandy stone gave palms in
    every market frame, even with no palm-like silhouettes. "clear central-Italian light"
    plus "no palm trees" removed them on the same seeds, with olives in the planters
    instead. Flux's T5 handles that short negation.
  - Check what a frame shows before using it as a reference: the user's match numbers and
    descriptions for two frames were swapped. Go by the description.

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

## Walled city and locked shots (2026-10-10)
- `CitySpec.enclosure`: blocks only inside a ring road behind a circular wall (archwall
  bays of one mesh, on land only, open at the gates), density floored inside (no fraying
  edge), a greenbelt outside in hole-free wedges (a lawn is drawn from its outline: an
  annulus would cover the city). `towns`: small settlements with lanes and a square;
  `terrain.bay_*` puts a bay between. A 2 km ring with 6 nodes: ~2.3k buildings, build 14 s.
- Steep hills inside a compact ring leave bare patches (slope cut); hill_height 170 m over
  500-3200 m inland filled them.
- The aerial of a walled city is a high oblique (`aerial_pitch`, 35-42 deg); its distance
  must fit the near and far edges in the frame's height, not just the width
  (`city.aerial_distance`). A wide looking down >= 28 deg is an "aerial" view.
- `ShotSpec.locked`: approved shot, never regenerated nor re-placed (`ap frames lock`).
- Concrete wording A/B (2 shots x 2 seeds, wording only): smooth fair-faced, "board-marked
  cast concrete, cool grey, fine horizontal formwork lines, no timber" and "porous
  concrete" gave near-identical frames per seed, none with wood (the round-1 wood came
  from "board-formed"). The cream cast comes from "pale"/"ivory"/"warm" words, not the
  material name.

## Raised shots: realism A/B and the forum centre (2026-10-10)
- Raised frames read as white CG scale models. A/B (`scripts/diag/frames_ab.py --set
  realism|realism2`, 4 raised mediums x 2 seeds, an approved eye-level frame as target):

  | Variant | Edge match | Look |
  |---|---|---|
  | relief 0.75 to 0.75 (old default) | 0.69 | Scale model |
  | **relief 0.75 to 0.5 / 0.6** | 0.48 / 0.54 | Only lever that helps: haze, hills, weathering; palms can creep back. Default now 0.55 |
  | own locked medium as ref at 0.10 / 0.15 | 0.68 / 0.66 | No visible change |
  | + "dense dark-green umbrella pine canopies, seen from above" | same | No change: crowns stay grey dishes |
  | img2img realism pass (`s0_frames.refine`, no control) 0.25-0.5 | -0.03 to -0.1 | Stains and sharper detail, harsher contrast; never softer or hazier, never less model-like |

  Smooth dome crowns seen from above read as dishes or mushrooms whatever the words
  (from the side they read as pines). Seen from above, the scale-model look comes from the
  greybox itself (big blank roofs, smooth crowns), not the sampler.
- Pine crowns are domes 0.32 x their width (were 0.24 x the height, ~0.2 x the width:
  discs in wides). Trunk 1.4 m.
- `CivicNode.centre = "forum"`: an enclosed rectangular court (0.83 R x 0.5 R), four
  12 m portico ranges (shops behind, two orders, attic), gate arches where each radial
  crosses a range (6 radials: both short ends, +-24 m on the long sides), a fountain; a
  market node fills it with a stall grid. Its own random stream, and the loose market's
  draws still taken, so the rest of the plan (other markets, every tree) stays put.
  - The eye-level camera must stand clear of the stall rows (a hut 5 m away fills the
    frame) and outside the gate piers: in the court just in front of the end colonnade,
    beside its gate, looking diagonally at the far side's gate.
  - The masked landmark ref makes each frame ~100 s.
- `ap frames lock SHOT --greybox`: camera and greybox locked, no frame (wides: Flux
  can't resolve a whole city at frame size; they render in UE from the real assets).

## Asset library plan (phase 3 step 1, 2026-10-10)
- `ap library plan` (CPU, ~2 s) groups greybox slots into entries (A landmarks and kit
  monuments, B singular civic, C fill typology x material, D props and trees) and finds each
  one's best crop in the locked shot frames through the object-id pass. `ap library check`
  (SAM 3.1, ~2 min for 6 frames, masks cached) flags crops mostly trees, sky or something
  else. Decisions per entry: Library tab › Asset review (`site/library_review.json`).
- **A locked frame can predate the current layout.** Locking keeps the camera, not the
  city: frames locked before a relayout no longer line up with the shot's current id pass,
  so their crops show trees or sky where the old layout had a building. `frame_fit` keeps a
  frame only if its edge match against the current canny is >= 0.9 x the one recorded
  when it was generated, and >= 0.25 (raised scale-model frames never followed theirs).
  Round 2 frames whose view didn't change (match unchanged) still pass.
- Trees and stalls are pieces inside a slot: the id pass can't separate them, so their
  crops are the whole slot (flagged as approximate).
- Crop refinement A/B (2026-10-10, `scripts/diag/crop_refine_ab.py` on the 5 worst crops;
  env `envs/segdepth` from `scripts/install_segdepth.sh`, GPU 0, ~1 s per frame per model
  warm): ID region ∩ ADE20K building-like labels, minus painted-depth occluders (Depth
  Anything V2 fitted affinely to the greybox's 1/Z), then SAM 3.1 inside the box
  (`workflows/sam3_box`, `segment.detect_box`).
  - Where the frame paints the asset, it cleans the crop (sky and trees out): 47-82% of
    the id region kept. Where it doesn't (an asset behind trees, a hillside drawn as a
    terrace wall), only 5-21% is kept: a reliable "no usable crop" signal.
  - OneFormer and Mask2Former (Swin-L ADE20K) agree within a few points; OneFormer's
    weights are MIT, Mask2Former's HF card says "other". Depth Anything V2 Small is
    Apache-2.0 (Base/Large/Giant are CC-BY-NC-4.0).
  - The SAM box step changed one crop of five (+14% on a clean facade).
  - transformers 5.19 reports the Swin backbone's final layernorm as missing for both:
    the heads read the stage outputs, and the labels are right (checked on a frame).

## Library 3D: TRELLIS vs procedural kit (2026-10-10)
- From one front view, TRELLIS guesses depth. A monumental gate came out as separate slabs with
  gaps, and a stoa bay as a four-legged table with no back wall. From a three-quarter view the
  gate's walls are solid, but it came out twice as deep as wide, with the arch on the short end.
- Flux won't draw a three-quarter view of a thin kit module. Eight stoa-bay sheets, in two
  wordings, gave open door frames or walls seen head-on; "side edges cut straight and open"
  reads as a doorway. For whole buildings the three-quarter prompt
  (`ap library sheets --view three-quarter`) works better than the turnaround: a basilica lost
  the roof crosses the turnaround drew, and a market hall showed its depth.
- Multi-storey facade bays (alone or on a kit sheet) come out as whole buildings; a two-column
  bay doesn't.
- So: simple architectural kit (arches, colonnades, walls, cornices) is procedural
  (`asset_pipeline/kit_proc.py`, `scripts/blender_kit.py`, spec in the project's
  `library/kits/<asset>.json`). Real size comes from the greybox, proportions are measured on
  the sheet, and the PBR maps come from the sheet: the delit front elevation projected onto the
  front and back faces, a mirrored seamless tile from a plain patch, and normal and roughness
  from the luminance. About 500 faces, a few seconds on the CPU. TRELLIS stays for complex and
  irregular assets.
- An arched wall's piers must run its full height. The comb of trapezoids only spans the
  opening, so piers cut at the springing line leave the sides open above it.
- Fill typologies (tier C) are one representative building at the catalog's middle sizes (a
  row form as 4 houses). Their slots hold whole blocks: 230 m "townhouses".
- A square 65 m courtyard block at 7 storeys still drew a 2-3-storey villa: the insula needs
  its storeys and the courtyard in the words.
- Procedural buildings (`asset_pipeline/building_proc.py`; `ap library brief` / `ap library build`):
  generators for hall_vault, cube, basilica, podium_tower, perimeter and row. Walls with
  windows, arches and round openings are sliced into convex prisms at every opening edge and
  along every curve. Overlapping walls must not share faces: side walls run between the front
  and back walls, or coplanar end caps show as dark seams.
- Reading a sheet: the palette (k-means) and a plain wall patch work on any view. The patch must
  avoid shadows: score on tone as well as flatness, and pin `patch_px` in the brief when it
  picks up ribs or glazing. The bay and storey rhythm (autocorrelation of the darkness profile;
  an edge profile counts both edges of every window) is only clean on near-frontal views.
  Three-quarter views gave 33-42 "storeys", so rhythms outside 2.5-15 m bays and 2.6-7 m
  storeys are dropped and the generator defaults apply.
- **Every procedural mesh was inside-out until 2026-10-10.** `_quad_prism` wound its faces
  inwards. Cycles shades both sides and hid it; Unreal culls back faces.
  `tests/test_building_proc.py::test_built_meshes_face_outwards` checks the signed volume.
  The Blender prism now orients each polygon counter-clockwise itself.
- Facade grammar (`building_parts.facade`) and per-form defaults (`building_gen.TYPE_FEATURES`).
  A part missing from a build goes into the type defaults, so every building of that form
  gets it; the brief's `features` override them per asset.
- Sheets are drawn at their own scale: a 65 m catalog-middle insula against a sheet whose front
  measures about 37 m at its own storey height made a long low slab. To match a sheet's massing,
  take the front width from storeys x storey height (the measured storey period); keep it inside
  the catalog range.
- Mirror-tiling a small sheet patch that has a gradient gives kaleidoscope patterns. Tiles now
  use the patch's high-pass detail on the storey band's median colour, made seamless by a
  cross-fade with a half-rolled copy and sized to the patch's real extent.
- Frontality (the share of strong edges within 4 degrees of square) picks the near-front view.
  Curved buildings (vaults) score low even on true elevations; pick those by eye.
- **Catalog storey ranges feed the type mix.** Raising library_cube to 11-13 storeys and
  market_hall to 5-6 reshuffled the whole city: footprint x storeys counts towards the type
  shares, and 246 of 485 slots moved. Reverted. `Typology.storey_m` (a per-type storey height)
  changes height only: same 485 slots, same ids, only those types' 18 slots changed. Check with
  an in-memory `city.plan` diff before a rebuild. The podium tower's `podium_storeys` and
  `tower_scale` are height- and width-only in the same way.
- Build review (Library › Asset review › Build review; `library_review.py`): renders per round
  in 3d/library/<asset>/review/round_N (a Workbench preview in about 6 s, Cycles about 12 s
  later), statuses and history in site/library_review.json under "_reviews",
  `ap library review apply`. Deep link: `/?project=<slug>&review=<asset>`.
- UI scripts share one global scope: a second top-level `const clone` silently broke a whole
  script. Check with `cat asset_pipeline/ui/static/*.js > all.js && node --check all.js`.
- Overlapping coplanar faces (a step's top in the plinth's plane) render black in Cycles: offset
  them or don't overlap.
