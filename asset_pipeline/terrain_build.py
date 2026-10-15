"""Terrain beyond the city, built from a terrain plan (terrain_plan.py: ranges fitted to the locked
shots' skylines). Writes site/terrain_far/:

    far.npz        the extended terrain: x0, y0, cell, h (rows south -> north), domain metres
    outer.npz      a low-detail outer ring to the horizon (NaN inside the far domain)
    core.png       the 8 km core heightmap with the ranges' foothills blended in (16-bit; the
                   layout's terrain.heightmap points here once applied)
    build.json     ranges, crest factors, the sky check, erosion stats

Shaping (no vertical clipping anywhere):
  1. each range is a crest line (heights from the frames' angles at its distance) with a ridge
     cross-section and a foothill apron that rises from the plain (wider, lower, eased), so a
     wide shot reads one landform
  2. sky fit: from every frame-locked camera a cell may rise only to the angle of that frame's
     lowest sky pixel in its column. Where a range breaks that, its crest factor (per crest
     sample) drops, spread along the crest by a min-filter and a smoothing over >= 1 km, and
     the whole range profile scales with it: slopes stay natural, no cliff, no flat top.
     Repeated until no cell breaks a sky.
  3. erosion (stream power + hillslope diffusion + thermal talus) on the added relief only, then
     the sky check again.
"""
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import sparse
from scipy.ndimage import gaussian_filter, gaussian_filter1d, map_coordinates, minimum_filter1d
from scipy.sparse.linalg import spsolve
from scipy.spatial import cKDTree

from . import terrain_plan as tp
from .project import ProjectStore, read_json, write_json

FAR_DIR = "site/terrain_far"


# --- sky constraint --------------------------------------------------------------------------

