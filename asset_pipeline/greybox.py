"""Site layout -> terrain heightmap + greybox build spec (pure Python, no Blender).

The spec is everything scripts/blender_greybox.py needs to build the .blend: slots (one per
building, plaza, ring, radial, plus terrain and sea), each with pieces made of a handful of
primitives. Pieces of the same kit piece type and size share one primitive, so Blender
links them to one mesh and they become one instanced mesh later.

Primitives (origin at the bottom centre unless noted, sizes in metres):
    box      w, d, h
    cylinder r, h, seg
    dome     r, seg              hemisphere standing on z = 0
    arc      r_in, r_out, a0, a1, h, seg   ring segment around the piece origin
                                           (a0 = 0, a1 = 360: closed annulus)
    ribbon   pts [[x, y, z]...], w, h      strip along a polyline, draped (z per point,
                                           relative to the slot): streets on terrain
    poly     pts [[x, y, z]...], h         flat-topped slab over a polygon outline (squares,
                                           parks); z per vertex, relative to the slot
A piece may carry "scale": [sx, sy, sz]. Instanced massing (city housing) is one unit box
scaled per piece, so thousands of buildings share one mesh.

Kit pieces are named "<kit>:<piece>" and snap to the kit's module, so a kit has a small,
fixed piece list (column base/shaft/capital, entablature, ...); curved pieces exist once
per supported ring radius. Unique massing (podium, cella, mass, dome...) is "mass"-like
and named by its role.
    gable    w, d, h             triangular roof, ridge along Y (pediment faces -Y)
    vault    w, d, h, seg        half-round barrel vault: spans X (w), runs along Y (d), rise h
    archwall w, d, h, span, spring   wall in the XZ plane, d thick, with an arched opening
                                     `span` wide whose arch springs at `spring`
"""
import hashlib
import math
import random

import numpy as np

from .schema import Kit, Plot, SiteLayout, TerrainSpec

PAVING_LIFT = 0.05   # paving/water sit just above the terrain to avoid z-fighting
RADIAL_LIFT = 0.08
ROW_SKIP_M = 4.0     # extra clearance to a radial avenue when spreading ring rows

CATEGORY = {"temple": "building", "block": "building", "villa": "building", "rotunda": "building",
            "stoa": "structure", "plaza": "terrain", "pool": "structure", "avenue": "terrain",
            "canal": "terrain", "garden": "terrain", "terrace": "terrain", "radial": "terrain",
            "terrain": "terrain", "sea": "terrain",
            # city mode (asset_pipeline/city.py)
            "housing": "building", "houses": "building", "street": "terrain", "park": "terrain",
            "market": "structure", "quay": "structure", "colonnade": "structure", "lagoon": "structure"}
DEFAULT_KIT = Kit(id="default")


# --- Terrain -------------------------------------------------------------------------------

def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _value_noise(n: int, cells: int, rng: np.random.Generator) -> np.ndarray:
    """Smooth value noise on an n x n grid with `cells` lattice cells per side, in [-1, 1]."""
    lat = rng.uniform(-1, 1, (cells + 2, cells + 2))
    t = np.linspace(0, cells, n)
    i = np.floor(t).astype(int)
    f = t - i
    f = f * f * (3 - 2 * f)
    a = lat[np.ix_(i, i)] * (1 - f)[None, :] + lat[np.ix_(i, i + 1)] * f[None, :]
    b = lat[np.ix_(i + 1, i)] * (1 - f)[None, :] + lat[np.ix_(i + 1, i + 1)] * f[None, :]
    return a * (1 - f)[:, None] + b * f[:, None]


def grid_coords(t: TerrainSpec) -> tuple[np.ndarray, np.ndarray]:
    """World x, y of every heightmap sample. Row 0 is the north edge (+Y), like an image."""
    s = np.linspace(-t.extent_m / 2, t.extent_m / 2, t.resolution)
    return np.meshgrid(s, s[::-1])


