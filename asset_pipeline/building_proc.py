"""Procedural buildings (PLAN.md "Build split"): the library's architectural types built in
Blender at real dimensions from a design brief read off their reference sheet.

The brief (the project's library/kits/<asset>.json, kind "building"):
    form      hall_vault | cube | basilica | podium_tower | perimeter | row (building_gen)
    dims      [w, d, h] metres (the entry's size: greybox or catalog)
    sheet, position, view
              the sheet and the view the brief was read from: a near-front view (`front_view`
              picks the most frontal one), so the bay and storey rhythm can be measured;
              position "all" takes the whole sheet (a row of houses drawn as one strip)
    measured  what `measure` read off the view: palette, plain patches per storey band (base,
              wall, top), the frontality score, bay and storey periods, and the bay width
              relative to the storey height (scale-free: it doesn't depend on how big the sheet
              drew the building)
    features  the parts the sheet shows, over building_gen.TYPE_FEATURES (what every building
              of the form gets): facades in the facade grammar (building_parts.FACADE), set-backs,
              accents, roof gardens and each form's own parts
    params    form-level numbers (storey_h, bay_m), over the measured rhythm
    colors    optional rgb for frame / wood / planting / glass / trim; patches: optional
              [x0, y0, x1, y1] per base / wall / top, over the measured ones
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import kit_proc
from .building_gen import FORMS, GENERATORS, features
from .project import ProjectStore, read_json, write_json

LUM = np.array([0.299, 0.587, 0.114])


# --- reading the sheet view ----------------------------------------------------------------

def _object_mask(a: np.ndarray) -> np.ndarray:
    """Pixels that are the building (not the white sheet ground)."""
    m = (a.min(2) < 232) | (a.max(2) - a.min(2) > 18)
    return ndimage.binary_opening(m, iterations=2)


def palette(a: np.ndarray, mask: np.ndarray, k: int = 5, seed: int = 0) -> list[dict]:
    """The view's main colours (k-means on building pixels): [{rgb, share, lum}] by share."""
    px = a[mask].reshape(-1, 3).astype(np.float64)
    if len(px) > 40000:
        px = px[np.random.default_rng(seed).choice(len(px), 40000, replace=False)]
    c = px[np.random.default_rng(seed).choice(len(px), k, replace=False)]
    for _ in range(20):
        lab = np.argmin(((px[:, None] - c[None]) ** 2).sum(2), 1)
        c = np.array([px[lab == i].mean(0) if (lab == i).any() else c[i] for i in range(k)])
    share = np.bincount(lab, minlength=k) / len(lab)
    out = [{"rgb": [int(round(v)) for v in c[i]], "share": round(float(share[i]), 3),
            "lum": round(float(c[i] @ LUM), 1)} for i in range(k)]
    return sorted(out, key=lambda x: -x["share"])