def first_sky_run(sky: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per column, the first run of sky from the top: (start row, one past its end); start = end
    = 0 where the column has none. Sky reflected in water lower down is not part of it."""
    h, w = sky.shape
    start = np.where(sky.any(0), np.argmax(sky, 0), 0)
    rows = np.arange(h)[:, None]
    after = (rows >= start) & ~sky
    end = np.where(after.any(0), np.argmax(after, 0), h)
    return start, np.where(sky.any(0), end, 0)


def sky_bands(store: ProjectStore, X: np.ndarray, Y: np.ndarray, base: np.ndarray,
              tol_px: float = 1.5) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per frame-locked shot, the heights (lo, hi) between which a cell would show in that frame's
    painted sky: lo at the bottom of the column's first sky run, hi at its top (land above hi is
    hidden behind what the frame paints there, or out of the frame), lo raised by tol_px pixels
    at the cell's distance. Cells the shot doesn't see, and columns without sky, get an empty
    band (inf, inf)."""
    from .stages import sw_site
    gb = {x["id"]: x for x in sw_site.load_greybox(store)["shots"]}
    bands = []
    for s in sw_site.load_layout(store).shots:
        if not s.locked or s.locked == "greybox":
            continue
        shot = gb[s.id]
        w, hh = shot["resolution"]
        lab, names, _ = tp._labels(store, s.locked)
        lab = np.asarray(Image.fromarray(lab).resize((w, hh), Image.NEAREST))
        start, end = first_sky_run(np.isin(lab, [k for k, v in names.items() if v == "sky"]))
        m = np.asarray(shot["matrix"], float)
        cam = m[:3, 3]
        f = shot["lens_mm"] / shot["sensor_mm"] * w
        c = (np.stack([X, Y, base], -1) - cam) @ m[:3, :3]
        zf = -c[..., 2]
        ok = zf > 1
        u = np.full(X.shape, -1)
        u[ok] = np.floor(w / 2 + f * c[ok][:, 0] / zf[ok]).astype(int)
        uc = np.clip(u, 0, w - 1)
        inside = ok & (u >= 0) & (u < w) & (end[uc] > start[uc])
        dist = np.hypot(X - cam[0], Y - cam[1])
        drop = dist * dist / (2 * tp.EARTH_R)

        def z_at(rows):
            d = tp._rays_cam(m, f, w, hh, uc.astype(float), rows.astype(float))
            return cam[2] + dist * d[..., 2] / np.maximum(np.hypot(d[..., 0], d[..., 1]), 1e-6) + drop
        tol = dist * tol_px / f                       # under tol_px pixels of land on the sky line doesn't count
        lo = np.where(inside, z_at(end[uc] - 0.5) + tol, np.inf)
        hi = np.where(inside, z_at(start[uc] - 0.5), np.inf)
        bands.append((lo, hi))
    return bands


def over_sky(H: np.ndarray, bands) -> tuple[np.ndarray, np.ndarray]:
    """Cells showing in some frame's sky, and the height each must drop to (the lowest lo among
    the bands it is in)."""
    over = np.zeros(H.shape, bool)
    need = np.full(H.shape, np.inf)
    for lo, hi in bands:
        b = (H > lo + 0.5) & (H < hi)
        over |= b
        need = np.where(b, np.minimum(need, lo), need)
    return over, need


# --- shaping ---------------------------------------------------------------------------------

def _fbm(n: int, seed: int, octaves=((6, 0.5), (18, 0.3), (54, 0.2))) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.zeros((n, n))
    gi = None
    for cells, amp in octaves:
        g = rng.random((cells + 1, cells + 1))
        gi = np.linspace(0, cells, n)
        out += amp * map_coordinates(g, np.meshgrid(gi, gi, indexing="ij"), order=3, mode="nearest")
    return out


MAX_MEAN_SLOPE_DEG = 35.0    # a range narrowed to clear a sky keeps its mean flank slope under this


def range_fields(ranges: list[dict], X: np.ndarray, Y: np.ndarray, base: np.ndarray, sea_level: float) -> list[dict]:
    """Per range, per cell: the nearest crest sample (si), the distance to the crest (dist), the
    crest's height above the plain there (zc), its width and the foothill apron's width, share and
    land weight (no apron over the sea)."""
    out = []
    pts = np.stack([X.ravel(), Y.ravel()], -1)
    land = np.clip((base - sea_level + 5.0) / 15.0, 0, 1)
    for r in ranges:
        P = np.array(r["crest"])
        seg, sid = [], []
        for k, (a, b) in enumerate(zip(P[:-1], P[1:])):
            n = max(2, int(np.hypot(*(b[:2] - a[:2])) / 15.0))
            t = np.linspace(0, 1, n, endpoint=False)[:, None]
            seg.append(a + (b - a) * t)
            sid.append(k + t[:, 0])
        D, S = np.concatenate(seg), np.concatenate(sid)
        dist, idx = cKDTree(D[:, :2]).query(pts)
        zc = np.maximum(D[idx, 2].reshape(X.shape) - base, 0.0)      # crest height above the plain here
        w = float(r["width_m"])
        out.append({"si": S[idx].reshape(X.shape), "dist": dist.reshape(X.shape), "zc": zc, "w": w,
                    "fw": float(r.get("foothill_width_m", 2.0 * w)), "share": float(r.get("foothill_share", 0.15)),
                    "land": land, "n": int(S.max()) + 2,
                    "g_min": min(1.0, float(np.percentile(zc[zc > 0], 95)) / (w * math.tan(math.radians(MAX_MEAN_SLOPE_DEG))))
                    if (zc > 0).any() else 1.0})
    return out


def _parts(fd, f, g, q):
    """A range's ridge and foothill relief with per-sample height (f), width (g) and foothill (q) factors."""
    si = fd["si"]
    at = lambda a: np.interp(si, np.arange(len(a)), a)              # noqa: E731
    fk, gk, qk = at(f), at(g), at(q)
    x = fd["dist"] / (fd["w"] * gk)
    ridge = fd["zc"] * fk * np.clip(1 - x ** 2, 0, 1) ** 1.4
    foot = fd["share"] * fd["zc"] * qk * np.exp(-3.0 * (fd["dist"] / fd["fw"]) ** 2) * fd["land"]
    return ridge, foot


def compose(fields, params, base, noise, city_w):
    """Base terrain plus each range's relief (ridge and foothills, by its factors), soft-max
    blended between ranges (~40 m knee), with multiplicative noise."""
    rel = np.zeros_like(base)
    for fd, (f, g, q) in zip(fields, params):
        ridge, foot = _parts(fd, f, g, q)
        add = np.maximum(ridge, foot) * (0.82 + 0.36 * noise)
        rel = np.log(np.exp(rel / 40.0) + np.exp(add / 40.0) - 1.0) * 40.0
    return base + np.maximum(rel, 0.0) * city_w


def _spread(req, win, sigma):
    """Caps per crest sample spread along the crest: min-filter, smoothing, never above a cap."""
    return np.minimum(gaussian_filter1d(minimum_filter1d(req, win, mode="nearest"), sigma, mode="nearest"), req)


def fit_sky(fields, base, noise, city_w, bands, cell: float, crest_step_m: float, falloff_m: float = 1000.0,
            min_relief_m: float = 3.0, passes: int = 16, log=print):
    """Per crest sample a height factor f, a width factor g and a foothill factor q that keep every
    range out of the frames' skies, without cliffs or flat tops. From full size, each pass takes
    the cells showing in a frame's sky (inside a band, relief >= min_relief_m) and, per cell:
      - mostly foothill apron: caps q;
      - on a flank (beyond 0.3 of the width): caps g, the range narrowing until the cell clears,
        but never below g_min (mean flank slope <= MAX_MEAN_SLOPE_DEG);
      - otherwise (near the crest, or g at its floor): caps f.
    The caps are absolute (no compounding) and spread along the crest (a min-filter over
    falloff_m, smoothing over falloff_m / 4, never above a cap): a change eases in over >= 1 km."""
    over0 = over_sky(base, bands)[0]
    win = max(3, int(falloff_m / crest_step_m) | 1)
    sig = max(1.0, falloff_m / 4 / crest_step_m)
    req = [{"f": np.ones(fd["n"]), "g": np.ones(fd["n"]), "q": np.ones(fd["n"])} for fd in fields]
    params = [(r["f"].copy(), r["g"].copy(), r["q"].copy()) for r in req]
    hist = []
    for it in range(passes + 1):
        H = compose(fields, params, base, noise, city_w)
        over, need = over_sky(H, bands)
        over &= ~over0
        hist.append({"pass": it, "cells_over_sky": int(over.sum()), "base_over_sky": int(over0.sum())})
        log(f"sky fit {it}: {int(over.sum())} cells in a frame's sky; f / g mean "
            + ", ".join(f"{p[0].mean():.2f}/{p[1].mean():.2f}" for p in params))
        if not over.any():
            break
        for k, fd in enumerate(fields):
            f, g, q = params[k]
            ridge, foot = _parts(fd, f, g, q)
            A = np.maximum(ridge, foot) * (0.82 + 0.36 * noise) * city_w
            bad = over & (A >= min_relief_m)
            if not bad.any():
                continue
            nz = (0.82 + 0.36 * noise[bad]) * city_w[bad]
            allow = np.maximum(need[bad] - base[bad], 0.0) / np.maximum(nz, 1e-6) * 0.98   # relief this cell may have
            idx = np.clip(np.round(fd["si"][bad]).astype(int), 0, fd["n"] - 1)
            rb, fb = ridge[bad], foot[bad]
            is_foot = fb >= rb
            # foothills: q scales the apron
            cap_q = np.clip(allow / np.maximum(fb, 1e-6), 0, 1) * np.interp(idx, np.arange(fd["n"]), q)
            np.minimum.at(req[k]["q"], idx[is_foot], cap_q[is_foot])
            # flanks: the width at which this cell's ridge relief drops to what's allowed
            x0 = fd["dist"][bad] / fd["w"]
            fk = np.interp(idx, np.arange(fd["n"]), f)
            ratio = np.clip(allow / np.maximum(fd["zc"][bad] * fk, 1e-6), 0, 1)       # p(x) must be <= ratio
            xs = np.sqrt(np.clip(1 - ratio ** (1 / 1.4), 0, 1))                        # p(xs) = ratio
            g_need = np.where(xs > 0, x0 / np.maximum(xs, 1e-6), 0.0)
            flank = ~is_foot & (x0 > 0.3) & (g_need >= fd["g_min"])
            np.minimum.at(req[k]["g"], idx[flank], g_need[flank])
            # near the crest (or the width at its floor): lower the crest
            crest = ~is_foot & ~flank
            prof = np.clip(1 - (fd["dist"][bad] / (fd["w"] * np.interp(idx, np.arange(fd["n"]), g))) ** 2, 0, 1) ** 1.4
            cap_f = np.clip(allow / np.maximum(fd["zc"][bad] * prof, 1e-6), 0, 1)
            np.minimum.at(req[k]["f"], idx[crest], cap_f[crest])
            req[k]["g"] = np.maximum(req[k]["g"], fd["g_min"])
            params[k] = (_spread(req[k]["f"], win, sig), np.maximum(_spread(req[k]["g"], win, sig), fd["g_min"]),
                         _spread(req[k]["q"], win, sig))
    return params, H, hist


# --- erosion ---------------------------------------------------------------------------------

def erode(h: np.ndarray, mask: np.ndarray, cell: float, iters: int = 40, k_sp: float = 2.0e-3,
          k_diff: float = 0.12, talus_deg: float = 38.0, log=print) -> dict:
    """Stream-power fluvial incision (dh = -k A^0.5 S, A from D8 flow accumulation), hillslope
    diffusion and a thermal step that lowers slopes steeper than talus_deg, applied where mask > 0
    (blended by it). In place."""
    n, m = h.shape
    N = n * m
    nbr = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    dl = np.array([math.hypot(a, b) * cell for a, b in nbr])
    ii, jj = np.mgrid[0:n, 0:m]
    talus = math.tan(math.radians(talus_deg))
    removed = 0.0
    for it in range(iters):
        hp = np.pad(h, 1, mode="edge")
        drops = np.stack([(h - hp[1 + a:1 + a + n, 1 + b:1 + b + m]) / dl[k] for k, (a, b) in enumerate(nbr)])
        best = drops.argmax(0)
        S = np.maximum(drops.max(0), 0.0)
        ri = np.clip(ii + np.array([a for a, _ in nbr])[best], 0, n - 1)
        rj = np.clip(jj + np.array([b for _, b in nbr])[best], 0, m - 1)
        recv = (ri * m + rj).ravel()
        src = np.arange(N)
        flow = S.ravel() > 0
        P = sparse.csr_matrix((np.ones(flow.sum()), (recv[flow], src[flow])), shape=(N, N))
        A = spsolve((sparse.identity(N, format="csr") - P).tocsc(), np.full(N, cell * cell)).reshape(n, m)
        inc = k_sp * np.sqrt(A) * S
        hr = h.ravel()[recv].reshape(n, m)
        inc = np.minimum(inc, np.maximum(h - hr, 0.0) * 0.5)           # never cut below the receiver
        lap = (np.roll(h, 1, 0) + np.roll(h, -1, 0) + np.roll(h, 1, 1) + np.roll(h, -1, 1) - 4 * h)
        dh = (-inc + k_diff * lap) * mask
        h += dh
        # thermal: slopes steeper than talus are lowered by a quarter of the excess
        hp = np.pad(h, 1, mode="edge")
        drop = np.stack([(h - hp[1 + a:1 + a + n, 1 + b:1 + b + m]) - talus * dl[k] for k, (a, b) in enumerate(nbr)])
        ex = np.maximum(drop.max(0), 0.0) * 0.25 * mask
        h -= ex
        removed += float((-dh[dh < 0]).sum() + ex.sum())
    log(f"erosion: {iters} iterations, {removed * cell * cell / 1e9:.2f} km3 moved")
    return {"iterations": iters, "moved_km3": round(removed * cell * cell / 1e9, 3)}


# --- the build -------------------------------------------------------------------------------

def build(store: ProjectStore, extent: float = 24000.0, cell: float = 40.0, outer_extent: float = 64000.0,
          outer_cell: float = 320.0, seed: int = 11, log=print) -> dict:
    """The far terrain from site/terrain_plan/proposal.json's ranges (see the module doc)."""
    from .greybox import Heights
    from .stages import sw_site
    t0 = time.time()
    layout = sw_site.load_layout(store)
    prop = read_json(store.root / "site" / "terrain_plan" / "proposal.json")
    hs = Heights(sw_site.plan_terrain(store, layout), layout.terrain.extent_m)   # the tile without a far build
    sea = layout.sea_level
    enc = layout.city.enclosure
    cx, cy, R = enc.center[0], enc.center[1], enc.radius
    n = int(round(extent / cell)) + 1
    x0 = y0 = -extent / 2
    X, Y = np.meshgrid(x0 + np.arange(n) * cell, y0 + np.arange(n) * cell)
    base = np.maximum(np.asarray(hs.many(X, Y), float), sea)
    # the city stays as it is: no relief inside the walls, eased in from R + 300 to R + 1500 m
    rc = np.hypot(X - cx, Y - cy)
    city_w = np.clip((rc - (R + 300.0)) / 1200.0, 0, 1) ** 2
    ranges = prop["ranges"]
    fields = range_fields(ranges, X, Y, base, sea)
    noise = _fbm(n, seed)
    bands = sky_bands(store, X, Y, base)
    step = float(np.median([np.hypot(*(np.diff(np.array(r["crest"])[:, :2], axis=0)).T).mean() for r in ranges]))
    params, H, fit_hist = fit_sky(fields, base, noise, city_w, bands, cell, step, log=log)
    relief = np.clip((H - base) / 20.0, 0, 1)
    mask = gaussian_filter(relief, 2.0)
    er = erode(H, mask, cell, log=log)
    H = np.maximum(H, base)                                             # erosion never digs below the plain
    base_over = over_sky(base, bands)[0]
    over = over_sky(H, bands)[0] & ~base_over
    log(f"after erosion: {int(over.sum())} cells in a frame's sky (the plain itself: {int(base_over.sum())})")
    d = store.root / FAR_DIR
    d.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(d / "far.npz", x0=x0, y0=y0, cell=cell, h=H.astype(np.float32), base=base.astype(np.float32))
    # the outer ring: the plain and coast carried out to the horizon, with low rolling hills inland
    m_ = int(round(outer_extent / outer_cell)) + 1
    ox = -outer_extent / 2
    OX, OY = np.meshgrid(ox + np.arange(m_) * outer_cell, ox + np.arange(m_) * outer_cell)
    ob = np.maximum(np.asarray(hs.many(OX, OY), float), sea)
    roll = (_fbm(m_, seed + 1, ((4, 0.6), (12, 0.4))) * 160.0 + 20.0) * np.clip((ob - sea - 1.0) / 10.0, 0, 1)
    # the far field's edge carries on outwards (no seam at the far domain's border)
    fx = np.clip((OX - x0) / cell, 0, n - 1)
    fy = np.clip((OY - y0) / cell, 0, n - 1)
    far_edge = map_coordinates(H, [fy, fx], order=1)
    outside = np.maximum(np.abs(OX), np.abs(OY)) - extent / 2
    fade = np.clip(outside / 6000.0, 0, 1)
    O = np.maximum(far_edge * (1 - fade) + (ob + roll) * fade, sea)
    O = np.where((np.abs(OX) < extent / 2 - cell) & (np.abs(OY) < extent / 2 - cell), np.nan, O)
    np.savez_compressed(d / "outer.npz", x0=ox, y0=ox, cell=outer_cell, h=O.astype(np.float32))
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "extent_m": extent, "cell_m": cell, "outer_extent_m": outer_extent,
           "outer_cell_m": outer_cell, "ranges": [{"name": r["name"], "shot": r["shot"], "dist_m": r["dist_m"],
                                                    "width_m": r["width_m"], "crest_z": r["crest_z"],
                                                    "height_factor": [round(float(p[0].min()), 3), round(float(p[0].mean()), 3)],
                                                    "width_factor": [round(float(p[1].min()), 3), round(float(p[1].mean()), 3)],
                                                    "foothill_factor": [round(float(p[2].min()), 3), round(float(p[2].mean()), 3)]}
                      for r, p in zip(ranges, params)],
           "sky_fit": fit_hist, "erosion": er, "cells_over_sky_after_erosion": int(over.sum()),
           "max_z": round(float(H.max()), 1), "seconds": round(time.time() - t0, 1)}
    write_json(d / "build.json", rec)
    return rec


