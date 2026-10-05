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
ap style star harbour-town style/derive/scene_003/obj_boat_0.png
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
- A scene analysis takes about 25 s warm, about 35 s with the model load. The very first
  request after installing took about 100 s (one-time GPU kernel warm-up).
- Log: `/mnt/storage/asset-pipeline/logs/vlm.log`.

**Paid providers are blocked by default.** Gemini and Claude (and the Gemini image
backend) refuse to run unless you allow it for that one command:

```bash
AP_ALLOW_PAID_APIS=1 ap ...
```
