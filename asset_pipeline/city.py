"""City-scale layout mode: SiteLayout.city -> civic nodes, streets, blocks, massing.

Pure Python (numpy + shapely), no Blender. `plan()` turns the city spec and the terrain
into a CityPlan; `greybox.build_spec` turns that into slots. Steps:

  1. Density field: each civic node pulls (bigger `weight`, wider core), the coast adds a
     little, steep ground and low-frequency noise take away. It drives block size,
     storeys and the building type, and where the city frays out into countryside.
  2. Node pads: each node's ground is levelled into a terrace that grades into the slope
     (`apply_pads`, used when the terrain is made), so there is no plateau or ring wall.
  3. Civic cores in the existing kit system, centred on each node: plaza, rings, a ring
     row of civic blocks, stoas, and at most one monument (rotunda / temple).
  4. Avenues: radials out of each node (straight, or a spiral arm), steered towards gentle
     grades so they follow the terrain; arterials between neighbouring nodes; a promenade
     along the coast; each node's outer ring.
  5. Superblocks are the faces between avenues. Each is split recursively along its long
     axis with a jittered cut (a local street) until it is block-sized for its density:
     irregular, organic blocks rather than a grid.
  6. Massing per block: perimeter courtyard blocks in the dense cores, terraces in the
     middle ring, detached houses at the edge. Every building is a scaled unit box.
  7. Green corridors follow valleys (steepest descent from the hills to the sea), some
     blocks stay parks, markets sit at avenue crossings, a harbour node gets piers.
  8. Optionally (`enclosure`) a walled city: blocks only inside a circle, dense up to a
     ring road behind the wall, monumental gates where avenues leave, a greenbelt
     outside; and (`towns`) smaller neighbouring towns with their own lanes and square.

Housing is grouped into slots per tile (`tile_m`) so the id pass and the engine import
stay small; it is never a per-building asset.
"""
import hashlib
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box as sbox
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, split, unary_union

from .schema import (CitySpec, CivicNode, District, Enclosure, Kit, Plaza, Plot, Radial, Ring, RingRow, SiteLayout,
                     TerrainSpec)

PAD_BLEND_M = 160.0          # a node's terrace grades into the terrain over this distance
STEP_M = 20.0                # avenue tracing step
MIN_BLOCK_M2 = 500.0
MIN_STRIP_M = 7.0            # shorter facade segments are skipped
TREE_CAP = 30000
MAX_SPIRAL_DEG = 75.0        # a spiral avenue turns at most this far in total
CORE_D, TERRACE_D = 0.6, 0.4  # density: perimeter courtyard blocks / terraces / detached houses


# --- small geometry helpers ----------------------------------------------------------------

def _polys(g) -> list[Polygon]:
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for x in getattr(g, "geoms", []) for p in _polys(x)]


def _dir(deg: float) -> np.ndarray:
    return np.array([math.cos(math.radians(deg)), math.sin(math.radians(deg))])


def _angdiff(a: float, b: float) -> float:
    return (a - b + 180) % 360 - 180