def sea_axes(t: TerrainSpec) -> tuple[np.ndarray, np.ndarray]:
    """Unit vectors: towards the sea, and along the coast (lateral)."""
    u = np.array([math.cos(math.radians(t.sea_dir)), math.sin(math.radians(t.sea_dir))])
    return u, np.array([-u[1], u[0]])


def shore_at(t: TerrainSpec, lateral):
    """Distance from the origin to the shoreline, towards the sea, at a lateral position:
    the headland plus seeded bays and points (sum of three sines)."""
    lateral = np.asarray(lateral, dtype=np.float64)
    out = t.shore_m + t.headland_m * np.exp(-(lateral / t.headland_width_m) ** 2)
    if t.coast_amp_m:
        rng = np.random.default_rng(t.seed + 101)
        for k, share in ((1.0, 0.6), (2.3, 0.28), (5.1, 0.12)):
            out = out + t.coast_amp_m * share * np.sin(2 * math.pi * k * lateral / t.coast_wavelength_m
                                                       + rng.uniform(0, 2 * math.pi))
    return out


def make_terrain(t: TerrainSpec) -> np.ndarray:
    """Fictional coastal terrain (float32 metres, rows north -> south): sea towards sea_dir,
    a headland (and with coast_amp_m, bays and points) on the shore, hills inland, and
    unless city_radius_m is 0, the city a flat plateau."""
    x, y = grid_coords(t)
    rng = np.random.default_rng(t.seed)
    u, v = sea_axes(t)
    along = x * u[0] + y * u[1]                     # towards the sea
    lateral = x * v[0] + y * v[1]
    inland = shore_at(t, lateral) - along           # > 0 on land
    n = t.resolution
    noise = (0.6 * _value_noise(n, 4, rng) + 0.3 * _value_noise(n, 9, rng)
             + 0.1 * _value_noise(n, 23, rng))
    land = (8 * (1 - np.exp(-np.maximum(inland, 0) / 30))        # low cliff at the shore
            + 0.03 * np.maximum(inland, 0)
            + t.hill_height_m * _smoothstep(t.hill_from_m, t.hill_to_m, inland) * (0.55 + 0.45 * noise))
    sea = np.maximum(-0.08 * (-inland), -40.0)      # shelves down to -40 m
    h = np.where(inland > 0, land, sea)
    if t.city_radius_m <= 0:
        return h.astype(np.float32)
    r = np.hypot(x, y)
    w = 1 - _smoothstep(t.city_radius_m, t.city_radius_m + 90, r)
    return (h * (1 - w) + t.city_z * w).astype(np.float32)


def to_png16(h: np.ndarray, z_range) -> np.ndarray:
    lo, hi = z_range
    return np.round(np.clip((h - lo) / (hi - lo), 0, 1) * 65535).astype(np.uint16)


def from_png16(a: np.ndarray, z_range) -> np.ndarray:
    lo, hi = z_range
    return (a.astype(np.float32) / 65535 * (hi - lo) + lo).astype(np.float32)