def core_heightmap(store: ProjectStore, z_range=(-60.0, 1200.0)) -> Path:
    """The core tile's raw terrain (before the node pads, which apply on load) with the far
    terrain's relief added, as a 16-bit PNG at the core's resolution: site/terrain_far/core.png."""
    from . import greybox
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    t = layout.terrain
    raw = greybox.make_terrain(t.model_copy(update={"heightmap": None, "far": None}))
    z = np.load(store.root / FAR_DIR / "far.npz")
    H, B, cell, x0 = z["h"], z["base"], float(z["cell"]), float(z["x0"])
    x, y = greybox.grid_coords(t)
    fx, fy = (x - x0) / cell, (y - float(z["y0"])) / cell
    rel = map_coordinates(H - B, [fy, fx], order=1)
    core = raw + np.maximum(rel, 0.0)
    p = store.root / FAR_DIR / "core.png"
    Image.fromarray(greybox.to_png16(np.clip(core, *z_range), z_range)).save(p)     # apply() refuses a clipped one
    return p


def apply(store: ProjectStore, z_range=(-60.0, 1200.0)) -> dict:
    """Point the layout's terrain at the build: the core heightmap and the far dir (the terrain
    spec from before the first apply is kept in site/terrain_far/terrain_before.json)."""
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    before = store.root / FAR_DIR / "terrain_before.json"
    if not before.exists():
        write_json(before, layout.terrain.model_dump(mode="json"))
    core = core_heightmap(store, z_range)
    a = np.asarray(Image.open(core))
    if a.min() == 0 or a.max() == 65535:
        raise ValueError(f"the core terrain reaches the edge of z_range {z_range}: widen it (no clipping)")
    layout.terrain.heightmap = core.relative_to(store.root).as_posix()
    layout.terrain.far = FAR_DIR
    layout.terrain.z_range = tuple(z_range)
    sw_site.save_layout(store, layout)
    return {"heightmap": layout.terrain.heightmap, "far": FAR_DIR, "z_range": list(z_range)}


