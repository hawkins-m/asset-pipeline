# Asset Pipeline — Project Plan

## Goal
A local-first tool that turns a single concept/scene image into organized, reviewable
reference images and then high-quality 3D assets. Assets must be usable for game work
where possible, and at minimum good enough for cinematics (Blender) when topology is poor.

Inspired by a public "scene breakdown" tool (拆件工具) whose pipeline was:
vision LLM parses scene → editable JSON asset plan → image model generates white-background
reference sheets per asset → local OpenCV cropping (connected components + divider lines)
→ human review/starring → image-to-3D (Tripo) → GLB. Its weaknesses: no real segmentation,
no scale, no consistency between modular pieces, no mesh cleanup, nothing is ever placed.
We aim to fix those.

## Architecture
- **Orchestrator (this repo, Python):** owns the JSON asset plan, review UI, run history,
  recursion ("break this building into components"), and stage routing.
  Start simple (FastAPI + minimal web UI, or Gradio).
- **ComfyUI as headless backend:** each generation stage is a saved API-format workflow JSON.
  The orchestrator fills inputs and queues jobs over HTTP. Tuning a stage = editing its
  workflow in Comfy, not code.
  - Port 8188 (main instance): image generation and SAM 3 segmentation
  - Port 8189 (ComfyUI-H3 instance): not used by this pipeline
  - Both instances run on GPU 1. GPU 0 runs TRELLIS.2 and other local 3D jobs.
- **Adapters:** one interface per stage (analyze, generate, segment, to_3d, cleanup) with
  swappable backends, so models can be upgraded without touching orchestration.

## Stages
1. **Analyze** — vision LLM reads the scene, outputs JSON plan: categories, instance counts,
   spatial relationships, estimated real-world dimensions. Use a strong API model here
   (cost is negligible per scene; quality matters most).
2. **Generate references** — local image model produces white-background sheets.
   Generate modular kit pieces (wall straight/corner/junction) together in one sheet with a
   shared style reference so trim and proportions match. Prefer multi-view (front/side/back).
3. **Segment** — Grounding DINO + SAM 2 masking instead of grid cropping, so layout
   deviations don't break extraction.
4. **Review** — star keepers; tag each asset `game` or `cine`.
5. **To 3D (local)** — TRELLIS.2 (primary, single-image, MIT, ~24GB VRAM) and
   Hunyuan3D 2.1 (strongest open PBR texturing) / Hunyuan3D-2mv (multi-view input).
   Run both where useful; keep the better result.
6. **Cleanup (Blender headless)** — scale-normalize to plan dimensions, set pivots;
   `game`-tagged assets get retopology (QuadriFlow / Instant Meshes). `cine` assets skip it.

## Known-site mode (later)
When the location is real: pull elevation (USGS 3DEP 1m DEM / lidar in the US;
Copernicus GLO-30 elsewhere) and layout (OpenStreetMap footprints, roads, water).
The plan then uses real footprints; the vision model only decides appearance.
A Blender script (BlenderGIS) places generated assets on real footprints at real scale.

## Machine
- Ubuntu 26.04, Ryzen 9950X3D, 64GB RAM
- 2× AMD Radeon AI Pro R9700, 32GB each, RDNA4 / gfx1201
- ROCm 7.2.4 runtime (`/opt/rocm`); the only `hipcc` is Ubuntu's 7.1.1 package, so HIP
  builds go through the AMD LLVM 22 shim (`scripts/rocm_build_env.sh`)
- PyTorch: 2.9.1+rocm7.2.1 in main ComfyUI's venv, 2.13.0+rocm7.2 in ComfyUI-H3's and
  in the TRELLIS.2 env
- Python: system default 3.14; venvs use 3.12
- Blender 5.2.2 LTS (snap)
- Existing ComfyUI on ports 8188/8189 — **do not modify its environment**
- Blender MCP is already connected to Claude Code

## ROCm notes
Most 3D-gen repos assume CUDA (nvdiffrast, flash-attn, custom kernels). Use ROCm forks:
a TRELLIS.2 ROCm branch exists that was tested on gfx1201 and auto-detects CUDA vs ROCm.
Build HIP extensions with the target arch set explicitly to gfx1201. Flash Attention on
ROCm uses the Triton backend. Install every model in its own isolated environment.

## Repo rules
- Code, workflow JSONs, configs, and docs only. No venvs, weights, or generated outputs —
  those live outside the repo on the storage drive.
- Commit after each working step.

## First milestone
TRELLIS.2 installed in isolation, extensions built for gfx1201, one test image → textured
GLB that opens in Blender.