class Heights:
    """Bilinear sampler over a heightmap (rows north -> south)."""

    def __init__(self, h: np.ndarray, extent_m: float):
        self.h, self.extent = h, extent_m

    def __call__(self, x: float, y: float) -> float:
        n = self.h.shape[0]
        fx = (x / self.extent + 0.5) * (n - 1)
        fy = (0.5 - y / self.extent) * (n - 1)
        fx, fy = min(max(fx, 0), n - 1), min(max(fy, 0), n - 1)
        i0, j0 = int(fy), int(fx)
        i1, j1 = min(i0 + 1, n - 1), min(j0 + 1, n - 1)
        a, b = fy - i0, fx - j0
        top = self.h[i0, j0] * (1 - b) + self.h[i0, j1] * b
        bot = self.h[i1, j0] * (1 - b) + self.h[i1, j1] * b
        return float(top * (1 - a) + bot * a)

    def many(self, x, y) -> np.ndarray:
        """Vectorised __call__ over arrays of points."""
        n = self.h.shape[0]
        fx = np.clip((np.asarray(x, float) / self.extent + 0.5) * (n - 1), 0, n - 1)
        fy = np.clip((0.5 - np.asarray(y, float) / self.extent) * (n - 1), 0, n - 1)
        i0, j0 = np.floor(fy).astype(int), np.floor(fx).astype(int)
        i1, j1 = np.minimum(i0 + 1, n - 1), np.minimum(j0 + 1, n - 1)
        a, b = fy - i0, fx - j0
        top = self.h[i0, j0] * (1 - b) + self.h[i0, j1] * b
        bot = self.h[i1, j0] * (1 - b) + self.h[i1, j1] * b
        return top * (1 - a) + bot * a


# --- Spec building --------------------------------------------------------------------------

def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return x * c - y * s, x * s + y * c


class SlotBuilder:
    """Collects one slot's pieces in world coordinates, stores them relative to the slot."""

    def __init__(self, id: str, type: str, loc, rot: float = 0.0, kit=None, district=None, category=None,
                 material=None, typology=None):
        self.slot = {"id": id, "type": type, "category": category or CATEGORY.get(type, "building"), "kit": kit,
                     "district": district, "material": material, "typology": typology,
                     "loc": [round(v, 4) for v in loc], "rot_z": round(rot, 4), "pieces": []}

    def add(self, piece: str, prim: dict, loc, rot: float = 0.0, scale=None) -> None:
        """loc / rot in world coordinates."""
        ox, oy, oz = self.slot["loc"]
        lx, ly = _rot(loc[0] - ox, loc[1] - oy, -self.slot["rot_z"])
        p = {"name": f"{self.slot['id']}.{piece}.{len(self.slot['pieces']):03d}",
             "piece": piece, "prim": prim,
             "loc": [round(lx, 4), round(ly, 4), round(loc[2] - oz, 4)],
             "rot_z": round((rot - self.slot["rot_z"]) % 360, 4)}
        if scale is not None:
            p["scale"] = [round(v, 3) for v in scale]
        self.slot["pieces"].append(p)

    def add_local(self, piece: str, prim: dict, lx: float, ly: float, lz: float, lrot: float = 0.0) -> None:
        """loc / rot relative to the slot."""
        ox, oy, oz = self.slot["loc"]
        wx, wy = _rot(lx, ly, self.slot["rot_z"])
        self.add(piece, prim, (ox + wx, oy + wy, oz + lz), self.slot["rot_z"] + lrot)


def box(w, d, h):
    return {"kind": "box", "w": round(w, 3), "d": round(d, 3), "h": round(h, 3)}


def cyl(r, h, seg=16):
    return {"kind": "cylinder", "r": round(r, 3), "h": round(h, 3), "seg": seg}


def ribbon(pts, w, h=0.1):
    """pts relative to the piece origin: [[x, y, z], ...]."""
    return {"kind": "ribbon", "pts": [[round(c, 2) for c in p] for p in pts], "w": round(w, 2), "h": round(h, 2)}


def poly(pts, h=0.1):
    return {"kind": "poly", "pts": [[round(c, 2) for c in p] for p in pts], "h": round(h, 2)}


UNIT_BOX = {"kind": "box", "w": 1.0, "d": 1.0, "h": 1.0}


def arc(r_in, r_out, a0, a1, h, seg=None):
    span = (a1 - a0) % 360 or 360
    seg = seg or max(2, int(math.ceil(span / 6)))
    return {"kind": "arc", "r_in": round(r_in, 3), "r_out": round(r_out, 3), "a0": round(a0, 4),
            "a1": round(a1, 4), "h": round(h, 3), "seg": seg}


