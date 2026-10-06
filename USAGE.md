# Running an image through TRELLIS.2

## Quick start

```bash
cp ~/Pictures/my_image.png /mnt/storage/asset-pipeline/inputs/
trellis my_image.png
```

The textured GLB lands in `/mnt/storage/asset-pipeline/outputs/trellis2/`. The last lines
the command prints show the exact path.

`trellis` is `scripts/trellis`, symlinked into `~/bin` (already on your PATH). It sets up
the ROCm environment and the venv for you, so it runs from any directory.

## Input images

- Put them in `/mnt/storage/asset-pipeline/inputs/` and pass the bare filename, or pass
  any path: `trellis ~/Downloads/chair.webp`.
- Use one object per image. A clean or white background, or a transparent PNG, works
  best. The background is removed automatically; an existing alpha channel is used as-is.
- PNG, JPG and WebP all work.

## Outputs

Everything goes to `/mnt/storage/asset-pipeline/outputs/trellis2/`, named
`<image name>_<mode>_s<seed>`. The image name is cut to 24 characters.

| File | What it is |
|---|---|
| `…_1024_cascade_s42.glb` | The asset: one mesh, UVs, a PBR material, 2048² textures. Opens in Blender. |
| `…_1024_cascade_s42_input.png` | The background-removed, cropped image the model actually saw. Check this first if a result looks wrong. |
| `…_1024_cascade_s42.json` | Timings, peak VRAM, and vertex/face counts. |

For example, `trellis crown.png` writes `crown_1024_cascade_s42.glb`.

Re-running with the same image, mode and seed overwrites the previous files. Use
`--seed` to get variations.

## Options

```bash
trellis my_image.png --type 512           # faster, less detail (about 3x quicker to generate)
trellis my_image.png --seed 7              # different variation (default 42)
trellis my_image.png --texture-size 4096   # bigger textures (default 2048)
trellis my_image.png --decimate 300000     # lighter mesh; target vertex count (default 1,000,000)
trellis my_image.png --out-dir ~/Desktop   # write somewhere else
TRELLIS_GPU=1 trellis my_image.png         # use GPU 1 instead of GPU 0
```

`--type` selects the resolution:

| Mode | Status |
|---|---|
| `1024_cascade` (default) | Verified. Best detail and colour. |
| `512` | Verified. Fast drafts. |
| `1024` | Not re-verified yet. |
| `1536_cascade` | Currently runs out of GPU memory. |

## How long it takes

On one R9700, each run loads the model for about 40 s first. After that:

| Mode | Generate | Export | Total | Peak VRAM |
|---|---|---|---|---|
| `1024_cascade` | ~85 s | ~20 s | ~2.5 min | ~8 GB |
| `512` | ~25 s | ~13 s | ~1.5 min | ~3 GB |

The first run after a reboot is slower while caches warm up.

## GPUs and ComfyUI

`trellis` uses GPU 0 by default. Both ComfyUI instances (ports 8188 and 8189) run on
GPU 1, so the two never compete. If you pick GPU 1 with `TRELLIS_GPU=1` while ComfyUI is
up, `trellis` prints a note. Both can share a GPU as long as their combined VRAM fits in
32 GB.

## Checking a result without opening Blender

```bash
blender -b --factory-startup --python scripts/blender_check_glb.py -- OUT.glb OUT_check.png
```

This prints the mesh, material and texture counts and writes four views,
`OUT_check_v0.png` to `OUT_check_v3.png`. Look at all four, because missing geometry
usually shows from only some angles.

## Without the wrapper

```bash
source scripts/rocm_build_env.sh
HIP_VISIBLE_DEVICES=1 PYTHONPATH=$AP_ROOT/src/TRELLIS.2 \
    $AP_ROOT/envs/trellis2/bin/python scripts/trellis2_image_to_glb.py IMAGE [options]
```

Never set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. On this GPU it produces
silent NaNs, and the script refuses to start if it is set.

---

# Stage 0: exploring scenes and setting a style anchor

ComfyUI must be running (`~/Projects/AI/ComfyUI/run_comfy.sh`; check with
`ap comfy-check`). Everything below runs locally. No paid APIs are used.

## In the browser

```bash
ap ui                 # then open http://127.0.0.1:8700
```

1. **New project.** Give it a slug and a brief, e.g. `harbour-town` and "a misty fishing
   harbour at dawn, painterly, muted blues and rust".
