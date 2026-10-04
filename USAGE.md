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
TRELLIS_GPU=0 trellis my_image.png         # use GPU 0 instead of GPU 1
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

`trellis` uses GPU 1 by default, which PLAN.md assigns to image-to-3D. ComfyUI's 3D
instance (port 8189) uses the same GPU. If it is running, `trellis` prints a note. Both can
run at once, as long as their combined VRAM fits in 32 GB.

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
