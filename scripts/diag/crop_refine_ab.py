"""A/B of library crop refinement on chosen entries (old id-pass crop vs refined).

    python scripts/diag/crop_refine_ab.py PROJECT ENTRY [ENTRY ...] [--seg DIR]

Needs the per-frame dumps of scripts/diag/seg_depth_dump.py (ADE20K labels from OneFormer
and Mask2Former, Depth Anything V2 Small) in --seg (default site/library/seg) and ComfyUI
for the SAM 3.1 box prompt. Per entry, on its best source frame and its largest instance:

  1. R  = the instance's id-pass region (where the greybox has it)
  2. L  = R and ADE20K building-like labels (building, house, wall, column, tower...),
          i.e. minus tree / sky / person / ground / water
  3. D  = L minus depth occluders: the painted depth is fitted to the greybox's own
          inverse depth over the frame's building pixels; pixels the painting puts clearly
          nearer than the greybox (a tree or another building in front) are dropped, then
          components smaller than 5% of the largest
  4. S  = SAM 3.1 inside D's box ("building"), kept where it overlaps D's dilation; D if SAM
          finds nothing

Writes frames/_ab_crops/<entry>.png (old crop | OneFormer refined | Mask2Former refined |
overlay), a sheet of all entries and results.json (share of R kept at each step, and the
final mask's tree / sky share by SAM).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_pipeline import segment  # noqa: E402
from asset_pipeline.comfy.client import connect  # noqa: E402
from asset_pipeline.project import ProjectStore, read_json  # noqa: E402
from asset_pipeline.stages import sw_library, sw_shots  # noqa: E402

KEEP = {"building", "house", "skyscraper", "wall", "column", "tower", "hovel", "door", "windowpane", "railing",
        "stairs", "step", "stairway", "awning", "canopy", "booth", "tent", "fountain", "sculpture", "bannister",
        "base", "arcade", "bridge", "pier", "shed", "ceiling", "fence", "balustrade", "kiosk", "stage", "counter",
        "signboard", "glass", "screen door"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _load_png(p, dtype=None):
    a = np.asarray(Image.open(p))
    return a.astype(dtype) if dtype else a


def refine(frame_path, shot_dir, R, labels, keep_ids, depth, client, noun="building"):
    """Steps 2-4. Returns dict of masks."""
    L = R & np.isin(labels, keep_ids)
    # greybox inverse depth (16-bit camera Z over 10 km; 0 = sky)
    zr = _load_png(shot_dir / "depth_raw.png", np.float64)
    zr = np.asarray(Image.fromarray(zr).resize(depth.shape[::-1], Image.NEAREST)) if zr.shape != depth.shape else zr
    hit = zr > 0
    inv = np.zeros_like(zr)
    inv[hit] = 1.0 / (zr[hit] / 65535.0 * 10000.0)
    fitp = hit & np.isin(labels, keep_ids)
    D = L.copy()
    if fitp.sum() > 500:
        x, y = inv[fitp], depth[fitp]
        # inverse depth from the painting is affine-invariant: fit y = a x + b, robustly
        sub = np.random.default_rng(0).choice(len(x), min(len(x), 20000), replace=False)
        A = np.stack([x[sub], np.ones(len(sub))], 1)
        coef = np.linalg.lstsq(A, y[sub], rcond=None)[0]
        for _ in range(2):
            res = y[sub] - A @ coef
            ok = np.abs(res) < 2.5 * np.median(np.abs(res)) + 1e-6
            coef = np.linalg.lstsq(A[ok], y[sub][ok], rcond=None)[0]
        pred = coef[0] * inv + coef[1]
        tau = max(0.06, 2.5 * float(np.median(np.abs(y - (coef[0] * x + coef[1])))))
        occl = (depth - pred) > tau
        D = L & ~occl
    lab, n = ndimage.label(D)
    if n:
        sizes = ndimage.sum(D, lab, range(1, n + 1))
        D = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 0.05 * sizes.max()])
    S = D
    if D.sum() > 50 and client is not None:
        ys, xs = np.nonzero(D)
        pad = 6
        box = (xs.min() - pad, ys.min() - pad, xs.max() + pad, ys.max() + pad)
        dets = segment.detect_box(client, frame_path, noun, box)
        if dets:
            near = ndimage.binary_dilation(D, iterations=12)
            m = np.zeros_like(D)
            for d in dets:
                if (d.mask & D).sum() >= 0.3 * d.mask.sum():
                    m |= d.mask
            m &= near
            if m.sum() > 0.3 * D.sum():
                S = m                            # SAM's outline, inside the dilated refined region
    return {"L": L, "D": D, "S": S}


def crop_of(frame, mask, out_px=480):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return Image.new("RGB", (out_px, out_px * 3 // 4), "grey")
    W, H = frame.size
    x0, x1, y0, y1 = xs.min() / W, xs.max() / W, ys.min() / H, ys.max() / H
    pad = 0.08 * max(x1 - x0, y1 - y0) + 0.01
    box = (int(max(0, x0 - pad) * W), int(max(0, y0 - pad) * H), int(min(1, x1 + pad) * W), int(min(1, y1 + pad) * H))
    dim = ImageEnhance.Brightness(frame).enhance(0.35)
    im = Image.composite(frame, dim, Image.fromarray(mask.astype(np.uint8) * 255)).crop(box)
    im.thumbnail((out_px, out_px * 3 // 4))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("entries", nargs="+")
    ap.add_argument("--seg")
    a = ap.parse_args()
    store = ProjectStore.open(a.project)
    seg = Path(a.seg) if a.seg else store.root / "site/library/seg"
    names = {int(k): v for k, v in json.loads((seg / "ade20k.json").read_text()).items()}
    keep_ids = [k for k, v in names.items() if v.split(",")[0].strip() in KEEP or v in KEEP]
    data = sw_library.load(store)
    by = {e["id"]: e for e in data["entries"]}
    client = connect()
    out = store.root / "frames/_ab_crops"
    out.mkdir(parents=True, exist_ok=True)
    results, rows = {}, []
    for eid in a.entries:
        e = by[eid]
        best = next(r for r in e["sources"] if r["frame"] == e["best"])
        shot, fkey = best["shot"], best["frame"]
        sd = sw_shots.shot_dir(store, shot)
        ids = read_json(sd / "ids.json")
        top = max(best["slots"], key=lambda s: ids["slots"][s]["frac"])        # the largest instance
        rgb = np.asarray(Image.open(sd / "ids.png").convert("RGB"))
        idx, nm, _ = sw_shots.decode_ids(rgb, {k: v["color"] for k, v in ids["slots"].items()})
        R = idx == nm.index(top)
        frame = Image.open(store.root / fkey).convert("RGB")
        if frame.size != R.shape[::-1]:
            R = np.asarray(Image.fromarray(R.astype(np.uint8) * 255).resize(frame.size)) > 127
        depth = _load_png(seg / f"{slug(fkey)}.depth.png", np.float64) / 65535.0
        tree = sw_library._sam_mask(store, fkey, "tree")
        sky = sw_library._sam_mask(store, fkey, "sky")
        res = {"frame": fkey, "instance": top, "R_px": int(R.sum())}
        tiles = [("old crop", Image.open(store.root / f"site/library/crops/{eid}.jpg").convert("RGB"))]
        masks = {}
        for model in ("oneformer", "mask2former"):
            labels = _load_png(seg / f"{slug(fkey)}.{model}.png")
            m = refine(store.root / fkey, sd, R, labels, keep_ids, depth, client)
            masks[model] = m
            n = max(int(R.sum()), 1)
            res[model] = {k: round(float(v.sum()) / n, 3) for k, v in m.items()} | {
                "trees_in_final": round(float((m["S"] & tree).sum()) / max(int(m["S"].sum()), 1), 3) if tree is not None else None,
                "sky_in_final": round(float((m["S"] & sky).sum()) / max(int(m["S"].sum()), 1), 3) if sky is not None else None}
            tiles.append((f"{model}: L {res[model]['L']:.2f} D {res[model]['D']:.2f} S {res[model]['S']:.2f}",
                          crop_of(frame, m["S"])))
        # overlay: R outline grey, labels-kept green, depth-dropped orange, SAM final blue (OneFormer)
        m = masks["oneformer"]
        ov = np.asarray(frame).astype(float) * 0.45
        for mm, c in ((R, (160, 160, 160)), (m["L"] & ~m["D"], (255, 140, 0)), (m["D"], (60, 200, 60)),
                      (m["S"] & ~m["D"], (60, 120, 255))):
            ov[mm] = ov[mm] * 0.3 + np.array(c) * 0.7
        tiles.append(("overlay: grey id, green kept, orange depth-cut, blue SAM-added",
                      crop_of(Image.fromarray(ov.astype(np.uint8)), R | m["S"])))
        w, h = 480, 380
        row = Image.new("RGB", (w * len(tiles), h), "white")
        for i, (label, im) in enumerate(tiles):
            t = Image.new("RGB", (w, h), "white")
            t.paste(im, ((w - im.width) // 2, 20))
            ImageDraw.Draw(t).text((4, 4), label, fill="black")
            row.paste(t, (i * w, 0))
        ImageDraw.Draw(row).text((4, h - 14), f"{eid}  ({fkey}, instance {top})", fill="black")
        row.save(out / f"{eid}.png")
        rows.append(row)
        results[eid] = res
        print(eid, json.dumps(res), flush=True)
    sheet = Image.new("RGB", (rows[0].width, sum(r.height for r in rows)), "white")
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height
    sheet.save(out / "sheet.png")
    (out / "results.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
