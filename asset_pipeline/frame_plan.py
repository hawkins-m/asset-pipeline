"""Read a painted shot frame back onto the site plan: what the frame draws on the ground (water,
grass, paving, gravel) in world coordinates, to compare with the layout and greybox.

  1. classes  per pixel, from OneFormer ADE20K labels (crop_refine's dump), with SAM 3.1 masks
              for the nouns ADE20K is weak on (pools, lawns, gravel strips) laid over them
  2. rays     the shot camera (Blender matrix, lens, sensor) gives each pixel's world ray
  3. ground   ground pixels meet a plane at the ground height read from the shot's greybox depth
              pass; Depth Anything V2's inverse depth, fitted affinely to the greybox's, is the
              cross-check (`depth_check`)
  4. plan     the hits rasterised on a metre grid: per cell the class most pixels voted for
"""
import numpy as np

CLASSES = ("other", "water", "grass", "paving", "gravel", "building", "tree", "hill", "sky")
GROUND = ("water", "grass", "paving", "gravel")
COLOURS = {"other": (0, 0, 0), "water": (40, 120, 200), "grass": (90, 170, 60), "paving": (205, 200, 185),
           "gravel": (150, 130, 105), "building": (200, 90, 70), "tree": (30, 90, 40), "hill": (140, 110, 160),
           "sky": (170, 210, 240)}
# ADE20K names (first synonym) -> class
ADE = {"water": "water", "sea": "water", "river": "water", "lake": "water", "pool": "water", "swimming pool": "water",
       "fountain": "water",
       "grass": "grass", "field": "grass", "plant": "grass",
       "floor": "paving", "road": "paving", "sidewalk": "paving", "path": "paving", "runway": "paving",
       "stairs": "paving", "step": "paving",
       "sand": "gravel", "earth": "gravel", "rock": "gravel", "dirt track": "gravel", "gravel": "gravel",
       "building": "building", "house": "building", "skyscraper": "building", "wall": "building", "tower": "building",
       "column": "building", "arcade": "building", "windowpane": "building", "door": "building",
       "tree": "tree", "palm": "tree",
       "mountain": "hill", "hill": "hill",
       "sky": "sky"}


def classes(labels: np.ndarray, names: dict, sam: dict | None = None) -> np.ndarray:
    """Per-pixel class index (CLASSES) from ADE20K labels and {class: bool mask} SAM overrides
    (applied in the given order, later ones win)."""
    lut = np.zeros(max(int(k) for k in names) + 2, np.uint8)
    for k, v in names.items():
        c = ADE.get(v.split(",")[0].strip())
        if c:
            lut[int(k)] = CLASSES.index(c)
    out = lut[np.clip(labels, 0, len(lut) - 1)]
    for c, m in (sam or {}).items():
        out[m] = CLASSES.index(c)
    return out


