"""Hero tier, PROTOTYPE: chosen view -> Wan 2.2 turntable orbit -> consistent frames.

See PLAN.md "Hero tier". Steps 1-2 only (no multi-view 3D backend yet):
- orbit(): Wan 2.2 I2V 14B (ComfyUI, GPU 1, 4-step lightx2v) animates the asset's chosen
  view as a turntable; frames saved as PNGs.
- analyse(): per-frame silhouette on white (centre, base, height) to catch drift, and the
  frame after the midpoint most like frame 0 to find where a full turn closes. If it
  closes, frames at 0/90/180/270 degrees are picked assuming constant speed.

Findings (2026-10-05, CLAUDE.md): the orbits are visually consistent, but the picks are
NOT at the angles they claim. Wan eases in and out (not constant speed), may swing back
rather than complete a turn, and front/back-similar objects (stalls, gable houses) fool
every image-only angle check. Frame angles need an independent source before a
multi-view backend can use them (PLAN.md "Hero tier").

Layout under the project:
    hero/<plan>/<asset id>/orbit_s<seed>/frame_NNN.png, meta.json, contact.png
"""
import random
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .. import config, review
from ..comfy import workflow
from ..comfy.client import ComfyClient
from ..project import ProjectStore, read_json, write_json

HERO = "hero"
PROMPT = ("A slow, smooth turntable rotation of the {name}: the object spins in place around "
          "its vertical axis through one full 360-degree turn while the camera stays still. "
          "The object stays centred and the same size on a plain pure white background, even "
          "studio lighting, no shadows. {description}")
NEGATIVE = ("camera movement, zoom, camera shake, cut, scene change, background change, coloured "
            "background, extra objects, people, hands, text, blurry, deformed, melting")


def orbit(store: ProjectStore, plan: str, asset_id: str, seed: int | None = None,
          frames: int = 81, size: int = 640, client: ComfyClient | None = None) -> Path:
    from . import s1_plan
    view = review.chosen(store, plan, asset_id)
    if not view:
        raise ValueError(f"{plan}/{asset_id}: choose a view first")
    p = s1_plan.load(store, plan)
    asset = next(a for a in p.assets if a.id == asset_id)
    seed = random.randrange(2**31) if seed is None else seed
    out = store.root / HERO / plan / asset_id / f"orbit_s{seed}"
    out.mkdir(parents=True, exist_ok=True)
    c = client or ComfyClient(config.backends()["comfyui"]["url"])
    graph, manifest = workflow.load_template("wan22_i2v_orbit")
    prompt = PROMPT.format(name=asset.name, description=asset.description)
    graph = workflow.fill(graph, manifest, {
        "image": c.upload_image(store.root / view), "prompt": prompt, "negative": NEGATIVE,
        "width": size, "height": size, "frames": frames, "seed": seed})
    t = time.time()
    images = c.run(graph, manifest["outputs"])
    for i, img in enumerate(images):
        (out / f"frame_{i:03d}.png").write_bytes(img.data)
    write_json(out / "meta.json", {"view": view, "seed": seed, "prompt": prompt, "frames": len(images),
                                   "size": size, "seconds": round(time.time() - t, 1)})
    store.log_run({"stage": "hero.orbit", "plan": plan, "asset": asset_id, "seed": seed,
                   "frames": len(images), "seconds": round(time.time() - t, 1)})
    return out


def _silhouette(im: Image.Image, tol: int = 28) -> np.ndarray:
    """Object pixels: clearly different from the background colour, estimated from the
    frame's border (Wan's "white" is an off-white a little darker than the input's)."""
    px = np.asarray(im.convert("RGB")).astype(int)
    border = np.concatenate([px[0], px[-1], px[:, 0], px[:, -1]])
    bg = np.median(border, axis=0)
    m = np.abs(px - bg).max(axis=2) > tol
    m[:4], m[-4:], m[:, :4], m[:, -4:] = False, False, False, False  # VAE edge specks
    return m


def _box(mask: np.ndarray, min_px: int = 3) -> tuple[int, int, int, int] | None:
    """Bounding box over rows/columns with at least min_px object pixels (ignores specks)."""
    rows = np.nonzero(mask.sum(axis=1) >= min_px)[0]
    cols = np.nonzero(mask.sum(axis=0) >= min_px)[0]
    if not len(rows) or not len(cols):
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _signature(im: Image.Image, mask: np.ndarray, n: int = 48) -> np.ndarray:
    """Grey image of the object's bounding box, resized to n x n, zero-mean unit-norm."""
    box = _box(mask)
    if not box:
        return np.zeros(n * n)
    crop = im.convert("L").crop(box).resize((n, n))
    v = np.asarray(crop, float).ravel()
    v -= v.mean()
    return v / (np.linalg.norm(v) or 1)