2. **Explore scenes.** Click *Generate scenes*. Each image is a 1344×768 environment
   concept, at about 45 s per batch of 4. Star the scenes worth keeping (☆ → ★). Starred
   scenes are what stage 1 (scene analysis) will use.
3. **Derive anchor objects.** Click a starred scene, type objects you can see in it
   (`barrel, market stall, lantern`), then *Derive objects*.
   - SAM 3 cuts each one out of the scene (*cutout*), skipping objects cut off by the frame.
   - Each cutout is redrawn as a whole object on white in the scene's style (*redraw*).
   - Star the good ones. Redraws are usually cleaner.
4. **Style anchor.** Write the style text (medium, palette, linework, lighting), then
   *Save anchor*. The starred objects plus this text condition every later image.
   - Keep the strength low (default 0.06).
   - If new images pick up parts of the anchor objects (an awning, produce), lower the
     strength or star more varied objects.
   - If the style is too weak, raise the strength slightly or strengthen the text.

## From the command line

```bash
ap new harbour-town --brief "a misty fishing harbour at dawn, painterly"
ap style explore harbour-town -n 8
ap style star harbour-town style/explore/batch_001/scene_003.png
ap style derive harbour-town style/explore/batch_001/scene_003.png \
    --nouns "boat, lantern, crate" --style-text "painterly, muted blues and rust"
ap style star harbour-town style/derive/batch_001__scene_003/obj_boat_0.png
ap style anchor harbour-town             # strength 0.06; keeps the derive style text
ap style show harbour-town               # batches, stars, anchor
ap gen "a wooden pier post, isolated on white" --project harbour-town --anchor
```

Projects live in `/mnt/storage/asset-pipeline/projects/<slug>/`:
- `style/explore/` holds the scenes.
- `style/derive/` holds cutouts and redraws.
- `style/anchor/` holds the saved anchor.
- `review.json` holds the stars.

# Vision LLM (scene analysis)

Stage 1 sends a scene image to a vision LLM and gets back a JSON asset plan. Each project
picks one provider:

| Provider | What | Cost |
|---|---|---|
| `local` (default) | Qwen3-VL-32B on GPU 0 (8B as fallback), started automatically | Free |
| `gemini` | Gemini API (`GEMINI_API_KEY`) | **Paid** |
| `claude` | Claude API (`ANTHROPIC_API_KEY`) | **Paid** |

The local server starts by itself on the first analysis, so usually you never touch it.
To manage it by hand:

```bash
ap vlm up             # start the default model (32B), or the 8B if the 32B can't start
ap vlm up --model 8b  # the 8B explicitly
ap vlm status         # which model, which GPU, busy or not
ap vlm down           # stop it now (waits for a request in flight; --force doesn't)
ap set harbour-town --llm local        # or gemini / claude
ap set harbour-town --backend references=gemini   # image backend per stage
```

| Model | Engine | GPU 0 while loaded | Per scene | Notes |
|---|---|---|---|---|
| 32B (default) | llama.cpp, Q5_K_M | ~27 GB | 55–105 s | Much better plans and boxes (A/B in CLAUDE.md) |
| 8B (fallback) | transformers, bf16 | ~18 GB | 45–75 s | Used when the 32B's files are missing or it fails to start |

- **It never runs alongside TRELLIS.** `trellis` stops the VLM before it starts (waiting
  for an analysis in progress to finish) and holds GPU 0 until it's done. Meanwhile the
  VLM refuses to start ("a TRELLIS job is using GPU 0"), so start the analysis again
  afterwards.
