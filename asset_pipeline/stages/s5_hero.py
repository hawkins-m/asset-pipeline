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


# --- Frame angles by render matching ---------------------------------------------------
# A hero asset also has a single-image TRELLIS mesh built from the same chosen view the
# orbit starts from. Rendered at known azimuths (scripts/blender_turntable.py: azimuth 0 =
# front, +90 = the asset's own left), it gives every orbit frame an angle: compare each
# frame with each render, then take the best SMOOTH path through the frames (Viterbi),
# because single frames can't tell a front from a look-alike back but the orbit can't
# jump 180 degrees between neighbouring frames.

SIDES = {"front": 0, "left": 90, "back": 180, "right": 270}


def _descriptor(im: Image.Image, h: int = 64, w: int = 192):
    """(mask, chroma) of the object scaled to a fixed HEIGHT on a wide canvas. A turntable
    keeps the height and changes the width (front vs side), so width must survive.
    Chroma (r, g over r+g+b) ignores brightness: Wan's painted shading and Blender's flat
    renders differ in light far more than in colour."""
    if im.mode == "RGBA":  # renders: transparent background -> white
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    m = _silhouette(im)
    box = _box(m)
    if not box:
        return None
    bw, bh = box[2] - box[0], box[3] - box[1]
    scale = (h * 0.9) / bh
    tw, th = min(w, max(1, round(bw * scale))), max(1, round(bh * scale))
    mask = np.asarray(Image.fromarray(m[box[1]:box[3], box[0]:box[2]].astype(np.uint8) * 255)
                      .resize((tw, th))) > 127
    rgb = np.asarray(im.crop(box).resize((tw, th)), float) + 1
    chroma = rgb[..., :2] / rgb.sum(axis=2, keepdims=True)
    M, C = np.zeros((h, w), bool), np.zeros((h, w, 2))
    x0, y0 = (w - tw) // 2, (h - th) // 2
    M[y0:y0 + th, x0:x0 + tw] = mask
    C[y0:y0 + th, x0:x0 + tw] = chroma
    return M, C


def _score(a, b) -> float:
    """Silhouette IoU (shape and width) plus colour-layout agreement inside the shared
    silhouette (1 - mean chroma distance, scaled)."""
    (ma, ca), (mb, cb) = a, b
    union = (ma | mb).sum()
    iou = (ma & mb).sum() / union if union else 0.0
    both = ma & mb
    if both.sum() < 20:
        return float(iou)
    dist = np.linalg.norm(ca[both] - cb[both], axis=1).mean()
    colour = max(0.0, 1 - dist / 0.15)
    return float(0.6 * iou + 0.4 * colour)


def match_angles(orbit_dir: Path, turntable_dir: Path, max_step_deg: float = 30,
                 smooth: float = 0.004, tol_deg: float = 12) -> dict:
    """Angle of every orbit frame from the asset's mesh renders; picks the best frame for
    each side (front / left / back / right) the orbit actually reaches."""
    meta = read_json(turntable_dir / "renders.json")
    step = meta["step"]
    azs = sorted({r["azimuth"] for r in meta["renders"]})
    elevs = sorted({r["elevation"] for r in meta["renders"]})
    frames = sorted(orbit_dir.glob("frame_*.png"))
    fdesc = [_descriptor(Image.open(f)) for f in frames]
    best = None
    for elev in elevs:  # the orbit's camera height is unknown: take the one that fits best
        rdesc = [_descriptor(Image.open(turntable_dir / f"e{int(elev):02d}_a{az:03d}.png")) for az in azs]
        E = np.array([[(_score(fd, rd) if fd and rd else 0.0) for rd in rdesc] for fd in fdesc])
        if best is None or E.max(axis=1).mean() > best[1].max(axis=1).mean():
            best = (elev, E)
    elev, E = best
    T, S = E.shape
    # Viterbi: maximise sum of scores minus smooth * (azimuth steps moved)^2, with moves
    # limited to max_step_deg per frame. Frame 0 is the chosen view = the mesh's front.
    d = np.arange(S)
    circ = np.minimum(np.abs(d[:, None] - d[None, :]), S - np.abs(d[:, None] - d[None, :]))
    trans = np.where(circ * step <= max_step_deg, -smooth * circ.astype(float) ** 2, -np.inf)
    V = np.full(S, -np.inf)
    V[0] = E[0, 0]
    back = np.zeros((T, S), int)
    for t in range(1, T):
        cand = V[:, None] + trans
        back[t] = cand.argmax(axis=0)
        V = cand.max(axis=0) + E[t]
    path = [int(V.argmax())]
    for t in range(T - 1, 0, -1):
        path.append(int(back[t][path[-1]]))
    path.reverse()
    angles = [azs[s] for s in path]
    # Unwrapped rotation: how far and which way the object turned.
    unwrapped = [0.0]
    for a, b in zip(angles, angles[1:]):
        unwrapped.append(unwrapped[-1] + ((b - a + 180) % 360 - 180))
    fit = [float(E[t, s]) for t, s in enumerate(path)]
    picks = {}
    for side, target in SIDES.items():
        near = [t for t, a in enumerate(angles) if abs((a - target + 180) % 360 - 180) <= tol_deg]
        if near:
            picks[side] = max(near, key=lambda t: fit[t])
    for f in orbit_dir.glob("view_*.png"):
        f.unlink()
    for side, t in picks.items():
        Image.open(frames[t]).save(orbit_dir / f"view_{side}.png")
    result = {"turntable": str(turntable_dir), "elevation": elev, "angles": angles,
              "fit": [round(x, 3) for x in fit], "mean_fit": round(float(np.mean(fit)), 3),
              "rotation_deg": round(unwrapped[-1], 1), "max_reach_deg": round(max(map(abs, unwrapped)), 1),
              "picks": picks, "sides": sorted(picks, key=lambda s: SIDES[s])}
    write_json(orbit_dir / "angles.json", result)
    return result