def _column_parts(kit: Kit, h: float) -> list[tuple[str, dict, float]]:
    """A column of total height h as kit pieces: (piece, primitive, z offset)."""
    d = kit.column_d_m
    base_h, cap_h = 0.35 * d, 0.6 * d
    return [(f"{kit.id}:column-base", box(1.35 * d, 1.35 * d, base_h), 0.0),
            (f"{kit.id}:column-shaft-h{round(h, 1):g}", cyl(d / 2, h - base_h - cap_h), base_h),
            (f"{kit.id}:column-capital", box(1.4 * d, 1.4 * d, cap_h), h - cap_h)]


def _column(sb: SlotBuilder, kit: Kit, h: float, lx: float, ly: float, lz: float) -> None:
    for piece, prim, dz in _column_parts(kit, h):
        sb.add_local(piece, prim, lx, ly, lz + dz)


def _grid(length: float, module: float) -> tuple[list[float], float]:
    """Positions at exactly `module` spacing, centred, spanning about `length`; and the
    span actually used (a whole number of modules, so every bay is the same kit piece)."""
    n = max(1, round(length / module))
    span = n * module
    return [-span / 2 + module * i for i in range(n + 1)], span


def _colonnade_rect(sb: SlotBuilder, kit: Kit, w: float, d: float, h: float, z: float) -> tuple[float, float]:
    """Columns around a rectangle of about w x d (snapped to the module) plus one
    entablature bay between neighbours. Returns the snapped (w, d)."""
    d_col = kit.column_d_m
    ent_h = 0.9 * d_col
    xs, w = _grid(w, kit.module_m)
    ys, d = _grid(d, kit.module_m)
    pts = {(x, -d / 2) for x in xs} | {(x, d / 2) for x in xs} | {(-w / 2, y) for y in ys} | {(w / 2, y) for y in ys}
    for x, y in sorted(pts):
        _column(sb, kit, h, x, y, z)
    bay = box(kit.module_m, 1.2 * d_col, ent_h)
    for coords, fixed, along_x in [(xs, -d / 2, True), (xs, d / 2, True), (ys, -w / 2, False), (ys, w / 2, False)]:
        for a, b in zip(coords, coords[1:]):
            if along_x:
                sb.add_local(f"{kit.id}:entablature", bay, (a + b) / 2, fixed, z + h)
            else:
                sb.add_local(f"{kit.id}:entablature", bay, fixed, (a + b) / 2, z + h, 90)
    return w, d


def _temple(sb: SlotBuilder, p: Plot, kit: Kit) -> None:
    w, d, h = p.size
    podium = max(1.2, 0.12 * h)
    sb.add_local("podium", box(w, d, podium), 0, 0, 0)
    inset = max(kit.module_m * 0.5, 1.5)
    col_h = min(kit.storey_m * 1.5, 0.62 * h)
    cw, cd = _colonnade_rect(sb, kit, w - 2 * inset, d - 2 * inset, col_h, podium)
    sb.add_local("cella", box(cw - 2 * kit.module_m, cd - 2 * kit.module_m, col_h), 0, 0, podium)
    roof_z = podium + col_h + 0.9 * kit.column_d_m
    sb.add_local("roof", {"kind": "gable", "w": round(cw + 1.5, 3), "d": round(cd + 1.5, 3),
                          "h": round(max(1.0, h - roof_z), 3)}, 0, 0, roof_z)


def _block(sb: SlotBuilder, p: Plot, kit: Kit) -> None:
    """Monumental civic block (Kahn-like mass) with an arcade loggia on the front."""
    w, d, h = p.size
    sb.add_local("mass", box(w, d, h), 0, 0, 0)
    depth = kit.module_m
    xs, span = _grid(w - kit.module_m, kit.module_m)
    pier = kit.column_d_m * 1.2
    for x in xs:
        sb.add_local(f"{kit.id}:pier", box(pier, pier, kit.storey_m), x, -d / 2 - depth, 0)
    for a, b in zip(xs, xs[1:]):
        sb.add_local(f"{kit.id}:arcade-lintel", box(kit.module_m, pier, 0.9 * kit.column_d_m),
                     (a + b) / 2, -d / 2 - depth, kit.storey_m)
    sb.add_local("loggia-roof", box(span + pier, depth + pier / 2, 0.5), 0, -d / 2 - depth / 2,
                 kit.storey_m + 0.9 * kit.column_d_m)


