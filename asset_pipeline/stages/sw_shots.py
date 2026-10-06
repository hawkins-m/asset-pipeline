"""World mode, shots: cameras in greybox.blend -> per-shot control passes.

Shots are the cameras in the .blend (tagged ap_shot; untagged cameras are adopted by name
on extract). `ap shots add` writes a camera into the .blend and the layout.

Layout under the project:
    shots/<shot>/ids.png        flat slot colours (ids.json maps them; sky black)
    shots/<shot>/ids.json       per visible slot: colour, pixels, fraction, bbox (0-1)
    shots/<shot>/depth_raw.png  16-bit camera Z / DEPTH_SCALE m (sky 0)
    shots/<shot>/depth.png      control image: inverse depth, near white, sky black
    shots/<shot>/normal.png     camera-space normals
    shots/<shot>/canny.png      control image: white edges where the slot, the surface
                                orientation or the depth changes (no texture noise)
    shots/<shot>/preview.png    grey preview render
    shots/<shot>/meta.json      shot, greybox hash, checks/warnings, timing
    site/preview/<view>/preview.png  four aerial views of the whole greybox
"""
import math
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .. import blender, config
from ..project import ProjectStore, read_json, write_json
from . import sw_site

SHOTS = "shots"
SCRIPT = config.REPO_ROOT / "scripts" / "blender_shots.py"
DEPTH_SCALE = 10000.0         # m at 65535 in depth_raw.png (15 cm steps; the sea reaches ~5 km)
ID_MULT = 0x9E3779            # odd -> i * ID_MULT mod 2^24 is a bijection (spread colours)
UNTAGGED_INDEX = (1 << 24) - 1
PASSES = ("ids", "depth", "normal", "preview")
NORMAL_EDGE_DEG = 30.0
DEPTH_EDGE_REL = 0.08


def shot_dir(store: ProjectStore, shot: str) -> Path:
    return store.root / SHOTS / shot


def id_color(i: int) -> list[int]:
    v = (i * ID_MULT) & 0xFFFFFF
    return [v >> 16, (v >> 8) & 0xFF, v & 0xFF]


def colors_for(slot_ids: list[str]) -> dict[str, list[int]]:
    """Slot id -> RGB bytes, by position in greybox.json (1-based; 0 = sky)."""
    out = {sid: id_color(i + 1) for i, sid in enumerate(slot_ids)}
    out["__untagged__"] = id_color(UNTAGGED_INDEX)
    return out


def _key(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.int64)
    return (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]


# --- post-processing ---------------------------------------------------------------------

def decode_ids(rgb: np.ndarray, colors: dict[str, list[int]]) -> tuple[np.ndarray, list[str], int]:
    """Per-pixel slot index into the returned id list (-1 sky, -2 unknown colour)."""
    names = list(colors)
    lookup = {(c[0] << 16) | (c[1] << 8) | c[2]: i for i, c in enumerate(colors.values())}
    keys = _key(rgb)
    uniq, inv = np.unique(keys, return_inverse=True)
    mapped = np.array([-1 if k == 0 else lookup.get(int(k), -2) for k in uniq])
    idx = mapped[inv.reshape(keys.shape)]
    return idx, names, int((idx == -2).sum())


def depth_control(z: np.ndarray, hit: np.ndarray) -> np.ndarray:
    """Inverse depth normalised over the visible surfaces (near 1, far 0), sky 0: the
    convention the depth control nets were trained on (Depth Anything style)."""
    out = np.zeros(z.shape, np.float32)
    if hit.sum() < 2:
        return out
    inv = 1.0 / np.maximum(z[hit], 1e-3)
    lo, hi = np.percentile(inv, 0.5), np.percentile(inv, 99.5)
    out[hit] = np.clip((inv - lo) / max(hi - lo, 1e-9), 0, 1)
    return out