# --- delivery: Unreal Landscape tiles, outer ring mesh -----------------------------------------

LANDSCAPE = {"resolution": 8129, "tiles": 4, "section_quads": 127, "sections_per_component": 2,
             "scale_z_cm": 500.0, "z_mid_m": 1150.0}


def landscape_tiles(store: ProjectStore, log=print) -> dict:
    """The core and the far terrain as one Unreal Landscape over the far domain: LANDSCAPE
    resolution samples (8129 = 32 components of 2 x 2 sections of 127 quads), written as 4 x 4
    16-bit PNG tiles for Landscape mode's tiled import (<name>_x<i>_y<j>.png, edges shared), with
    the import values in landscape.json. Rows run north -> south: Unreal's +Y is south."""
    from .greybox import Heights
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    core = Heights(sw_site.terrain(store, layout), layout.terrain.extent_m)
    half_core = layout.terrain.extent_m / 2
    z = np.load(store.root / FAR_DIR / "far.npz")
    H, cell, x0, y0 = z["h"].astype(np.float64), float(z["cell"]), float(z["x0"]), float(z["y0"])
    extent = cell * (H.shape[0] - 1)
    L = LANDSCAPE
    n, k = L["resolution"], L["tiles"]
    step = extent / (n - 1)
    tile = (n - 1) // k + 1
    sz = L["scale_z_cm"] / 128.0                       # cm per unit
    out = store.root / FAR_DIR / "landscape"
    out.mkdir(parents=True, exist_ok=True)
    zmin, zmax = np.inf, -np.inf
    for ty in range(k):
        for tx in range(k):
            ii = ty * (tile - 1) + np.arange(tile)
            jj = tx * (tile - 1) + np.arange(tile)
            X, Y = np.meshgrid(x0 + jj * step, (y0 + extent) - ii * step)       # rows north -> south
            far = map_coordinates(H, [(Y - y0) / cell, (X - x0) / cell], order=1)
            inside = (np.abs(X) <= half_core) & (np.abs(Y) <= half_core)
            h = np.where(inside, np.asarray(core.many(X, Y), float), far)
            zmin, zmax = min(zmin, float(h.min())), max(zmax, float(h.max()))
            v = np.clip(np.round(32768 + (h * 100 - L["z_mid_m"] * 100) / sz), 0, 65535).astype(np.uint16)
            Image.fromarray(v).save(out / f"terrain_x{tx}_y{ty}.png")
    lim = (32767 * sz / 100, )
    if zmin < L["z_mid_m"] - lim[0] or zmax > L["z_mid_m"] + lim[0]:
        raise ValueError(f"terrain {zmin:.0f}..{zmax:.0f} m outside the Landscape's z range: change scale_z_cm")
    rec = {"files": f"{FAR_DIR}/landscape/terrain_x<i>_y<j>.png", "tiles": [k, k], "tile_resolution": tile,
           "resolution": n, "sample_spacing_m": round(step, 4),
           "location_cm": [round(x0 * 100, 1), round(-(y0 + extent) * 100, 1), L["z_mid_m"] * 100],
           "scale_cm": [round(step * 100, 4), round(step * 100, 4), L["scale_z_cm"]],
           "section_size_quads": L["section_quads"], "sections_per_component": L["sections_per_component"],
           "components": (n - 1) // (L["section_quads"] * L["sections_per_component"]),
           "z_range_m": [round(zmin, 1), round(zmax, 1)], "material": "/Game/AP/Terrain/M_AP_LandscapeAuto",
           "note": "Landscape mode > Import from file, tiled heightmap: these values; World Partition on"}
    write_json(out / "landscape.json", rec)
    log(f"landscape: {k}x{k} tiles of {tile} px, {step:.3f} m spacing, z {zmin:.0f}..{zmax:.0f} m")
    return rec


