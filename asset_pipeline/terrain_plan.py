"""Terrain beyond the city, read off the locked shots (analysis only: nothing here changes the
layout or the terrain).

A painted ridge pins elevation angles, not distances: a range twice as far and twice as high
draws the same line. So each locked frame gives, per image column, the bearing and the elevation
angle of its far skyline (the top of the far land: hill / mountain labels, or trees and houses
in the far depth band), and the current terrain gives the highest elevation angle it reaches
along the same bearing. Where the frame's angle is above the terrain's, the terrain lacks hills.
A proposed range then picks a distance per sector and gets its crest height from the angle:
z = camera z + d tan(elevation) (+ the earth's drop, d^2 / 2R, which matters past ~5 km).

Labels: crop_refine's OneFormer ADE20K dump (site/library/seg). Output: site/terrain_plan/.
"""
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from .project import ProjectStore, read_json, write_json

EARTH_R = 6.371e6
FAR_LAND = {"mountain", "hill", "rock", "tree", "palm", "house", "building", "skyscraper", "plant", "field",
            "grass", "land", "earth", "tower", "hovel"}
COVER = {"forest": {"tree", "palm", "plant"}, "villages": {"house", "building", "skyscraper", "tower", "hovel"},
         "bare / rock": {"mountain", "rock", "earth", "land"}, "grass / fields": {"grass", "field", "hill"}}


def _labels(store: ProjectStore, frame: str):
    from . import crop_refine
    seg = store.root / "site" / "library" / "seg"
    names = {int(k): v.split(",")[0].split(";")[0].strip() for k, v in json.load(open(seg / "ade20k.json")).items()}
    lab = np.asarray(Image.open(seg / f"{crop_refine._slug(frame)}.oneformer.png"))
    dep = np.asarray(Image.open(seg / f"{crop_refine._slug(frame)}.depth.png")).astype(float)
    return lab, names, dep


def skyline(store: ProjectStore, frame: str, size, step: int = 8) -> dict:
    """Per column: the top non-sky row, whether it is far land, its label."""
    lab, names, dep = _labels(store, frame)
    lab = np.asarray(Image.fromarray(lab).resize(size, Image.NEAREST))
    dep = np.asarray(Image.fromarray(dep.astype(np.float32)).resize(size))
    sky = [k for k, v in names.items() if v == "sky"]
    nonsky = ~np.isin(lab, sky)
    far_cut = np.percentile(dep[nonsky], 12) if nonsky.any() else 0     # the farthest 12% of the land
    w, h = size
    cols, rows, kinds, far = [], [], [], []
    for x in range(0, w, step):
        c = nonsky[:, x]
        if not c.any():
            continue
        r = int(np.argmax(c))
        nm = names.get(int(lab[r, x]), "?")
        cols.append(x)
        rows.append(r)
        kinds.append(nm)
        far.append(nm in ("mountain", "hill"))       # skyline trees and roofs are mostly the city's own
    land = np.isin(lab, [k for k, v in names.items() if v in FAR_LAND]) & (dep <= far_cut)
    cover = {}
    tot = max(1, int(land.sum()))
    for k, labs in COVER.items():
        cover[k] = round(float(np.isin(lab, [i for i, v in names.items() if v in labs])[land].sum()) / tot, 2)
    return {"cols": cols, "rows": rows, "labels": kinds, "far": far, "cover": cover,
            "far_share": round(float(land.mean()), 4), "sea_skyline": round(kinds.count("sea") / max(1, len(kinds)), 2)}