def edges(idx: np.ndarray, normal: np.ndarray, z: np.ndarray, hit: np.ndarray) -> np.ndarray:
    """1-px edge map: slot changes, creases (normal angle) and depth jumps."""
    e = np.zeros(idx.shape, bool)
    cos_t = math.cos(math.radians(NORMAL_EDGE_DEG))
    for axis in (0, 1):
        sl_a = (slice(None, -1), slice(None)) if axis == 0 else (slice(None), slice(None, -1))
        sl_b = (slice(1, None), slice(None)) if axis == 0 else (slice(None), slice(1, None))
        a_id, b_id = idx[sl_a], idx[sl_b]
        both = hit[sl_a] & hit[sl_b]
        diff = a_id != b_id
        crease = both & ((normal[sl_a] * normal[sl_b]).sum(-1) < cos_t)
        za, zb = z[sl_a], z[sl_b]
        jump = both & (np.abs(za - zb) > DEPTH_EDGE_REL * np.minimum(za, zb))
        e[sl_a] |= diff | crease | jump
    return e


def post(store: ProjectStore, shot: dict, colors: dict[str, list[int]]) -> dict:
    d = shot_dir(store, shot["id"])
    rgb = np.array(Image.open(d / "ids.png").convert("RGB"))
    idx, names, unknown = decode_ids(rgb, colors)
    raw = np.array(Image.open(d / "depth_raw.png")).astype(np.float64)
    z = raw / 65535.0 * DEPTH_SCALE
    hit = raw > 0
    n = np.array(Image.open(d / "normal.png").convert("RGB")).astype(np.float32) / 255 * 2 - 1
    n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)

    Image.fromarray((depth_control(z, hit) * 255).round().astype(np.uint8)).save(d / "depth.png")
    Image.fromarray(edges(idx, n, z, hit).astype(np.uint8) * 255).save(d / "canny.png")

    h, w = idx.shape
    slots = {}
    for i, sid in enumerate(names):
        m = idx == i
        cnt = int(m.sum())
        if not cnt:
            continue
        ys, xs = np.nonzero(m)
        slots[sid] = {"color": colors[sid], "pixels": cnt, "frac": round(cnt / idx.size, 6),
                      "bbox": [round(xs.min() / w, 4), round(ys.min() / h, 4),
                               round((xs.max() + 1) / w, 4), round((ys.max() + 1) / h, 4)]}
    id_hit = idx != -1
    mismatch = float((id_hit != hit).mean())
    ctrl = np.array(Image.open(d / "depth.png"))
    stats = {"hit_frac": round(float(hit.mean()), 4), "unknown_pixels": unknown,
             "pass_mismatch": round(mismatch, 5), "depth_std": round(float(ctrl[hit].std() / 255), 4) if hit.any() else 0.0,
             "z_range_m": [round(float(z[hit].min()), 2), round(float(z[hit].max()), 2)] if hit.any() else None,
             "visible_slots": len(slots)}
    warnings = []
    if stats["hit_frac"] < 0.01:
        warnings.append("the camera sees (almost) no geometry")
    if hit.any() and stats["depth_std"] < 0.02:
        warnings.append(f"depth is nearly flat (std {stats['depth_std']})")
    if unknown:
        warnings.append(f"{unknown} id pixels have no known colour (blended or stale ids)")
    if mismatch > 0.005:
        warnings.append(f"id and depth passes disagree on {mismatch:.2%} of pixels")
    if "__untagged__" in slots:
        warnings.append(f"untagged meshes cover {slots['__untagged__']['frac']:.1%} of the frame")
    write_json(d / "ids.json", {"shot": shot["id"], "size": [w, h], "slots": slots,
                                "sky_frac": round(float((idx == -1).mean()), 4)})
    return {"stats": stats, "warnings": warnings}


# --- render ------------------------------------------------------------------------------