def outer_glb(store: ProjectStore) -> Path:
    """The outer ring as a GLB in Blender world metres (Unreal imports it as one Nanite mesh)."""
    from . import blender, config
    d = store.root / FAR_DIR
    blender.run(config.REPO_ROOT / "scripts/blender_npz_glb.py", [d / "outer.npz", d / "outer.glb"], d / "outer_glb.log",
                "outer ring mesh")
    return d / "outer.glb"


def ue_assets(store: ProjectStore) -> dict:
    """The auto material (rock: Travertine009; grass and scrub: Plaster001's fine detail, tinted:
    the scans on disk have no vegetation) and the outer ring mesh in the Unreal project
    (unreal/ap_terrain.py, headless, no GPU)."""
    from . import config, ue
    acg = config.ap_root() / "materials" / "ambientcg"
    maps = lambda n: {"color": str(acg / n / f"{n}_2K-JPG_Color.jpg"), "normal": str(acg / n / f"{n}_2K-JPG_NormalDX.jpg"),  # noqa: E731
                      "rough": str(acg / n / f"{n}_2K-JPG_Roughness.jpg")}
    glb = store.root / FAR_DIR / "outer.glb"
    rep = ue.run_script(store, "ap_terrain.py", {"rock": maps("Travertine009"), "detail": maps("Plaster001"),
                                                 "outer_glb": str(glb) if glb.exists() else None}, "UE terrain assets")
    if not rep.get("ok"):
        raise RuntimeError(f"UE terrain assets failed: {rep.get('error')}\n{rep.get('traceback', '')}")
    write_json(store.root / FAR_DIR / "ue_assets.json", rep)
    return rep