## Current status / next steps (2026-10-05, updated after stage 1)
Done and verified:
- **Stage 5 (TRELLIS.2):** `trellis IMAGE` on GPU 0. Modes `512` and `1024_cascade` work.
  The gfx1201 GEMM guard and the CuMesh memcpy patch fix silent corruption.
- **Orchestrator foundation:** `ap` CLI, ComfyUI client and workflow templates, and an
  image adapter (ComfyUI by default, Gemini optional, chosen per stage).
- **Stage 0 (style anchor):** scenes are explored, then object-on-white anchors are
  derived from starred scenes via SAM 3.1, with duplicates removed. The anchor is Flux
  Redux at 0.06 plus style text. There is a UI on 127.0.0.1:8700.
- **Step 3 (vision LLM):** local Qwen3-VL-8B on GPU 0 (`ap vlm`), live-verified at about
  25 s per scene. Gemini and Claude adapters exist but are tested only with mocked SDKs.
  Paid APIs stay blocked unless `AP_ALLOW_PAID_APIS=1` is set.

- **Stage 1 (asset plan):** `ap plan analyze` / the UI's Plan tab drafts one `AssetPlan`
  per scene (`plan/<batch>__<scene>.json`) with the local Qwen3-VL. Live-verified on both
  test scenes (45–70 s each). The editor edits, includes/excludes, adds and deletes
  assets and relations, and protects hand edits from re-analysis. Outside images can be
  imported as scenes. SAM 3.1 redraws each box from an LLM-given noun (~12/16 correct vs
  3-4/16 for the 8B's own boxes). Default local VLM: Qwen3-VL-32B (llama.cpp), 8B as
  fallback; auto-started, and stopped before any TRELLIS job on the same GPU.

- **Stage 2 (reference sheets):** `ap refs generate` / the References tab draw every
  included asset on white with the style anchor: objects as a three-up sheet, kits in one
  sheet, terrain as a top-down swatch. Flux.1-dev keeps the three copies consistent but
  rarely gives true side/back views (see CLAUDE.md).

- **Stages 3-4 (views, review, 3D):** starred sheets are cut into views (SAM 3.1 finds
  them, full-height band crops keep thin/white parts); the Review tab chooses a view per
  asset, tags it game / cine / hero, and runs TRELLIS.2 on GPU 0 with a 3D preview.
- **UI jobs:** two lanes (ComfyUI on GPU 1, VLM/TRELLIS on GPU 0) and a Stop button per
  action that removes or interrupts only that action's ComfyUI prompts and kills TRELLIS
  cleanly (GPU claim released).

- **Stage 6 (cleanup):** headless Blender on the CPU. Every asset is scaled to the plan's
  size (height fit), pivoted at its base centre; game assets are decimated to a category
  budget with colour/roughness/normal maps baked from the original. Warnings for flat
  results and proportions far from the plan. `ap cleanup` and *Clean up* in the Review tab.

## World mode (one environment, many shots)
For a single coherent place seen in many shots, instead of unrelated concept scenes. The
geometry is fixed first; images are generated onto it.

1. **Site and greybox.**
   - `site/layout.json` (districts, rings, radial avenues, plazas, plots, ring rows of
     plots, classical modular kits, vegetation zones, terrain, shots) generates a tagged
     Blender greybox (`sw_site`, headless).
   - After that the .blend is the source of truth for layout until the engine import.
     Hand edits are protected from rebuilds, and tags are read back into `greybox.json`.
2. **Shots.** Cameras in the .blend. Each renders exact object-ID, depth, normal and canny
   passes plus a preview (`sw_shots`).
3. **Concept frames per shot.** Depth/canny-guided Flux (ControlNet Union Pro 2.0; BFL
   depth LoRA for A/B), with the style anchor. Moodboard images can be imported as anchor
   sources.
4. **Asset library.** All approved frames merge into one deduplicated library:
   - greybox types are keyed by slot type, with counts from the greybox;
   - props are deduplicated across shots;
   - classical kits are a fixed piece list, with curved pieces per ring radius.

   Stages 2–6 run on it. Vegetation is scattered (baked points), not generated per asset.
5. **Export:** a manifest plus asset package (cm, left-handed for the engine).
6. **Unreal Engine 5.8 importer** (editor Python):
   - Interchange glTF → Nanite static meshes, instanced static meshes, material instances
     from the PBR maps;
   - a Landscape from the heightmap, CineCameras and a Level Sequence.

   After the import the UE level is the layout source of truth. Actor names and tags stay
   stable for editing over an Unreal MCP server.

Status (2026-10-06):

| Step | State |
|---|---|
| 1–2 Site, greybox, shots | Done, verified (`ap site`, `ap shots`, UI Site · Shots) |
| 3 Concept frames | Done (`ap frames`, `ap moodboard`, UI Frames); frames generated for every shot. Default: Union depth alone, 0.6 strength to 60% of the steps (A/B in CLAUDE.md "Concept frames") |
| 4 Asset library | **Not started: waits for the frame review** |
| 5–6 Export and UE 5.8 import | Proven early with the greybox as stand-in assets (UE 5.8.3, live). `ap export`, `ap ue import / render / pull-layout / backup`. Real assets will replace the stand-ins by asset id. |

What phases 5–6 produce today: Nanite meshes, one ISM actor per slot, material instances,
CineCameras, a Level Sequence, and renders through the shot cameras. Layout edits made in
UE survive a re-import.
- The UE project lives on a different drive from `$AP_ROOT` (`[unreal] projects_dir`).
- Pipeline-imported content is disposable (a re-import recreates it).
- `ap ue backup` snapshots the project for the work done in UE itself (lighting,
  Sequencer). It goes to `[unreal] backup_dir`, timestamped, hard-linked and rotated
  (10 kept).
- Terrain is a Nanite mesh: UE's Python can't create a Landscape. The values for a
  manual Landscape import are in the manifest.

City-scale mode (2026-10-06, after the first frame review: the single-precinct wides
read as a memorial, not a city): `SiteLayout.city` plans a multi-km coastal city with
several civic nodes, terrain-following radial and spiral avenues, organic blocks, a
density gradient from mid-rise cores to low-rise edges, green corridors, markets and a
waterfront. Housing is instanced massing per tile, never per-building assets. Wides frame
the whole city; mediums frame one node; tight shots are mood/material only and never seed
assets. Materials are named precisely per slot type (`SiteLayout.materials`), with an
optional moodboard reference applied as Redux masked to that material's slots.

Typology catalog (2026-10-07): the city's blocks get 20-30 catalog typologies by district
mix, density band and zone, with per-building variation modelled in the greybox and an
anti-repetition report (`city_types.py`, `catalog.py`). Prompts are short and built from
structured fields. A landmark typology carries a reference image masked to its slots.
Shot prompts and cameras, district notes, materials, the catalog and the mix are editable
in the UI (yours vs auto). The UE master material exposes tint, roughness (x multiplier),
metallic and normal strength on every instance, and one instance per layout material.
The rotunda is a landmark ensemble: an open octagon with a low dome, a lagoon in front,
and two curved colonnades.

Next, in order:
1. **Frame review (the user)** of typology frames for every shot; then lock the rotunda
   design (decided after the typology frames).
2. **Step 4: asset library from the object-ID pass** (`s1_library`):
   - greybox types keyed by slot type, counts from the greybox;
   - props deduplicated across the starred frames;
   - the classical kit piece list;
   - then stages 2–6 run on the library.
3. **Step 5 with real assets:** cleanup GLBs and textures in the manifest instead of
   stand-ins. The master material gets ORM/roughness textures.
4. **Vegetation scatter** (baked points → ISMs per zone).

Open questions:
- Whether approved frames used as references (`--ref`) make shots consistent enough.
- Whether a moodboard-derived anchor helps beyond the style text.

## Hero tier (multi-view path)
Default assets (game, cine) stay single-image TRELLIS.2. Assets tagged **hero** get a
second path that gives the 3D model real side and back information:

1. **Orbit video.** Wan 2.2 I2V 14B (fp8, in ComfyUI on GPU 1, lightx2v 4-step LoRAs)
   animates the chosen view on white as a slow turntable orbit (81 frames, ~5 s).
2. **Consistent frames.** Find where the orbit closes (the frame after the midpoint most
   like frame 0) to estimate the angle per frame, take frames at 0/90/180/270 degrees,
   and reject the orbit if the object drifts (silhouette scale or centre changes, it
   leaves the frame, the background stops being white) or if it never comes back round.
   BiRefNet / white-band crop per frame.
3. **Multi-view 3D backend.** Candidates, to evaluate once the orbit works:
   - Hunyuan3D-2mv (front/left/back/right in, shape out). ComfyUI has the node built in,
     but the weights would have to go into ComfyUI's model folders, which are off-limits
     without the user's OK; otherwise its own env on GPU 0.
   - TRELLIS.2 conditioned on several images, if its pipeline supports it (TRELLIS v1
     had a multi-image mode); keeps the texturing we already trust.
   Texture from the front view (TRELLIS) or Hunyuan3D 2.1 paint.
4. **Fallback:** an orbit that fails the checks leaves the asset on single-image TRELLIS.

Prototype status (`ap hero orbit`, `stages/s5_hero.py`): step 1 works (consistent full
turns with the "quick complete turn" prompt, ~2.5-3 min per orbit). Step 2's angle picking
does not: frames labelled 90/180/270 are really ~45/90/135 (non-constant speed, possible
swing-back, front/back-similar objects). Render matching (frames vs renders of the asset's TRELLIS mesh, smoothed over time) was
tried and fails on real orbits: Wan barely changes the silhouette while "turning", and the
mesh's and the orbit's unseen sides are different inventions (details in CLAUDE.md). The
tracker itself is verified on synthetic data. Hunyuan3D-2mv turbo now runs in ComfyUI:
with hand-picked frames a 4-view shape beats front-only from the sides but is untextured
and less detailed than TRELLIS. The hero path stays experimental (`ap hero orbit|angles|
mesh`). Options if it's pursued: a pose estimator on the frames (VGGT / MASt3R, own env),
an orbit model that keeps geometry (a dedicated multi-view diffusion model instead of a
general video model), or texture the Hunyuan shape from the front view.

Next, in order:
1. **Hero path (experimental):** decide whether to pursue it (options above).
2. **Stage 1 follow-up:** "break this asset into components" (recursion, `parent` field).
3. **Stage 6 follow-ups:** quad retopology (QuadriFlow rejects TRELLIS meshes; try
   Instant Meshes, or repair harder), LODs, collision meshes for game assets.
4. **Docs pass:** this file's Stages and Architecture sections need several updates:
   - stage 0;
   - the adapter;
   - SAM 3.1 replacing GDINO+SAM2;
   - the GPU map (ComfyUI → GPU 1, TRELLIS/VLM → GPU 0).

Open items:
- `1536_cascade` runs out of memory (parked: see "Revisit when models improve").
- Non-cascade `1024` mode hasn't been re-verified since the GEMM guard went in.
- The first real Gemini or Claude call needs the user's OK.

## Revisit when models improve
Parked ideas, each tracked as a GitHub issue labelled `revisit`
(`scripts/create_revisit_issues.sh` creates them). Details of what was tried are in
CLAUDE.md. Rerun commands use placeholders (PROJECT, PLAN, ASSET): pick a comparable
asset and test on a scratch copy, not a live project.

| Idea | What we found | Retry when | Rerun |
|---|---|---|---|
| **Hero tier / multi-view 3D** | Flux sheets don't give true side/back views; Wan orbit frames + Hunyuan3D-2mv give a shape slightly cleaner from the sides than front-only, but untextured, on a ground slab, with spikes, and less detailed than single-image TRELLIS. | An open multi-view model that keeps geometry across views (a turnaround / multi-view diffusion model that runs locally), or a TRELLIS release that takes several images. | `ap hero orbit` then `ap hero mesh … orbit_sN --front 0 --left 20 --back 40 --right 60` on a multi-part asset (e.g. a market stall); compare with `ap 3d` for the same asset. |
| **Orbit frame angles** | Render matching is right on synthetic data but fails on real Wan orbits (tracks only 25-50 deg): Wan's "turn" barely changes the silhouette, and TRELLIS's and Wan's unseen sides are different inventions. | A video model whose turntables keep 3D proportions, or a feed-forward pose estimator (VGGT / MASt3R successor) that runs on ROCm. | `pytest tests/test_hero.py::test_match_angles_recovers_a_full_turn` (must still pass), then `ap hero orbit` + `ap hero angles … orbit_sN`: success = rotation close to 360 and all four sides picked. |
| **Hunyuan texturing** | Not attempted: Hunyuan3D-2mv shapes come out untextured; Hunyuan3D 2.1 paint builds custom rasterizer extensions that were written for CUDA and are untried on ROCm here. | Hunyuan3D paint with ROCm support, or native ComfyUI texturing nodes for Hunyuan meshes. | `ap hero mesh …` (above), then texture it from the chosen view; compare with the TRELLIS GLB in the Review tab. |
| **TRELLIS.2 `1536_cascade` OOM** | A genuine out-of-memory error in CuMesh `fill_holes` -> `get_edges` on the 32 GB R9700 (not silent corruption); `512` and `1024_cascade` work. | A TRELLIS.2 / CuMesh release with chunked or lower-memory hole filling, or a GPU with more memory. | `trellis turret.webp --type 1536_cascade` and `trellis crown.png --type 1536_cascade`: no OOM, raw F/V close to 2.0 in the run's JSON, and all four views fine in `scripts/blender_check_glb.py`. |
