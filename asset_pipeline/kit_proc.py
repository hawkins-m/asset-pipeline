"""Procedural kit pieces: simple architectural kit (arch gates, colonnade bays) built in
Blender from a reference sheet's proportions at real dimensions, with PBR maps taken from the
sheet. TRELLIS guesses depth from one front view and collapses walls or leaves out a back
wall on these; geometry this simple is better built (TRELLIS stays for complex and irregular
assets).

A piece's spec lives in the project, library/kits/<asset id>.json:
    {"kind": "arch_gate" | "colonnade_bay", "params": {... metres ...},
     "facade": {"sheet": ..., "box_px": [x0, y0, x1, y1], "origin_px": [x, y], "m_per_px": s},
     "tile":   {"sheet": ..., "box_px": [...], "tile_m": 3.0}}
facade (optional): a front elevation on the sheet, projected onto the piece's front and back
faces; origin_px is the sheet pixel at local x = 0, z = 0 (the base's centre). tile: a plain
patch of the material, box-mapped on every other face.

Output: 3d/library/<asset id>/procedural.glb (+ procedural.json, the maps in procedural/).
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import blender, filestore
from .project import ProjectStore, read_json, write_json

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/blender_kit.py"
KINDS = ("arch_gate", "colonnade_bay")


def spec_path(store: ProjectStore, asset: str) -> Path:
    return store.root / "library/kits" / f"{asset}.json"


def out_dir(store: ProjectStore, asset: str) -> Path:
    return store.root / "3d/library" / asset


# --- maps -------------------------------------------------------------------------------

def _lum(a: np.ndarray) -> np.ndarray:
    return a[..., :3] @ np.array([0.299, 0.587, 0.114])


def _blur(a: np.ndarray, r: float) -> np.ndarray:
    return ndimage.gaussian_filter(a.astype(np.float64), r, mode="reflect")


def delight(rgb: np.ndarray, radius: float = 24.0, amount: float = 0.6) -> np.ndarray:
    """Even out the sheet's baked light (big shading gradients) and keep the fine detail."""
    lum = _lum(rgb) + 1.0
    low = _blur(lum, radius)
    gain = (np.median(lum) / low) ** amount
    return np.clip(rgb * gain[..., None], 0, 255)


def normal_map(rgb: np.ndarray, strength: float = 2.0) -> np.ndarray:
    """A tangent-space normal map from the image's fine luminance relief (joints, edges)."""
    h = _lum(rgb) / 255.0
    h = _blur(h, 0.8) - _blur(h, 12.0)
    gy, gx = np.gradient(h)
    n = np.dstack([-gx * strength * 10, gy * strength * 10, np.ones_like(h)])
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    return ((n * 0.5 + 0.5) * 255).astype(np.uint8)


def rough_map(rgb: np.ndarray, base: float = 0.72, spread: float = 0.18) -> np.ndarray:
    """Roughness: honed stone, rougher in dark joints and stains."""
    lum = _lum(rgb) / 255.0
    d = _blur(lum, 1.0) - _blur(lum, 16.0)
    r = np.clip(base - spread * d / max(float(np.abs(d).max()), 1e-6), 0.4, 0.95)
    return (np.dstack([r] * 3) * 255).astype(np.uint8)