- Both models free GPU 0 after 10 minutes idle (config `[local] idle_unload_s`) and
  reload on the next request (~2 s for the 32B's server, 6–9 s for the 8B).
- Logs: `/mnt/storage/asset-pipeline/logs/vlm-32b.log`, `vlm-8b.log`.
- Installing: `scripts/install_llamacpp_vlm.sh` (llama.cpp + 32B), `scripts/install_qwen_vl.sh` (8B).

**Paid providers are blocked by default.** Gemini and Claude (and the Gemini image
backend) refuse to run unless you allow it for that one command:

```bash
AP_ALLOW_PAID_APIS=1 ap ...
```

# Stage 1: scene → asset plan

The vision LLM reads a scene and drafts an **asset plan**: the things to model, each with
a category, a count, a real-world size in metres, a description for drawing it alone, where
it sits, and a box on the scene. If ComfyUI is running, SAM 3.1 then redraws each box (see
*Boxes* below). You then fix it in the editor. Stage 2 will generate
references from the included assets.

The project's provider is used (`ap set`); the local one starts by itself. Paid ones stay
blocked unless `AP_ALLOW_PAID_APIS=1` is set.

## In the browser

`ap ui`, then the **1 · Plan** tab.

1. **Scenes** lists the starred stage-0 scenes plus any you upload. Pick one and click
   *Analyse scene*. It runs as a background job; the plan appears when it's done.
2. **Asset plan**: the scene with each asset's box, and a card per asset. Hovering a card
   highlights its box; clicking a box jumps to its card. Solid boxes come from SAM 3.1,
   dashed ones are the LLM's own.
   - Untick *Include* to keep an asset in the plan but not generate it.
   - Fix names, sizes, counts and descriptions. Set *Use* to `game` or `cine`.
   - *Kit* groups modular pieces (wall segments, fence sections) that stage 2 draws
     together in one sheet. Leave it empty for everything else.
   - *Segment as* is the plain noun SAM 3.1 looks for ("market stall", not "alpine
     timber stall"). Fix it if a box is wrong, save, then *Refine boxes*.
   - *SAM: N* is how many copies SAM found for that noun (at most 8). Far from the
     count usually means a wrong count or a noun SAM can't see.
   - × deletes an asset (and its relations); *Add asset* adds one.
   - *Save plan* writes it. *Discard changes* goes back to the saved copy.
3. Re-analysing a plan you've edited asks first. The previous plan is kept as
   `plan/<name>.prev.json`.

What to expect from the local Qwen3-VL-32B: the main assets, counts and sizes are usually
right. It occasionally still lists a part (chimneys) despite being told not to. Anything
over 200 m in any dimension (a mountain) is left out as backdrop and named under the
plan. Treat the plan as a draft to edit.

## Boxes

The LLM's own boxes are approximate. After each analysis, if
ComfyUI is up, SAM 3.1 segments every asset's noun and the box becomes the copy that
overlaps the LLM's box most (else the largest copy not cut off by the frame); about 12 of
16 then sit on a correct whole object. It takes ~1.6 s per asset on GPU 1. The LLM's box
is kept (`bbox_llm`), and the mask is saved in `plan/masks/<plan>/<asset id>.npz`.
Without ComfyUI the LLM boxes stay; run `ap plan refine` (or *Refine boxes*) later.

## From the command line

```bash
ap plan analyze alpine-market                     # every starred/imported scene
ap plan analyze alpine-market style/explore/batch_001/scene_002.png
ap plan analyze alpine-market --force             # also replace plans edited in the UI
ap plan refine alpine-market                      # redo boxes with SAM 3.1 (ComfyUI up)
ap plan import alpine-market ~/Pictures/town.png  # use your own concept image
ap plan show alpine-market                        # summary table of every plan
ap plan show alpine-market batch_001__scene_002 --json
```

Plans live in `/mnt/storage/asset-pipeline/projects/<slug>/plan/`, one per scene, named
after the scene's batch and file (`batch_001__scene_002.json`). Imported scenes are copied
to `scenes/`. Asset `id`s are stable once created; later stages name files after them.

# Stage 2: reference sheets

Every included asset in every plan gets reference images on white, in the project's style
(the style anchor's images and text). ComfyUI must be running; it uses GPU 1.

| Asset | Unit | Sheet |
|---|---|---|
| Ordinary object | its id | The object drawn three times side by side (asked for as front, side and back) |
| Pieces sharing a *Kit* name in one plan | `kit-<name>` | All pieces in one sheet, so trim and proportions match |
| Ground surface (category `terrain`) | its id | A square, top-down texture swatch (style text only) |

**In the browser:** the **2 · References** tab. *Generate missing* makes sheets for every
asset that has none (2 each by default); *+ 2 more* adds variants for one asset. Star the
sheets worth keeping; stage 3 cuts the views out of starred sheets.

**From the command line:**

```bash
ap refs generate alpine-market                      # every asset without sheets, 2 each
ap refs generate alpine-market --unit wooden-barrel -n 4   # 4 more for one asset
ap refs show alpine-market                          # units, sheet counts, stars
```

Sheets land in `/mnt/storage/asset-pipeline/projects/<slug>/refs/<plan>/<unit>/sheet_NNN.png`
with a `meta.json` (prompt, seed, size). The canvas shape follows the asset's real
proportions (wide strips for barrels and walls, taller cells for houses and trees). About
55 s per sheet.

What to expect from Flux.1-dev: the three copies match each other well (shape, colours,
trim), but they're usually three similar three-quarter views rather than a true front,
side and back. Stage 3 cuts them apart, and you pick the best one for TRELLIS.

# Stages 3–4: views, review and 3D

**In the browser:** the **3–4 · Review** tab.

1. Star the good sheets in **References**, then click **Cut views** here. Each starred sheet
   is cut into its separate views (SAM 3.1 finds them; each view is cropped on white so
   thin or white parts like flowers and snow stay). About 2 s per sheet.
   - Object sheets give views 1–3. Each records the view it was *asked* for (front, side,
     back); Flux often draws three-quarter views instead.
   - Kit sheets are split into their pieces, left to right in the order asked.
   - Ground textures aren't cut.
2. For each asset, click the view to build it from (✓ marks it; click again to clear) and
   tag it **game**, **cine** or **hero**. The tag is stored on the plan asset.
3. **Make 3D** runs TRELLIS.2 on GPU 0 (mode picker at the top; ~2.5 min for
   `1024_cascade`, ~1.5 min for `512`). The result shows with a 3D preview, its stats, the
   GLB and the image TRELLIS actually saw. Hero assets use TRELLIS too until the
   multi-view path exists (PLAN.md).

**From the command line:**

```bash
ap views cut alpine-market                          # every starred sheet without views
ap review show alpine-market                        # assets: tag, views, choice, 3D results
ap review choose alpine-market batch_001__scene_002 wooden-barrel \
    views/batch_001__scene_002/wooden-barrel/sheet_000_v1.png
ap review tag alpine-market batch_001__scene_002 wooden-barrel hero
ap 3d alpine-market batch_001__scene_002 wooden-barrel --mode 512
```

Files: `views/<plan>/<unit>/sheet_NNN_vK.png` (+ `meta.json`), `3d/<plan>/<asset>/*.glb`
with the stats JSON, the TRELLIS input image and `trellis.log`. The chosen views are in
`review.json`.

# Stopping a running action

Every long action in the UI has a **Stop** button next to it while it runs or waits:
scene generation, derive, reference sheets (*Generate missing* and each *+ N more*), cutting
views, box refinement and 3D. The header also lists running jobs, each with its own Stop.
Stop affects only that action:

- A job still waiting in the UI's queue is dropped.
- Its ComfyUI prompt is removed if it's still waiting in ComfyUI's queue, or interrupted if
  it's the one running. Other clients' prompts (another browser tab, the CLI) aren't touched.
- A TRELLIS job's process is stopped (SIGTERM, SIGKILL after 15 s) and its GPU claim
  released, so the VLM can use GPU 0 again.
- What already finished is kept: a scene run stopped after its first batch keeps those 4
  scenes (`meta.json` says `"complete": false`); a stopped derive keeps the objects
  already cut.

Scene analysis has no Stop: it runs inside the VLM server and can't be interrupted
mid-request.

The UI runs ComfyUI work (GPU 1) and GPU 0 work (analysis, 3D) in two separate queues, so a
3D build doesn't wait behind sheet generation.

# Hero assets (prototype)

Tag an asset **hero** in the Review tab for the planned multi-view path (PLAN.md). Until it
exists, hero assets go through TRELLIS like the others. The first step can be tried from
the command line:

```bash
ap hero orbit alpine-market batch_001__scene_000 timber-market-stall   # ~3 min, GPU 1
```

It animates the asset's chosen view as a turntable with Wan 2.2 and writes the frames, a
contact sheet and an analysis to `hero/<plan>/<asset>/orbit_s<seed>/`. Its automatic
0/90/180/270-degree picks are only approximate.

Then look at `contact.png`, pick the frames that show the asset's front, left, back and
right yourself, and build a shape from them with Hunyuan3D-2mv (~40 s, GPU 1, untextured):

```bash
ap hero mesh alpine-market batch_001__scene_000 timber-market-stall orbit_s2 \
    --front 0 --left 20 --back 40 --right 60
```

"Left" is the asset's own left side. If the shape has a lump where two views disagree,
swap `--left` and `--right`. `ap hero angles ... orbit_s2` tries to find the angles by
matching the frames to renders of the asset's TRELLIS mesh, but on real orbits it isn't
reliable yet. Hunyuan3D-2mv's license excludes the EU, UK and South Korea.