def analyse(orbit_dir: Path, drift_tol: float = 0.12) -> dict:
    """Drift and loop-closure checks; picks 0/90/180/270-degree frames if the turn closes."""
    files = sorted(orbit_dir.glob("frame_*.png"))
    ims = [Image.open(f) for f in files]
    masks = [_silhouette(im) for im in ims]
    rows = []
    for m in masks:
        box = _box(m)
        rows.append({"area": float(m.mean()),
                     "cx": (box[0] + box[2]) / 2 / m.shape[1] if box else None,
                     "bottom": box[3] / m.shape[0] if box else None,
                     "height": (box[3] - box[1]) / m.shape[0] if box else 0.0,
                     "touches_edge": bool(box and (box[0] <= 4 or box[1] <= 4 or
                                                   box[2] >= m.shape[1] - 4 or box[3] >= m.shape[0] - 4))})
    # A turntable keeps the object's horizontal centre and its base fixed. Its silhouette
    # height legitimately changes (a house seen slightly from above shows more roof from
    # the side), so only a large change counts.
    h0, c0, b0 = rows[0]["height"], rows[0]["cx"], rows[0]["bottom"]
    drift = [i for i, r in enumerate(rows) if r["cx"] is None or r["touches_edge"]
             or abs(r["cx"] - c0) > drift_tol or abs(r["bottom"] - b0) > drift_tol / 2
             or not 0.7 <= r["height"] / max(h0, 1e-6) <= 1.4]
    sig = [_signature(im, m) for im, m in zip(ims, masks)]
    sim = [float(sig[0] @ s) for s in sig]
    half = len(files) // 2
    late = max(range(half, len(files)), key=lambda i: sim[i])
    mid_low = min(sim[len(files) // 4: 3 * len(files) // 4]) if len(files) > 8 else 1.0
    # A full turn comes back to look like frame 0 after looking unlike it in between. Wan
    # softens detail from frame to frame, so "like frame 0" is relative to an early frame.
    early = sim[min(4, len(sim) - 1)]
    closed = sim[late] >= 0.85 * early and sim[late] - mid_low > 0.25
    picks, turn = {}, None
    if closed:
        period = late
        picks = {deg: round(period * deg / 360) for deg in (0, 90, 180, 270)}
        # Turn or swing? In a real turn the 90- and 270-degree frames show opposite sides
        # (mirror images); a back-and-forth swing shows the same side twice.
        a, b = picks[90], picks[270]
        direct = float(sig[a] @ sig[b])
        mirrored = float(sig[a] @ _signature(ims[b].transpose(Image.FLIP_LEFT_RIGHT),
                                             masks[b][:, ::-1]))
        turn = {"direct": round(direct, 3), "mirrored": round(mirrored, 3), "is_turn": mirrored >= direct}
    result = {"frames": len(files), "drift_frames": drift, "drift_frac": round(len(drift) / len(files), 3),
              "similarity_to_first": [round(s, 3) for s in sim], "closure_frame": late,
              "closure_similarity": round(sim[late], 3), "mid_min_similarity": round(mid_low, 3),
              "closed": closed, "picks": picks, "turn_check": turn,
              "usable": bool(closed and turn and turn["is_turn"] and len(drift) / len(files) < 0.1)}
    for f in orbit_dir.glob("pick_*.png"):
        f.unlink()
    for deg, i in picks.items():
        ims[i].save(orbit_dir / f"pick_{deg:03d}.png")
    write_json(orbit_dir / "analysis.json", result)
    _contact(orbit_dir, ims, picks)
    return result


def _contact(orbit_dir: Path, ims: list[Image.Image], picks: dict, step: int = 5, cell: int = 128) -> None:
    idx = list(range(0, len(ims), step))
    cols = 9
    rows = (len(idx) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 16)), "grey")
    chosen = {v: k for k, v in picks.items()}
    d = ImageDraw.Draw(sheet)
    for k, i in enumerate(idx):
        im = ims[i].convert("RGB").resize((cell, cell))
        x, y = (k % cols) * cell, (k // cols) * (cell + 16)
        sheet.paste(im, (x, y + 16))
        near = [deg for f, deg in chosen.items() if abs(f - i) < step / 2 + 0.5]
        d.text((x + 3, y + 1), f"{i}" + (f"  {near[0]}deg" if near else ""), fill="yellow" if near else "white")
    sheet.save(orbit_dir / "contact.png")
