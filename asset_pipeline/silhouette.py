"""Silhouette check of library builds against a locked frame: each build rendered alone from the
shot camera at its look pose (scripts/blender_silhouette.py) against that building's mask in the
frame (SAM 3.1 "building" inside a box: the entry's library crop from this shot, else the
render's box grown by 25%).

    iou      intersection over union of the two masks, leaving out what hides the building in
             the frame (its tree class, from the reconcile's class map)
    aspect   bounding-box width / height of each; match = the smaller over the larger
    score    iou x aspect match (1 = same outline)

Results per asset in 3d/library/<asset>/silhouette.json ({shot: result}); masks and overlays in
export/look/<shot>/silhouette/. `ap library silhouette`, and every `ap ue look` for its placed
builds.
"""
import json
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import blender, config
from .project import ProjectStore, read_json, write_json

NOUN = "building"


def _bbox(m: np.ndarray):
    ys, xs = np.nonzero(m)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None


def aspect(m: np.ndarray) -> float | None:
    b = _bbox(m)
    return round((b[2] - b[0]) / max(1, b[3] - b[1]), 3) if b else None


def score(frame: np.ndarray, render: np.ndarray, ignore: np.ndarray | None = None) -> dict:
    """IoU (over the pixels not in ignore: occluders in front of the building, such as trees, which
    the frame's mask leaves out), each mask's bounding-box aspect (w / h), their match and the score."""
    keep = ~ignore if ignore is not None else np.ones_like(frame)
    u = ((frame | render) & keep).sum()
    iou = float((frame & render & keep).sum() / u) if u else 0.0
    af, ar = aspect(frame), aspect(render)
    match = min(af / ar, ar / af) if af and ar else 0.0
    return {"iou": round(iou, 3), "aspect_frame": af, "aspect_build": ar, "aspect_match": round(match, 3),
            "score": round(iou * match, 3), "bbox_frame": _bbox(frame), "bbox_build": _bbox(render)}


def render_masks(store: ProjectStore, shot: dict, items: list[dict], out: Path) -> dict[str, np.ndarray]:
    job = {"camera": shot["matrix"], "lens_mm": shot["lens_mm"], "sensor_mm": shot["sensor_mm"],
           "resolution": shot["resolution"], "out_dir": str(out),
           "items": [{k: it[k] for k in ("key", "x", "y", "z", "yaw")} | {"glb": str(store.root / it["glb"])} for it in items]}
    jp = out / "silhouette_job.json"
    out.mkdir(parents=True, exist_ok=True)
    write_json(jp, job)
    blender.run(config.REPO_ROOT / "scripts/blender_silhouette.py", [jp], out / "silhouette.log", "silhouettes")
    return {it["key"]: np.asarray(Image.open(out / f"{it['key']}.png").convert("L")) > 127 for it in items}


def crop_box(store: ProjectStore, entry: str, shot_id: str, size) -> tuple[int, int, int, int] | None:
    """The entry's library crop in this shot's frame (library.json sources), in pixels."""
    lib = read_json(store.root / "site" / "library.json", default={}) or {}
    e = next((x for x in lib.get("entries", []) if x["id"] == entry), None)
    for s in (e or {}).get("sources", []):
        if s.get("shot") == shot_id and s.get("locked"):
            x0, y0, x1, y1 = s["bbox"]
            w, h = size
            return int(x0 * w), int(y0 * h), int(math.ceil(x1 * w)), int(math.ceil(y1 * h))
    return None


def frame_mask(store: ProjectStore, frame: Path, box, cache: Path) -> np.ndarray | None:
    """SAM 3.1's largest "building" inside box (cached by box)."""
    meta = cache.with_suffix(".json")
    if cache.exists() and (read_json(meta, default={}) or {}).get("box") == list(box):
        return np.asarray(Image.open(cache).convert("L")) > 127
    from . import segment
    from .comfy.client import ComfyClient
    dets = segment.detect_box(ComfyClient(config.backends()["comfyui"]["url"]), frame, NOUN, box)
    if not dets:
        return None
    m = dets[0].mask.copy()
    x0, y0, x1, y1 = box
    clip = np.zeros_like(m)
    clip[max(0, y0):y1, max(0, x0):x1] = True
    m &= clip
    cache.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((m * 255).astype(np.uint8)).save(cache)
    write_json(meta, {"box": list(box), "noun": NOUN, "detections": len(dets)})
    return m


def overlay(frame: Path, fm: np.ndarray, rm: np.ndarray, out: Path, box=None) -> None:
    """The frame dimmed, the frame's mask in cyan, the build's outline in orange, both in white."""
    a = np.asarray(Image.open(frame).convert("RGB").resize(fm.shape[::-1]), float) * 0.55
    a[fm & ~rm] = a[fm & ~rm] * 0.4 + np.array([40, 200, 220]) * 0.6
    a[rm & ~fm] = a[rm & ~fm] * 0.4 + np.array([240, 130, 40]) * 0.6
    a[fm & rm] = a[fm & rm] * 0.4 + np.array([240, 240, 240]) * 0.6
    im = Image.fromarray(a.astype(np.uint8))
    if box:
        x0, y0, x1, y1 = box
        pad = 20
        im = im.crop((max(0, x0 - pad), max(0, y0 - pad), min(im.width, x1 + pad), min(im.height, y1 + pad)))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)