def rays(matrix, lens_mm: float, sensor_mm: float, size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """The camera's origin and unit world ray per pixel [h, w, 3] (Blender camera: -Z forward,
    +Y up, horizontal sensor fit)."""
    m = np.asarray(matrix, float)
    w, h = size
    f = lens_mm / sensor_mm * w                       # focal length in pixels
    x = (np.arange(w) + 0.5 - w / 2) / f
    y = -(np.arange(h) + 0.5 - h / 2) / f
    X, Y = np.meshgrid(x, y)
    d = np.stack([X, Y, -np.ones_like(X)], -1) @ m[:3, :3].T
    return m[:3, 3], d / np.linalg.norm(d, axis=-1, keepdims=True)


def plane_hits(origin, dirs: np.ndarray, z: float) -> tuple[np.ndarray, np.ndarray]:
    """Where each ray meets the horizontal plane at height z: points [h, w, 3] and the distance
    along the ray (inf where it never does)."""
    dz = dirs[..., 2]
    t = np.where(dz < -1e-6, (z - origin[2]) / np.where(dz < -1e-6, dz, -1), np.inf)
    return origin + dirs * t[..., None], t


def ground_height(origin, dirs: np.ndarray, greybox_z: np.ndarray, ground: np.ndarray) -> float:
    """The ground's height from the greybox depth pass (camera Z, 0 = sky) over the frame's
    ground pixels: the median world z of their greybox hits."""
    fwd = dirs[dirs.shape[0] // 2, dirs.shape[1] // 2]
    cosang = dirs @ fwd                                # camera Z -> distance along the ray
    sel = ground & (greybox_z > 0)
    if sel.sum() < 50:
        raise ValueError("too few ground pixels with greybox depth")
    t = greybox_z[sel] / np.maximum(cosang[sel], 1e-3)
    return float(np.median(origin[2] + dirs[sel][:, 2] * t))


def depth_check(rel_inv: np.ndarray, greybox_z: np.ndarray, fit_mask: np.ndarray, t_plane: np.ndarray,
                check_mask: np.ndarray, dirs: np.ndarray) -> dict:
    """Depth Anything's inverse depth fitted affinely to the greybox's 1/Z over fit_mask, then
    compared with the plane distances over check_mask: median and 90th percentile relative
    difference."""
    sel = fit_mask & (greybox_z > 0)
    A = np.stack([rel_inv[sel], np.ones(sel.sum())], 1)
    a, b = np.linalg.lstsq(A, 1.0 / greybox_z[sel], rcond=None)[0]
    inv = a * rel_inv + b
    fwd = dirs[dirs.shape[0] // 2, dirs.shape[1] // 2]
    t_da = np.where(inv > 1e-4, 1.0 / np.maximum(inv, 1e-4), np.inf) / np.maximum(dirs @ fwd, 1e-3)
    ok = check_mask & np.isfinite(t_plane) & np.isfinite(t_da)
    rel = np.abs(t_da[ok] - t_plane[ok]) / t_plane[ok]
    return {"fit": [float(a), float(b)], "pixels": int(ok.sum()),
            "median_rel": round(float(np.median(rel)), 3) if ok.any() else None,
            "p90_rel": round(float(np.percentile(rel, 90)), 3) if ok.any() else None}


def plan(points: np.ndarray, cls: np.ndarray, mask: np.ndarray, bounds, cell: float = 1.0) -> np.ndarray:
    """Rasterise classed ground hits on a grid over bounds (x0, y0, x1, y1) in metres: per cell
    the class with most votes (0 = no hit). Row 0 is the north edge (y1)."""
    x0, y0, x1, y1 = bounds
    W, H = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
    p, c = points[mask], cls[mask]
    i = ((y1 - p[:, 1]) / cell).astype(int)
    j = ((p[:, 0] - x0) / cell).astype(int)
    ok = (i >= 0) & (i < H) & (j >= 0) & (j < W)
    votes = np.zeros((len(CLASSES), H, W), np.int32)
    np.add.at(votes, (c[ok], i[ok], j[ok]), 1)
    out = votes.argmax(0).astype(np.uint8)
    out[votes.sum(0) == 0] = 0
    return out


def regions(grid: np.ndarray, cls: str, bounds, cell: float = 1.0, min_area: float = 20.0) -> list[dict]:
    """Connected regions of one class on the plan: area (m2), centre and bbox in world metres."""
    from scipy import ndimage
    x0, _, _, y1 = bounds
    lab, n = ndimage.label(grid == CLASSES.index(cls))
    out = []
    for k, sl in enumerate(ndimage.find_objects(lab), 1):
        area = float((lab[sl] == k).sum() * cell * cell)
        if area < min_area:
            continue
        ii, jj = np.nonzero(lab == k)
        out.append({"class": cls, "area_m2": round(area), "centre": [round(x0 + (jj.mean() + 0.5) * cell, 1),
                                                                     round(y1 - (ii.mean() + 0.5) * cell, 1)],
                    "bbox": [round(x0 + jj.min() * cell, 1), round(y1 - (ii.max() + 1) * cell, 1),
                             round(x0 + (jj.max() + 1) * cell, 1), round(y1 - ii.min() * cell, 1)]})
    return sorted(out, key=lambda r: -r["area_m2"])


def plan_inverse(cls: np.ndarray, matrix, lens_mm: float, sensor_mm: float, z: float, bounds, cell: float = 1.0,
                 max_dist: float = 250.0) -> np.ndarray:
    """The plan by inverse warp: each cell centre on the ground plane at height z, projected into
    the frame, takes the class of the pixel it lands on when that pixel is a ground class (else
    0: behind a building or a tree, or out of view). Dense at any distance, unlike splatting
    pixels forward (far rows leave streaks)."""
    m = np.asarray(matrix, float)
    h, w = cls.shape
    x0, y0, x1, y1 = bounds
    W, H = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
    X, Y = np.meshgrid(x0 + (np.arange(W) + 0.5) * cell, y1 - (np.arange(H) + 0.5) * cell)
    P = np.stack([X, Y, np.full_like(X, z)], -1) - m[:3, 3]
    c = P @ m[:3, :3]                                  # camera frame: x right, y up, -z forward
    f = lens_mm / sensor_mm * w
    zf = -c[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = (w / 2 + f * c[..., 0] / zf).astype(int)
        v = (h / 2 - f * c[..., 1] / zf).astype(int)
    ok = (zf > 0.5) & (u >= 0) & (u < w) & (v >= 0) & (v < h) & (np.linalg.norm(P, axis=-1) < max_dist)
    out = np.zeros((H, W), np.uint8)
    k = cls[np.clip(v, 0, h - 1), np.clip(u, 0, w - 1)]
    grd = np.isin(k, [CLASSES.index(g) for g in GROUND])
    out[ok & grd] = k[ok & grd]
    return out
