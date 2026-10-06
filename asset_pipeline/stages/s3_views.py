"""Stage 3: starred reference sheets -> individual views, cut out with SAM 3.1.

- object sheet: the asset's noun is segmented; the largest distinct objects, left to
  right, become views 1..3, each with the view the sheet asked for there (front / side /
  back; Flux often draws three-quarter views anyway, so treat it as a hint).
- kit sheet: every piece's noun is segmented; pieces are assigned left to right in the
  order the sheet asked for them (only when the count matches; otherwise unassigned).
- material sheets aren't cut (a texture isn't a 3D asset).
If SAM finds fewer objects than expected, the sheet is split on its white background
instead (segment.white_components).

Layout under the project:
    views/<plan>/<unit>/<sheet stem>_v<i>.png   the view on white, square, padded (see band_crops)
    views/<plan>/<unit>/meta.json               {"views": [{file, sheet, asset, position, asked, method, ...}]}
"""
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .. import config, review, segment
from ..comfy.client import ComfyClient
from ..jobs import check_canceled
from ..project import ProjectStore, read_json, write_json
from . import s2_refs

VIEWS = "views"
ASKED = ("front", "side", "back")


def _unit_for_sheet(store: ProjectStore, sheet_key: str) -> s2_refs.Unit:
    parts = Path(sheet_key).parts
    if len(parts) != 4 or parts[0] != s2_refs.REFS:
        raise ValueError(f"not a reference sheet: {sheet_key}")
    plan, key = parts[1], parts[2]
    for u in s2_refs.units(store, plan):
        if u.key == key:
            return u
    raise ValueError(f"{sheet_key}: no included asset or kit {key!r} in plan {plan} any more")


def view_dir(store: ProjectStore, unit: s2_refs.Unit) -> Path:
    return store.root / VIEWS / unit.plan / unit.key


def _dedupe(dets: list[segment.Detection]) -> list[segment.Detection]:
    """Largest first; drop masks mostly inside one already kept (a part of an object, or
    the same object found under two nouns)."""
    kept = []
    for d in sorted(dets, key=lambda d: -d.area_frac):
        if all(segment.overlap(d.mask, k.mask) <= 0.5 for k in kept):
            kept.append(d)
    return kept


def band_crops(sheet: Image.Image, dets: list[segment.Detection], white: int = 245,
               pad_frac: float = 0.08) -> list[Image.Image]:
    """One crop per view (dets sorted left to right): the sheet's full-height band between
    the midpoints to the neighbouring views, trimmed to its non-white content, padded to a
    white square. Masks are only used to find the views: tight masks drop white or thin
    parts (snow on a shrub, the flowers in a planter), and TRELLIS removes the white
    background itself (BiRefNet)."""
    w, h = sheet.size
    px = np.asarray(sheet)
    edges = [0] + [(dets[i - 1].bbox[2] + dets[i].bbox[0]) // 2 for i in range(1, len(dets))] + [w]
    crops = []
    for i, d in enumerate(dets):
        x0, x1 = edges[i], edges[i + 1]
        if x1 <= x0:  # overlapping boxes: fall back to the view's own box
            x0, x1 = d.bbox[0], d.bbox[2]
        band = px[:, x0:x1].min(axis=2) < white
        ys, xs = np.nonzero(band)
        if not len(xs):
            bx0, by0, bx1, by1 = d.bbox
        else:
            bx0, by0, bx1, by1 = x0 + xs.min(), ys.min(), x0 + xs.max() + 1, ys.max() + 1
        side = int(max(bx1 - bx0, by1 - by0) * (1 + 2 * pad_frac))
        sq = Image.new("RGB", (side, side), "white")
        region = sheet.crop((bx0, by0, bx1, by1))
        sq.paste(region, ((side - region.width) // 2, (side - region.height) // 2))
        crops.append(sq)
    return crops


def cut(store: ProjectStore, sheet: str, client: ComfyClient | None = None) -> list[dict]:
    """Cut one sheet into views (replacing earlier views of that sheet)."""
    key = review.rel(store, sheet)
    unit = _unit_for_sheet(store, key)
    if unit.kind == "material":
        return []
    c = client or ComfyClient(config.backends()["comfyui"]["url"])
    path = store.root / key
    expected = len(unit.assets) if unit.kind == "kit" else len(ASKED)
    nouns = list(dict.fromkeys((a.noun or a.name) for a in unit.assets))
    t = time.time()
    dets = []
    for noun in nouns:
        check_canceled()
        dets += segment.detect(c, path, noun, max_dets=expected + 3)
    dets = _dedupe(dets)
    if dets:  # stray specks and props next to the object are much smaller than the views
        biggest = dets[0].area_frac
        dets = [d for d in dets if d.area_frac >= 0.2 * biggest]
    method = "sam"
    if len(dets) < expected:
        fallback = segment.white_components(path)
        if len(fallback) > len(dets):
            dets, method = fallback, "white"
    dets = sorted(dets[:expected], key=lambda d: (d.bbox[0] + d.bbox[2]) / 2)

    out = view_dir(store, unit)
    out.mkdir(parents=True, exist_ok=True)
    meta = read_json(out / "meta.json", default=None) or {"plan": unit.plan, "unit": unit.key, "views": []}
    stale = [v for v in meta["views"] if v["sheet"] == key]
    review.forget(store, [(out / v["file"]).relative_to(store.root).as_posix() for v in stale])
    for v in stale:
        (out / v["file"]).unlink(missing_ok=True)
    meta["views"] = [v for v in meta["views"] if v["sheet"] != key]

    views = []
    sheet_img = Image.open(path).convert("RGB")
    for i, (d, crop) in enumerate(zip(dets, band_crops(sheet_img, dets))):
        f = out / f"{Path(key).stem}_v{i + 1}.png"
        crop.save(f)
        if unit.kind == "kit":
            asset = unit.assets[i].id if len(dets) == expected else None
            asked = None
        else:
            asset, asked = unit.assets[0].id, ASKED[i] if len(dets) == len(ASKED) else None
        views.append({"file": f.name, "sheet": key, "asset": asset, "position": i + 1,
                      "asked": asked, "method": method, "bbox": list(d.bbox),
                      "area_frac": round(d.area_frac, 4)})
    meta["views"] += views
    write_json(out / "meta.json", meta)
    store.log_run({"stage": "views.cut", "sheet": key, "unit": unit.key, "n": len(views),
                   "expected": expected, "method": method, "seconds": round(time.time() - t, 1)})
    return views


def cut_starred(store: ProjectStore, log=print) -> int:
    """Cut every starred sheet that has no views yet. Returns the number of views made."""
    n = 0
    for key in review.starred(store, s2_refs.REFS + "/"):
        try:
            unit = _unit_for_sheet(store, key)
        except ValueError as e:
            log(f"skip {e}")
            continue
        if unit.kind == "material":
            continue
        meta = read_json(view_dir(store, unit) / "meta.json", default=None) or {"views": []}
        if any(v["sheet"] == key for v in meta["views"]):
            continue
        log(key)
        n += len(cut(store, key))
    return n


def views(store: ProjectStore, plan: str, asset_id: str) -> list[dict]:
    """Views of one asset (from its own sheets and from kit sheets), with project-relative
    keys under "key"."""
    out = []
    for d in sorted((store.root / VIEWS / plan).glob("*")):
        meta = read_json(d / "meta.json", default=None) or {"views": []}
        for v in meta["views"]:
            if v["asset"] == asset_id and (d / v["file"]).is_file():
                out.append(v | {"key": (d / v["file"]).relative_to(store.root).as_posix()})
    return out