def _villa(sb: SlotBuilder, p: Plot, kit: Kit) -> None:
    """Courtyard villa: four wings, a pool in the court and a peristyle around it."""
    w, d, h = p.size
    t = 0.26 * min(w, d)
    sb.add_local("mass", box(w, t, h), 0, -d / 2 + t / 2, 0)
    sb.add_local("mass", box(w, t, h), 0, d / 2 - t / 2, 0)
    sb.add_local("mass", box(t, d - 2 * t, h), -w / 2 + t / 2, 0, 0)
    sb.add_local("mass", box(t, d - 2 * t, h), w / 2 - t / 2, 0, 0)
    cw, cd = w - 2 * t, d - 2 * t
    sb.add_local("court-pool", box(cw * 0.45, cd * 0.6, 0.15), 0, 0, PAVING_LIFT)
    if p.kit and cw > 2 * kit.module_m and cd > 2 * kit.module_m:
        _colonnade_rect(sb, kit, cw - kit.module_m, cd - kit.module_m, min(kit.storey_m * 0.6, h * 0.8), 0)


def snap_radius(kit: Kit, r: float) -> float:
    return min(kit.ring_radii, key=lambda x: abs(x - r)) if kit.ring_radii else r


def _rotunda(sb: SlotBuilder, p: Plot, kit: Kit) -> None:
    """An open octagonal rotunda: eight tall arched openings between corner piers, paired
    columns at each corner, an entablature and attic ring, then a low dome (never taller
    than a half sphere) on a short round drum. size = (corner diameter, -, total height)."""
    diam, _, h = p.size
    R = diam / 2
    c8 = math.cos(math.radians(22.5))
    apo, side = R * c8, 2 * R * math.sin(math.radians(22.5))
    pier = 0.2 * side
    span = side - pier
    base_h, ent_h, attic_h, drum_h = 1.2, 0.06 * h, 0.06 * h, 0.04 * h
    rd = 0.92 * apo
    dome_h = 0.9 * rd
    spring = max(0.6 * span, h - (base_h + span / 2 + 1.0 + ent_h + attic_h + drum_h + dome_h))
    wall_h = spring + span / 2 + 1.0
    sb.add_local("platform", cyl(R + 4, base_h, 8), 0, 0, 0, 22.5)
    for i in range(8):     # sides face 22.5 + 45 i; corners sit at 45 i
        a = 22.5 + 45 * i
        x, y = _rot(apo, 0, a)
        sb.add_local("arch-side", {"kind": "archwall", "w": round(side + 0.4, 3), "d": round(pier, 3),
                                   "h": round(wall_h, 3), "span": round(span, 3), "spring": round(spring, 3)},
                     x, y, base_h, a + 90)
    col_d = round(wall_h / 10, 2)
    big = _scaled_kit(kit, col_d)
    for i in range(8):
        a = 45 * i
        for off in (-1.3 * col_d, 1.3 * col_d):
            x, y = _rot(R + 1.0 + col_d, off, a)
            _column(sb, big, wall_h, x, y, base_h)
    z = base_h + wall_h
    sb.add_local("entablature", arc(R - pier, R + 2.2 + col_d, 0, 360, ent_h, 8), 0, 0, z)
    sb.add_local("attic", arc(R - pier, R + 0.6, 0, 360, attic_h, 8), 0, 0, z + ent_h)
    z += ent_h + attic_h
    sb.add_local("drum", cyl(rd, drum_h, 48), 0, 0, z)
    sb.add("dome", {"kind": "dome", "r": round(rd, 3), "seg": 48}, _world(sb, 0, 0, z + drum_h), sb.slot["rot_z"],
           (1.0, 1.0, round(dome_h / rd, 3)))