def _run(store: ProjectStore, out: Path, colors: dict, passes, shots: list[str] | None = None,
         virtual: list[dict] | None = None, what: str = "shot passes") -> None:
    work = sw_site.site_dir(store) / "build"
    write_json(work / "colors.json", colors)
    args = ["--out-dir", out, "--colors", work / "colors.json", "--passes", ",".join(passes),
            "--depth-scale", DEPTH_SCALE]
    if shots:
        args += ["--shots", ",".join(shots)]
    if virtual:
        write_json(work / "virtual.json", virtual)
        args += ["--virtual", work / "virtual.json"]
    blender.run(SCRIPT, args, out / "blender.log", what, blend=sw_site.blend_path(store))


def render(store: ProjectStore, shots: list[str] | None = None) -> dict[str, dict]:
    """Render every pass for the given shots (default: all) and post-process them."""
    if sw_site.stale(store):
        sw_site.extract(store)
    gb = sw_site.load_greybox(store)
    by_id = {s["id"]: s for s in gb["shots"]}
    ids = shots or list(by_id)
    missing = [s for s in ids if s not in by_id]
    if missing:
        raise ValueError(f"no shot(s) {', '.join(missing)}; shots: {', '.join(by_id) or 'none'}")
    colors = colors_for([s["id"] for s in gb["slots"]])
    out = store.root / SHOTS
    t = time.time()
    _run(store, out, colors, PASSES, ids)
    results = {}
    for sid in ids:
        r = post(store, by_id[sid], colors)
        meta = {"shot": by_id[sid], "greybox_sha256": gb["blend_sha256"],
                "rendered": time.strftime("%Y-%m-%dT%H:%M:%S"), "passes": list(PASSES), **r}
        write_json(shot_dir(store, sid) / "meta.json", meta)
        results[sid] = meta
    store.log_run({"stage": "shots.render", "shots": ids, "seconds": round(time.time() - t, 1),
                   "warnings": {k: v["warnings"] for k, v in results.items() if v["warnings"]}})
    return results


def preview_site(store: ProjectStore, distance_frac: float = 0.42, elevation_deg: float = 32) -> list[Path]:
    """Four aerial previews (from the N, E, S, W) of the whole greybox."""
    if sw_site.stale(store):
        sw_site.extract(store)
    gb = sw_site.load_greybox(store)
    layout = sw_site.load_layout(store)
    t = layout.terrain
    dist = t.extent_m * distance_frac
    cams = []
    for name, ang in (("north", 90), ("east", 0), ("south", 270), ("west", 180)):
        a, el = math.radians(ang), math.radians(elevation_deg)
        pos = [dist * math.cos(el) * math.cos(a), dist * math.cos(el) * math.sin(a), t.city_z + dist * math.sin(el)]
        cams.append({"id": f"site-{name}", "pos": pos, "look_at": [0, 0, t.city_z], "lens_mm": 32,
                     "resolution": [1344, 768]})
    out = sw_site.site_dir(store) / "preview"
    _run(store, out, colors_for([s["id"] for s in gb["slots"]]), ["preview"],
         virtual=cams, shots=[c["id"] for c in cams], what="site preview")
    return [out / c["id"] / "preview.png" for c in cams]


def status(store: ProjectStore) -> list[dict]:
    """Every shot with whether its passes exist and match the current greybox."""
    gb = sw_site.load_greybox(store)
    rows = []
    for s in gb["shots"]:
        meta = read_json(shot_dir(store, s["id"]) / "meta.json", default=None)
        rows.append({"id": s["id"], "tier": s["tier"], "lens_mm": s["lens_mm"],
                     "resolution": s["resolution"], "district": s["district"],
                     "rendered": meta["rendered"] if meta else None,
                     "stale": bool(meta and meta["greybox_sha256"] != gb["blend_sha256"]),
                     "warnings": meta["warnings"] if meta else []})
    return rows


def remove(store: ProjectStore, shot: str) -> None:
    shutil.rmtree(shot_dir(store, shot), ignore_errors=True)
