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
  alpine-market scenes (45–70 s each). The editor edits, includes/excludes, adds and deletes
  assets and relations, and protects hand edits from re-analysis. Outside images can be
  imported as scenes. SAM 3.1 redraws each box from an LLM-given noun (~12/16 correct vs
  3-4/16 for the 8B's own boxes). Default local VLM: Qwen3-VL-32B (llama.cpp), 8B as
  fallback; auto-started, and stopped before any TRELLIS job on the same GPU.

- **Stage 2 (reference sheets):** `ap refs generate` / the References tab draw every
  included asset on white with the style anchor: objects as a three-up sheet, kits in one
  sheet, terrain as a top-down swatch. Flux.1-dev keeps the three copies consistent but
  rarely gives true side/back views (see CLAUDE.md).

Next, in order:
1. **Stage 1 follow-up:** "break this asset into components" (recursion, `parent` field).
2. **Multi-view decision:** true front/side/back needs another model (none installed
   locally); options in the stage 2 report.
3. **Stage 3:** SAM 3.1 segmentation with view labels and view-sets in `review.json`.
4. **Stage 4:** review grid (star, game/cine tag) and the send-to-trellis job queue.
5. **Docs pass:** this file's Stages and Architecture sections need several updates:
   - stage 0;
   - the adapter;
   - SAM 3.1 replacing GDINO+SAM2;
   - the GPU map (ComfyUI → GPU 1, TRELLIS/VLM → GPU 0).

Open items:
- `1536_cascade` runs out of memory in CuMesh `fill_holes`.
- Non-cascade `1024` mode hasn't been re-verified since the GEMM guard went in.
- The first real Gemini or Claude call needs the user's OK.