def _scaled_kit(kit: Kit, column_d: float) -> Kit:
    """The kit with thicker columns (a landmark's taller order). Its pieces get their own
    names ("<kit>-d<diameter>:column-base"), so each piece name stays one mesh."""
    return Kit(id=f"{kit.id}-d{column_d:g}", module_m=kit.module_m, storey_m=kit.storey_m, column_d_m=column_d)


def _world(sb: SlotBuilder, lx: float, ly: float, lz: float) -> tuple[float, float, float]:
    ox, oy, oz = sb.slot["loc"]
    wx, wy = _rot(lx, ly, sb.slot["rot_z"])
    return ox + wx, oy + wy, oz + lz


def _colonnade(sb: SlotBuilder, p: Plot, kit: Kit, slot_z: float) -> None:
    """A landmark colonnade along an arc around p.center: two rows of columns (the inner
    on the arc, the outer COLONNADE_ROW_M further out), entablatures on both, a roof over
    the walk between, and planter blocks on the roof every third bay. Open both sides."""
    from .city import COLONNADE_ROW_M
    R, a0, a1 = p.arc
    R = snap_radius(kit, R)
    R2 = snap_radius(kit, R + COLONNADE_ROW_M)
    span = (a1 - a0) % 360 or 360
    step = math.degrees(kit.module_m * 1.25 / R)
    n = max(1, round(span / step))
    a0 = a0 + span / 2 - n * step / 2
    col_h = 1.6 * kit.storey_m
    big = _scaled_kit(kit, round(col_h / 10, 2))
    d_col = big.column_d_m
    cx, cy = p.center
    for i in range(n + 1):
        for r in (R, R2):
            x, y = _rot(r, 0, a0 + i * step)
            for piece, prim, dz in _column_parts(big, col_h):
                sb.add(piece, prim, (cx + x, cy + y, slot_z + dz), a0 + i * step)
    for i in range(n):
        a = a0 + i * step
        for r in (R, R2):
            sb.add(f"{kit.id}:colonnade-entablature-r{r:g}", arc(r - 0.7 * d_col, r + 0.7 * d_col, 0, step, 1.2 * d_col, 4),
                   (cx, cy, slot_z + col_h), a)
        sb.add(f"{kit.id}:colonnade-roof-r{R:g}", arc(R - 0.7 * d_col, R2 + 0.7 * d_col, 0, step, 0.6, 4),
               (cx, cy, slot_z + col_h + 1.2 * d_col), a)
        if i % 3 == 1:
            x, y = _rot((R + R2) / 2, 0, a + step / 2)
            sb.add(f"{kit.id}:colonnade-planter", box(2.2, R2 - R + 1.0, 2.4), (cx + x, cy + y, slot_z + col_h + 1.2 * d_col + 0.6),
                   a + step / 2 + 90)