def _smooth(e0, e1, x):
    t = np.clip((np.asarray(x, float) - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


@dataclass
class Box:
    """A building (or stall) as a scaled unit box: centre, size, rotation, base z."""
    x: float
    y: float
    w: float
    d: float
    rot: float
    z: float
    h: float
    kind: str                # housing | houses | stall
    district: str | None = None


@dataclass
class Way:
    id: str
    kind: str                # avenue | arterial | promenade | ring | street
    line: LineString
    width: float
    district: str | None = None


@dataclass
class WallPart:
    """One piece of a city wall or gate; `group` is its slot (wall-<k> | gate-<k>)."""
    group: str
    piece: str
    prim: dict
    x: float
    y: float
    z: float
    rot: float
    scale: list | None = None


@dataclass
class CityPlan:
    layout: SiteLayout                       # the input plus the civic cores (rings, plots...)
    ways: list[Way] = field(default_factory=list)
    blocks: list[Polygon] = field(default_factory=list)
    buildings: list[Box] = field(default_factory=list)
    parks: list[Polygon] = field(default_factory=list)
    markets: list[tuple[Polygon, list[Box]]] = field(default_factory=list)
    piers: list[Box] = field(default_factory=list)
    trees: list[tuple] = field(default_factory=list)  # x y z width height species (pine | cypress | olive)
    typed: list = field(default_factory=list)  # city_types.Building, when the layout has a typology catalog
    walls: list[WallPart] = field(default_factory=list)   # the enclosure's wall and gates
    gates: list[float] = field(default_factory=list)      # gate angles (deg)
    stats: dict = field(default_factory=dict)


# --- fields ---------------------------------------------------------------------------------

class Fields:
    """Terrain height, slope, land and the density field, sampled at points."""

    def __init__(self, layout: SiteLayout, heights: np.ndarray):
        from .greybox import Heights, sea_axes, shore_at, _value_noise
        self.t: TerrainSpec = layout.terrain
        self.city: CitySpec = layout.city
        self.sea = layout.sea_level
        self.hs = Heights(heights, self.t.extent_m)
        n = heights.shape[0]
        sp = self.t.extent_m / (n - 1)
        gy, gx = np.gradient(heights.astype(np.float64), sp)
        self.slope_grid = np.hypot(gx, gy)
        self.u, self.v = sea_axes(self.t)
        self._shore = lambda lat: shore_at(self.t, lat)
        rng = np.random.default_rng(self.city.seed + 7)
        self.noise = _value_noise(129, 6, rng)       # organic asymmetry of the density

    def z(self, x, y):
        return self.hs.many(x, y)

    def slope(self, x, y):
        n = self.slope_grid.shape[0]
        e = self.t.extent_m
        j = np.clip(np.round((np.asarray(x) / e + 0.5) * (n - 1)).astype(int), 0, n - 1)
        i = np.clip(np.round((0.5 - np.asarray(y) / e) * (n - 1)).astype(int), 0, n - 1)
        return self.slope_grid[i, j]

    def inland(self, x, y):
        """Metres from the shoreline, positive on land."""
        x, y = np.asarray(x, float), np.asarray(y, float)
        along = x * self.u[0] + y * self.u[1]
        lat = x * self.v[0] + y * self.v[1]
        return self._shore(lat) - along

    def land(self, x, y):
        return (self.inland(x, y) > 0) & (self.z(x, y) > self.sea + 0.5)

    def density(self, x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        c = self.city
        keep = np.ones_like(x)
        for nd in c.nodes:
            d = np.hypot(x - nd.center[0], y - nd.center[1])
            keep = keep * (1 - np.exp(-(d / (c.density_falloff_m * nd.weight)) ** 2))
        dens = 1 - keep
        dens = dens + 0.12 * np.exp(-np.maximum(self.inland(x, y), 0) / 350)   # the waterfront
        e = self.t.extent_m
        nn = self.noise.shape[0]
        j = np.clip(((x / e + 0.5) * (nn - 1)).astype(int), 0, nn - 1)
        i = np.clip(((0.5 - y / e) * (nn - 1)).astype(int), 0, nn - 1)
        dens = dens + 0.1 * self.noise[i, j]
        if c.enclosure:        # dense right up to the wall, nothing outside it
            e = c.enclosure
            d = np.hypot(x - e.center[0], y - e.center[1])
            dens = np.maximum(dens, e.density_floor) * (1 - _smooth(e.radius - 60, e.radius - 20, d))
        for tw in c.towns:
            d = np.hypot(x - tw.center[0], y - tw.center[1])
            dens = np.maximum(dens, tw.density * (1 - _smooth(0.55 * tw.radius, tw.radius, d)))
        dens = dens * (1 - _smooth(0.5 * c.max_slope, c.max_slope, self.slope(x, y)))
        return np.clip(dens, 0, 1)


# --- terrain pads ---------------------------------------------------------------------------

ORGANIC_PAD_BLEND_M = 45.0   # an organic node levels only its summit plaza, blending quickly
STAIR_GRADE = 0.12           # steeper lanes and streets are stairs


def organic_plaza_r(nd: CivicNode) -> float:
    return max(26.0, 0.16 * nd.radius)


def summit(nd: CivicNode, zfn) -> tuple[float, float]:
    """The highest ground within 0.6 x the node's radius (the centroid of everything within
    0.5 m of the top, so a levelled summit gives its centre)."""
    R = 0.6 * nd.radius
    rr, aa = np.meshgrid(np.linspace(0, R, 13), np.linspace(0, 2 * math.pi, 36, endpoint=False))
    xs = nd.center[0] + (rr * np.cos(aa)).ravel()
    ys = nd.center[1] + (rr * np.sin(aa)).ravel()
    z = np.asarray(zfn(xs, ys), float)
    top = z >= z.max() - 0.5
    return float(xs[top].mean()), float(ys[top].mean())


def pads(layout: SiteLayout, heights: np.ndarray) -> list[tuple[float, float, float, float, float]]:
    """(x, y, radius, z, blend) per node: the mean ground of its disc, at least 3 m above
    the sea. An organic node levels only a small summit plaza (at the 85th percentile of
    its ground, so the top is cut, not filled), and the hillside keeps its slope."""
    from .greybox import Heights
    hs = Heights(heights, layout.terrain.extent_m)
    out = []
    for nd in layout.city.nodes:
        if nd.layout == "organic":
            sx, sy = summit(nd, hs.many)
            r = organic_plaza_r(nd) * 1.15
            a = np.linspace(0, 2 * math.pi, 24, endpoint=False)
            z = float(np.percentile(hs.many(sx + r * np.cos(a), sy + r * np.sin(a)), 85))
            out.append((sx, sy, r, max(z, layout.sea_level + 3.0), ORGANIC_PAD_BLEND_M))
            continue
        r = nd.radius * 1.05
        a = np.linspace(0, 2 * math.pi, 24, endpoint=False)
        xs = np.concatenate([[nd.center[0]], nd.center[0] + 0.6 * r * np.cos(a), nd.center[0] + r * np.cos(a)])
        ys = np.concatenate([[nd.center[1]], nd.center[1] + 0.6 * r * np.sin(a), nd.center[1] + r * np.sin(a)])
        z = float(np.mean(hs.many(xs, ys)))
        out.append((nd.center[0], nd.center[1], r, max(z, layout.sea_level + 3.0), PAD_BLEND_M))
    return out


def apply_pads(layout: SiteLayout, heights: np.ndarray) -> np.ndarray:
    """Level each node's ground into a terrace that grades into the terrain (no wall).
    The sea is left alone except right at a pad's edge."""
    from .greybox import grid_coords, sea_axes, shore_at
    t = layout.terrain
    x, y = grid_coords(t)
    u, v = sea_axes(t)
    inland = shore_at(t, x * v[0] + y * v[1]) - (x * u[0] + y * u[1])
    h = heights.astype(np.float32).copy()
    for cx, cy, r, z, blend in pads(layout, heights):
        d = np.hypot(x - cx, y - cy)
        w = (1 - _smooth(r, r + blend, d)) * _smooth(-40, 10, inland)
        h = h * (1 - w) + z * w
    return h.astype(np.float32)


# --- civic cores (kit system) ----------------------------------------------------------------

def _rings(nd: CivicNode) -> dict:
    R = nd.radius
    return {"plaza": max(30.0, 0.3 * R), "ring1": 0.36 * R + 6, "blocks": 0.5 * R + 6,
            "garden": 0.64 * R + 6, "core": 0.7 * R + 6, "outer": R}


def civic_layout(layout: SiteLayout, f: Fields) -> SiteLayout:
    """A copy of the layout with every node's civic core added (centred rings, plazas,
    plots, ring rows) and a district per node."""
    L = layout.model_copy(deep=True)
    kit = L.kits[0] if L.kits else Kit(id="civic", module_m=4, storey_m=6, column_d_m=0.9)
    if not L.kits:
        L.kits.append(kit)
    radii = set(kit.ring_radii)
    have = {d.id for d in L.districts}
    for nd in L.city.nodes:
        if nd.id not in have:
            L.districts.append(District(id=nd.id, name=nd.id, notes=nd.notes))
        c = tuple(nd.center)
        r = _rings(nd)
        pr = r["plaza"]
        if nd.layout == "organic":
            organic_core(L, nd, f, kit)
            continue
        centre = nd.centre if nd.centre != "auto" else ("paved" if nd.monument or nd.role == "market" else "pool")
        if centre == "quad":
            quad_core(L, nd, pr, kit, f)
        else:
            L.plazas.append(Plaza(id=f"{nd.id}-plaza", center=c, radius=pr,
                                  type="paved" if centre == "plaza" else centre, district=nd.id))
        L.rings.append(Ring(id=f"{nd.id}-ring", center=c, radius=r["ring1"], width=14, district=nd.id))
        if nd.radius >= 180:
            L.rings.append(Ring(id=f"{nd.id}-garden", center=c, radius=r["garden"], width=0.08 * nd.radius,
                                role="garden", district=nd.id))
        if nd.monument == "rotunda":
            radii |= rotunda_ensemble(L, nd, pr, f, kit)
        elif nd.monument == "temple":
            L.plots.append(Plot(id=f"{nd.id}-temple", type="temple", center=c, rot=nd.rot,
                                size=(0.3 * pr + 12, 0.5 * pr + 18, 18), kit=kit.id, district=nd.id))
        if nd.role in ("forum", "hill", "harbour") and nd.radials and nd.stoa_share > 0 and centre != "quad":
            rs = round(pr - 4)
            radii.add(rs)
            span = 360 / nd.radials
            keep = max(1, round(nd.radials * nd.stoa_share))     # spread over the gaps
            for k in sorted({int(i * nd.radials / keep) for i in range(keep)}):
                a0 = nd.rot + k * span + 0.22 * span
                L.plots.append(Plot(id=f"{nd.id}-stoa-{k}", type="stoa", center=c,
                                    arc=(rs, a0, a0 + 0.56 * span), kit=kit.id, district=nd.id))
        circ = 2 * math.pi * r["blocks"]
        count = max(4, int(circ / 46))
        if nd.civic_ring != "none":
            L.rows.append(RingRow(id=f"{nd.id}-civic", center=c, radius=r["blocks"], count=count, type="block",
                                  size=(min(34.0, circ / count - 12), 20.0, 16.0), height_jitter=0.15,
                                  angle_offset=nd.rot + 180 / count, kit=kit.id, district=nd.id,
                                  variant="plain" if nd.civic_ring == "plain" else None))
        for k in range(nd.radials):   # inside the core: kit radials; ways() carries them on
            L.radials.append(Radial(id=f"{nd.id}-r{k}", center=c, angle=nd.rot + 360 * k / nd.radials,
                                    width=L.city.avenue_w, r_from=pr, r_to=r["core"]))
    kit.ring_radii = sorted(radii)
    return L


QUAD_HALF = 0.62            # the court's half side, x the plaza radius
CLOISTER_DEPTH_M = 11.0     # a 6 m walk behind the arcade: deep enough to read in the depth pass


def quad_axis(nd: CivicNode, f: Fields) -> float:
    """The court's axis nearest the sea: its channel pool and its medium shot run along it."""
    sea = math.degrees(math.atan2(f.u[1], f.u[0]))
    return min((nd.rot + 90 * k for k in range(4)), key=lambda q: abs(_angdiff(q, sea)))


def quad_core(L: SiteLayout, nd: CivicNode, pr: float, kit: Kit, f: Fields) -> None:
    """An arcaded courtyard: a square court (paved, a long channel pool down its axis)
    enclosed by four two-storey cloister ranges whose arcades face the court."""
    c = np.array(nd.center, float)
    half = QUAD_HALF * pr
    L.plazas.append(Plaza(id=f"{nd.id}-plaza", center=tuple(c), radius=pr, type="court",
                          rot=quad_axis(nd, f) - 90, district=nd.id))
    for k in range(4):
        a = nd.rot + 90 * k
        m = c + (half + CLOISTER_DEPTH_M / 2) * _dir(a)
        # a plot's front (local -Y) faces rot - 90: towards the court's centre
        L.plots.append(Plot(id=f"{nd.id}-cloister-{k}", type="cloister", center=tuple(m), rot=a - 90,
                            size=(2 * half + 2 * CLOISTER_DEPTH_M, CLOISTER_DEPTH_M, 9.0), kit=kit.id,
                            district=nd.id))


def organic_core(L: SiteLayout, nd: CivicNode, f: Fields, kit: Kit) -> None:
    """A hill town's core: a plaza on the summit with the monument on it, facing downhill.
    No rings, ring rows or stoas: its lanes and stairs come from ways()."""
    sx, sy = summit(nd, f.z)
    pr = organic_plaza_r(nd)
    L.plazas.append(Plaza(id=f"{nd.id}-plaza", center=(sx, sy), radius=pr, type="paved", district=nd.id))
    down = _downhill_deg(f, sx, sy, pr)
    if nd.monument == "temple":
        L.plots.append(Plot(id=f"{nd.id}-temple", type="temple", center=(sx, sy), rot=down + 90,
                            size=(0.45 * pr + 8, 0.7 * pr + 10, 18), kit=kit.id, district=nd.id))


def _downhill_deg(f: Fields, x: float, y: float, r: float) -> float:
    """Direction of the steepest descent around a point, averaged over a ring of radius r."""
    a = np.linspace(0, 2 * math.pi, 24, endpoint=False)
    z = f.z(x + 2 * r * np.cos(a), y + 2 * r * np.sin(a))
    i = int(np.argmin(z))
    return float(math.degrees(a[i]))


def rotunda_ensemble(L: SiteLayout, nd: CivicNode, pr: float, f: Fields, kit: Kit) -> set[float]:
    """The rotunda as a landmark ensemble in its plaza: the open domed rotunda set back
    inland, a reflecting lagoon in front of it (towards the sea, where the node's eye-level
    shot stands) and two curved colonnades on one arc around the lagoon, flanking the
    rotunda. Returns the ring radii the curved kit pieces need."""
    t = next((t for t in (L.city.typologies if L.city else []) if t.id == "rotunda"), None)
    mat = t.material if t else None
    c = np.array(nd.center, float)
    diam = float(np.clip(0.42 * pr, 24, 44))
    rc = c - f.u * 0.3 * pr                      # the rotunda, set back from the lagoon
    lc = c + f.u * 0.28 * pr                     # the lagoon's centre
    R = float(np.linalg.norm(rc - lc))           # the colonnades' arc runs through the rotunda
    lag_r = R - diam / 2 - 7
    L.plots.append(Plot(id=f"{nd.id}-rotunda", type="rotunda", center=tuple(rc), size=(diam, diam, diam * 1.15),
                        kit=kit.id, district=nd.id, material=mat, typology="rotunda"))
    L.plazas.append(Plaza(id=f"{nd.id}-lagoon", center=tuple(lc), radius=lag_r, type="lagoon", district=nd.id))
    back = math.degrees(math.atan2(*(rc - lc)[::-1]))
    gap = math.degrees((diam / 2 + 6) / R)
    for k, (a0, a1) in enumerate(((back + gap, back + 85), (back - 85, back - gap))):
        L.plots.append(Plot(id=f"{nd.id}-colonnade-{k}", type="colonnade", center=tuple(lc), arc=(R, a0, a1),
                            kit=kit.id, district=nd.id, material=mat, typology="rotunda"))
    return {round(R), round(R) + COLONNADE_ROW_M}


COLONNADE_ROW_M = 5          # a landmark colonnade is two rows of columns this far apart


# --- ways --------------------------------------------------------------------------------------

def _trace(f: Fields, start, heading, length, curve, target=None, free_m=0.0, stop=None):
    """Polyline from start, STEP_M at a time. Past `free_m` it steers (+-10 deg) towards the
    gentlest grade, within 50 deg of its planned course; `target` pulls it there."""
    pts = [np.array(start, float)]
    hd = planned = heading
    walked = 0.0
    while walked < length:
        if abs(planned - heading) < MAX_SPIRAL_DEG:   # an arm, not a coil
            planned += curve * STEP_M / 100
        if target is not None:
            want = math.degrees(math.atan2(target[1] - pts[-1][1], target[0] - pts[-1][0]))
            planned = want         # steering stays within 50 deg of the bearing: no loops
        cands = [planned] if walked < free_m else [hd + d for d in (-10, -5, 0, 5, 10)]
        best, best_s = None, 1e9
        z0 = float(f.z(*pts[-1]))
        for a in cands:
            if abs(_angdiff(a, planned)) > 50:
                continue
            p = pts[-1] + STEP_M * _dir(a)
            grade = abs(float(f.z(*p)) - z0) / STEP_M
            s = grade * 40 + 0.04 * abs(_angdiff(a, planned)) + 0.02 * abs(_angdiff(a, hd))
            if s < best_s:
                best, best_s = a, s
        hd = best if best is not None else planned
        p = pts[-1] + STEP_M * _dir(hd)
        walked += STEP_M
        if not f.land(*p):
            break
        pts.append(p)
        if stop is not None and stop(p):
            break
        if target is not None and np.hypot(*(np.array(target) - p)) < STEP_M:
            break
    return LineString(pts) if len(pts) > 1 else None


# --- enclosure: a walled city -------------------------------------------------------------------

WALL_BAY_M = 9.0             # one wall bay (chord); whole bays go round the circle
TOWN_PLAZA_M = 30.0


def ring_road_r(c: CitySpec) -> float:
    """The ring road just inside the wall; blocks stop at its inner edge."""
    e = c.enclosure
    return e.radius - e.wall_d / 2 - 6 - c.arterial_w / 2


def gate_angles(c: CitySpec, f: Fields) -> list[float]:
    """The gates' angles: as given, or `gate_count` spread around the side facing inland;
    each moved (<= 60 deg) to where the wall stands on land, well clear of the shore."""
    e = c.enclosure
    if e.gates:
        want = list(e.gates)
    else:
        inl = math.degrees(math.atan2(-f.u[1], -f.u[0]))
        want = [inl - 180 + 360 * (k + 0.5) / e.gate_count for k in range(e.gate_count)]
    out: list[float] = []
    for a in want:
        for d in sorted(range(-60, 61, 2), key=abs):
            p = np.array(e.center) + e.radius * _dir(a + d)
            if float(f.inland(*p)) > 80 and bool(f.land(*p)):
                if all(abs(_angdiff(a + d, o)) > 15 for o in out):
                    out.append(round((a + d) % 360, 2))
                break
    return sorted(out)


def wall_parts(c: CitySpec, f: Fields, gates: list[float]) -> list[WallPart]:
    """The wall as bays of one mesh round the circle (on land, open at the gates), each on
    the ground at its centre, so the top steps with the terrain; and the gates: a tall
    arch between two towers, across the avenue."""
    e = c.enclosure
    R = e.radius
    n = max(8, round(2 * math.pi * R / WALL_BAY_M))
    step = 360 / n
    chord = round(2 * R * math.sin(math.radians(step / 2)) + 0.1, 2)
    clear = math.degrees((e.gate_w / 2 + 12) / R)
    out: list[WallPart] = []
    order = sorted(gates)

    def stretch(a):        # which stretch between gates a bay is in
        return sum(1 for g in order if g <= a % 360) % max(len(order), 1)

    for i in range(n):
        a = i * step
        if any(abs(_angdiff(a, g)) < clear for g in gates):
            continue
        x, y = np.array(e.center) + R * _dir(a)
        if float(f.inland(x, y)) < 15 or not bool(f.land(x, y)):
            continue
        z = float(f.z(x, y)) - 0.5
        g = f"wall-{stretch(a)}"
        if e.kind == "wall":
            span = round(WALL_BAY_M * 0.55, 2)
            out.append(WallPart(g, "wall:bay", {"kind": "archwall", "w": chord, "d": e.wall_d, "h": e.wall_h,
                                                 "span": span, "spring": round(max(e.wall_h - span / 2 - 3.5, 2.0), 2)},
                                float(x), float(y), z, a + 90))
            out.append(WallPart(g, "wall:parapet", {"kind": "box", "w": chord, "d": e.wall_d + 0.8, "h": 1.2},
                                float(x), float(y), z + e.wall_h, a + 90))
        elif e.kind == "colonnade":
            ch = round(e.wall_h * 0.75, 1)
            for off in (0.0, -6.0):
                cx, cy = np.array(e.center) + (R + off) * _dir(a - step / 2)
                out.append(WallPart(g, "wall:column", {"kind": "cylinder", "r": 0.75, "h": ch, "seg": 12},
                                    float(cx), float(cy), z, a))
            out.append(WallPart(g, "wall:entablature", {"kind": "box", "w": chord, "d": 8.0, "h": 1.6},
                                *(np.array(e.center) + (R - 3) * _dir(a)), z + ch, a + 90))
        else:              # greenbelt: a low wall in a park ring
            out.append(WallPart(g, "wall:low", {"kind": "box", "w": chord, "d": 1.2, "h": 2.5},
                                float(x), float(y), z, a + 90))
    for k, a in enumerate(gates):
        x, y = np.array(e.center) + R * _dir(a)
        t = _dir(a + 90)
        zs = [float(f.z(*(np.array([x, y]) + s * t))) for s in (-e.gate_w / 2, 0, e.gate_w / 2)]
        z = min(zs) - 0.3
        d = round(e.wall_d * 3, 2)
        span = round(min(c.avenue_w * 0.9, e.gate_w * 0.55), 2)
        out.append(WallPart(f"gate-{k}", "gate:arch", {"kind": "archwall", "w": e.gate_w, "d": d, "h": e.gate_h,
                                                       "span": span,
                                                       "spring": round(max(e.gate_h - span / 2 - 5, 3.0), 2)},
                            float(x), float(y), z, a + 90))
        for side in (-1, 1):
            tx, ty = np.array([x, y]) + side * (e.gate_w / 2 + 5) * t
            out.append(WallPart(f"gate-{k}", "gate:tower", {"kind": "box", "w": 10.0, "d": d + 4, "h": e.gate_h + 6},
                                float(tx), float(ty), z, a + 90))
    return out


def greenbelt(c: CitySpec, region: Polygon, cut) -> list[Polygon]:
    """The park ring outside the wall, in wedges (a lawn is drawn from its outline, so no
    piece may have a hole)."""
    e = c.enclosure
    if not e.greenbelt_w:
        return []
    ring = Point(e.center).buffer(e.radius + e.greenbelt_w, quad_segs=48).difference(
        Point(e.center).buffer(e.radius + e.wall_d / 2 + 2, quad_segs=48))
    out = []
    far = e.radius + e.greenbelt_w + 50
    for k in range(24):
        a0, a1 = 15 * k, 15 * (k + 1)
        wedge = Polygon([e.center] + [tuple(np.array(e.center) + far * _dir(a)) for a in np.linspace(a0, a1, 5)])
        g = ring.intersection(wedge).intersection(region)
        if not cut.is_empty:
            g = g.difference(cut)
        out += [q for q in _polys(g) if q.area > 400]
    return out


def ways(L: SiteLayout, f: Fields) -> list[Way]:
    c = L.city
    x0, y0, x1, y1 = c.bounds
    e = c.enclosure
    rr = ring_road_r(c) if e else 0.0
    in_bounds = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1  # noqa: E731
    # inside a walled city, avenues end at the ring road
    inside = (lambda p: in_bounds(p) and math.dist(p, e.center) < rr - 10) if e else in_bounds
    out: list[Way] = []
    nodes = c.nodes

    def in_other(p, me):
        return any(o.id != me and np.hypot(p[0] - o.center[0], p[1] - o.center[1]) < o.radius
                   for o in nodes)

    for nd in nodes:
        r = _rings(nd)
        if nd.layout == "organic":
            out += organic_lanes(nd, f, c, lambda p, me=nd.id: not inside(p) or in_other(p, me))
            continue
        out.append(Way(f"{nd.id}-outer", "ring", Point(nd.center).buffer(nd.radius, quad_segs=24).exterior,
                       c.avenue_w * 0.8, nd.id))
        for k in range(nd.radials):
            a = nd.rot + 360 * k / nd.radials
            start = np.array(nd.center) + r["core"] * _dir(a)
            ln = _trace(f, start, a, nd.avenue_m, nd.spiral_deg_per_100m, free_m=nd.radius - r["core"],
                        stop=lambda p, me=nd.id: not inside(p) or in_other(p, me))
            if ln is not None and ln.length > 40:
                out.append(Way(f"{nd.id}-av{k}", "avenue", ln, c.avenue_w, nd.id))
    # arterials: each node to its two nearest
    pairs = set()
    for nd in nodes:
        near = sorted((o for o in nodes if o.id != nd.id),
                      key=lambda o: math.dist(o.center, nd.center))[:2]
        for o in near:
            pairs.add(tuple(sorted((nd.id, o.id))))
    by_id = {n.id: n for n in nodes}
    for a_id, b_id in sorted(pairs):
        a, b = by_id[a_id], by_id[b_id]
        hd = math.degrees(math.atan2(b.center[1] - a.center[1], b.center[0] - a.center[0]))
        start = np.array(a.center) + a.radius * _dir(hd)
        end = np.array(b.center) - b.radius * _dir(hd)
        ln = _trace(f, start, hd, 3 * math.dist(start, end), 0.0, target=end)
        if ln is not None and ln.length > 40:
            out.append(Way(f"art-{a_id}-{b_id}", "arterial", ln, c.arterial_w, a_id))
    if e:
        out += enclosure_ways(L, f, rr)
    for tw in c.towns:
        out += town_lanes(tw, f, c, lambda p, tw=tw: not in_bounds(p) or math.dist(p, tw.center) > tw.radius)
    # promenade: the shoreline, 30 m inland (a walled city: inside the wall, and each town's)
    lat = np.arange(-f.t.extent_m / 2, f.t.extent_m / 2, STEP_M)
    s = f._shore(lat) - 30 - c.promenade_w / 2
    pts = np.outer(s, f.u) + np.outer(lat, f.v)
    if e:
        ok = lambda p: inside(p) or any(math.dist(p, tw.center) < tw.radius for tw in c.towns)  # noqa: E731
    else:
        ok = inside
    runs, run = [], []
    for p in pts:
        if ok(p):
            run.append(p)
        elif run:
            runs.append(run)
            run = []
    runs.append(run)
    runs = [r for r in runs if len(r) > 1]
    for i, r in enumerate(runs):
        out.append(Way("promenade" if len(runs) == 1 else f"promenade{i}", "promenade", LineString(r),
                       c.promenade_w, None))
    return out


def enclosure_ways(L: SiteLayout, f: Fields, rr: float) -> list[Way]:
    """The ring road behind the wall (on land), an avenue from each gate in to its nearest
    node, and a road out of each gate (towards a town within 3 km)."""
    c, e = L.city, L.city.enclosure
    out: list[Way] = []
    land = _land_region(L, f, 20)
    ring = Point(e.center).buffer(rr, quad_segs=64).exterior.intersection(land)
    for k, g in enumerate(getattr(ring, "geoms", [ring])):
        if isinstance(g, LineString) and g.length > 40:
            out.append(Way(f"enclosure-ring{k}", "ring", g, c.arterial_w, None))
    for k, a in enumerate(gate_angles(c, f)):
        gp = np.array(e.center) + rr * _dir(a)
        if c.nodes:
            nd = min(c.nodes, key=lambda n: math.dist(n.center, gp))
            hd = math.degrees(math.atan2(nd.center[1] - gp[1], nd.center[0] - gp[0]))
            end = np.array(nd.center) - nd.radius * _dir(hd)
            if math.dist(end, gp) > 60:
                ln = _trace(f, gp, hd, 3 * math.dist(end, gp), 0.0, target=end)
                if ln is not None and ln.length > 40:
                    out.append(Way(f"gate{k}-av", "avenue", ln, c.avenue_w, nd.id))
        town = min(c.towns, key=lambda t: math.dist(t.center, gp), default=None)
        target = None
        if town is not None and math.dist(town.center, gp) < 3000:
            hd = math.degrees(math.atan2(town.center[1] - gp[1], town.center[0] - gp[0]))
            target = np.array(town.center) - town.radius * _dir(hd)
        length = e.greenbelt_w + 400 if target is None else 1.6 * math.dist(target, gp)
        ln = _trace(f, gp, a, length, 0.0, target=target, free_m=e.radius - rr + e.greenbelt_w)
        if ln is not None and ln.length > 40:
            out.append(Way(f"gate{k}-road", "arterial", ln, c.arterial_w, None))
    return out


def town_lanes(tw, f: Fields, c: CitySpec, stop) -> list[Way]:
    """A neighbouring town's lanes out of its square, jittered, steered to gentle grades."""
    rng = np.random.default_rng(int(hashlib.sha256(f"{c.seed}:{tw.id}".encode()).hexdigest()[:8], 16))
    out = []
    n = max(tw.radials, 0)
    for k in range(n):
        a = 360 * k / n + rng.uniform(-0.3, 0.3) * 360 / n
        start = np.array(tw.center) + (TOWN_PLAZA_M + 2) * _dir(a)
        ln = _trace(f, start, a, tw.radius, rng.uniform(-6, 6), free_m=0.0, stop=stop)
        if ln is not None and ln.length > 40:
            out += split_by_grade(f, ln, f"{tw.id}-lane{k}", "lane", c.street_w * 1.3, tw.id)
    return out


def organic_lanes(nd: CivicNode, f: Fields, c: CitySpec, stop) -> list[Way]:
    """A hill town's lanes: from the summit plaza out and down at unequal, jittered angles
    and lengths (re-rolled by nd.seed), steered to gentle grades so they wind across the
    slope; their steep stretches are stairs."""
    rng = np.random.default_rng(int(hashlib.sha256(f"{c.seed}:{nd.id}:{nd.seed}".encode()).hexdigest()[:8], 16))
    sx, sy = summit(nd, f.z)
    pr = organic_plaza_r(nd)
    n = max(3, nd.radials)
    span = 360 / n
    out = []
    for k in range(n):
        a = nd.rot + k * span + rng.uniform(-0.35, 0.35) * span
        start = np.array([sx, sy]) + (pr + 2) * _dir(a)
        length = nd.avenue_m * rng.uniform(0.45, 1.0)
        ln = _trace(f, start, a, length, rng.uniform(-8, 8), free_m=0.0, stop=stop)
        if ln is not None and ln.length > 40:
            out += split_by_grade(f, ln, f"{nd.id}-lane{k}", "lane", c.street_w * 1.3, nd.id)
    return out


def split_by_grade(f: Fields, ln: LineString, wid: str, kind: str, width: float, district) -> list[Way]:
    """The line as ways of `kind`, with its stretches steeper than STAIR_GRADE as stairs."""
    pts = np.array(LineString(ln.coords).segmentize(8.0).coords)
    if len(pts) < 2:
        return []
    z = f.z(pts[:, 0], pts[:, 1])
    seg = np.hypot(*np.diff(pts, axis=0).T)
    steep = np.abs(np.diff(z)) / np.maximum(seg, 1e-6) > STAIR_GRADE
    out, i = [], 0
    while i < len(steep):
        j = i
        while j + 1 < len(steep) and steep[j + 1] == steep[i]:
            j += 1
        part = LineString(pts[i:j + 2])
        if part.length > 4:
            k = "stair" if steep[i] else kind
            out.append(Way(f"{wid}-{len(out)}" if len(steep) > 1 else wid, k, part,
                           width * (0.8 if k == "stair" else 1.0), district))
        i = j + 1
    return out


# --- blocks ------------------------------------------------------------------------------------

def _land_region(L: SiteLayout, f: Fields, margin: float) -> Polygon:
    c = L.city
    lat = np.arange(-2 * f.t.extent_m, 2 * f.t.extent_m, STEP_M)
    s = f._shore(lat) - margin
    shore = np.outer(s, f.u) + np.outer(lat, f.v)
    far = [shore[-1] - 4 * f.t.extent_m * f.u, shore[0] - 4 * f.t.extent_m * f.u]
    land = Polygon(np.vstack([shore, far])).buffer(0)
    return land.intersection(sbox(*c.bounds))


def _cut(P: Polygon, rng, jitter_deg=10.0, pos=0.15, angle: float | None = None):
    """Cut a polygon across its long axis (jittered), or along `angle` (a contour, on a
    hillside). Returns (pieces, cut line inside)."""
    obb = P.minimum_rotated_rectangle
    q = np.array(obb.exterior.coords)[:4]
    e1, e2 = q[1] - q[0], q[2] - q[1]
    long_v = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
    L_len = float(np.linalg.norm(long_v))
    long_u = long_v / max(L_len, 1e-9)
    a = math.degrees(math.atan2(long_u[1], long_u[0])) + 90 + rng.uniform(-jitter_deg, jitter_deg)
    if angle is not None:
        a = angle + rng.uniform(-jitter_deg / 2, jitter_deg / 2)
        long_u = _dir(angle + 90)
    c = np.array(P.centroid.coords[0]) + long_u * rng.uniform(-pos, pos) * L_len
    big = 2 * L_len + 10
    line = LineString([c - big * _dir(a), c + big * _dir(a)])
    return _polys(split(P, line)), line.intersection(P)


HILL_STRIP_M = (50.0, 140.0)   # terrace strips on an organic hillside: deep (downhill), long


def _hill_cut(P: Polygon, f: Fields, c: CitySpec, rng) -> float | str | None:
    """On a hillside near an organic node, terrace strips every building of which fronts a
    street: cut along the contour while a strip is deeper than HILL_STRIP_M[0] (a contour
    street between strips), straight down the slope while it's longer than HILL_STRIP_M[1]
    (a stair), then "stop". None elsewhere (the usual cut across the long axis)."""
    cx, cy = P.centroid.coords[0]
    if not any(nd.layout == "organic" and math.dist(nd.center, (cx, cy)) < 1.6 * nd.radius for nd in c.nodes):
        return None
    if float(f.slope(cx, cy)) < 0.08:
        return None
    e = 10.0
    down = math.degrees(math.atan2(-(float(f.z(cx, cy + e)) - float(f.z(cx, cy - e))),
                                   -(float(f.z(cx + e, cy)) - float(f.z(cx - e, cy)))))
    pts = np.array(P.exterior.coords)
    deep, long_ = np.ptp(pts @ _dir(down)), np.ptp(pts @ _dir(down + 90))
    if long_ > HILL_STRIP_M[1] and (deep <= HILL_STRIP_M[0] or rng.random() < 0.5):
        return down                  # a cut down the slope: it becomes a stair
    if deep > HILL_STRIP_M[0]:
        return down + 90             # along the contour
    return "stop"


def subdivide(P: Polygon, f: Fields, c: CitySpec, rng, streets: list, depth=0) -> list[Polygon]:
    """Split with local streets until each piece is block-sized for its density."""
    if P.area < MIN_BLOCK_M2:
        return []
    cx, cy = P.centroid.coords[0]
    d = float(f.density(cx, cy))
    if d < c.urban_threshold * 0.7:
        return []           # countryside: no local streets
    size = c.block_m[0] + (c.block_m[1] - c.block_m[0]) * (1 - d)
    hill = _hill_cut(P, f, c, rng)
    if hill == "stop" or depth > 12 or (hill is None and (P.area <= size * size * 1.3 or depth > 9)):
        return [P]
    parts, line = _cut(P, rng, angle=hill)
    if len(parts) < 2 or line.is_empty:
        return [P]
    gap = line.buffer(c.street_w / 2, cap_style="flat")
    streets.append(line)
    out = []
    for part in parts:
        for q in _polys(part.difference(gap)):
            if q.area >= MIN_BLOCK_M2 and q.area / max(q.minimum_rotated_rectangle.area, 1) > 0.35:
                out += subdivide(q, f, c, rng, streets, depth + 1)
    return out


# --- massing ---------------------------------------------------------------------------------

def _edges(P: Polygon, min_len: float):
    """(a, b, inward normal) of each exterior edge, counter-clockwise."""
    P = orient(P.simplify(2.0), 1.0)
    pts = np.array(P.exterior.coords)
    for a, b in zip(pts[:-1], pts[1:]):
        e = b - a
        n = float(np.linalg.norm(e))
        if n >= min_len:
            yield a, b, np.array([-e[1], e[0]]) / n


def _box_on(f: Fields, cx, cy, w, d, rot, storeys, c, kind, district) -> Box:
    cs = [np.array([cx, cy]) + sx * w / 2 * _dir(rot) + sy * d / 2 * _dir(rot + 90)
          for sx in (-1, 1) for sy in (-1, 1)]
    zs = f.z(np.array([p[0] for p in cs]), np.array([p[1] for p in cs]))
    lo, hi = float(zs.min()), float(zs.max())
    return Box(cx, cy, w, d, rot, lo - 0.3, storeys * c.storey_m + (hi - lo) + 0.3, kind, district)


def mass_block(P: Polygon, f: Fields, c: CitySpec, rng, district) -> list[Box]:
    cx, cy = P.centroid.coords[0]
    d = float(f.density(cx, cy))
    t = float(np.clip((d - TERRACE_D) / (1 - TERRACE_D), 0, 1))
    out = []
    obb = P.minimum_rotated_rectangle
    q = np.array(obb.exterior.coords)[:4]
    short = min(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[1]))
    if d >= TERRACE_D:      # perimeter blocks / terraces: facade segments along every edge
        inner = P.buffer(-1.5)
        if inner.is_empty:
            return []
        for poly in _polys(inner):
            depth = min(14.0 if d >= CORE_D else 12.0, short / 2)
            seg = c.parcel_m[0] + (c.parcel_m[1] - c.parcel_m[0]) * (1 - t)
            lo_s = round(3 + (c.storeys[0] - 3) * t)
            hi_s = round(4 + (c.storeys[1] - 4) * t)
            for a, b, n in _edges(poly, MIN_STRIP_M):
                L_e = float(np.linalg.norm(b - a))
                k = max(1, round(L_e / seg))
                rot = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                for i in range(k):
                    m = a + (b - a) * (i + 0.5) / k
                    if d < CORE_D and rng.random() < 0.15:
                        continue    # gaps in the terraces
                    ctr = m + n * depth / 2
                    out.append(_box_on(f, ctr[0], ctr[1], L_e / k - 0.4, depth, rot,
                                       int(rng.integers(lo_s, hi_s + 1)), c,
                                       "housing", district))
    else:                   # detached houses along the block's edges (and a second row inside)
        keep = 0.5 + 0.4 * d / TERRACE_D
        rows = [P.buffer(-5)] + ([P.buffer(-34)] if short > 90 else [])
        for poly in [q for r in rows for q in _polys(r)]:
            for a, b, n in _edges(poly, 12.0):
                L_e = float(np.linalg.norm(b - a))
                k = max(1, int(L_e / (c.parcel_m[1] * 1.2)))
                rot = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                for i in range(k):
                    if rng.random() > keep:
                        continue
                    w, dd = rng.uniform(10, 16), rng.uniform(9, 13)
                    m = a + (b - a) * (i + 0.5) / k + n * (dd / 2 + 1)
                    out.append(_box_on(f, m[0], m[1], w, dd, rot + rng.choice([0, 0, 90]),
                                       int(rng.integers(c.storeys[2], c.storeys[3] + 1)), c,
                                       "houses", district))
    return out


# --- green, markets, harbour --------------------------------------------------------------------

def corridors(L: SiteLayout, f: Fields, rng) -> list[LineString]:
    """Valleys: steepest descent from the inland edge of the city to the sea."""
    c = L.city
    x0, y0, x1, y1 = c.bounds
    out = []
    if not c.green_corridors:
        return out
    # start points spread along the coast, on the inland edge of the bounds
    corners = np.array([[x0, y0], [x0, y1], [x1, y0], [x1, y1]])
    inl = f.inland(corners[:, 0], corners[:, 1])
    lat_all = corners @ f.v
    lats = np.linspace(lat_all.min(), lat_all.max(), c.green_corridors + 2)[1:-1]
    deep = float(inl.max()) * 0.9
    for lat in lats:
        lat += rng.uniform(-0.25, 0.25) * (lat_all.max() - lat_all.min()) / (c.green_corridors + 1)
        p = (f._shore(lat) - deep) * f.u + lat * f.v
        pts = [p]
        hd = None
        phase = rng.uniform(0, 2 * math.pi)
        for step in range(600):
            best, bz = None, 1e9
            for a in range(0, 360, 15):
                if hd is not None and abs(_angdiff(a, hd)) > 60:
                    continue
                q = pts[-1] + 25 * _dir(a)
                z = float(f.z(*q))
                if z < bz:
                    best, bz = a, z
            hd = best      # downhill, meandering like a stream (+-35 deg over ~1 km)
            q = pts[-1] + 25 * _dir(hd + 35 * math.sin(2 * math.pi * step * 25 / 1000 + phase))
            pts.append(q)
            if not f.land(*q):
                break
        if len(pts) > 4:
            out.append(LineString(pts).simplify(10))
    return out


def market(f: Fields, c: CitySpec, x, y, rot, size, rng, district) -> tuple[Polygon, list[Box]]:
    sq = shapely.affinity.rotate(sbox(x - size / 2, y - size / 2, x + size / 2, y + size / 2), rot,
                                 origin=(x, y))
    stalls = []
    for i in range(-2, 3):
        for j in range(-3, 4):
            if rng.random() < 0.2:
                continue
            p = np.array([x, y]) + i * 9 * _dir(rot + 90) + j * 6.5 * _dir(rot)
            stalls.append(_box_on(f, p[0], p[1], 5.0, 3.5, rot, 1, c, "stall", district))
            stalls[-1].h = 3.0
    return sq, stalls


def piers(L: SiteLayout, f: Fields, nd: CivicNode) -> list[Box]:
    lat = float(np.dot(nd.center, f.v))
    out = []
    for k in (-1, 0, 1):
        la = lat + k * 70
        s = float(f._shore(la))
        mid = (s + 60) * f.u + la * f.v
        rot = math.degrees(math.atan2(f.u[1], f.u[0]))
        out.append(Box(float(mid[0]), float(mid[1]), 150.0, 12.0, rot, L.sea_level - 2, 3.5, "pier", nd.id))
    return out


# --- the plan --------------------------------------------------------------------------------

def nearest_node(c: CitySpec, x, y) -> str | None:
    """The district at a point: a town's within 1.4 x its radius, else the nearest node's."""
    for tw in c.towns:
        if math.dist(tw.center, (x, y)) < 1.4 * tw.radius:
            return tw.id
    return min(c.nodes, key=lambda n: math.dist(n.center, (x, y))).id if c.nodes else None


def plan(layout: SiteLayout, heights: np.ndarray) -> CityPlan:
    if layout.city is None:
        raise ValueError("layout has no city spec")
    c = layout.city
    f = Fields(layout, heights)
    rng = np.random.default_rng(c.seed)
    L = civic_layout(layout, f)
    P = CityPlan(layout=L)
    P.ways = ways(L, f)

    # superblocks: faces between the ways, on land, minus the ways and the civic cores
    region = _land_region(L, f, 30 + c.promenade_w)
    if c.enclosure:    # a walled city: blocks inside the ring road, and in the towns
        e = c.enclosure
        region = region.intersection(unary_union(
            [Point(e.center).buffer(ring_road_r(c) - c.arterial_w / 2, quad_segs=64)]
            + [Point(tw.center).buffer(tw.radius, quad_segs=32) for tw in c.towns]))
    lines = [w.line for w in P.ways] + [region.boundary]
    faces = [g for g in polygonize(unary_union(lines)) if region.contains(g.representative_point())]
    cut = unary_union([w.line.buffer(w.width / 2, cap_style="flat") for w in P.ways]
                      + [Point(summit(n, f.z)).buffer(organic_plaza_r(n) + 6, quad_segs=24) if n.layout == "organic"
                         else Point(n.center).buffer(_rings(n)["core"], quad_segs=24) for n in c.nodes]
                      + [Point(tw.center).buffer(TOWN_PLAZA_M, quad_segs=24) for tw in c.towns])
    green_lines = corridors(L, f, rng)
    green = unary_union([g.buffer(c.corridor_w / 2) for g in green_lines]).intersection(region) \
        if green_lines else Polygon()
    supers = [q for g in faces for q in _polys(g.difference(cut).difference(green))]
    P.parks += [q for q in _polys(green.difference(cut)) if q.area > 400]
    if c.enclosure:
        P.gates = gate_angles(c, f)
        P.walls = wall_parts(c, f, P.gates)
        P.parks += greenbelt(c, _land_region(L, f, 30).difference(
            unary_union([Point(tw.center).buffer(tw.radius) for tw in c.towns]) if c.towns else Polygon()), cut)

    streets: list = []
    for sb in supers:
        P.blocks += subdivide(sb, f, c, rng, streets)
    for i, s in enumerate(streets):
        geoms = list(s.geoms) if hasattr(s, "geoms") else [s]
        for g, ln in enumerate(geoms):        # a cut can cross a block in several pieces
            if isinstance(ln, LineString) and ln.length > 15:
                m = ln.interpolate(0.5, normalized=True)
                P.ways += split_by_grade(f, ln, f"st{i}" if len(geoms) == 1 else f"st{i}g{g}", "street", c.street_w,
                                         nearest_node(c, m.x, m.y))

    # markets: avenue crossings at middling density, spread out; and each market node
    avs = [w for w in P.ways if w.kind in ("avenue", "arterial")]
    spots = []
    for nd in c.nodes:
        if nd.role == "market":
            spots.append((nd.center[0], nd.center[1], nd.rot, _rings(nd)["plaza"] * 1.4, nd.id, True))
    for tw in c.towns:    # the town square
        spots.append((tw.center[0], tw.center[1], 0.0, TOWN_PLAZA_M * 1.6, tw.id, True))
    cand = []
    for i, a in enumerate(avs):
        for b in avs[i + 1:]:
            g = a.line.intersection(b.line)
            for pt in getattr(g, "geoms", [g]):
                if isinstance(pt, Point) and 0.35 <= float(f.density(pt.x, pt.y)) <= 0.85 \
                        and all(math.dist(n.center, (pt.x, pt.y)) > n.radius + 40 for n in c.nodes) \
                        and region.contains(pt):
                    cand.append(pt)
    rng.shuffle(cand)
    for pt in cand:
        if len(spots) >= c.markets + sum(1 for n in c.nodes if n.role == "market") + len(c.towns):
            break
        if all(math.dist((pt.x, pt.y), s[:2]) > 450 for s in spots):
            spots.append((pt.x + 30, pt.y + 30, rng.uniform(0, 90), 56.0, nearest_node(c, pt.x, pt.y), False))
    for x, y, rot, size, dist, on_plaza in spots:
        P.markets.append(market(f, c, x, y, rot, size, rng, dist))
    market_cut = unary_union([m[0].buffer(4) for m in P.markets if m]) if P.markets else Polygon()

    # massing, or parks; the city frays out below the threshold
    def keep(i, b):
        cx, cy = b.centroid.coords[0]
        d = float(f.density(cx, cy))
        if d < c.urban_threshold and rng.random() > d / c.urban_threshold * 0.4:
            return False
        if rng.random() < c.park_share * (1.6 - d):
            P.parks.append(b)
            return False
        return nearest_node(c, cx, cy)

    if c.typologies:   # the catalog: typologies per block, line features, variation
        from . import city_types
        P.typed = [b for b in city_types.mass(P, f, rng, green_lines, keep)
                   if market_cut.is_empty or not market_cut.contains(Point(b.x, b.y))]
    else:
        for i, b in enumerate(P.blocks):
            dist = keep(i, b)
            if dist is False:
                continue
            for bx in mass_block(b, f, c, rng, dist):
                if market_cut.is_empty or not market_cut.contains(Point(bx.x, bx.y)):
                    P.buildings.append(bx)
    for nd in c.nodes:
        if nd.role == "harbour":
            P.piers += piers(L, f, nd)

    if c.trees:
        P.trees = trees(P, f, rng)
    P.stats = stats(P)
    return P


STAIR_TREAD_M = 1.6


def stair_treads(sb, w: Way, hs) -> None:
    """Steps on a stair: one riser box per tread length, so the depth pass shows a
    staircase rather than a smooth ramp."""
    from .greybox import UNIT_BOX
    ln = w.line
    for s in np.arange(0, ln.length - STAIR_TREAD_M, STAIR_TREAD_M):
        p, q = ln.interpolate(s), ln.interpolate(s + STAIR_TREAD_M)
        z0, z1 = float(hs(p.x, p.y)), float(hs(q.x, q.y))
        lo, hi = min(z0, z1), max(z0, z1)
        a = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
        sb.add("stair:tread", UNIT_BOX, ((p.x + q.x) / 2, (p.y + q.y) / 2, lo), a + 90,
               (w.width, STAIR_TREAD_M, round(max(hi - lo, 0.15) + 0.12, 2)))


TREE_SPECIES = {          # (width, height) ranges, m
    # stone pine: a broad, flat canopy on a short stout trunk. A tall thin trunk under a
    # domed crown reads as a palm in the depth pass (and Flux draws palms).
    "pine": ((13.0, 17.0), (10.0, 14.0)),
    "cypress": ((2.0, 2.8), (11.0, 16.0)),    # tall and narrow
    "olive": ((5.0, 7.0), (4.5, 6.5)),        # low and round
}


def _tree(rng, x, y, z, weights) -> tuple:
    kinds = list(weights)
    p = np.array([weights[k] for k in kinds], float)
    k = kinds[int(rng.choice(len(kinds), p=p / p.sum()))]
    (w0, w1), (h0, h1) = TREE_SPECIES[k]
    return (float(x), float(y), float(z), float(rng.uniform(w0, w1)), float(rng.uniform(h0, h1)), k)


def trees(P: CityPlan, f: Fields, rng) -> list[tuple]:
    """Street trees along avenues (mostly umbrella pines, some cypresses) and park trees
    (pines, cypresses and olives): three silhouettes, so the depth pass asks for them."""
    out = []
    plazas = [Point(n.center).buffer(_rings(n)["plaza"] + 10) for n in P.layout.city.nodes]
    for w in P.ways:
        if w.kind not in ("avenue", "arterial", "promenade"):
            continue
        for s in np.arange(10, w.line.length, 16):
            p = w.line.interpolate(s)
            q = w.line.interpolate(min(s + 1, w.line.length))
            t = np.array([q.x - p.x, q.y - p.y])
            n = np.array([-t[1], t[0]]) / max(np.linalg.norm(t), 1e-9)
            for side in (-1, 1):
                x, y = np.array([p.x, p.y]) + side * n * (w.width / 2 - 2)
                if any(z.contains(Point(x, y)) for z in plazas) or not f.land(x, y):
                    continue
                out.append(_tree(rng, x, y, f.z(x, y), {"pine": 0.65, "cypress": 0.35}))
    for pk in P.parks:
        x0, y0, x1, y1 = pk.bounds
        n = int(pk.area / 260)
        xs, ys = rng.uniform(x0, x1, n * 2), rng.uniform(y0, y1, n * 2)
        inside = shapely.contains_xy(pk, xs, ys)
        for x, y in zip(xs[inside][:n], ys[inside][:n]):
            out.append(_tree(rng, x, y, f.z(x, y), {"pine": 0.4, "cypress": 0.25, "olive": 0.35}))
    if len(out) > TREE_CAP:
        idx = rng.choice(len(out), TREE_CAP, replace=False)
        out = [out[i] for i in sorted(idx)]
    return out


def stats(P: CityPlan) -> dict:
    """Footprint and floor area by use, and the checks the brief asks for."""
    from .greybox import expand_rows
    foot = {"housing": 0.0, "civic": 0.0, "monument": 0.0, "market": 0.0}
    floor = dict(foot)
    for b in P.buildings:
        a = b.w * b.d
        foot["housing"] += a
        floor["housing"] += a * max(1, round(b.h / P.layout.city.storey_m))
    for b in P.typed:        # catalog buildings: housing and mixed use count as housing
        key = "housing" if b.family in ("housing", "mixed") else "civic"
        foot[key] += b.foot
        floor[key] += b.foot * max(1, b.storeys)
    for pl in expand_rows(P.layout):
        if pl.type == "rotunda":
            a = math.pi * (pl.size[0] / 2) ** 2
            foot["monument"] += a
            floor["monument"] += a
        elif pl.type == "temple":
            foot["monument"] += pl.size[0] * pl.size[1]
            floor["monument"] += pl.size[0] * pl.size[1]
        elif pl.type == "stoa":
            R, a0, a1 = pl.arc
            a = math.radians((a1 - a0) % 360) * R * 7
            foot["civic"] += a
            floor["civic"] += a
        else:
            a = pl.size[0] * pl.size[1]
            foot["civic"] += a
            floor["civic"] += a * max(1, round(pl.size[2] / 4))
    for sq, stalls in P.markets:
        foot["market"] += sq.area
        floor["market"] += sq.area
    total = sum(foot.values()) or 1.0
    share = {k: round(float(v / total), 4) for k, v in foot.items()}
    warn = []
    if share["housing"] < 0.6:
        warn.append(f"housing is {share['housing']:.0%} of the built footprint (want >= 60%)")
    if share["monument"] > 0.03:
        warn.append(f"monuments are {share['monument']:.1%} of the built footprint (want <= 3%)")
    allb = [b for b in P.buildings] + [b for b in P.typed if b.storeys > 0]
    hs = np.array([b.h for b in allb]) if allb else np.zeros(1)
    xs = [p for b in P.blocks for p in b.bounds]
    foot = {k: float(v) for k, v in foot.items()}
    floor = {k: float(v) for k, v in floor.items()}
    out_rep = None
    if P.typed:
        from . import city_types
        out_rep = city_types.report(P, P.typed)
        warn += out_rep["warnings"]
    return {"buildings": len(allb), "housing_blocks": sum(1 for b in allb if getattr(b, "kind", "") == "housing"),
            "houses": sum(1 for b in allb if getattr(b, "kind", "") == "houses"), "blocks": len(P.blocks),
            "repetition": out_rep,
            "streets": sum(1 for w in P.ways if w.kind == "street"),
            "avenues": sum(1 for w in P.ways if w.kind in ("avenue", "arterial", "promenade", "ring")),
            "parks": len(P.parks), "park_area_ha": round(sum(p.area for p in P.parks) / 1e4, 1),
            "markets": len(P.markets), "trees": len(P.trees),
            "monuments": sum(1 for n in P.layout.city.nodes if n.monument),
            "footprint_m2": {k: round(v) for k, v in foot.items()}, "footprint_share": share,
            "floor_m2": {k: round(v) for k, v in floor.items()},
            "height_m": {"median": round(float(np.median(hs)), 1), "p90": round(float(np.percentile(hs, 90)), 1)},
            "extent_m": [round(float(max(xs[2::4]) - min(xs[0::4]))), round(float(max(xs[3::4]) - min(xs[1::4])))] if xs else [0, 0],
            "warnings": warn}


# --- slots ------------------------------------------------------------------------------------

def _drape(f_z, coords, lift: float, step: float = 10.0) -> list[list[float]]:
    ln = LineString(coords).segmentize(step)
    pts = np.array(ln.coords)
    z = f_z(pts[:, 0], pts[:, 1]) + lift
    return [[float(x), float(y), float(zz)] for (x, y), zz in zip(pts, z)]


def add_slots(P: CityPlan, new, hs) -> None:
    """Add the plan's ways, housing, parks, trees, markets and piers as greybox slots
    (`new` is build_spec's slot factory). Housing, streets and trees go per tile."""
    from .greybox import UNIT_BOX, poly, ribbon
    c = P.layout.city
    # x<i>y<j>: stays unique when made file-safe (negative indices keep their sign)
    tile = lambda x, y: f"x{int(math.floor(x / c.tile_m))}y{int(math.floor(y / c.tile_m))}"  # noqa: E731
    groups: dict[tuple[str, str], list] = {}

    def group(kind, x, y, item, district):
        groups.setdefault((kind, tile(x, y)), []).append((item, district))

    def ribbon_piece(sb, name, coords, w, lift):
        pts = _drape(hs.many, coords, lift)
        o = pts[0]
        sb.add(name, ribbon([[p[0] - o[0], p[1] - o[1], p[2] - o[2]] for p in pts], w, 0.1), tuple(o))

    for w in P.ways:
        if w.kind == "street":
            m = w.line.interpolate(0.5, normalized=True)
            group("street", m.x, m.y, w, w.district)
            continue
        x, y = w.line.coords[0]
        kind = w.kind if w.kind in ("lane", "stair") else "avenue"
        sb = new(f"way-{w.id}", kind, (x, y, float(hs(x, y))), district=w.district,
                 category="terrain")
        ribbon_piece(sb, w.kind, list(w.line.coords), w.width, 0.12)
        if w.kind == "stair":
            stair_treads(sb, w, hs)
    for b in P.buildings:
        group(b.kind, b.x, b.y, b, b.district)
    typed: dict[tuple, list] = {}
    for b in P.typed:
        typed.setdefault((b.typology, tile(b.x, b.y), b.material), []).append(b)
    fam = {t.id: t.family for t in c.typologies}
    for (typ, key, mat), bs in sorted(typed.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
        ds = [b.district for b in bs if b.district]
        district = max(set(ds), key=ds.count) if ds else None
        cx = float(np.mean([b.x for b in bs]))
        cy = float(np.mean([b.y for b in bs]))
        sid = f"{typ}-{key}" + (f"-{mat}" if mat and sum(1 for k in typed if k[:2] == (typ, key)) > 1 else "")
        sb = new(sid, typ, (cx, cy, float(hs(cx, cy))), district=district,
                 category="structure" if fam.get(typ) == "public_realm" else "building", material=mat, typology=typ)
        for b in bs:
            for pt in b.parts:
                prim, scale = unit_prim(pt)
                sb.add(f"{typ}:{pt.role}", prim, (pt.x, pt.y, pt.z), pt.rot, scale)
    for pk in P.parks:
        m = pk.representative_point()
        group("park", m.x, m.y, pk, nearest_node(c, m.x, m.y))
    for t in P.trees:
        group("park", t[0], t[1], t, nearest_node(c, t[0], t[1]))

    for (kind, key), items in sorted(groups.items()):
        ds = [d for _, d in items if d]
        district = max(set(ds), key=ds.count) if ds else None
        cx = float(np.mean([_xy(it)[0] for it, _ in items]))
        cy = float(np.mean([_xy(it)[1] for it, _ in items]))
        sb = new(f"{kind}-{key}", kind, (cx, cy, float(hs(cx, cy))), district=district)
        for it, _ in items:
            if isinstance(it, Box):
                sb.add(f"{kind}:box", UNIT_BOX, (it.x, it.y, it.z), it.rot, (it.w, it.d, it.h))
            elif isinstance(it, Way):
                ribbon_piece(sb, "street", list(it.line.coords), it.width, 0.1)
            elif isinstance(it, Polygon):
                pts = _drape(hs.many, list(it.exterior.coords)[:-1], 0.25, 15.0)
                o = pts[0]
                sb.add("lawn", poly([[p[0] - o[0], p[1] - o[1], p[2] - o[2]] for p in pts], 0.1), tuple(o))
            else:
                x, y, z, w, h, kind = it
                if kind == "cypress":
                    sb.add("tree:cypress", TREE, (x, y, z), 0.0, (w, w, h))
                else:   # a trunk and a crown: a wide flat umbrella (pine) or a low round one (olive)
                    trunk = 0.5 if kind == "pine" else 0.3
                    sb.add(f"tree:{kind}-trunk", TREE, (x, y, z), 0.0, (1.1, 1.1, h * trunk + 0.5))
                    crown_h = h * (0.24 if kind == "pine" else 1 - trunk)    # pines: flat
                    sb.add(f"tree:{kind}-crown", UNIT["dome"], (x, y, z + h * trunk), 0.0, (w, w, crown_h * 2))
    for i, (sq, stalls) in enumerate(P.markets):
        m = sq.centroid
        sb = new(f"market-{i}", "market", (m.x, m.y, float(hs(m.x, m.y))),
                 district=stalls[0].district if stalls else None)
        pts = _drape(hs.many, list(sq.exterior.coords)[:-1], 0.15, 15.0)
        o = pts[0]
        sb.add("paving", poly([[p[0] - o[0], p[1] - o[1], p[2] - o[2]] for p in pts], 0.1), tuple(o))
        for b in stalls:
            sb.add("market:stall", UNIT_BOX, (b.x, b.y, b.z), b.rot, (b.w, b.d, b.h))
    if P.piers:
        b0 = P.piers[0]
        sb = new("quay", "quay", (b0.x, b0.y, b0.z), district=b0.district)
        for b in P.piers:
            sb.add("quay:pier", UNIT_BOX, (b.x, b.y, b.z), b.rot, (b.w, b.d, b.h))
    walls: dict[str, list[WallPart]] = {}
    for wp in P.walls:
        walls.setdefault(wp.group, []).append(wp)
    for g, parts in walls.items():
        p0 = parts[len(parts) // 2]
        sb = new(g, g.split("-")[0], (p0.x, p0.y, p0.z), district=nearest_node(c, p0.x, p0.y))
        for wp in parts:
            sb.add(wp.piece, wp.prim, (wp.x, wp.y, wp.z), wp.rot, wp.scale)


TREE = {"kind": "cylinder", "r": 0.5, "h": 1.0, "seg": 8}
UNIT = {"box": {"kind": "box", "w": 1.0, "d": 1.0, "h": 1.0},
        "gable": {"kind": "gable", "w": 1.0, "d": 1.0, "h": 1.0},
        "vault": {"kind": "vault", "w": 1.0, "d": 1.0, "h": 1.0, "seg": 12},
        "cyl": {"kind": "cylinder", "r": 0.5, "h": 1.0, "seg": 24},
        "dome": {"kind": "dome", "r": 0.5, "seg": 24}}


def unit_prim(pt) -> tuple[dict, list | None]:
    """A catalog part as (primitive, scale): unit primitives scaled, others as given (with
    their own scale if any: an arcade stretched to its facade)."""
    if isinstance(pt.prim, dict):
        return pt.prim, list(pt.scale) if pt.scale else None
    if pt.prim == "dome":                 # the unit dome is r 0.5, 0.5 tall
        return UNIT["dome"], [pt.w, pt.d, pt.h * 2]
    return UNIT[pt.prim], [pt.w, pt.d, pt.h]


def _xy(it) -> tuple[float, float]:
    if isinstance(it, Box):
        return it.x, it.y
    if isinstance(it, Way):
        m = it.line.interpolate(0.5, normalized=True)
        return m.x, m.y
    if isinstance(it, Polygon):
        m = it.representative_point()
        return m.x, m.y
    return it[0], it[1]


# --- plan image ------------------------------------------------------------------------------

PLAN_COLORS = {"sea": (46, 84, 110), "land": (196, 188, 168), "park": (110, 150, 90), "way": (236, 232, 222),
               "housing": (150, 110, 90), "houses": (190, 150, 120), "civic": (240, 240, 235),
               "market": (215, 170, 60), "node": (250, 250, 250), "tree": (70, 110, 60), "wall": (60, 55, 50)}


def plan_image(P: CityPlan, heights: np.ndarray, px: int = 2400, margin: float = 400.0):
    """Top-down plan of the city (PIL image): hillshaded terrain, sea, parks, ways,
    massing by type, civic cores."""
    from PIL import Image, ImageDraw
    from .greybox import Heights, expand_rows
    c, t = P.layout.city, P.layout.terrain
    x0, y0, x1, y1 = c.bounds
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    half = t.extent_m / 2          # never past the terrain
    x0, y0, x1, y1 = max(x0, -half), max(y0, -half), min(x1, half), min(y1, half)
    s = px / (x1 - x0)
    W, H = px, int(round((y1 - y0) * s))
    xs = np.linspace(x0, x1, W)
    ys = np.linspace(y1, y0, H)
    gx, gy = np.meshgrid(xs, ys)
    z = Heights(heights, t.extent_m).many(gx, gy)
    dzx, dzy = np.gradient(z, 1 / s)
    shade = np.clip(0.75 + 1.8 * (-dzx * 0.6 + dzy * 0.8) / np.sqrt(1 + dzx ** 2 + dzy ** 2), 0.45, 1.15)
    land = z > P.layout.sea_level
    img = np.where(land[..., None], np.array(PLAN_COLORS["land"]) * shade[..., None], PLAN_COLORS["sea"])
    contour = land & (np.floor(z / 10) != np.floor(np.roll(z, 1, 0) / 10))
    img[contour] *= 0.85
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    to = lambda x, y: ((x - x0) * s, (y1 - y) * s)  # noqa: E731

    def fill(poly, col):
        for q in _polys(poly):
            d.polygon([to(*p) for p in q.exterior.coords], fill=col)

    for pk in P.parks:
        fill(pk, PLAN_COLORS["park"])
    for w in P.ways:
        d.line([to(*p) for p in w.line.coords], fill=PLAN_COLORS["way"], width=max(1, int(w.width * s)))
    for b in P.buildings:
        u, v = _dir(b.rot) * b.w / 2, _dir(b.rot + 90) * b.d / 2
        pts = [np.array([b.x, b.y]) + a * u + e * v for a, e in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon([to(*p) for p in pts], fill=PLAN_COLORS[b.kind])
    if P.typed:
        from .city_types import FAMILY_COLORS
        for b in P.typed:
            for pt in b.parts:
                if not isinstance(pt.prim, str) or pt.role not in ("mass", "ground", "podium", "base", "step"):
                    continue
                u, v = _dir(pt.rot) * pt.w / 2, _dir(pt.rot + 90) * pt.d / 2
                pts = [np.array([pt.x, pt.y]) + a * u + e * v for a, e in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
                d.polygon([to(*p) for p in pts], fill=FAMILY_COLORS.get(b.family, PLAN_COLORS["housing"]))
    for sq, stalls in P.markets:
        fill(sq, PLAN_COLORS["market"])
    for b in P.piers:
        u, v = _dir(b.rot) * b.w / 2, _dir(b.rot + 90) * b.d / 2
        pts = [np.array([b.x, b.y]) + a * u + e * v for a, e in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon([to(*p) for p in pts], fill=PLAN_COLORS["civic"])
    for r in P.layout.rings:
        cx, cy = r.center
        for rr in (r.radius - r.width / 2, r.radius + r.width / 2):
            d.ellipse([to(cx - rr, cy + rr), to(cx + rr, cy - rr)], outline=PLAN_COLORS["way"],
                      width=max(1, int(2 * s)))
    for pz in P.layout.plazas:
        cx, cy = pz.center
        d.ellipse([to(cx - pz.radius, cy + pz.radius), to(cx + pz.radius, cy - pz.radius)], fill=PLAN_COLORS["node"])
    for pl in expand_rows(P.layout):
        cx, cy = pl.center
        if pl.type == "stoa":
            continue
        if pl.type == "rotunda":
            r = pl.size[0] / 2
            d.ellipse([to(cx - r, cy + r), to(cx + r, cy - r)], fill=PLAN_COLORS["civic"], outline=(90, 90, 90))
            continue
        u, v = _dir(pl.rot) * pl.size[0] / 2, _dir(pl.rot + 90) * pl.size[1] / 2     # its footprint, rotated
        pts = [np.array([cx, cy]) + a * u + e * v for a, e in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon([to(*p) for p in pts], fill=PLAN_COLORS["civic"], outline=(90, 90, 90))
    for tr in P.trees[::3]:
        x, y = to(tr[0], tr[1])
        d.point((x, y), fill=PLAN_COLORS["tree"])
    for wp in P.walls:     # wall bays and gates: their footprint, rotated
        if wp.prim["kind"] == "cylinder":
            continue
        u, v = _dir(wp.rot) * wp.prim["w"] / 2, _dir(wp.rot + 90) * wp.prim["d"] / 2
        pts = [np.array([wp.x, wp.y]) + a * u + e * v for a, e in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        d.polygon([to(*p) for p in pts], fill=PLAN_COLORS["wall"])
    for n in list(c.nodes) + list(c.towns):
        x, y = to(*n.center)
        d.text((x + 6, y - 6), n.id, fill=(20, 20, 20))
    return im


# --- shots -------------------------------------------------------------------------------------

def _fit_lens(width_m: float, dist_m: float, sensor: float = 36.0, margin: float = 1.15) -> float:
    fov = 2 * math.atan(width_m * margin / 2 / max(dist_m, 1.0))
    return float(np.clip(sensor / (2 * math.tan(fov / 2)), 14.0, 85.0))


def shots(layout: SiteLayout, heights: np.ndarray) -> list:
    """Cameras for city mode: three wides that frame the whole city (from the hills, from
    the sea, a high aerial over the main node for the plan), two mediums per civic node
    (eye level down a radial to its centre, and a raised view of the whole node), and a
    tight on the monument (mood only)."""
    from .greybox import Heights
    from .schema import ShotSpec
    c, t = layout.city, layout.terrain
    hs = Heights(heights, t.extent_m)
    f = Fields(layout, heights)
    u, v = f.u, f.v
    x0, y0, x1, y1 = c.bounds
    corners = np.array([[x0, y0], [x0, y1], [x1, y0], [x1, y1]])
    width = float(np.ptp(corners @ v))
    w = np.array([n.radius for n in c.nodes])
    C = (np.array([n.center for n in c.nodes]) * w[:, None]).sum(0) / w.sum()
    zc = float(hs(*C))
    deep = float(f.inland(corners[:, 0], corners[:, 1]).max())
    out = _enclosure_wides(c, f, hs) if c.enclosure else _wides(c, f, hs, C, zc, deep, width)
    for nd in c.nodes:
        r = _rings(nd)
        cz = float(hs(*nd.center))
        # down the radial that faces the sea most, from the ring avenue to the centre
        angles = [nd.rot + 360 * k / max(nd.radials, 1) for k in range(max(nd.radials, 1))]
        a = min(angles, key=lambda a: abs(_angdiff(a, math.degrees(math.atan2(u[1], u[0])))))
        centre = np.array(nd.center, float)
        if nd.layout == "organic":        # up the lanes from below to the summit
            centre = np.array(summit(nd, f.z))
            cz = float(hs(*centre))
            a = _downhill_deg(f, *centre, organic_plaza_r(nd))
            p = centre + (organic_plaza_r(nd) + 70) * _dir(a)
            out.append(ShotSpec(id=f"med-{nd.id}", tier="medium", pos=(*p, float(hs(*p)) + 1.7),
                                look_at=(*centre, cz + 10.0), lens_mm=24.0, district=nd.id,
                                notes=f"eye-level view up the lanes and stairs of the {nd.role} quarter"))
        elif nd.centre == "quad":
            # the cloister view: from near one corner of the court, along one arcade (close,
            # its arches receding: depth only shows arches within ~20 m) to the far range
            half = QUAD_HALF * r["plaza"]
            a = quad_axis(nd, f)
            side = _dir(a + 90)
            p = centre + 0.7 * half * _dir(a) + 0.45 * half * side
            look = centre - half * _dir(a) + 0.75 * half * side
            out.append(ShotSpec(id=f"med-{nd.id}", tier="medium", pos=(*p, cz + 1.7),
                                look_at=(*look, cz + 4.0), lens_mm=24.0, district=nd.id,
                                notes="eye-level view along the arcades of a courtyard"))
        else:
            p = centre + r["ring1"] * _dir(a)
            target_h = 14.0 if nd.monument else 6.0
            out.append(ShotSpec(id=f"med-{nd.id}", tier="medium", pos=(*p, cz + 1.7),
                                look_at=(*nd.center, cz + target_h), lens_mm=24.0, district=nd.id,
                                notes=f"eye-level view across the {nd.role} plaza"))
        p = centre + (nd.radius * 1.15) * _dir(a + 25)
        out.append(ShotSpec(id=f"med-{nd.id}-high", tier="medium", pos=(*p, float(hs(*p)) + 55),
                            look_at=(*centre, cz), lens_mm=24.0, district=nd.id,
                            notes=f"raised view over the {nd.role} quarter"))
        if nd.monument:
            mon = next(pl for pl in civic_layout(layout, f).plots if pl.id == f"{nd.id}-{nd.monument}")
            rr = max(mon.size[0], mon.size[1]) / 2
            p = np.array(mon.center) + (rr + 26) * _dir(a + 8)
            mz = float(hs(*mon.center))
            out.append(ShotSpec(id=f"tight-{nd.id}", tier="tight", pos=(*p, max(mz, float(hs(*p))) + 1.7),
                                look_at=(*mon.center, mz + rr * 0.45), lens_mm=50.0, district=nd.id,
                                notes=f"the {nd.monument} up close, shallow depth of field"))
    return out


def _wides(c: CitySpec, f: Fields, hs, C, zc: float, deep: float, width: float) -> list:
    """An open city's wides: from the hills, from the sea, and a high aerial over the main
    node for the plan."""
    from .schema import ShotSpec
    u, v = f.u, f.v
    out = []
    # from the hills behind the city, towards the sea, looking down ~18 deg
    p = C - u * (deep * 0.45 + 1200)
    dist = float(np.linalg.norm(p - C))
    z = max(float(hs(*p)) + 150, zc + dist * math.tan(math.radians(18)))
    out.append(ShotSpec(id="wide-hills", tier="wide", pos=(*p, z), look_at=(*(C + u * 300), zc),
                        lens_mm=round(_fit_lens(width, float(np.linalg.norm(p - C))), 1),
                        notes="the whole city from the hills, spreading down to the sea"))
    # from the sea
    s0 = float(f._shore(float(C @ v)))
    p = (s0 + 1800) * u + float(C @ v) * v
    dist = float(np.linalg.norm(p - (C - u * 600)))
    out.append(ShotSpec(id="wide-sea", tier="wide", pos=(*p, zc + dist * math.tan(math.radians(13))),
                        look_at=(*(C - u * 600), zc + 20),
                        lens_mm=round(_fit_lens(width, float(np.linalg.norm(p - C))), 1),
                        notes="the coastal city seen from the sea, rising up the hills"))
    main = max(c.nodes, key=lambda n: n.weight * n.radius)
    m = np.array(main.center)
    p = m + u * 1500
    out.append(ShotSpec(id="wide-aerial", tier="wide", pos=(*p, float(hs(*m)) + 2300), look_at=(*(m - u * 300), float(hs(*m))),
                        lens_mm=24.0, notes="high aerial view of the city's radial plan"))
    return out


def aerial_distance(r: float, pitch: float, lens: float, sensor=(36.0, 36.0 * 768 / 1344)) -> float:
    """How far a camera looking down at `pitch` (rad) at a disc's centre must be for the
    whole disc of radius r to fit the frame: its sides in the width, its near and far
    edges in the height."""
    hh, vh = math.atan(sensor[0] / 2 / lens), math.atan(sensor[1] / 2 / lens)
    dist = r / math.tan(hh)
    while dist < 100 * r:
        H, X = dist * math.sin(pitch), dist * math.cos(pitch)
        near = math.atan2(H, X - r) if X > r else math.pi / 2
        if near <= pitch + vh and math.atan2(H, X + r) >= pitch - vh:
            break
        dist *= 1.03
    return dist


def _enclosure_wides(c: CitySpec, f: Fields, hs) -> list:
    """A walled city's wides frame the whole circle and its neighbouring towns: from the
    hills behind, from the sea (across the bay to a town), and a high oblique over the
    city at `aerial_pitch` that just fits the circle (24 mm)."""
    from .schema import ShotSpec
    u, v = f.u, f.v
    e = c.enclosure
    C = np.array(e.center, float)
    zc = float(hs(*C))
    # what the wides must show: the circle, and the towns
    pts = [C + e.radius * _dir(a) for a in range(0, 360, 15)]
    pts += [np.array(tw.center) + tw.radius * _dir(a) for tw in c.towns for a in range(0, 360, 30)]
    lat = np.array([p @ v for p in pts])
    width = float(np.ptp(lat))
    G = C + (0.5 * (lat.max() + lat.min()) - C @ v) * 0.6 * v      # towards the towns, the city first
    town = " and a smaller town nearby" if c.towns else ""
    out = []
    back = 1.5 * e.radius + 600
    p = G - u * back
    z = max(float(hs(*p)) + 150, zc + back * math.tan(math.radians(16)))
    out.append(ShotSpec(id="wide-hills", tier="wide", pos=(*p, z), look_at=(*(G + u * 0.2 * e.radius), zc),
                        lens_mm=round(_fit_lens(width, back), 1),
                        notes=f"a compact walled city by the sea seen from the hills{town}"))
    s0 = float(f._shore(float(G @ v)))
    p = (s0 + 1800.0) * u + float(G @ v) * v
    dist = float(np.linalg.norm(p - G))
    out.append(ShotSpec(id="wide-sea", tier="wide", pos=(*p, zc + dist * math.tan(math.radians(12))),
                        look_at=(*G, zc + 20), lens_mm=round(_fit_lens(width, dist), 1),
                        notes=f"a compact walled city seen from the sea, its ring wall and gates{town}"))
    lens, pitch = 24.0, math.radians(c.aerial_pitch)
    dist = aerial_distance(1.1 * e.radius, pitch, lens)
    p = C + u * dist * math.cos(pitch)            # over the sea, looking inland and down
    out.append(ShotSpec(id="wide-aerial", tier="wide", pos=(*p, zc + dist * math.sin(pitch)), look_at=(*C, zc),
                        lens_mm=lens, notes="high oblique aerial of a walled city's circular plan"))
    return out