def rays(shot: dict, us, vs) -> np.ndarray:
    m = np.asarray(shot["matrix"], float)
    w, h = shot["resolution"]
    f = shot["lens_mm"] / shot["sensor_mm"] * w
    d = np.stack([(np.asarray(us) + 0.5 - w / 2) / f, (h / 2 - np.asarray(vs) - 0.5) / f, -np.ones(len(us))], -1) @ m[:3, :3].T
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def terrain_angle(hs, cam, bearing: float, r_from: float, r_to: float, step: float = 20.0) -> tuple[float, float, float]:
    """The highest elevation angle (deg) the terrain reaches along a bearing between r_from and
    r_to metres from the camera (earth drop included), with its distance and height."""
    a = math.radians(bearing)
    d = np.arange(r_from, r_to, step)
    z = np.asarray(hs.many(cam[0] + d * math.cos(a), cam[1] + d * math.sin(a)), float)
    el = np.degrees(np.arctan2(z - d * d / (2 * EARTH_R) - cam[2], d))
    i = int(np.argmax(el))
    return float(el[i]), float(d[i]), float(z[i])


def crest_z(cam_z: float, el_deg: float, d: float) -> float:
    return cam_z + d * math.tan(math.radians(el_deg)) + d * d / (2 * EARTH_R)


def tile_exit(cam, bearing: float, half: float) -> float:
    """Distance from the camera to the terrain tile's edge along a bearing."""
    a = math.radians(bearing)
    dx, dy = math.cos(a), math.sin(a)
    ts = [t for t in ((half - cam[0]) / dx if dx > 1e-9 else None, (-half - cam[0]) / dx if dx < -1e-9 else None,
                      (half - cam[1]) / dy if dy > 1e-9 else None, (-half - cam[1]) / dy if dy < -1e-9 else None) if t]
    return min(t for t in ts if t > 0)


def analyse(store: ProjectStore, wall_margin: float = 50.0) -> dict:
    """Per locked shot: what it shows beyond the walls (frames: skyline per column against the
    current terrain), written to site/terrain_plan/analysis.json."""
    from .greybox import Heights
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    gb = {s["id"]: s for s in sw_site.load_greybox(store)["shots"]}
    hs = Heights(sw_site.terrain(store, layout), layout.terrain.extent_m)
    half = layout.terrain.extent_m / 2
    enc = layout.city.enclosure if layout.city else None
    centre, radius = (np.array(enc.center), enc.radius) if enc else (np.zeros(2), 0.0)
    out = []
    for s in layout.shots:
        if not s.locked:
            continue
        shot = gb[s.id]
        cam = np.asarray(shot["matrix"], float)[:3, 3]
        rec = {"shot": s.id, "tier": s.tier, "lock": "greybox" if s.locked == "greybox" else "frame",
               "frame": None if s.locked == "greybox" else s.locked, "camera": [round(float(v), 1) for v in cam]}
        if s.locked != "greybox":
            sk = skyline(store, s.locked, tuple(shot["resolution"]))
            d = rays(shot, sk["cols"], sk["rows"])
            cols = []
            for x, r, nm, far, v in zip(sk["cols"], sk["rows"], sk["labels"], sk["far"], d):
                b = math.degrees(math.atan2(v[1], v[0]))
                el = math.degrees(math.asin(v[2]))
                # the wall's distance along this bearing: beyond it is "beyond the walls"
                a = math.radians(b)
                rel = cam[:2] - centre
                p = rel @ np.array([math.cos(a), math.sin(a)])
                q = rel @ rel - radius * radius
                wall = max(0.0, -p + math.sqrt(max(0.0, p * p - q))) + wall_margin
                edge = tile_exit(cam, b, half)
                tel, tdist, tz = terrain_angle(hs, cam, b, wall, edge) if edge > wall else (-90.0, 0.0, 0.0)
                cols.append({"col": x, "row": r, "label": nm, "far": bool(far), "bearing": round(b, 2),
                             "elevation": round(el, 3), "terrain_elevation": round(tel, 3),
                             "terrain_at_m": round(tdist), "terrain_z": round(tz, 1), "wall_m": round(wall),
                             "tile_edge_m": round(edge)})
            far = [c for c in cols if c["far"]]
            rec |= {"cover": sk["cover"], "far_share": sk["far_share"], "sea_skyline": sk["sea_skyline"], "columns": cols,
                    "shows_beyond": bool(far) and len(far) >= 0.08 * len(cols),
                    "bearing_span": [min(c["bearing"] for c in far), max(c["bearing"] for c in far)] if far else None,
                    "elevation_span": [min(c["elevation"] for c in far), max(c["elevation"] for c in far)] if far else None,
                    "deficit_deg": round(float(np.median([c["elevation"] - c["terrain_elevation"] for c in far])), 2) if far else None}
        out.append(rec)
    d = store.root / "site" / "terrain_plan"
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "analysis.json", {"terrain": {"extent_m": layout.terrain.extent_m, "sea_dir": layout.terrain.sea_dir},
                                     "enclosure": {"center": centre.tolist(), "radius": radius}, "shots": out})
    return {"shots": out}