def seamless(rgb: np.ndarray, size: int = 512) -> np.ndarray:
    """A tileable square from a patch: mirror it 2x2 (seams match by symmetry)."""
    p = np.asarray(Image.fromarray(rgb.astype(np.uint8)).resize((size // 2, size // 2), Image.LANCZOS))
    top = np.concatenate([p, p[:, ::-1]], 1)
    return np.concatenate([top, top[::-1]], 0)


def _save_maps(rgb: np.ndarray, d: Path, name: str, normal_strength: float = 2.0) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    paths = {"base": d / f"{name}_base.png", "normal": d / f"{name}_normal.png", "rough": d / f"{name}_rough.png"}
    Image.fromarray(rgb.astype(np.uint8)).save(paths["base"])
    Image.fromarray(normal_map(rgb, normal_strength)).save(paths["normal"])
    Image.fromarray(rough_map(rgb)).save(paths["rough"])
    return {k: str(v) for k, v in paths.items()}


def facade_maps(root: Path, f: dict, d: Path) -> tuple[dict, list[float]]:
    """The sheet's front elevation as maps, and its rect in local metres [x0, x1, z0, z1]."""
    x0, y0, x1, y1 = f["box_px"]
    rgb = np.asarray(Image.open(root / f["sheet"]).convert("RGB").crop((x0, y0, x1, y1))).astype(np.float64)
    m = _save_maps(delight(rgb), d, "facade")
    ox, oy = f["origin_px"]
    s = f["m_per_px"]
    return m, [(x0 - ox) * s, (x1 - ox) * s, (oy - y1) * s, (oy - y0) * s]


def tile_maps(root: Path, t: dict, d: Path) -> dict:
    x0, y0, x1, y1 = t["box_px"]
    rgb = np.asarray(Image.open(root / t["sheet"]).convert("RGB").crop((x0, y0, x1, y1))).astype(np.float64)
    return _save_maps(seamless(delight(rgb, radius=max(4.0, min(rgb.shape[:2]) / 4))), d, "tile", 1.5)


# --- geometry ---------------------------------------------------------------------------

def _box(x0, x1, y0, y1, z0, z1, mat="stone"):
    return {"kind": "box", "x0": x0, "x1": x1, "y0": y0, "y1": y1, "z0": z0, "z1": z1, "mat": mat}


def arch_gate(p: dict) -> list[dict]:
    """A monumental gate: one round-arched passage through a deep block, pilasters on the
    front and back faces, an archivolt and keystone, entablature band, stepped cornice, attic.
    p: w d plinth_h body_h span spring pilasters [[x, width], ...] pilaster_proj band_h
    cornice [[height, overhang], ...] attic_h attic_inset archivolt_w keystone [w, h]."""
    w, d, r = p["w"], p["d"], p["span"] / 2
    hx, hy = w / 2, d / 2
    ph, top = p["plinth_h"], p["plinth_h"] + p["body_h"]
    parts = [_box(-hx - 0.3, -r, -hy - 0.3, hy + 0.3, 0, ph), _box(r, hx + 0.3, -hy - 0.3, hy + 0.3, 0, ph),
             {"kind": "archwall", "x0": -hx, "x1": hx, "y0": -hy, "y1": hy, "z0": 0, "z1": top, "cx": 0.0,
              "span": p["span"], "spring": p["spring"], "seg": 24, "mat": "facade"}]
    pp = p["pilaster_proj"]
    for x, pw in p["pilasters"]:
        for y0, y1 in ((-hy - pp, -hy), (hy, hy + pp)):
            parts.append(_box(x - pw / 2, x + pw / 2, y0, y1, ph, top - 0.2, "facade"))
    aw = p["archivolt_w"]
    for y0, y1 in ((-hy - 0.12, -hy), (hy, hy + 0.12)):
        parts.append({"kind": "archband", "cx": 0.0, "span": p["span"], "spring": p["spring"], "width": aw,
                      "y0": y0, "y1": y1, "seg": 24, "mat": "facade"})
        kw, kh = p["keystone"]
        parts.append(_box(-kw / 2, kw / 2, y0 - (0.1 if y0 < 0 else 0), y1 + (0.1 if y0 >= 0 else 0),
                          p["spring"] + r - 0.1, p["spring"] + r + kh - 0.1, "facade"))
    z = top
    parts.append(_box(-hx - 0.1, hx + 0.1, -hy - 0.1, hy + 0.1, z, z + p["band_h"], "facade"))
    z += p["band_h"]
    for ch, ov in p["cornice"]:
        parts.append(_box(-hx - ov, hx + ov, -hy - ov, hy + ov, z, z + ch, "facade"))
        z += ch
    ai = p["attic_inset"]
    parts.append(_box(-hx + ai, hx - ai, -hy + ai, hy - ai, z, z + p["attic_h"] - 0.3, "facade"))
    parts.append(_box(-hx + ai - 0.15, hx - ai + 0.15, -hy + ai - 0.15, hy - ai + 0.15,
                      z + p["attic_h"] - 0.3, z + p["attic_h"], "facade"))
    return parts


def column(x, y, z, p: dict, seg: int = 32, mat: str = "stone") -> list[dict]:
    """A plain round column: square plinth, torus base, tapered shaft (entasis), echinus and
    abacus. p: d h plinth_h base_h cap_h abacus_h taper."""
    r, h = p["d"] / 2, p["h"]
    pl, bh, ch, ab = p["plinth_h"], p["base_h"], p["cap_h"], p["abacus_h"]
    shaft = h - pl - bh - ch - ab
    top_r = r * (1 - p.get("taper", 0.12))
    prof = [(r * 1.28, 0.0), (r * 1.32, bh * 0.35), (r * 1.18, bh * 0.75), (r, bh)]
    for i in range(1, 9):              # entasis: a slight swell low on the shaft
        t = i / 8
        prof.append((r - (r - top_r) * t ** 1.6, bh + shaft * t))
    prof += [(top_r * 1.05, bh + shaft + ch * 0.2), (top_r * 1.4, bh + shaft + ch)]
    parts = [_box(x - r * 1.45, x + r * 1.45, y - r * 1.45, y + r * 1.45, z, z + pl, mat),
             {"kind": "lathe", "x": x, "y": y, "z": z + pl, "profile": [list(q) for q in prof], "seg": seg, "mat": mat}]
    a = top_r * 1.55
    parts.append(_box(x - a, x + a, y - a, y + a, z + h - ab, z + h, mat))
    return parts


def colonnade_bay(p: dict) -> list[dict]:
    """One bay module of a stoa: a column on the front line at x = 0, the entablature and a
    cornice over the whole bay width, a roof slab back to a solid rear wall, a stone floor on a
    stylobate. Side edges are open, so modules tile along X every `w` metres.
    p: w d stylobate_h column {...} col_y ent_h ent_d cornice_h cornice_proj roof_h wall_t
    wall_base_h."""
    w, d = p["w"], p["d"]
    hx, hy = w / 2, d / 2
    sh = p["stylobate_h"]
    col = p["column"]
    zc = sh + col["h"]
    cy = p["col_y"]
    parts = [_box(-hx, hx, -hy, hy - p["wall_t"], 0, sh)]
    parts += column(0.0, cy, sh, col)
    ed = p["ent_d"]
    parts.append(_box(-hx, hx, cy - ed / 2, cy + ed / 2, zc, zc + p["ent_h"]))
    cp = p["cornice_proj"]
    parts.append(_box(-hx, hx, cy - ed / 2 - cp, cy + ed / 2, zc + p["ent_h"], zc + p["ent_h"] + p["cornice_h"]))
    zr = zc + p["ent_h"] + p["cornice_h"]
    parts.append(_box(-hx, hx, cy - ed / 2 - cp, hy, zr, zr + p["roof_h"]))
    parts.append(_box(-hx, hx, cy + ed / 2, hy - p["wall_t"], zc + 0.35, zc + p["ent_h"]))      # soffit beam
    wy = hy - p["wall_t"]
    parts.append(_box(-hx, hx, wy, hy, 0, zr))
    parts.append(_box(-hx, hx, wy - 0.08, wy, sh, sh + p["wall_base_h"]))                       # wall base
    return parts


GEOMETRY = {"arch_gate": arch_gate, "colonnade_bay": colonnade_bay}


def width(spec: dict) -> float:
    return spec["params"]["w"]


def build(store: ProjectStore, asset: str, repeat: int = 1, out: Path | None = None, log=print) -> dict:
    """Build an asset's procedural piece from its spec: maps from the sheet, parts, Blender
    -> GLB. repeat > 1 writes a tiled-run preview instead (out required)."""
    spec = read_json(spec_path(store, asset), default=None)
    if spec is None:
        raise FileNotFoundError(f"no procedural spec: {spec_path(store, asset).relative_to(store.root)}")
    if spec["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    d = out_dir(store, asset)
    maps = d / "procedural"
    t = spec["tile"]
    materials = {"stone": {**tile_maps(store.root, t, maps), "uv": "tile", "tile_m": t.get("tile_m", 3.0)}}
    if spec.get("facade"):
        fm, rect = facade_maps(store.root, spec["facade"], maps)
        materials["facade"] = {**fm, "uv": "facade", "rect": rect, "fallback": "stone"}
    parts = GEOMETRY[spec["kind"]](spec["params"])
    if "facade" not in materials:
        for q in parts:
            q["mat"] = "stone"
    glb = out or d / "procedural.glb"
    return run_blender(store, asset, parts, materials, glb, maps, repeat=repeat, width=width(spec),
                       extra={"kind": spec["kind"]}, log=log)


def zones_path(glb: Path) -> Path:
    """A build's vegetation zones: <name>.zones.json next to its GLB."""
    return glb.with_name(glb.stem + ".zones.json")


def build_sha(job: dict) -> str:
    """What decides a build's result: the Blender job without its output paths, every texture by
    its content (not its path) and the script itself. Two builds with the same build_sha make the
    same model, whatever the GLB's bytes (library_review.reroll adds no version for it)."""
    def norm(v):
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, str) and v.startswith("/") and Path(v).is_file():
            return "sha:" + filestore.digest(Path(v))
        return v
    core = {k: v for k, v in job.items() if k not in ("name", "out")}
    blob = json.dumps(norm(core), sort_keys=True) + filestore.digest(SCRIPT)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def run_blender(store: ProjectStore, asset: str, parts: list[dict], materials: dict, glb: Path, maps: Path,
                repeat: int = 1, width: float | None = None, extra: dict | None = None, log=print,
                shading: dict | None = None) -> dict:
    """Build parts with materials in Blender (scripts/blender_kit.py) into glb; the report
    goes to <asset dir>/procedural.json for the asset's own GLB. shading: {"smooth", "angle"
    deg} (building_proc.shading_of); None keeps the script's default."""
    maps.mkdir(parents=True, exist_ok=True)
    from .building_parts import resolve_coincident, zone_records
    zones = [p for p in parts if p["kind"] == "zone"]          # vegetation zones: data, no geometry
    parts = [p for p in parts if p["kind"] != "zone"]
    resolve_coincident(parts)          # coplanar twins render black (Cycles) and z-fight (Unreal)
    job = {"name": asset, "out": str(glb), "repeat": repeat, "width": width, "parts": parts, "materials": materials}
    if shading:
        job["shading"] = shading
    jp = maps / ("job.json" if repeat == 1 else f"job_x{repeat}.json")
    write_json(jp, job)
    log_path = maps / "blender.log"
    log_path.unlink(missing_ok=True)
    filestore.detach(Path(glb))                       # a new build: never write into a shared file
    blender.run(SCRIPT, [jp], log_path, f"procedural {asset}")
    filestore.intern(store, Path(glb))
    lines = [ln for ln in log_path.read_text().splitlines() if ln.startswith('{"out"')]
    if not lines:
        raise RuntimeError("blender_kit wrote no report:\n" + "\n".join(log_path.read_text().splitlines()[-20:]))
    zrec = zone_records(zones)
    write_json(zones_path(glb), {"asset": asset, "frame": "building: metres, z up, front facing -Y", "zones": zrec})
    res = json.loads(lines[-1]) | (extra or {}) | {"parts": len(parts), "repeat": repeat, "zones": len(zrec),
                                                   "build_sha": build_sha(job)}
    if repeat == 1 and glb == out_dir(store, asset) / "procedural.glb":
        from .areas import brief_sha                 # batch builds skip a build whose brief hasn't changed
        res["brief_sha"] = brief_sha(store, asset)
        write_json(out_dir(store, asset) / "procedural.json", res)
    log(json.dumps(res))
    return res


def build_any(store: ProjectStore, asset: str, log=print, surface: str | None = None, make_current: bool = True) -> dict:
    """An asset's procedural piece, whatever its spec: a kit piece or a whole building (surface:
    its band surfaces, building_proc.DEFAULT_SURFACE). make_current=False: the new version
    doesn't become the version in use (library_review.reroll)."""
    from . import versions
    if spec_path(store, asset).is_file():
        versions.migrate_asset(store, asset)      # a build from before the bank becomes v1 before this one replaces it
    spec = read_json(spec_path(store, asset), default=None)
    if spec is not None and spec.get("kind") == "building":
        from . import building_proc
        res = building_proc.build(store, asset, log=log, surface=surface)
    else:
        res = build(store, asset, log=log)
    v = versions.record_procedural(store, asset, res, surface=surface, make_current=make_current)       # every successful build is a version
    return {**res, "version": v["n"]}


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) > 2:
        build(ProjectStore.open(a[0]), a[1], repeat=int(a[2]), out=Path(a[3]) if len(a) > 3 else None)
    else:
        build_any(ProjectStore.open(a[0]), a[1])