def _stoa(sb: SlotBuilder, p: Plot, kit: Kit, slot_z: float) -> None:
    """Curved colonnade along an arc around p.center, open outwards (columns on the arc,
    back wall `depth` further in). Pieces are placed in world coordinates; the curved
    pieces' origin is the arc's centre."""
    R, a0, a1 = p.arc
    R = snap_radius(kit, R)
    span = (a1 - a0) % 360 or 360
    depth = 1.5 * kit.module_m
    step = math.degrees(kit.module_m / R)     # one bay; fixed per radius, so pieces repeat
    n = max(1, round(span / step))
    a0 = a0 + span / 2 - n * step / 2         # centre the whole bays on the requested arc
    col_h = kit.storey_m
    d_col = kit.column_d_m
    cx, cy = p.center
    for i in range(n + 1):
        x, y = _rot(R, 0, a0 + i * step)
        for piece, prim, dz in _column_parts(kit, col_h):
            sb.add(piece, prim, (cx + x, cy + y, slot_z + dz), a0 + i * step)
    for i in range(n):
        a = a0 + i * step
        tag = f"r{R:g}"
        sb.add(f"{kit.id}:stoa-entablature-{tag}", arc(R - 0.6 * d_col, R + 0.6 * d_col, 0, step, 0.9 * d_col, 4),
               (cx, cy, slot_z + col_h), a)
        sb.add(f"{kit.id}:stoa-backwall-{tag}", arc(R - depth - 0.8, R - depth, 0, step, col_h, 4),
               (cx, cy, slot_z), a)
        sb.add(f"{kit.id}:stoa-roof-{tag}", arc(R - depth - 0.8, R + 0.6 * d_col, 0, step, 0.5, 4),
               (cx, cy, slot_z + col_h + 0.9 * d_col), a)


def _seeded(seed: int, key: str) -> random.Random:
    return random.Random(int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:12], 16))


def _angle_in(a: float, lo: float, hi: float) -> bool:
    a, lo, hi = a % 360, lo % 360, hi % 360
    return lo <= a <= hi if lo <= hi else a >= lo or a <= hi


def district_at(layout: SiteLayout, x: float, y: float) -> str | None:
    """First district whose ring band and sector contain the point."""
    r, a = math.hypot(x, y), math.degrees(math.atan2(y, x)) % 360
    for d in layout.districts:
        if d.radius[0] <= r <= d.radius[1] and (d.sector == (0.0, 360.0) or _angle_in(a, *d.sector)):
            return d.id
    return None


def expand_rows(layout: SiteLayout) -> list[Plot]:
    """Explicit plots plus the plots of every ring row (skipping radial avenues around the
    same centre)."""
    plots = list(layout.plots)
    for row in layout.rows:
        half_w = row.size[0] / 2
        for i in range(row.count):
            a = row.angle_offset + 360 * i / row.count
            blocked = False
            for rad in layout.radials:
                if tuple(rad.center) != tuple(row.center):
                    continue
                if rad.r_from <= row.radius <= rad.r_to:
                    # angular half-width the plot needs to clear the avenue
                    need = math.degrees((rad.width / 2 + half_w + ROW_SKIP_M) / row.radius)
                    if abs((a - rad.angle + 180) % 360 - 180) < need:
                        blocked = True
                        break
            if blocked:
                continue
            x, y = _rot(row.radius, 0, a)
            x, y = x + row.center[0], y + row.center[1]
            h = row.size[2]
            if row.height_jitter:
                h *= 1 + _seeded(layout.terrain.seed, f"{row.id}-{i}").uniform(-1, 1) * row.height_jitter
            plots.append(Plot(id=f"{row.id}-{i:02d}", type=row.type, center=(x, y), rot=a - 90,
                              size=(row.size[0], row.size[1], round(h, 2)), kit=row.kit,
                              district=row.district))
    return plots