# --- Multi-view shape (Hunyuan3D-2mv in ComfyUI) ---------------------------------------
_SIDE_NODES = {"front": ("10", "11"), "left": ("12", "13"), "back": ("14", "15"), "right": ("16", "17")}


def multiview_mesh(views: dict[str, Path], out: Path, seed: int = 0, steps: int = 8,
                   client: ComfyClient | None = None) -> Path:
    """Shape (untextured GLB) from any subset of front / left / back / right images, via
    Hunyuan3D-2mv turbo in ComfyUI (GPU 1). "left" is the asset's own left side."""
    if "front" not in views:
        raise ValueError("a front view is required")
    c = client or ComfyClient(config.backends()["comfyui"]["url"])
    graph, manifest = workflow.load_template("hunyuan3d_mv")
    values = {"seed": seed, "steps": steps}
    for side, (load, enc) in _SIDE_NODES.items():
        if side in views:
            values[side] = c.upload_image(Path(views[side]))
        else:  # drop the unused view: its loader, its encoder and the conditioning input
            graph.pop(load)
            graph.pop(enc)
            graph["20"]["inputs"].pop(side)
            manifest = {**manifest, "bindings": {k: v for k, v in manifest["bindings"].items() if k != side}}
    graph = workflow.fill(graph, manifest, values)
    files = [f for f in c.run_files(graph, manifest["outputs"]) if f.filename.endswith(".glb")]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(files[0].data)
    return out



def render_turntable(glb: Path, out: Path, step: int = 5, elevations: str = "0,10,20,30",
                     size: int = 192) -> Path:
    """Blender headless (CPU-light Workbench): the asset's mesh at known azimuths."""
    import subprocess
    cmd = ["/snap/bin/blender", "-b", "--factory-startup", "--python",
           str(config.REPO_ROOT / "scripts" / "blender_turntable.py"), "--", str(glb), str(out),
           "--step", str(step), "--elevations", elevations, "--size", str(size)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not (out / "renders.json").is_file():
        raise RuntimeError(f"blender turntable failed: {r.stdout[-800:]}{r.stderr[-800:]}")
    return out


def trellis_mesh_for_chosen(store: ProjectStore, plan: str, asset_id: str) -> Path:
    """The newest TRELLIS GLB built from the asset's chosen view (the orbit's start)."""
    from . import s5_3d
    view = review.chosen(store, plan, asset_id)
    stem = Path(view).stem[:24] if view else None
    glbs = sorted((s5_3d.out_dir(store, plan, asset_id)).glob(f"{stem}_*.glb"),
                  key=lambda p: -p.stat().st_mtime) if stem else []
    if not glbs:
        raise ValueError(f"{plan}/{asset_id}: no TRELLIS mesh from the chosen view yet (Make 3D first)")
    return glbs[0]
