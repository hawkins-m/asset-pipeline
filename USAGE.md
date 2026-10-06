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
| `local` (default) | Qwen3-VL-8B-Instruct on GPU 0 (`ap vlm`) | Free |
| `gemini` | Gemini API (`GEMINI_API_KEY`) | **Paid** |
| `claude` | Claude API (`ANTHROPIC_API_KEY`) | **Paid** |

```bash
ap vlm up             # start the local server on GPU 0 (http://127.0.0.1:8710)
ap vlm status         # is it up, and is the model loaded?
ap vlm down           # stop it and free GPU 0
ap set harbour-town --llm local        # or gemini / claude
ap set harbour-town --backend references=gemini   # image backend per stage
```

- `ap vlm up` returns once the server answers. The model itself loads on the first
  request (6–9 s) and unloads after 10 minutes idle (`--idle-unload SECONDS`), freeing
  GPU 0 for `trellis` again. While loaded it holds about 17.5 GB of GPU 0, so don't run
  `trellis` at the same time (`ap vlm down` frees it at once).
- A full stage 1 plan takes 45–70 s warm (6–10 assets, 1–1.6k output tokens at ~22 tok/s).
  The model load adds about 7 s. The very first request after installing took about
  100 s (one-time GPU kernel warm-up).
- Log: `/mnt/storage/asset-pipeline/logs/vlm.log`.

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

Start the local LLM first (`ap vlm up`). The project's provider is used (`ap set`); paid
ones stay blocked unless `AP_ALLOW_PAID_APIS=1` is set.

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

What to expect from the local Qwen3-VL-8B: the main assets and counts are usually right.
It still lists some parts (doors, windows, chimneys) or backdrop (a distant mountain)
despite being told not to, and sizes can be off (a 1.5 m barrel). The boxes are rough:
some are tight, others are offset or far too large. Treat the plan as a draft to edit.

## Boxes

The LLM's boxes are rough (3–4 of 16 usable on alpine-market). After each analysis, if
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