# --- the proposal ---------------------------------------------------------------------------

def _smooth(v: np.ndarray, k: int = 5) -> np.ndarray:
    if len(v) < k:
        return v
    pad = np.pad(v, k // 2, mode="edge")
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def crest(store: ProjectStore, analysis: dict, shot_id: str, dist: float, pad_deg: float = 15.0) -> list[list[float]]:
    """A range's crest from one shot's far skyline: [[x, y, z], ...] at `dist` metres from its
    camera, heights from the elevation angles (smoothed), padded pad_deg past each end and
    falling there."""
    s = next(x for x in analysis["shots"] if x["shot"] == shot_id)
    far = sorted((c for c in s["columns"] if c["far"]), key=lambda c: c["bearing"])
    b = np.array([c["bearing"] for c in far])
    el = _smooth(np.array([c["elevation"] for c in far]))
    cam = s["camera"]
    bs = np.concatenate([np.linspace(b[0] - pad_deg, b[0], 6)[:-1], b, np.linspace(b[-1], b[-1] + pad_deg, 6)[1:]])
    es = np.concatenate([np.linspace(el[0] * 0.55, el[0], 6)[:-1], el, np.linspace(el[-1], el[-1] * 0.55, 6)[1:]])
    out = []
    for bb, ee in zip(bs, es):
        a = math.radians(bb)
        out.append([cam[0] + dist * math.cos(a), cam[1] + dist * math.sin(a), crest_z(cam[2], ee, dist)])
    return out


def heightfield(ranges: list[dict], hs, extent: float, cell: float, sea_level: float, seed: int = 3) -> dict:
    """The proposed ranges as one heightfield over extent x extent metres (centred), cell metres
    a sample: per range, a ridge whose height falls off across the crest line over `width`
    (h = crest z x (1 - (s / width)^2)^1.4), with fBm detail. NaN where it doesn't rise above the
    current terrain (inside the tile) or above the sea (outside it): those keep what they have."""
    n = int(extent / cell) + 1
    x0 = y0 = -extent / 2
    X, Y = np.meshgrid(x0 + np.arange(n) * cell, y0 + np.arange(n) * cell)
    H = np.zeros_like(X)
    for r in ranges:
        P = np.array(r["crest"])
        # dense crest samples: nearest sample's distance and height
        seg = []
        for a, b in zip(P[:-1], P[1:]):
            k = max(2, int(np.hypot(*(b[:2] - a[:2])) / (cell / 2)))
            t = np.linspace(0, 1, k)[:, None]
            seg.append(a + (b - a) * t)
        D = np.concatenate(seg)
        from scipy.spatial import cKDTree
        dist, idx = cKDTree(D[:, :2]).query(np.stack([X.ravel(), Y.ravel()], -1))
        zc = D[idx, 2].reshape(X.shape)
        s = (dist / r["width_m"]).reshape(X.shape)
        H = np.maximum(H, zc * np.clip(1 - s ** 2, 0, 1) ** 1.4)
    rng = np.random.default_rng(seed)
    noise = np.zeros_like(H)
    for cells, amp in ((6, 0.5), (18, 0.3), (54, 0.2)):
        g = rng.random((cells + 1, cells + 1))
        gi = np.linspace(0, cells, n)
        from scipy.ndimage import map_coordinates
        noise += amp * map_coordinates(g, np.meshgrid(gi, gi, indexing="ij"), order=1)
    H = H * (0.85 + 0.3 * noise)
    half = extent / 2
    cur = np.asarray(hs.many(X, Y), float)
    inside = (np.abs(X) <= hs.extent / 2) & (np.abs(Y) <= hs.extent / 2)
    base = np.maximum(np.where(inside, cur, sea_level), sea_level)      # never below the sea
    keep = H > base + 5.0
    from scipy.ndimage import binary_dilation
    skirt = binary_dilation(keep) & ~keep                               # one cell down onto the base: no hanging edge
    out = np.where(keep, H, np.where(skirt, base - 1.0, np.nan))
    return {"x0": x0, "y0": y0, "cell": cell, "h": out,
            "max_z": float(np.nanmax(np.where(keep, H, np.nan))) if keep.any() else 0.0}


def propose(store: ProjectStore, spec: list[dict], extent: float = 24000.0, cell: float = 60.0,
            carve_locks: bool = True) -> dict:
    """Ranges from spec [{"name", "shot", "dist_m", "width_m"}] -> site/terrain_plan/proposal.npz
    (heightfield) and proposal.json (crests, heights, extent)."""
    from .greybox import Heights
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    hs = Heights(sw_site.terrain(store, layout), layout.terrain.extent_m)
    d = store.root / "site" / "terrain_plan"
    an = read_json(d / "analysis.json")
    ranges = []
    for r in spec:
        c = crest(store, an, r["shot"], r["dist_m"], r.get("pad_deg", 15.0))
        ranges.append(r | {"crest": [[round(v, 1) for v in p] for p in c],
                           "crest_z": [round(min(p[2] for p in c)), round(max(p[2] for p in c))]})
    hf = heightfield(ranges, hs, extent, cell, layout.sea_level)
    if carve_locks:
        hf["carved"] = carve(store, hf)
    np.savez_compressed(d / "proposal.npz", x0=hf["x0"], y0=hf["y0"], cell=hf["cell"], h=hf["h"])
    write_json(d / "proposal.json", {"extent_m": extent, "cell_m": cell, "max_z": round(hf["max_z"]), "ranges": ranges,
                                     "carved": hf.get("carved")})
    return {"ranges": ranges, "max_z": hf["max_z"]}


def preview(store: ProjectStore, shots: list[str]) -> Path:
    """Greybox previews of the shots with the proposal added, to site/terrain_plan/preview/."""
    from . import blender, config
    from .stages import sw_shots, sw_site
    d = store.root / "site" / "terrain_plan"
    gb = sw_site.load_greybox(store)
    colors = sw_shots.colors_for([s["id"] for s in gb["slots"]])
    write_json(d / "colors.json", colors)
    blender.run(config.REPO_ROOT / "scripts/blender_terrain_preview.py",
                ["--terrain", d / "proposal.npz", "--out-dir", d / "preview", "--colors", d / "colors.json",
                 "--shots", ",".join(shots), "--passes", "preview", "--preview-samples", "16"],
                d / "preview.log", "terrain preview", blend=sw_site.blend_path(store))
    return d / "preview"


def violations(store: ProjectStore, shots: list[str], threshold: int = 6, before: Path | None = None,
               after: Path | None = None, out: Path | None = None) -> dict:
    """Per frame-locked shot: the share of the frame where the proposal adds land (its preview
    differs from the current one) but the locked frame paints sky: a range there breaks the lock.
    Also fill: the share of the frame's painted mountain / hill the proposal covers (not sky in its
    preview), against fill_now for the current terrain. Needs preview/ and now/ renders of the shot."""
    from .stages import sw_site
    d = store.root / "site" / "terrain_plan"
    locks = {s.id: s.locked for s in sw_site.load_layout(store).shots}
    out_path, res = out, {}
    for sh in shots:
        a = np.asarray(Image.open((before or d / "now") / sh / "preview.png").convert("RGB"), int)
        b = np.asarray(Image.open((after or d / "preview") / sh / "preview.png").convert("RGB"), int)
        added = np.abs(a - b).max(-1) > threshold
        rec = {"changed": round(float(added.mean()), 4)}
        if locks.get(sh) and locks[sh] != "greybox":
            lab, names, _ = _labels(store, locks[sh])
            lab = np.asarray(Image.fromarray(lab).resize(added.shape[::-1], Image.NEAREST))
            sky = np.isin(lab, [k for k, v in names.items() if v == "sky"])
            rec["over_frame_sky"] = round(float((added & sky).mean()), 4)
            mtn = np.isin(lab, [k for k, v in names.items() if v in ("mountain", "hill")])
            sky_col = np.median(a[:4].reshape(-1, 3), 0)
            p_sky = np.abs(b - sky_col).max(-1) <= 4
            n_sky = np.abs(a - sky_col).max(-1) <= 4
            if mtn.sum() > 500:
                rec["fill_now"] = round(float((~n_sky)[mtn].mean()), 3)
                rec["fill"] = round(float((~p_sky)[mtn].mean()), 3)
        res[sh] = rec
    write_json(out_path or d / "changed_share.json", res)
    return res


def carve(store: ProjectStore, hf: dict) -> dict:
    """Keep the proposal out of every locked frame's painted sky: from each frame-locked camera,
    a cell inside the frame's horizontal view may rise only to the elevation angle of that
    frame's skyline at its bearing (the top of whatever the frame paints there: a roof, a tree
    or the ridge itself). Lowers hf["h"] in place; returns the cells lowered per shot."""
    from .stages import sw_site
    gb = {x["id"]: x for x in sw_site.load_greybox(store)["shots"]}
    h = hf["h"]
    n = h.shape[0]
    X, Y = np.meshgrid(hf["x0"] + np.arange(n) * hf["cell"], hf["y0"] + np.arange(n) * hf["cell"])
    lowered = {}
    for s in sw_site.load_layout(store).shots:
        if not s.locked or s.locked == "greybox":
            continue
        shot = gb[s.id]
        w, hh = shot["resolution"]
        lab, names, _ = _labels(store, s.locked)
        lab = np.asarray(Image.fromarray(lab).resize((w, hh), Image.NEAREST))
        sky = np.isin(lab, [k for k, v in names.items() if v == "sky"])
        cols = np.arange(w)
        # the lowest sky pixel per column (sky through arches and between columns counts too)
        top = np.where(sky.any(0), hh - 1 - np.argmax(sky[::-1], 0) + 1, 0)
        m = np.asarray(shot["matrix"], float)
        cam = m[:3, 3]
        f = shot["lens_mm"] / shot["sensor_mm"] * w
        # each cell's crest point into the camera: column, and the row its top lands on
        P = np.stack([X, Y, np.nan_to_num(h, nan=-1e4)], -1) - cam
        c = P @ m[:3, :3]
        zf = -c[..., 2]
        ok = (zf > 1) & np.isfinite(h)
        u = np.full(h.shape, -1)
        u[ok] = np.round(w / 2 + f * c[ok][:, 0] / zf[ok] - 0.5).astype(int)
        inside = ok & (u >= 0) & (u < w)
        if not inside.any():
            continue
        r_top = top[np.clip(u, 0, w - 1)]
        # the allowed height: the skyline row's ray at this cell's horizontal distance
        d = _rays_cam(m, f, w, hh, np.clip(u, 0, w - 1), r_top)
        dist = np.hypot(X - cam[0], Y - cam[1])
        hz = np.hypot(d[..., 0], d[..., 1])
        allowed = cam[2] + dist * d[..., 2] / np.maximum(hz, 1e-6) + dist * dist / (2 * EARTH_R)
        cut = inside & (h > allowed)
        lowered[s.id] = int(cut.sum())
        h[cut] = allowed[cut]
    return lowered


def _rays_cam(m, f, w, hh, us, vs) -> np.ndarray:
    d = np.stack([(us + 0.5 - w / 2) / f, (hh / 2 - vs - 0.5) / f, -np.ones(us.shape)], -1) @ m[:3, :3].T
    return d / np.linalg.norm(d, axis=-1, keepdims=True)