def plain_patch(a: np.ndarray, mask: np.ndarray, size: int = 48, rows: tuple[int, int] | None = None) -> list[int] | None:
    """The plainest square patch of building surface (lowest detail, typical tone, not a
    shadow), optionally within a band of rows: [x0, y0, x1, y1] in the view's pixels. Tries
    smaller squares when no clean one of `size` fits between the openings."""
    lum = a @ LUM
    r0, r1 = rows or (0, mask.shape[0])
    band = mask[r0:r1]
    if band.sum() < 50:
        return None
    med = float(np.median(lum[r0:r1][band]))
    W = mask.shape[1]
    found = None
    for s in (size, size * 2 // 3, size // 2, size // 3):
        s = max(8, min(s, (r1 - r0) - 1, W // 3))
        best, bb = None, None
        for y in range(r0, max(r0 + 1, r1 - s), max(2, s // 8)):
            for x in range(0, W - s, max(2, s // 8)):
                if mask[y:y + s, x:x + s].mean() < 0.999:
                    continue
                p = lum[y:y + s, x:x + s]
                score = p.std() + 1.5 * abs(p.mean() - med) + 0.5 * max(0.0, med - p.mean())
                if best is None or score < best:
                    best, bb = score, [x, y, x + s, y + s]
        if bb is not None:
            found = found or bb
            if lum[bb[1]:bb[3], bb[0]:bb[2]].std() < 6.0:
                return bb
    return found


def rhythm(a: np.ndarray, mask: np.ndarray, axis: int, lo: float = 6, hi: float | None = None) -> float | None:
    """The dominant period (px) of the openings along an axis (1 = across: bays, 0 = down:
    storeys): the strongest autocorrelation peak of the facade's darkness profile (windows are
    dark), so a window's two edges don't count as two bays."""
    dark = np.where(mask, 255.0 - a @ LUM, 0.0)
    cnt = mask.sum(0 if axis == 1 else 1).astype(np.float64)
    prof = dark.sum(0 if axis == 1 else 1) / np.maximum(cnt, 1)
    prof = prof[cnt > 0.3 * cnt.max()]
    n = len(prof)
    if n < 40:
        return None
    prof = prof - ndimage.uniform_filter1d(prof, max(9, n // 4))
    ac = np.correlate(prof, prof, "full")[n - 1:]
    ac /= ac[0] + 1e-9
    top = int(min(n // 2, hi or n // 2))
    peaks = [i for i in range(max(6, int(lo)), top) if ac[i] > ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > 0.15]
    return float(max(peaks, key=lambda i: ac[i])) if peaks else None


def frontality(a: np.ndarray, mask: np.ndarray) -> float:
    """How square-on a view is: the share of strong edges inside the building that run
    horizontal or vertical (within 4 degrees). A front elevation is mostly that; a
    three-quarter view's floor lines run slanted."""
    lum = ndimage.gaussian_filter(a @ LUM, 1.0)
    gx, gy = ndimage.sobel(lum, 1), ndimage.sobel(lum, 0)
    mag = np.hypot(gx, gy)
    inner = ndimage.binary_erosion(mask, iterations=4)
    sel = inner & (mag > np.percentile(mag[inner], 85)) if inner.sum() > 100 else inner
    if sel.sum() < 50:
        return 0.0
    ang = np.degrees(np.arctan2(gy[sel], gx[sel])) % 90.0
    return round(float(((ang < 4) | (ang > 86)).mean()), 3)


def measure(view: Path, dims, storey_h: float = 3.4) -> dict:
    """What the sheet view says: palette, plain patches per storey band, frontality, and the
    rhythm. bay_m is the bay period as a share of the storey period times the brief's storey
    height (scale-free); counts and periods outside plausible ranges are dropped."""
    a = np.asarray(Image.open(view).convert("RGB")).astype(np.float64)
    m = _object_mask(a)
    ys, xs = np.nonzero(m)
    out = {"palette": palette(a, m), "patch_px": plain_patch(a, m), "frontality": frontality(a, m)}
    if not len(xs):
        return out
    y0, y1 = int(ys.min()), int(ys.max())
    bh = y1 - y0
    sub = (slice(y0, y1 + 1), slice(int(xs.min()), int(xs.max()) + 1))
    sp = rhythm(a[sub], m[sub], 0)
    storeys = round(bh / sp) if sp else None
    if storeys and not 2.6 <= dims[2] / storeys <= 7.0:
        storeys, sp = None, None
    # bays: a period between half a storey and four storeys (not the gaps between volumes)
    bp = rhythm(a[sub], m[sub], 1, *((0.5 * sp, 4.0 * sp) if sp else (6, None)))
    bay_m = round(float(storey_h * bp / sp), 2) if (bp and sp) else None
    if bay_m and not 2.5 <= bay_m <= 15.0:
        bay_m = None
    n = storeys or max(2, round(dims[2] / storey_h))
    band = bh / n                                         # storey bands: ground, the middle, the top
    out |= {"bbox_px": [int(xs.min()), y0, int(xs.max()), y1], "bay_px": bp, "storey_px": sp, "storeys": storeys,
            "bay_m": bay_m, "storey_h": round(float(dims[2] / storeys), 2) if storeys else None,
            "patches": {"base": plain_patch(a, m, 32, (int(y1 - band), y1)),
                        "wall": plain_patch(a, m, 48, (int(y0 + band), int(y1 - band))),
                        "top": plain_patch(a, m, 32, (y0, int(y0 + band)))}}
    return out


def cut_view(store: ProjectStore, sheet: str, position, asset: str) -> str:
    """One of a sheet's three views (0 left, 1 middle, 2 right; "all": the whole sheet) as the
    brief's view: the building's bounds there, on white (views/library/<asset>/<sheet>_p<pos>.png)."""
    im = Image.open(store.root / sheet).convert("RGB")
    a = np.asarray(im).astype(np.float64)
    W = im.width
    x0, x1 = (0, W) if position == "all" else (W * position // 3, W * (position + 1) // 3)
    m = _object_mask(a[:, x0:x1])
    m[:, :4] = m[:, -4:] = False
    lab, n = ndimage.label(m)
    if not n:
        raise ValueError(f"nothing drawn in view {position} of {sheet}")
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    keep = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 0.02 * sizes.max()])
    ys, xs = np.nonzero(keep)
    pad = 12
    box_ = (max(0, x0 + xs.min() - pad), max(0, ys.min() - pad), min(W, x0 + xs.max() + pad), min(im.height, ys.max() + pad))
    out = Path("views/library") / asset / f"{Path(sheet).stem}_p{position}.png"
    (store.root / out).parent.mkdir(parents=True, exist_ok=True)
    im.crop(box_).save(store.root / out)
    return out.as_posix()


def front_view(store: ProjectStore, sheets: list[str], asset: str, min_score: float = 0.55) -> dict:
    """Score every view of the sheets for frontality: {"best": (sheet, position, score) or None
    when none reaches min_score, "scores": [...]}."""
    scores = []
    for sh in sheets:
        for pos in (0, 1, 2):
            try:
                v = cut_view(store, sh, pos, asset)
            except ValueError:
                continue
            a = np.asarray(Image.open(store.root / v).convert("RGB")).astype(np.float64)
            scores.append((sh, pos, frontality(a, _object_mask(a))))
    best = max(scores, key=lambda s: s[2]) if scores else None
    return {"best": best if best and best[2] >= min_score else None, "scores": scores}


# --- materials and the build -----------------------------------------------------------------

def band_colour(a: np.ndarray, mask: np.ndarray, rows: tuple[int, int]) -> list[int]:
    """A storey band's wall colour: the median of its building pixels, leaving out the dark
    ones (windows, shadow) and the brightest (glare, sky in glass)."""
    r0, r1 = rows
    px = a[r0:r1][mask[r0:r1]]
    if len(px) < 50:
        px = a[mask]
    lum = px @ LUM
    lo, hi = np.percentile(lum, 45), np.percentile(lum, 95)
    sel = px[(lum >= lo) & (lum <= hi)]
    return [int(v) for v in np.median(sel if len(sel) else px, 0)]


def seamless_detail(patch: np.ndarray, colour, size: int = 256) -> np.ndarray:
    """A tileable texture: the patch's fine detail (its gradient and large shading removed) on
    the band's own colour, made seamless by cross-fading with a copy rolled by half (no mirrored
    kaleidoscope), resampled to `size`."""
    p = patch.astype(np.float64)
    h, w = p.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    A = np.stack([xx.ravel(), yy.ravel(), np.ones(h * w)], 1)
    out = np.empty_like(p)
    for c in range(3):                                   # remove a plane, then what's left of big shading
        coef = np.linalg.lstsq(A, p[..., c].ravel(), rcond=None)[0]
        flat = p[..., c] - (A @ coef).reshape(h, w)
        out[..., c] = flat - ndimage.gaussian_filter(flat, max(1.5, min(h, w) / 8))
    det = np.clip(out, -40, 40)
    r = np.roll(np.roll(det, h // 2, 0), w // 2, 1)
    # a smooth window (product of raised cosines): 1 in the middle, 0 on the borders, where the
    # rolled copy (whose seam is in the middle) takes over. A min(u, v) pyramid printed diamonds.
    m = ((0.5 - 0.5 * np.cos(2 * np.pi * (xx + 0.5) / w)) * (0.5 - 0.5 * np.cos(2 * np.pi * (yy + 0.5) / h)))[..., None]
    det = (det * m + r * (1 - m)) / np.sqrt(m ** 2 + (1 - m) ** 2)      # keep the detail's contrast through the blend
    tex = np.clip(np.array(colour, np.float64)[None, None] + det, 0, 255)
    return np.asarray(Image.fromarray(tex.astype(np.uint8)).resize((size, size), Image.LANCZOS))


def _band_rows(meas: dict) -> dict:
    x0, y0, x1, y1 = meas["bbox_px"]
    n = meas.get("storeys") or 4
    band = (y1 - y0) / n
    return {"base": (int(y1 - band), y1), "wall": (int(y0 + band), int(y1 - band)), "top": (y0, int(y0 + band))}


def _plain(rgb, rough, metal=0.0):
    return {"uv": "plain", "color": [(c / 255.0) ** 2.2 for c in rgb], "rough": rough, "metal": metal}


def materials(store: ProjectStore, brief: dict, maps: Path) -> dict:
    """base / wall / top tiles from the view's storey-band patches (wall's when a band has none);
    glass, trim, frame, planting and wood as plain materials in the palette's colours unless the
    brief names them."""
    view = store.root / brief["view"]
    meas = brief["measured"]
    pat = {**(meas.get("patches") or {}), **(brief.get("patches") or {})}
    wall_box = brief.get("patch_px") or pat.get("wall") or meas["patch_px"]
    tm = brief.get("tile_m", 4.0)
    out = {}
    a = np.asarray(Image.open(view).convert("RGB")).astype(np.float64)
    mask = _object_mask(a)
    rows = _band_rows(meas) if meas.get("bbox_px") else {}
    # pixels per metre on the view: from the storey period when measured, else the bbox width
    st = (brief.get("params") or {}).get("storey_h", 3.4)
    ppm = (meas["storey_px"] / st) if meas.get("storey_px") else ((meas["bbox_px"][2] - meas["bbox_px"][0]) / brief["dims"][0]
                                                                    if meas.get("bbox_px") else 20.0)
    col = brief.get("colors") or {}
    for name in ("wall", "base", "top"):
        bx = wall_box if name == "wall" else (pat.get(name) or wall_box)
        x0, y0, x1, y1 = bx
        colour = col.get(name) or band_colour(a, mask, rows.get(name, (0, a.shape[0])))
        tex = seamless_detail(a[y0:y1, x0:x1], colour)
        mp = kit_proc._save_maps(tex, maps / name, "tile", 1.5)
        out[name] = {**mp, "uv": "tile", "tile_m": round(max(1.0, (x1 - x0) / ppm), 2) if ppm else tm,
                     "colour": colour}
    out["vault"] = {**out["wall"], "tile_m": tm * 1.5}
    out["roof"] = {**out["wall"], "tile_m": tm * 2}
    pal = sorted(meas["palette"], key=lambda c: c["lum"])
    main = meas["palette"][0]["rgb"]
    dark = pal[0]["rgb"]
    out["glass"] = _plain(col.get("glass") or [int(0.6 * c + 0.4 * g) for c, g in zip(dark, (70, 80, 88))], 0.06)
    out["rail_glass"] = _plain(col.get("rail_glass") or [168, 180, 186], 0.05)       # balustrade glass reads light
    out["trim"] = _plain(col.get("trim") or [int(min(255, c * 1.02)) for c in out["wall"]["colour"]], 0.5)
    out["frame"] = _plain(col.get("frame") or [58, 52, 46], 0.4, 0.6)
    out["planting"] = _plain(col.get("planting") or [78, 98, 58], 0.9)
    out["wood"] = _plain(col.get("wood") or [150, 106, 70], 0.7)
    return out


def build(store: ProjectStore, asset: str, log=print, out: Path | None = None, dims=None) -> dict:
    """Build an asset's procedural building from its brief (measured once). dims overrides the
    brief's size (a variant, e.g. a taller cube); out the GLB path."""
    sp = kit_proc.spec_path(store, asset)
    brief = read_json(sp, default=None)
    if brief is None or brief.get("kind") != "building":
        raise FileNotFoundError(f"no building brief: {sp.relative_to(store.root)}")
    if brief["form"] not in FORMS:
        raise ValueError(f"form must be one of {FORMS}")
    if "measured" not in brief:
        brief["measured"] = measure(store.root / brief["view"], brief["dims"])
        write_json(sp, brief)
    d = kit_proc.out_dir(store, asset)
    maps = d / "procedural"
    mats = materials(store, brief, maps)
    meas = brief["measured"]
    sheet = {k: meas[k] for k in ("bay_m", "storey_h") if meas.get(k)}       # the sheet's rhythm, unless the brief sets it
    f = features(brief["form"], brief.get("features"))
    parts = GENERATORS[brief["form"]](dims or brief["dims"], sheet | brief.get("params", {}), f)
    return kit_proc.run_blender(store, asset, parts, mats, out or d / "procedural.glb", maps, log=log,
                                extra={"form": brief["form"]})


# catalog form -> generator (the forms with one so far)
FORM_OF = {"hall": "hall_vault", "cube": "cube", "podium_tower": "podium_tower", "tower": "podium_tower",
           "perimeter": "perimeter", "row": "row"}


def brief(store: ProjectStore, asset: str, sheet: str, position, form: str, dims, params=None, feats=None) -> dict:
    """Write an asset's building brief from one view of its sheet (measured now). An existing
    brief's features, params, colors and patches are kept unless new ones are given."""
    if form not in FORMS:
        raise ValueError(f"form must be one of {FORMS}")
    old = read_json(kit_proc.spec_path(store, asset), default=None) or {}
    v = cut_view(store, sheet, position, asset)
    st = (params or old.get("params") or {}).get("storey_h", features(form, feats or old.get("features")).get("storey_h", 3.4))
    b = {"kind": "building", "form": form, "dims": [float(x) for x in dims], "sheet": sheet, "position": position,
         "view": v, "params": params if params is not None else old.get("params", {}),
         "features": feats if feats is not None else old.get("features", {}),
         "measured": measure(store.root / v, dims, st)}
    for k in ("colors", "patches"):
        if k in old:
            b[k] = old[k]
    write_json(kit_proc.spec_path(store, asset), b)
    return b
