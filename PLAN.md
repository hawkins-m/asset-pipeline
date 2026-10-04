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
  - Port 8188 → GPU 0: image generation
  - Port 8189 → GPU 1: image-to-3D
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
- PyTorch: 2.12.0+rocm7.2 in ComfyUI's env, 2.13.0+rocm7.2 in the TRELLIS.2 env
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