def check(store: ProjectStore, shot: dict, items: list[dict], log=print) -> dict[str, dict]:
    """Silhouette results per placed build (items: look placements with "entry"); also saved per
    asset. shot: the greybox shot dict plus "locked" (its frame)."""
    if not shot.get("locked"):
        raise ValueError(f"{shot['id']} has no locked frame to compare with")
    frame = store.root / shot["locked"]
    w, h = shot["resolution"]
    out = store.root / "export" / "look" / shot["id"] / "silhouette"
    renders = render_masks(store, shot, items, out / "render")
    from . import frame_plan, look
    cls = look.frame_classes(store, shot["id"], (w, h))
    occ = np.isin(cls, [frame_plan.CLASSES.index("tree")]) if cls is not None else None
    res = {}
    for it in items:
        rm = renders[it["key"]]
        if not rm.any():
            res[it["key"]] = {"entry": it["entry"], "error": "the build is out of view"}
            continue
        box = crop_box(store, it["entry"], shot["id"], (w, h))
        src = "library crop"
        if box is None:
            x0, y0, x1, y1 = _bbox(rm)
            gx, gy = 0.25 * (x1 - x0), 0.25 * (y1 - y0)
            box, src = (max(0, int(x0 - gx)), max(0, int(y0 - gy)), min(w, int(x1 + gx)), min(h, int(y1 + gy))), "build box"
        fm = frame_mask(store, frame, box, out / "frame" / f"{it['key']}.png")
        if fm is None:
            res[it["key"]] = {"entry": it["entry"], "error": "SAM found no building in the frame there", "box": list(box)}
            continue
        ign = occ & ~fm if occ is not None else None
        r = score(fm, rm, ign) | {"occluded_px": int((ign & rm).sum()) if ign is not None else 0, "entry": it["entry"], "box": list(box), "box_from": src, "shot": shot["id"],
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        ov = out / "overlay" / f"{it['key']}.png"
        overlay(frame, fm, rm, ov, box)
        r["overlay"] = ov.relative_to(store.root).as_posix()
        res[it["key"]] = r
        log(f"{it['entry']} ({it['key']}): IoU {r['iou']}, aspect frame {r['aspect_frame']} / build "
            f"{r['aspect_build']}, score {r['score']}")
    for key, r in res.items():                 # per asset, the best-placed instance per shot
        if "score" not in r:
            continue
        p = store.root / "3d" / "library" / r["entry"] / "silhouette.json"
        d = read_json(p, default={}) or {}
        old = d.get(shot["id"])
        if not old or old.get("key") == key or r["score"] >= old.get("score", 0) or old.get("stale"):
            hist = (old or {}).get("history", []) + ([{k: old[k] for k in ("score", "iou", "at")}] if old else [])
            d[shot["id"]] = r | {"key": key, "history": hist[-10:]}
        write_json(p, d)
    return res


def load(store: ProjectStore, asset: str) -> dict:
    return read_json(store.root / "3d" / "library" / asset / "silhouette.json", default={}) or {}


def _rays(shot: dict, us, vs) -> np.ndarray:
    m = np.asarray(shot["matrix"], float)
    w, h = shot["resolution"]
    f = shot["lens_mm"] / shot["sensor_mm"] * w
    d = np.stack([(np.asarray(us) + 0.5 - w / 2) / f, (h / 2 - np.asarray(vs) - 0.5) / f, -np.ones(len(us))], -1) @ m[:3, :3].T
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def on_plane(shot: dict, us, vs, pose: dict, front_y: float) -> np.ndarray:
    """Pixels back-projected onto the slot's front plane (local y = front_y): local (x, z)."""
    cam = np.asarray(shot["matrix"], float)[:3, 3]
    a = math.radians(pose["yaw"])
    ex, ey = np.array([math.cos(a), math.sin(a), 0.0]), np.array([-math.sin(a), math.cos(a), 0.0])
    d = _rays(shot, us, vs)
    p0 = np.array([pose["x"], pose["y"], 0.0]) + ey * front_y
    t = ((p0 - cam) @ ey) / (d @ ey)
    P = cam + d * t[:, None]
    return np.stack([(P - p0) @ ex, P[:, 2]], -1)


def measure(shot: dict, mask: np.ndarray, pose: dict, front_y: float) -> dict:
    """A building's front in metres, from its mask: the front-left corner is the column where the
    roof line peaks (left of it the side face recedes), the right end is the mask's edge; both
    back-projected onto the slot's front plane (local y = front_y; at eye level the ground line
    is too flat to read depth from: one pixel there is metres of ground)."""
    cols = np.nonzero(mask.any(0))[0]
    top = np.array([np.nonzero(mask[:, c])[0].min() for c in cols])
    bot = np.array([np.nonzero(mask[:, c])[0].max() for c in cols])
    k = int(np.argmin(top))
    c0, c1 = int(cols[k]), int(cols[-1])
    r1 = int(np.median(bot[-8:]))
    (x0, zt), (_, zb) = on_plane(shot, [c0, c0], [top[k], bot[k]], pose, front_y)
    (x1, _), = on_plane(shot, [c1], [r1], pose, front_y)
    H = float(zt - zb)
    return {"length_m": round(float(x1 - x0), 2), "height_m": round(H, 2),
            "length_over_height": round(float(x1 - x0) / H, 2), "corner_px": [c0, int(top[k]), int(bot[k])]}