def build_spec(layout: SiteLayout, heights: np.ndarray) -> dict:
    """The greybox build spec (JSON-able). `heights` is the terrain (rows north -> south)."""
    city_plan = None
    if layout.city is not None:
        from . import city
        city_plan = city.plan(layout, heights)
        layout = city_plan.layout          # plus the civic cores
    t = layout.terrain
    hs = Heights(heights, t.extent_m)
    kits = {k.id: k for k in layout.kits}
    slots: list[dict] = []
    ids: set[str] = set()

    def new(id, type, loc, rot=0.0, kit=None, district=None, **tags) -> SlotBuilder:
        if id in ids:
            raise ValueError(f"duplicate slot id {id!r} in the layout")
        ids.add(id)
        sb = SlotBuilder(id, type, loc, rot, kit, district, **tags)
        slots.append(sb.slot)
        return sb

    for ring in layout.rings:
        cx, cy = ring.center
        z = hs(cx + ring.radius, cy)
        sb = new(f"ring-{ring.id}", ring.role, (cx, cy, z),
                 district=ring.district or district_at(layout, cx + ring.radius, cy))
        sb.add_local(ring.role, arc(ring.radius - ring.width / 2, ring.radius + ring.width / 2, 0, 360, 0.1, 128),
                     0, 0, PAVING_LIFT)
    for rad in layout.radials:
        mid = (rad.r_from + rad.r_to) / 2
        x, y = _rot(mid, 0, rad.angle)
        x, y = x + rad.center[0], y + rad.center[1]
        sb = new(f"radial-{rad.id}", "radial", (x, y, hs(x, y)), rad.angle - 90,
                 district=district_at(layout, x, y))
        sb.add_local("radial", box(rad.width, rad.r_to - rad.r_from, 0.1), 0, 0, RADIAL_LIFT)
    for pz in layout.plazas:
        x, y = pz.center
        sb = new(f"plaza-{pz.id}", "plaza" if pz.type == "paved" else "pool", (x, y, hs(x, y)),
                 district=pz.district or district_at(layout, x, y))
        if pz.type == "lagoon":    # sits on a paved plaza: a kerb and water just above it
            sb.add_local("lagoon-kerb", cyl(pz.radius + 1.2, 0.35, 96), 0, 0, PAVING_LIFT + 0.1)
            sb.add_local("pool-water", cyl(pz.radius, 0.12, 96), 0, 0, PAVING_LIFT + 0.36)
            continue
        sb.add_local("paving", cyl(pz.radius, 0.1, 96), 0, 0, PAVING_LIFT + 0.04)
        if pz.type == "pool":
            sb.add_local("pool-water", cyl(pz.radius * 0.6, 0.12, 64), 0, 0, PAVING_LIFT + 0.06)
    for i, p in enumerate(expand_rows(layout)):
        pid = p.id or f"{p.type}-{i:02d}"
        kit = kits.get(p.kit) if p.kit else None
        if p.kit and not kit:
            raise ValueError(f"plot {pid}: unknown kit {p.kit!r}")
        kit = kit or DEFAULT_KIT
        tags = {"material": p.material, "typology": p.typology}
        if p.type in ("stoa", "colonnade"):
            if not p.arc:
                raise ValueError(f"{p.type} {pid} needs arc: [radius, angle_from, angle_to]")
            R, a0, a1 = p.arc
            amid = a0 + ((a1 - a0) % 360) / 2
            x, y = _rot(R, 0, amid)
            x, y = x + p.center[0], y + p.center[1]
            z = hs(x, y)
            sb = new(pid, p.type, (x, y, z), amid + 90, p.kit, p.district or district_at(layout, x, y), **tags)
            (_stoa if p.type == "stoa" else _colonnade)(sb, p, kit, z)   # a stoa opens outwards
            continue
        x, y = p.center
        sb = new(pid, p.type, (x, y, hs(x, y)), p.rot, p.kit, p.district or district_at(layout, x, y), **tags)
        {"temple": _temple, "block": _block, "villa": _villa, "rotunda": _rotunda}[p.type](sb, p, kit)
    if city_plan is not None:
        city.add_slots(city_plan, new, hs)
    return {"terrain": {"extent_m": t.extent_m, "resolution": int(heights.shape[0])},
            "sea_level": layout.sea_level, "slots": slots,
            "shots": [s.model_dump(mode="json") for s in layout.shots],
            "city": city_plan.stats if city_plan is not None else None}


def piece_counts(spec: dict) -> dict[str, int]:
    """How many of each kit piece / massing type the spec contains."""
    out: dict[str, int] = {}
    for s in spec["slots"]:
        for p in s["pieces"]:
            out[p["piece"]] = out.get(p["piece"], 0) + 1
    return dict(sorted(out.items()))
