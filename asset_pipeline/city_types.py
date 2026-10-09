"""City mode with a typology catalog: which building type goes on each block, its greybox
massing, per-building variation, the line features (aqueducts, pergolas, sea steps, gates,
a lighthouse) and the anti-repetition report.

Assignment, per block (city.plan has already cut the blocks, parks and markets):
  1. The block's density band (core >= 0.6, middle >= 0.4, else edge) and zones: avenue
     frontage, crossing, waterfront, hillside, corridor, node ring.
  2. Weights: the district's mix for the band, plus each zone's overlay. On a hillside the
     overlay can turn every housing type into its terrace type.
  3. A type is dropped where its `place` doesn't fit the block, where it doesn't fit the
     block's size, or once it reaches its cap. A type already used on nearby blocks is
     damped (REPEAT_DAMP per neighbour), so neighbours differ.
  4. Big single-building types may merge the block with 1-3 neighbours across their local
     streets (typology merge_chance x district merge_chance): one large building, and the
     streets between them go.
  5. Massing by `form`. Fill forms (perimeter, row, bar, stepped, crescent, small courtyard
     houses) fill the block themselves. Any other form is one building, and the rest of the
     block gets the strongest fill-type housing along the edges it leaves free.

Every building picks its storeys (typology range +- jitter, + district bias, + 1 on an
avenue, + a smooth height field that drifts across its district, + now and then an accent
at an avenue crossing), roof, facade and ground-floor use, re-picking when it would extend a run of
identical neighbours along one frontage. The variants that change the silhouette are
modelled, since the depth pass is what the frames follow: arcades as a recessed ground floor,
set-back top storeys, roof gardens and pergolas as low masses on the roof, pitched roofs,
vaults and domes as real shapes.

Parts are scaled unit primitives (box, gable, vault, cylinder, dome), so the greybox and
the engine import share one mesh per primitive across the city.
"""
import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import city as C
from .schema import District, SiteLayout, Typology

BANDS = (("core", 0.6), ("middle", 0.4), ("edge", 0.0))
WATERFRONT_M = 250.0
HILLSIDE_SLOPE = 0.12
CORRIDOR_M = 80.0
CROSSING_M = 90.0
RING_M = 80.0
NEIGHBOUR_R = 350.0
NEIGHBOURS = 6
REPEAT_DAMP = 0.45
FILL_FORMS = {"perimeter", "row", "bar", "stepped", "crescent"}
ARCADE_DEPTH = 3.5
GARDEN = {"roof_garden", "garden_crown", "planted_walk"}
PERGOLA = {"pergola", "solar_pergola"}
PITCHED = {"terracotta_hip", "terracotta_pitch", "pitched_bronze"}
VAULTS = {"barrel_vault", "solar_vault"}
DOMES = {"shallow_dome", "half_domes"}
CROWNS = {"bronze_crown", "lantern"}
COLUMN_FACADES = ("colonnade", "pilasters")
FLAT_ROOFS_TOO = GARDEN | PERGOLA | {"skylight", "flat_stone", "flat_white", "flat_cornice", ""}
BAY_M = 4.5               # one arcade bay; arcade meshes are shared per (bays, height)
CORNICE_M = 0.45


def arcade_prim(n: int, h: float, d: float = 0.8, open_frac: float = 0.72) -> dict:
    """A wall of n arched bays BAY_M wide (n * BAY_M overall), h tall, d thick; the
    arches spring so their crown sits 0.6 m under the top."""
    span = BAY_M * open_frac
    return {"kind": "arcade", "n": int(n), "bay": BAY_M, "span": round(span, 3), "h": round(h, 2), "d": d,
            "spring": round(max(h - span / 2 - 0.6, 1.0), 3)}
FAMILY_COLORS = {"housing": (190, 140, 110), "mixed": (205, 120, 80), "civic": (245, 245, 238),
                 "infrastructure": (120, 120, 150), "public_realm": (150, 185, 120)}


@dataclass
class Part:
    """One scaled unit primitive (box | gable | vault | cyl | dome), or `prim` given as a
    full primitive dict (arc rings, arch walls)."""
    prim: str | dict
    x: float
    y: float
    z: float
    w: float
    d: float
    h: float
    rot: float
    role: str = "mass"
    scale: tuple | None = None        # for a dict prim: its scale (an arcade stretched to its facade)


@dataclass
class Building:
    typology: str
    family: str
    district: str | None
    x: float
    y: float
    rot: float
    storeys: int
    h: float
    foot: float                       # footprint, m2
    roof: str = ""
    facade: str = ""
    ground: str = ""
    columns: bool = False
    block: int = -1
    run: tuple | None = None          # (block, edge): frontage it sits on, in order
    material: str | None = None
    merged: int = 1                   # blocks this building stands on (plot merging)
    accent: bool = False              # raised as an accent at an avenue crossing
    parts: list[Part] = field(default_factory=list)


def site_of(t: Typology) -> str:
    if t.site != "auto":
        return t.site
    if t.width is None or t.depth is None:
        return "kit"
    pl = set(t.place)
    if t.form == "arcade_line" and pl & {"corridor", "waterfront"} and t.family in ("infrastructure", "public_realm"):
        return "corridor"
    if t.form == "pavilion" and "node_ring" in pl:
        return "node_gate"
    if t.form == "stepped" and pl == {"waterfront"} and t.storeys[1] <= 1:
        return "shore"
    if t.form == "tower" and pl == {"waterfront"}:
        return "pier_end"
    return "block"


def band_of(d: float) -> str:
    return next(b for b, lo in BANDS if d >= lo)


def _rect(cx, cy, w, d, rot) -> Polygon:
    u, v = C._dir(rot) * w / 2, C._dir(rot + 90) * d / 2
    c = np.array([cx, cy])
    return Polygon([c - u - v, c + u - v, c + u + v, c - u + v])


def _obb(P: Polygon):
    """Centre, angle of the long side, long and short length."""
    q = np.array(P.minimum_rotated_rectangle.exterior.coords)[:4]
    e1, e2 = q[1] - q[0], q[2] - q[1]
    if np.linalg.norm(e1) < np.linalg.norm(e2):
        e1, e2 = e2, e1
    return q.mean(0), math.degrees(math.atan2(e1[1], e1[0])), float(np.linalg.norm(e1)), float(np.linalg.norm(e2))


class Typer:
    def __init__(self, P, f, rng, green_lines):
        self.P, self.f, self.rng = P, f, rng
        L: SiteLayout = P.layout
        self.c = L.city
        self.types = {t.id: t for t in self.c.typologies}
        self.districts: dict[str, District] = {d.id: d for d in L.districts}
        self.mats = {m.id for m in L.materials}
        self.placed = Counter()                      # typology -> placements (blocks / features)
        self.by_district = defaultdict(Counter)      # district -> typology -> buildings
        self.foot = defaultdict(Counter)             # district -> typology -> footprint m2
        self.grid = defaultdict(list)                # cell -> [(x, y, typology)]
        ways = [w for w in P.ways if w.kind in ("avenue", "arterial", "promenade", "ring")]
        self.av_buf = unary_union([w.line.buffer(w.width / 2 + 6) for w in ways]) if ways else Polygon()
        self.crossings = []
        avs = [w for w in P.ways if w.kind in ("avenue", "arterial")]
        for i, a in enumerate(avs):
            for b in avs[i + 1:]:
                g = a.line.intersection(b.line)
                self.crossings += [p for p in getattr(g, "geoms", [g]) if isinstance(p, Point)]
        self.green = unary_union([g for g in green_lines]) if green_lines else None
        self.avoid = Polygon()                       # line features: no buildings on them
        self.out: list[Building] = []
        self.cross_xy = np.array([[p.x, p.y] for p in self.crossings]) if self.crossings else np.zeros((0, 2))
        self._fields: dict = {}                      # district -> height noise grid
        self.done: set[int] = set()                  # blocks assigned or merged into another
        self.tree = None                             # STRtree of the plan's blocks (merging)

    # --- zones and weights -------------------------------------------------------------

    def zones(self, b: Polygon, d: float) -> set[str]:
        cx, cy = b.centroid.coords[0]
        band = band_of(d)
        z = {"interior"} | ({band} if band != "middle" else set())
        if not self.av_buf.is_empty and b.intersects(self.av_buf):
            z.add("avenue")
        if any(p.distance(b) < CROSSING_M for p in self.crossings):
            z.add("crossing")
        if float(self.f.inland(cx, cy)) < WATERFRONT_M:
            z.add("waterfront")
        if float(self.f.slope(cx, cy)) > HILLSIDE_SLOPE:
            z.add("hillside")
        if self.green is not None and self.green.distance(b) < CORRIDOR_M:
            z.add("corridor")
        if any(math.dist(n.center, (cx, cy)) < n.radius + RING_M for n in self.c.nodes):
            z.add("node_ring")
        return z

    def weights(self, district: str | None, band: str, zones: set[str]) -> dict[str, float]:
        rule = self.districts.get(district)
        mix = dict(rule.mix.get(band, {})) if rule and rule.mix else {}
        if not mix:   # a district without a mix: every block-sited housing type, equally
            mix = {t.id: 1.0 for t in self.types.values() if t.family == "housing" and site_of(t) == "block"}
        for z in sorted(zones):
            ov = self.c.overlays.get(z)
            if ov:
                for k, v in ov.adds.items():
                    mix[k] = mix.get(k, 0) + v
        hill = self.c.overlays.get("hillside")
        rep = hill.replaces_housing_with if hill else None
        if "hillside" in zones and rep in self.types:
            moved = sum(v for k, v in mix.items() if k in self.types and self.types[k].family == "housing" and k != rep)
            mix = {k: v for k, v in mix.items() if not (k in self.types and self.types[k].family == "housing" and k != rep)}
            mix[rep] = mix.get(rep, 0) + moved
        out = {}
        for k, v in mix.items():
            t = self.types.get(k)
            if t is None or v <= 0 or site_of(t) != "block":
                continue
            if t.place and not (set(t.place) & zones):
                continue
            if t.max_count is not None and self.placed[k] >= t.max_count:
                continue
            if t.max_share is not None and district:   # by footprint, as the report measures
                n = sum(self.by_district[district].values())
                ft = sum(self.foot[district].values())
                if n > 20 and ft and self.foot[district][k] / ft >= t.max_share:
                    continue
            out[k] = v
        return out

    def _near(self, x, y) -> list[str]:
        cell = (int(x // NEIGHBOUR_R), int(y // NEIGHBOUR_R))
        cand = [it for dx in (-1, 0, 1) for dy in (-1, 0, 1) for it in self.grid[(cell[0] + dx, cell[1] + dy)]]
        cand.sort(key=lambda it: (it[0] - x) ** 2 + (it[1] - y) ** 2)
        return [it[2] for it in cand[:NEIGHBOURS] if math.dist(it[:2], (x, y)) < NEIGHBOUR_R]

    def _remember(self, x, y, k):
        self.grid[(int(x // NEIGHBOUR_R), int(y // NEIGHBOUR_R))].append((x, y, k))

    def _fits(self, t: Typology, L: float, S: float, r_in: float = math.inf) -> bool:
        """Roughly: does the type fit a block with this oriented box (L x S) and inscribed
        circle radius r_in? (Wedge-shaped blocks have a large box but a thin interior.)"""
        if self._is_fill(t):
            return S >= 2 * t.depth[0] * 0.6 or t.form in ("row", "bar", "stepped")
        return t.width[0] <= L - 6 and t.depth[0] <= S - 6 and min(t.width[0], t.depth[0]) <= 2 * r_in - 2

    # --- per building ------------------------------------------------------------------

    def _pick(self, pool: list[str], avoid: str | None = None) -> str:
        if not pool:
            return ""
        opts = [p for p in pool if p != avoid] or pool
        return opts[int(self.rng.integers(len(opts)))]

    def height_field(self, district, x: float, y: float) -> float:
        """Smooth noise in [-1, 1] that drifts across a district: its own seeded field per
        district, with highs and lows about height_noise_scale_m apart."""
        v = self.c.variation
        x0, y0, x1, y1 = self.c.bounds
        side = max(x1 - x0, y1 - y0)
        g = self._fields.get(district)
        if g is None:
            from .greybox import _value_noise
            seed = int(hashlib.sha256(f"{self.c.seed}:{district}".encode()).hexdigest()[:8], 16)
            cells = max(2, int(math.ceil(side / v.height_noise_scale_m)))
            g = self._fields[district] = _value_noise(max(65, cells * 8 + 1), cells, np.random.default_rng(seed))
        n = g.shape[0]
        i = int(np.clip((y1 - y) / side * (n - 1), 0, n - 1))
        j = int(np.clip((x - x0) / side * (n - 1), 0, n - 1))
        return float(g[i, j])

    def near_crossing(self, x: float, y: float) -> bool:
        if not len(self.cross_xy):
            return False
        return float(np.min(np.hypot(self.cross_xy[:, 0] - x, self.cross_xy[:, 1] - y))) < CROSSING_M

    def _storeys(self, t: Typology, district, avenue: bool, x: float | None = None, y: float | None = None) -> int:
        v = self.c.variation
        lo, hi = t.storeys
        s = int(self.rng.integers(lo, hi + 1))
        j = v.storeys_jitter
        if j:
            s += int(self.rng.integers(-j, j + 1))
        rule = self.districts.get(district)
        s += rule.height_bias if rule else 0
        if avenue:
            s += v.avenue_bonus
        if v.height_noise and x is not None and hi >= 2:
            s += int(round(v.height_noise * self.height_field(district, x, y)))
        floor = 1 if hi >= 1 else 0
        top = hi + max(2, j) + int(math.ceil(v.height_noise))
        return int(np.clip(s, max(floor, lo - 1 if lo > 1 else floor), top))

    def _columns(self, t: Typology, district, facade: str) -> bool:
        rule = self.districts.get(district)
        policy = rule.columns if rule else "accent"
        if t.columns == "order":
            return True
        if t.columns == "none" or policy == "none":
            return False
        if policy == "accent_on_civic_only" and t.family != "civic":
            return False
        p = 0.1 if policy == "rare" else 0.3
        return any(w in facade for w in COLUMN_FACADES) or float(self.rng.random()) < p

    def make(self, t: Typology, district, cx, cy, w, d, rot, block, avenue=False, run=None, last=None,
             storeys=None, bonus: int = 0) -> Building:
        """A building of type t (local -Y faces the street). `last`: the previous building on
        the same frontage (its variant is avoided once a run gets long)."""
        c = self.c
        st = storeys if storeys is not None else self._storeys(t, district, avenue, cx, cy) + bonus
        accent = False
        if (storeys is None and c.variation.accent_chance and t.family in ("housing", "mixed") and st >= 3
                and self.near_crossing(cx, cy) and self.rng.random() < c.variation.accent_chance):
            st += c.variation.accent_storeys      # a taller marker where avenues cross
            accent = True
        roof, facade = self._pick(t.roofs), self._pick(t.facades)
        ground = self._pick([g for g in t.ground if g in ("shops", "cafe", "market")] if avenue else t.ground) \
            or self._pick(t.ground)
        if last is not None and last.run == run and (last.storeys, last.roof, last.facade) == (st, roof, facade):
            # second identical in a row: change something visible
            if len(t.facades) > 1:
                facade = self._pick(t.facades, facade)
            elif len(t.roofs) > 1:
                roof = self._pick(t.roofs, roof)
            elif st <= max(1, t.storeys[0]):
                st += 1
            else:
                st += 1 if self.rng.random() < 0.5 else -1
        corners = [np.array([cx, cy]) + sx * w / 2 * C._dir(rot) + sy * d / 2 * C._dir(rot + 90)
                   for sx in (-1, 1) for sy in (-1, 1)]
        zs = self.f.z(np.array([p[0] for p in corners]), np.array([p[1] for p in corners]))
        z0, slack = float(zs.min()) - 0.3, float(zs.max() - zs.min()) + 0.3
        h = st * c.storey_m
        b = Building(t.id, t.family, district, cx, cy, rot, st, h + slack, w * d, roof, facade, ground,
                     self._columns(t, district, facade), block, run, accent=accent)
        P = b.parts
        loc = lambda lx, ly: (cx + lx * math.cos(math.radians(rot)) - ly * math.sin(math.radians(rot)),  # noqa: E731
                              cy + lx * math.sin(math.radians(rot)) + ly * math.cos(math.radians(rot)))
        top = z0 + slack + h
        if st >= 2 and "arcade" in facade:     # recessed ground floor behind an arcade of round arches
            gh = c.storey_m * 1.3
            x, y = loc(0, ARCADE_DEPTH / 2)
            P.append(Part("box", x, y, z0, w, d - ARCADE_DEPTH, slack + gh, rot, "ground"))
            P.append(Part("box", cx, cy, z0 + slack + gh, w, d, h - gh, rot))
            self.arcade(P, *loc(0, -d / 2 + 0.4), z0 + slack, w, round(gh, 1), rot)
        elif st >= 1:
            P.append(Part("box", cx, cy, z0, w, d, slack + h, rot))
        top_w, top_d, off = w, d, 0.0
        if st >= 4 and d > 9 and ("loggia" in facade or self.rng.random() < c.variation.step_back_top):
            # the top storey steps back from the street (and the court); tall ones twice
            steps = 2 if st >= 8 and d > 16 else 1
            P[-1].h -= c.storey_m * steps
            for k in range(steps):
                top_w, top_d, off = top_w - 2, top_d - 5 + (0 if k == 0 else -2), off + 1.0
                x, y = loc(0, off)
                z = top - c.storey_m * (steps - k)
                P.append(Part("box", x, y, z, top_w, top_d, c.storey_m, rot, "setback"))
                if k < steps - 1:       # the lower setback keeps its own cornice
                    self.cornice(P, roof, x, y, z + c.storey_m, top_w, top_d, rot)
            if "loggia" in facade:    # a slender pier row on the old facade line, in front of the loggia
                self.arcade(P, *loc(0, -d / 2 + 0.3), top - c.storey_m, w, round(c.storey_m, 1), rot,
                            open_frac=0.82, d=0.5)
        x, y = loc(0, off)
        self.roof(P, roof, x, y, top, top_w, top_d, rot)
        if st >= 2:
            self.cornice(P, roof, x, y, top, top_w, top_d, rot)
        return b

    def arcade(self, P: list, x, y, z, w, h, rot, open_frac=0.72, d=0.8):
        """An arcade along a facade w wide: whole bays, the shared mesh stretched to fit."""
        n = max(1, round(w / BAY_M))
        P.append(Part(arcade_prim(n, h, d, open_frac), x, y, z, 1, 1, 1, rot, "arcade",
                      scale=(round(w / (n * BAY_M), 3), 1.0, 1.0)))

    def cornice(self, P: list, roof: str, x, y, z, w, d, rot):
        """A crisp projecting cornice at a flat roofline (pitched, vaulted and domed roofs
        have their own edge)."""
        if roof in FLAT_ROOFS_TOO or roof not in (PITCHED | VAULTS | DOMES | CROWNS | {"vault_series", "sawtooth"}):
            P.append(Part("box", x, y, z - CORNICE_M, w + 1.2, d + 1.2, CORNICE_M, rot, "cornice"))

    def roof(self, P: list, roof: str, cx, cy, z, w, d, rot):
        lo, hi = min(w, d), max(w, d)
        along = rot if w >= d else rot + 90     # local axis of the long side
        if roof in GARDEN:
            P.append(Part("box", cx, cy, z, w * 0.72, d * 0.62, 1.0, rot, "roof-garden"))
        elif roof in PERGOLA:
            pw, pd = w * 0.5, d * 0.55
            P.append(Part("box", cx, cy, z + 2.8, pw, pd, 0.3, rot, "pergola"))
            for sx in (-1, 1):
                for sy in (-1, 1):
                    q = np.array([cx, cy]) + sx * (pw / 2 - 0.3) * C._dir(rot) + sy * (pd / 2 - 0.3) * C._dir(rot + 90)
                    P.append(Part("box", float(q[0]), float(q[1]), z, 0.4, 0.4, 2.8, rot, "pergola-post"))
        elif roof in PITCHED:
            P.append(Part("gable", cx, cy, z, lo, hi, float(np.clip(0.22 * lo, 1.5, 6.0)), along + 90, "roof"))
        elif roof in VAULTS:
            P.append(Part("vault", cx, cy, z, lo, hi, float(np.clip(0.35 * lo, 2.0, 14.0)), along + 90, "vault"))
        elif roof == "vault_series":
            n = max(2, round(hi / 12))
            for i in range(n):
                q = np.array([cx, cy]) + (-hi / 2 + hi / n * (i + 0.5)) * C._dir(along)
                P.append(Part("vault", float(q[0]), float(q[1]), z, hi / n, lo, float(np.clip(0.3 * hi / n, 1.5, 8)),
                              along, "vault"))
        elif roof == "sawtooth":
            n = max(2, round(hi / 8))
            for i in range(n):
                q = np.array([cx, cy]) + (-hi / 2 + hi / n * (i + 0.5)) * C._dir(along)
                P.append(Part("gable", float(q[0]), float(q[1]), z, hi / n, lo, 2.5, along, "sawtooth"))
        elif roof in DOMES:
            r = 0.42 * lo
            P.append(Part("dome", cx, cy, z, 2 * r, 2 * r, 0.45 * r, rot, "dome"))
        elif roof in CROWNS:
            P.append(Part("cyl", cx, cy, z, 0.7 * lo, 0.7 * lo, 0.25 * lo, rot, "crown"))
        elif roof == "skylight":
            P.append(Part("box", cx, cy, z, w * 0.3, d * 0.3, 1.5, rot, "skylight"))

    def add(self, b: Building) -> Building:
        self.out.append(b)
        self.by_district[b.district][b.typology] += 1
        self.foot[b.district][b.typology] += max(b.foot, 1.0)
        return b

    def balance(self, district, w: dict[str, float]) -> dict[str, float]:
        """Back off types that near their share cap (by footprint), in the district and
        city-wide, so the plan keeps to the thresholds the report checks."""
        ck = self.c.checks
        d = self.foot[district]
        dt = sum(d.values())
        ct = Counter()
        for cnt in self.foot.values():
            ct.update(cnt)
        ctot = sum(ct.values())
        out = {}
        for k, v in w.items():
            if dt > 20000 and d[k] / dt > 0.85 * ck.max_typology_share_district:
                v *= 0.15
            if ctot > 80000 and ct[k] / ctot > 0.85 * ck.max_typology_share_city:
                v *= 0.15
            out[k] = v
        return out

    # --- forms ---------------------------------------------------------------------------

    def fill(self, t: Typology, P: Polygon, district, bi: int, avoid=None) -> int:
        """Fill forms along the block's edges (or across it). Returns buildings made."""
        n0 = len(self.out)
        f = {"perimeter": self._perimeter, "row": self._row, "bar": self._bar, "stepped": self._stepped,
             "crescent": self._crescent, "courtyard": self._houses}[t.form]
        f(t, P, district, bi, avoid)
        return len(self.out) - n0

    def _ok(self, foot: Polygon, P: Polygon, avoid) -> bool:
        if not P.buffer(1.0).contains(foot):
            return False
        if avoid is not None and foot.intersects(avoid):
            return False
        return self.avoid.is_empty or not foot.intersects(self.avoid)

    def _perimeter(self, t, P, district, bi, avoid):
        dep = float(self.rng.uniform(*t.depth))
        _, _, _, S = _obb(P)
        dep = min(dep, S / 2 - 1)
        if dep < 7:
            return
        for poly in C._polys(P.buffer(-1.5)):
            for ei, (a, b, n) in enumerate(C._edges(poly, C.MIN_STRIP_M)):
                L_e = float(np.linalg.norm(b - a))
                k = max(1, round(L_e / float(self.rng.uniform(*self.c.parcel_m))))
                rot = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                last = None
                on_av = self._edge_on_avenue(a, b)
                for i in range(k):
                    m = a + (b - a) * (i + 0.5) / k
                    ctr = m + n * dep / 2
                    w = L_e / k - 0.4
                    if not self._ok(_rect(*ctr, w, dep, rot), P, avoid):
                        continue
                    cb = self.c.variation.corner_bonus      # corners mark the block: a little taller
                    corner = cb and k >= 2 and i in (0, k - 1)
                    last = self.add(self.make(t, district, float(ctr[0]), float(ctr[1]), w, dep, rot, bi,
                                              avenue=on_av, run=(bi, ei), last=last,
                                              bonus=int(self.rng.integers(1, cb + 1)) if corner else 0))

    def _edge_on_avenue(self, a, b) -> bool:
        if self.av_buf.is_empty:
            return False
        return LineString([a, b]).buffer(2).intersects(self.av_buf)

    def _row(self, t, P, district, bi, avoid):
        dep = float(self.rng.uniform(*t.depth))
        _, _, _, S = _obb(P)
        rows = [P.buffer(-2)] + ([P.buffer(-(2 * dep + 14))] if S > 4 * dep + 30 else [])
        for ri, poly0 in enumerate(rows):
            for poly in C._polys(poly0):
                for ei, (a, b, n) in enumerate(C._edges(poly, 12.0)):
                    L_e = float(np.linalg.norm(b - a))
                    rot = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                    s, last, in_row = 3.0, None, 0
                    on_av = self._edge_on_avenue(a, b)
                    group = int(self.rng.integers(5, 12))
                    while s < L_e - 3:
                        w = float(self.rng.uniform(*t.width))
                        if s + w > L_e - 3:
                            break
                        m = a + (b - a) * (s + w / 2) / L_e
                        ctr = m + n * (dep / 2 + 0.5)
                        if self._ok(_rect(*ctr, w - 0.2, dep, rot), P, avoid):
                            last = self.add(self.make(t, district, float(ctr[0]), float(ctr[1]), w - 0.2, dep, rot, bi,
                                                      avenue=on_av, run=(bi, ri * 100 + ei), last=last))
                        s += w
                        in_row += 1
                        if in_row >= group:          # a gap (a passage to the gardens) every few houses
                            s += float(self.rng.uniform(5, 12))
                            in_row, group = 0, int(self.rng.integers(5, 12))

    def _bar(self, t, P, district, bi, avoid):
        c, ang, L, S = _obb(P)
        dep = float(self.rng.uniform(*t.depth))
        length = min(float(self.rng.uniform(*t.width)), L - 12)
        if length < t.width[0] * 0.6:
            return
        st = self._storeys(t, district, False, *c)
        gap = max(2.0 * st * self.c.storey_m * 0.6, 22.0)
        n = max(1, int((S - 10 + gap) // (dep + gap)))
        off0 = -(n - 1) * (dep + gap) / 2
        last = None
        for i in range(n):
            q = c + (off0 + i * (dep + gap)) * C._dir(ang + 90) + float(self.rng.uniform(-6, 6)) * C._dir(ang)
            if self._ok(_rect(*q, length, dep, ang), P, avoid):
                last = self.add(self.make(t, district, float(q[0]), float(q[1]), length, dep, ang, bi,
                                          run=(bi, 0), last=last))

    def _stepped(self, t, P, district, bi, avoid):
        """Strips along the contour, each stepping down the slope: every step's roof is the
        planted terrace of the one above it."""
        cx, cy = P.centroid.coords[0]
        e = 8.0
        dz_x = float(self.f.z(cx + e, cy) - self.f.z(cx - e, cy))
        dz_y = float(self.f.z(cx, cy + e) - self.f.z(cx, cy - e))
        down = math.degrees(math.atan2(-dz_y, -dz_x)) if abs(dz_x) + abs(dz_y) > 1e-3 else _obb(P)[1] + 90
        along = down + 90
        c0, _, L, S = _obb(P)
        span = math.hypot(L, S)
        step_d = 8.0
        hi = max(t.storeys[1], 3)
        n_steps = max(2, min(hi, int(float(self.rng.uniform(*t.depth)) // step_d)))
        strip_d = n_steps * step_d
        last = None
        for j in np.arange(-span / 2 + strip_d / 2, span / 2, strip_d + 10):
            for i in np.arange(-span / 2, span / 2, 46):
                w = float(self.rng.uniform(*t.width)) if t.width[1] < 44 else float(self.rng.uniform(30, 44))
                ctr = c0 + (i + w / 2) * C._dir(along) + j * C._dir(down)
                if not self._ok(_rect(*ctr, w, strip_d, along), P, avoid):
                    continue
                top = self._storeys(t, district, False, float(ctr[0]), float(ctr[1]))
                b0 = None
                for k in range(n_steps):
                    q = ctr + (-strip_d / 2 + step_d * (k + 0.5)) * C._dir(down)
                    st = max(1, top - k)
                    bk = self.make(t, district, float(q[0]), float(q[1]), w, step_d, along + 180, bi,
                                   run=(bi, int(j)), last=last, storeys=st)
                    bk.roof = "roof_garden"
                    bk.parts = [p for p in bk.parts if p.role not in ("roof", "vault", "pergola", "pergola-post")]
                    if not any(p.role == "roof-garden" for p in bk.parts):
                        m = bk.parts[-1]
                        bk.parts.append(Part("box", m.x, m.y, m.z + m.h, w * 0.9, step_d * 0.5, 0.9, along, "roof-garden"))
                    b0 = b0 or bk
                    last = self.add(bk)

    def _crescent(self, t, P, district, bi, avoid):
        """An arc of segments, concave towards the sea or the nearest park."""
        cx, cy = P.centroid.coords[0]
        towards = math.degrees(math.atan2(self.f.u[1], self.f.u[0]))     # the sea
        if self.green is not None and self.green.distance(Point(cx, cy)) < 250:
            from shapely.ops import nearest_points
            g = nearest_points(self.green, Point(cx, cy))[0]
            towards = math.degrees(math.atan2(g.y - cy, g.x - cx))
        _, _, L, S = _obb(P)
        R = float(np.clip(0.55 * L, 50, 160))
        dep = float(self.rng.uniform(*t.depth))
        centre = np.array([cx, cy]) + (R - dep) * C._dir(towards) * 0.7
        length = min(float(self.rng.uniform(*t.width)), 2.2 * R)
        span = math.degrees(length / R)
        seg = 18.0
        k = max(3, int(length / seg))
        back = towards + 180
        st = self._storeys(t, district, False, cx, cy)
        last = None
        for i in range(k):
            a = back - span / 2 + span * (i + 0.5) / k
            q = centre + R * C._dir(a)
            rot = a + 90 + 180      # local -Y faces the centre of the arc
            w = R * math.radians(span / k) - 0.3
            if self._ok(_rect(*q, w, dep, rot), P, avoid):
                last = self.add(self.make(t, district, float(q[0]), float(q[1]), w, dep, rot, bi,
                                          run=(bi, 0), last=last, storeys=st))

    def _houses(self, t, P, district, bi, avoid):
        """Small courtyard houses on parcels along the block's edges (and a second row)."""
        _, _, _, S = _obb(P)
        rows = [P.buffer(-4)] + ([P.buffer(-(t.depth[1] + 14))] if S > 2.5 * t.depth[1] + 20 else [])
        for ri, poly0 in enumerate(rows):
            for poly in C._polys(poly0):
                for ei, (a, b, n) in enumerate(C._edges(poly, 16.0)):
                    L_e = float(np.linalg.norm(b - a))
                    rot = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                    s, last = 2.0, None
                    while True:
                        w = float(self.rng.uniform(*t.width))
                        d = float(self.rng.uniform(*t.depth))
                        if s + w > L_e - 2:
                            break
                        ctr = a + (b - a) * (s + w / 2) / L_e + n * (d / 2 + 1)
                        s += w + float(self.rng.uniform(3, 9))
                        if self.rng.random() < 0.15 or not self._ok(_rect(*ctr, w, d, rot), P, avoid):
                            continue
                        last = self.add(self._court_house(t, district, float(ctr[0]), float(ctr[1]), w, d, rot, bi,
                                                          (bi, ri * 100 + ei), last))

    def _court_house(self, t, district, cx, cy, w, d, rot, bi, run, last):
        b = self.make(t, district, cx, cy, w, d, rot, bi, run=run, last=last)
        return self._hollow(b, w, d, min(5.5, 0.28 * min(w, d)), court="pool")

    def _hollow(self, b: Building, w, d, wing, court=None) -> Building:
        """Replace a building's main mass by four wings around a court."""
        m = next(p for p in b.parts if p.role == "mass")
        z0 = min(p.z for p in b.parts if p.role in ("mass", "ground"))
        m = Part("box", m.x, m.y, z0, w, d, m.z + m.h - z0, m.rot)
        rest = [p for p in b.parts if p.role not in ("mass", "ground", "roof-garden", "roof", "vault", "dome", "crown",
                                                     "pergola", "pergola-post", "setback", "cornice", "arcade")]
        loc = lambda lx, ly: (m.x + lx * math.cos(math.radians(m.rot)) - ly * math.sin(math.radians(m.rot)),  # noqa: E731
                              m.y + lx * math.sin(math.radians(m.rot)) + ly * math.cos(math.radians(m.rot)))
        parts = []
        for lx, ly, ww, dd in ((0, -d / 2 + wing / 2, w, wing), (0, d / 2 - wing / 2, w, wing),
                               (-w / 2 + wing / 2, 0, wing, d - 2 * wing), (w / 2 - wing / 2, 0, wing, d - 2 * wing)):
            x, y = loc(lx, ly)
            parts.append(Part("box", x, y, m.z, ww, dd, m.h, m.rot))
            self.roof(parts, b.roof, x, y, m.z + m.h, ww, dd, m.rot)
            if b.storeys >= 2:
                self.cornice(parts, b.roof, x, y, m.z + m.h, ww, dd, m.rot)
        if court == "pool":
            parts.append(Part("box", m.x, m.y, m.z + 0.3, (w - 2 * wing) * 0.5, (d - 2 * wing) * 0.7, 0.35, m.rot, "pool"))
        b.parts = parts + rest
        b.foot = w * d - (w - 2 * wing) * (d - 2 * wing)
        return b

    def single(self, t: Typology, P: Polygon, district, bi: int, merged: int = 1) -> Polygon | None:
        """One building of a non-fill form, centred in the block (towers at the corner nearest
        a crossing). Returns its footprint (buffered: the clearance the fill keeps). On merged
        blocks it grows past the typology's range (up to 1 + 0.5 per extra block)."""
        c, ang, L, S = _obb(P)
        grow = 1 + 0.5 * (merged - 1)
        if merged > 1:
            w = float(np.clip(0.75 * L, t.width[0], min(L - 8, t.width[1] * grow)))
            d = float(np.clip(0.75 * S, t.depth[0], min(S - 8, t.depth[1] * grow)))
        else:
            w = float(np.clip(self.rng.uniform(*t.width), t.width[0], L - 8))
            d = float(np.clip(self.rng.uniform(*t.depth), t.depth[0], S - 8))
        if t.form in ("drum", "cube", "tower", "podium_tower"):
            w = d = min(w, d) if t.form != "drum" else min(w, S - 8)
            if t.form == "drum":
                d = min(float(self.rng.uniform(*t.depth)), S - 8)
        ctr = np.array(c)
        if t.form in ("tower", "podium_tower") and self.crossings:
            p = min(self.crossings, key=lambda q: q.distance(Point(*c)))
            towards = np.array([p.x, p.y]) - c
            if np.linalg.norm(towards) > 1:
                ctr = c + towards / np.linalg.norm(towards) * max(0.0, min(L, S) / 2 - w)
        fits = lambda c, w, d, a: (P.buffer(1.0).contains(_rect(*c, w, d, a))   # noqa: E731
                                   and (self.avoid.is_empty or not _rect(*c, w, d, a).intersects(self.avoid)))
        # wedge-shaped and merged blocks: try the most interior point too, both ways round,
        # and shrink (down to the typology's minimum) until it fits
        from shapely.ops import polylabel
        inner = polylabel(P, tolerance=1.0)
        centres = [ctr] + ([np.array([inner.x, inner.y])] if t.form not in ("tower", "podium_tower") else [])
        found = None
        for f in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
            w2, d2 = max(t.width[0], w * f), max(t.depth[0], d * f)
            for c0 in centres:
                for a in (ang, ang + 90):
                    if fits(c0, w2, d2, a):
                        found = (c0, w2, d2, a)
                        break
                if found:
                    break
            if found:
                break
        if found is None:
            return None
        ctr, w, d, ang = found
        foot = _rect(*ctr, w, d, ang)
        x, y = float(ctr[0]), float(ctr[1])
        b = self.make(t, district, x, y, w, d, ang, bi)
        b.merged = merged
        z0 = min(p.z for p in b.parts) if b.parts else float(self.f.z(x, y))
        top = z0 + b.h
        if t.form in ("courtyard", "u_shape", "l_shape"):
            wing = float(np.clip(0.22 * min(w, d), 9, 16))
            self._hollow(b, w, d, wing, court="pool" if t.family != "mixed" else None)
            if t.form != "courtyard":     # open one side (U) or two (L)
                drop = {"u_shape": 1, "l_shape": 2}[t.form]
                masses = [p for p in b.parts if p.role == "mass"]
                for p in masses[:drop]:
                    b.parts = [q for q in b.parts if not (abs(q.x - p.x) < 0.01 and abs(q.y - p.y) < 0.01)]
            elif t.family in ("civic", "mixed") and w <= d * 1.6 and self.rng.random() < 0.5:
                self._curve_back_wing(b, x, y, z0, w, d, wing, ang)
        elif t.form == "hall":
            self._hall(b, t, x, y, z0, w, d, ang)
        elif t.form == "cube":       # a cut light-well court; a stepped attic on the rim
            wing = float(np.clip(0.3 * min(w, d), 9, 14))
            self._hollow(b, w, d, wing)
        elif t.form == "drum":
            ring = t.family in ("civic",) and t.storeys[1] <= 3 and w > 80        # a stadium: ring + field
            b.parts = []
            if ring:
                r = min(w, d) / 2
                b.parts.append(Part({"kind": "arc", "r_in": round(r - 14, 2), "r_out": round(r, 2), "a0": 0, "a1": 360,
                                     "h": round(b.h, 2), "seg": 64}, x, y, z0, 1, 1, 1, ang, "ring"))
                b.foot = math.pi * (r ** 2 - (r - 14) ** 2)
            else:
                b.parts.append(Part("cyl", x, y, z0, w, d, b.h, ang))
                self.roof(b.parts, b.roof or "shallow_dome", x, y, top, w, d, ang)
                b.foot = math.pi * w * d / 4
        elif t.form == "cavea":
            down = self._downhill(x, y, ang)
            r = min(w, d * 2) / 2
            b.parts = []
            tiers = max(2, min(5, b.storeys))
            for k in range(tiers):
                ri = r * (0.35 + 0.65 * k / tiers)
                b.parts.append(Part({"kind": "arc", "r_in": round(ri, 2), "r_out": round(r, 2), "a0": 0, "a1": 180,
                                     "h": round(b.h * (k + 1) / tiers, 2), "seg": 24}, x, y, z0, 1, 1, 1, down + 180 - 90,
                                    "cavea"))
            q = np.array([x, y]) + 0.15 * r * C._dir(down)
            b.parts.append(Part("box", float(q[0]), float(q[1]), z0, r * 1.2, 5.0, b.h * 0.8, down + 90, "stage"))
            b.foot = math.pi * r * r / 2
        elif t.form == "podium_tower":
            pod = 3 * self.c.storey_m
            slack = b.h - b.storeys * self.c.storey_m
            b.parts = [Part("box", x, y, z0, min(w * 1.8, L - 8), min(d * 1.8, S - 8), pod + slack, ang, "podium"),
                       Part("box", x, y, z0 + pod, w, d, b.h - pod - self.c.storey_m * 2, ang)]
            crown_z = z0 + b.h - self.c.storey_m * 2
            b.parts.append(Part("box", x, y, crown_z, w - 4, d - 4, self.c.storey_m * 2, ang, "setback"))
            self.roof(b.parts, b.roof, x, y, z0 + b.h, w - 4, d - 4, ang)
            b.foot = b.parts[0].w * b.parts[0].d
        elif t.form == "tower" and t.family == "infrastructure":
            b.parts = [Part("cyl", x, y, z0, w, w, b.h, ang),
                       Part("cyl", x, y, z0 + b.h, w * 1.25, w * 1.25, 4.0, ang, "crown")]
            b.foot = math.pi * w * w / 4
        self.add(b)
        return foot.buffer(8)

    def _curve_back_wing(self, b: Building, x, y, z0, w, d, wing, ang):
        """Replace the courtyard's back wing (local +Y) with a curved one: an arc whose ends
        meet the side wings."""
        back = [p for p in b.parts if p.role in ("mass", "cornice", "roof-garden", "roof", "vault")
                and abs((p.x - x) * -math.sin(math.radians(ang)) + (p.y - y) * math.cos(math.radians(ang)) - (d / 2 - wing / 2)) < 0.5]
        if not back:
            return
        b.parts = [p for p in b.parts if p not in back]
        R = d / 2
        half = math.degrees(math.asin(min(1.0, (w / 2) / R)))
        a_mid = ang + 90
        b.parts.append(Part({"kind": "arc", "r_in": round(R - wing, 2), "r_out": round(R, 2), "a0": 0,
                             "a1": round(2 * half, 3), "h": round(b.h, 2), "seg": 16},
                            x, y, z0, 1, 1, 1, a_mid - half, "mass"))

    def _hall(self, b: Building, t: Typology, x, y, z0, w, d, ang):
        """A basilica section: lower aisles, a raised clerestory nave down the long axis with
        the roof on it, and for civic halls an apse at one end."""
        along = ang if w >= d else ang + 90
        L, S = max(w, d), min(w, d)
        loc = lambda la, lc: (x + la * math.cos(math.radians(along)) - lc * math.sin(math.radians(along)),  # noqa: E731
                              y + la * math.sin(math.radians(along)) + lc * math.cos(math.radians(along)))
        H = b.h
        aisle_h = max(self.c.storey_m * 1.5, 0.6 * H)
        keep = [p for p in b.parts if p.role in ("ground", "arcade")]
        b.parts = [Part("box", x, y, z0, w, d, aisle_h, ang, "mass"),
                   Part("box", x, y, z0 + aisle_h, L, S * 0.5, H - aisle_h, along, "nave")] + keep
        self.roof(b.parts, b.roof or "barrel_vault", x, y, z0 + H, L, S * 0.5, along)
        self.cornice(b.parts, "", x, y, z0 + aisle_h, w, d, ang)
        if t.family == "civic":
            ex, ey = loc(L / 2, 0)
            b.parts.append(Part({"kind": "arc", "r_in": 0.1, "r_out": round(S * 0.3, 2), "a0": 0, "a1": 180,
                                 "h": round(aisle_h, 2), "seg": 16}, ex, ey, z0, 1, 1, 1, along - 90, "apse"))

    def _downhill(self, x, y, fallback):
        e = 10.0
        dz_x = float(self.f.z(x + e, y) - self.f.z(x - e, y))
        dz_y = float(self.f.z(x, y + e) - self.f.z(x, y - e))
        return math.degrees(math.atan2(-dz_y, -dz_x)) if abs(dz_x) + abs(dz_y) > 0.05 else fallback

    # --- blocks --------------------------------------------------------------------------

    def block(self, P: Polygon, bi: int, district) -> None:
        cx, cy = P.centroid.coords[0]
        d = float(self.f.density(cx, cy))
        band = band_of(d)
        zones = self.zones(P, d)
        w = self.weights(district, band, zones)
        _, _, L, S = _obb(P)
        from shapely.ops import polylabel
        r_in = P.exterior.distance(polylabel(P, tolerance=2.0))
        w = {k: v for k, v in w.items() if self._fits(self.types[k], L, S, r_in)}
        if not w:
            return
        near = Counter(self._near(cx, cy))
        w = self.balance(district, w)
        keys = sorted(w)
        p = np.array([w[k] * REPEAT_DAMP ** near[k] for k in keys])
        k = keys[int(self.rng.choice(len(keys), p=p / p.sum()))]
        t = self.types[k]
        self.placed[k] += 1
        self._remember(cx, cy, k)
        self.done.add(bi)
        merged = 1
        if not self._is_fill(t) and t.form != "open" and t.merge_chance > 0:
            rule = self.districts.get(district)
            if self.rng.random() < t.merge_chance * (rule.merge_chance if rule else 1.0):
                P, merged = self.merge(P, bi, district)
        if self._is_fill(t):
            if not self.fill(t, P, district, bi) and t.form == "crescent":
                self._fill_rest(w, P, district, bi, None, exclude=k)
            return
        if t.form == "open":
            self.open_space(t, P, district, bi)
            return
        foot = self.single(t, P, district, bi, merged)
        self._fill_rest(w, P, district, bi, foot, exclude=k)

    def merge(self, P: Polygon, bi: int, district) -> tuple[Polygon, int]:
        """Join the block with 1-3 neighbours of the same district across their local
        streets (blocks not assigned yet); the streets between them are dropped."""
        if self.tree is None:
            return P, 1
        gap = self.c.street_w / 2 + 1.0
        want = int(self.rng.integers(1, 4))
        near = [int(j) for j in self.tree.query(P.buffer(self.c.street_w + 3))
                if int(j) != bi and int(j) not in self.done]
        near = [j for j in near if self.P.blocks[j].distance(P) <= self.c.street_w + 1     # across a local street
                and C.nearest_node(self.c, *self.P.blocks[j].centroid.coords[0]) == district]
        near.sort(key=lambda j: self.P.blocks[j].distance(P))
        group = near[:want]
        if not group:
            return P, 1
        joined = unary_union([q.buffer(gap, join_style="mitre") for q in [P] + [self.P.blocks[j] for j in group]]) \
            .buffer(-gap, join_style="mitre")
        if not isinstance(joined, Polygon) or joined.is_empty:
            return P, 1
        self.done.update(group)
        cut = joined.buffer(0.5)
        ways = []
        for w in self.P.ways:            # the streets between the merged blocks go
            if w.kind != "street" or not w.line.intersects(cut):
                ways.append(w)
                continue
            rest = w.line.difference(cut)
            for k, seg in enumerate(getattr(rest, "geoms", [rest])):
                if isinstance(seg, LineString) and seg.length > 15:
                    ways.append(C.Way(f"{w.id}m{k}", w.kind, seg, w.width, w.district))
        self.P.ways = ways
        return joined, 1 + len(group)

    def _fill_rest(self, w, P, district, bi, avoid, exclude):
        fills = {k: v for k, v in w.items() if k != exclude and self._is_fill(self.types[k])
                 and self.types[k].form != "stepped" and self.types[k].family in ("housing", "mixed")}
        if not fills:
            fills = {t.id: 1.0 for t in self.types.values() if t.form in ("perimeter", "row")
                     and t.family == "housing" and site_of(t) == "block"}
        if not fills:
            return
        fills = self.balance(district, fills)
        keys = sorted(fills)
        p = np.array([fills[k] for k in keys])
        k = keys[int(self.rng.choice(len(keys), p=p / p.sum()))]
        self.fill(self.types[k], P, district, bi, avoid)

    @staticmethod
    def _is_fill(t: Typology) -> bool:
        return t.form in FILL_FORMS or (t.form == "courtyard" and t.width[1] <= 30)

    def open_space(self, t, P, district, bi):
        """Gardens and orchards: the block becomes a park (trees come with parks); orchard
        terraces get low retaining walls across the slope, a sunken garden a long pool."""
        self.P.parks.append(P)
        c, ang, L, S = _obb(P)
        b = Building(t.id, t.family, district, float(c[0]), float(c[1]), ang, 0, 1.2, 0.0, block=bi)
        if "orchard" in t.id or "terrace" in t.id:
            down = self._downhill(float(c[0]), float(c[1]), ang + 90)
            for j in np.arange(-S / 2 + 8, S / 2 - 4, 14):
                q = c + j * C._dir(down)
                seg = LineString([q - L * C._dir(down + 90), q + L * C._dir(down + 90)]).intersection(P.buffer(-4))
                for s in getattr(seg, "geoms", [seg]):
                    if isinstance(s, LineString) and s.length > 12:
                        m = s.interpolate(0.5, normalized=True)
                        b.parts.append(Part("box", m.x, m.y, float(self.f.z(m.x, m.y)) - 0.5, s.length, 0.8, 1.7,
                                            down + 90, "terrace-wall"))
        else:
            z = float(self.f.z(*c))
            b.parts.append(Part("box", float(c[0]), float(c[1]), z, min(L * 0.6, 70), min(S * 0.18, 10), 0.4, ang, "pool"))
            for s in (-1, 1):
                q = c + s * (S * 0.3) * C._dir(ang + 90)
                b.parts.append(Part("box", float(q[0]), float(q[1]), z, min(L * 0.7, 80), 2.5, 1.2, ang, "terrace-wall"))
        if b.parts:
            self.add(b)

    # --- line features -------------------------------------------------------------------

    def features(self, green_lines, wanted: dict[str, float]) -> None:
        """Typologies placed on the city's lines rather than on blocks. `wanted`: weight of
        each in any district mix or overlay (0 = not in this city)."""
        for t in self.c.typologies:
            s = site_of(t)
            if s in ("block", "kit") or wanted.get(t.id, 0) <= 0:
                continue
            fn = {"corridor": self._corridor, "shore": self._shore, "pier_end": self._pier_end,
                  "node_gate": self._gates}[s]
            fn(t, green_lines)

    def _feature_avoid(self, parts: list[Part], pad=3.0):
        polys = [_rect(p.x, p.y, p.w + pad, p.d + pad, p.rot) for p in parts if isinstance(p.prim, str)]
        if polys:
            self.avoid = unary_union([self.avoid, *polys])

    def _corridor(self, t, green_lines):
        cap = t.max_count or 3
        aqueduct = t.family == "infrastructure"
        lines = list(green_lines) if aqueduct or "corridor" in t.place else []
        if not aqueduct and "waterfront" in t.place:
            lines += [w.line for w in self.P.ways if w.kind == "promenade"]
        cands = []
        for li, ln in enumerate(lines):
            for s in np.arange(0.15, 0.86, 0.1) * ln.length:
                p = ln.interpolate(s)
                if self.f.land(p.x, p.y) and float(self.f.density(p.x, p.y)) >= 0.3:
                    cands.append((li, float(s)))
        self.rng.shuffle(cands)
        done = []
        for li, s in cands:
            if self.placed[t.id] >= cap:
                break
            ln = lines[li]
            p, q = ln.interpolate(s), ln.interpolate(min(s + 5, ln.length))
            if any(math.dist((p.x, p.y), d) < 600 for d in done):
                continue
            tang = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
            length = float(self.rng.uniform(*t.width))
            if aqueduct:     # across the valley, level on top
                ok = self._aqueduct(t, np.array([p.x, p.y]), tang + 90, min(length, 420))
            else:            # along the line
                ok = self._pergola(t, ln, s, min(length, 260))
            if ok:
                done.append((p.x, p.y))
                self.placed[t.id] += 1

    def _aqueduct(self, t, mid, ang, length) -> bool:
        bay = 14.0
        n = int(length // bay)
        xs = [mid + (-length / 2 + bay * (i + 0.5)) * C._dir(ang) for i in range(n)]
        if sum(bool(self.f.land(*x)) for x in xs) < n:
            return False
        zs = [float(self.f.z(*x)) for x in xs]
        deck = max(zs[0], zs[-1]) + 2.0
        if deck - min(zs) < 8:          # no valley here: the arches would be stubs
            return False
        dep = float(self.rng.uniform(*t.depth))
        b = Building(t.id, t.family, C.nearest_node(self.c, *mid), float(mid[0]), float(mid[1]), ang,
                     int((deck - min(zs)) // self.c.storey_m), deck - min(zs), length * dep, roof="planted_walk",
                     facade="tiered_arches")
        for x, z in zip(xs, zs):
            h = round(max(deck - z, 4.0) / 2) * 2         # quantised: bays of one height share a mesh
            span = bay * 0.72
            spring = max(h - span / 2 - 1.8, 1.0)
            b.parts.append(Part({"kind": "archwall", "w": bay, "d": round(dep, 2), "h": float(h), "span": round(span, 2),
                                 "spring": round(spring, 2)}, float(x[0]), float(x[1]), deck - h, 1, 1, 1, ang, "arch"))
        b.parts.append(Part("box", float(mid[0]), float(mid[1]), deck, length, dep * 0.6, 1.0, ang, "roof-garden"))
        self._feature_avoid([Part("box", float(mid[0]), float(mid[1]), 0, length, dep, 1, ang)])
        self.add(b)
        return True

    def _pergola(self, t, ln, s, length) -> bool:
        seg = LineString([ln.interpolate(x) for x in np.linspace(s, min(s + length, ln.length), 12)])
        if seg.length < 40:
            return False
        dep = float(self.rng.uniform(*t.depth))
        m = seg.interpolate(0.5, normalized=True)
        b = Building(t.id, t.family, C.nearest_node(self.c, m.x, m.y), m.x, m.y, 0, 1, 3.4, seg.length * dep,
                     roof="pergola")
        for x in np.arange(2, seg.length - 2, 5.0):
            p, q = seg.interpolate(x), seg.interpolate(min(x + 1, seg.length))
            a = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
            z = float(self.f.z(p.x, p.y))
            for side in (-1, 1):
                o = np.array([p.x, p.y]) + side * dep / 2 * C._dir(a + 90)
                b.parts.append(Part("box", float(o[0]), float(o[1]), z, 0.5, 0.5, 3.2, a, "pergola-post"))
            b.parts.append(Part("box", p.x, p.y, z + 3.2, 5.0, dep + 0.6, 0.3, a, "pergola"))
        self._feature_avoid(b.parts)
        self.add(b)
        return True

    def _shore(self, t, _):
        cap = t.max_count or 3
        prom = next((w for w in self.P.ways if w.kind == "promenade"), None)
        if prom is None:
            return
        ln = prom.line
        sea = math.degrees(math.atan2(self.f.u[1], self.f.u[0]))
        spots = [n for n in self.c.nodes if float(self.f.inland(*n.center)) < 900]
        spots.sort(key=lambda n: -n.weight)
        for nd in spots[:cap]:
            s = ln.project(Point(nd.center))
            p = ln.interpolate(s)
            length = float(self.rng.uniform(*t.width))
            dep = float(self.rng.uniform(*t.depth))
            top = float(self.f.z(p.x, p.y))
            sea_z = self.P.layout.sea_level
            steps = 5
            mid = np.array([p.x, p.y]) + (self.c.promenade_w / 2 + 2) * C._dir(sea)
            b = Building(t.id, t.family, nd.id, float(mid[0]), float(mid[1]), sea + 90, 0, top - sea_z, length * dep)
            for k in range(steps):
                q = mid + (dep * (k + 0.5) / steps) * C._dir(sea)
                h = (top - sea_z) * (steps - k) / steps
                b.parts.append(Part("box", float(q[0]), float(q[1]), sea_z - 1.0, length, dep / steps + 0.05, h + 1.0,
                                    sea + 90, "step"))
            self.add(b)
            self.placed[t.id] += 1

    def _pier_end(self, t, _):
        if not self.P.piers or self.placed[t.id] >= (t.max_count or 1):
            return
        pier = self.P.piers[len(self.P.piers) // 2]
        end = np.array([pier.x, pier.y]) + (pier.w / 2 + 6) * C._dir(pier.rot)
        w = float(self.rng.uniform(*t.width))
        st = int(self.rng.integers(t.storeys[0], t.storeys[1] + 1))
        h = st * self.c.storey_m
        z = self.P.layout.sea_level - 2
        b = Building(t.id, t.family, pier.district, float(end[0]), float(end[1]), pier.rot, st, h, w * w)
        b.parts += [Part("box", b.x, b.y, z, w, w, h * 0.35 + 2, pier.rot, "base"),
                    Part({"kind": "cylinder", "r": round(w * 0.36, 2), "h": round(h * 0.4, 2), "seg": 8},
                         b.x, b.y, z + h * 0.35 + 2, 1, 1, 1, pier.rot + 22.5, "shaft"),
                    Part("cyl", b.x, b.y, z + h * 0.75 + 2, w * 0.5, w * 0.5, h * 0.25, pier.rot, "lantern")]
        self.add(b)
        self.placed[t.id] += 1

    def _gates(self, t, _):
        cap = t.max_count or 6
        for nd in sorted(self.c.nodes, key=lambda n: -n.weight * n.radius):
            for k in range(nd.radials):
                if self.placed[t.id] >= cap:
                    return
                if k % 2:        # every other avenue: gates mark the main approaches
                    continue
                a = nd.rot + 360 * k / nd.radials
                p = np.array(nd.center) + nd.radius * C._dir(a)
                if not self.f.land(*p):
                    continue
                w = float(self.rng.uniform(*t.width))
                d = float(self.rng.uniform(*t.depth))
                st = int(self.rng.integers(t.storeys[0], t.storeys[1] + 1))
                h = st * self.c.storey_m + 3
                z = float(self.f.z(*p)) - 0.3
                b = Building(t.id, t.family, nd.id, float(p[0]), float(p[1]), a + 90, st, h, w * d,
                             columns=t.columns != "none" and self.rng.random() < 0.3)
                span = min(self.c.avenue_w * 0.75, w * 0.6)
                b.parts.append(Part({"kind": "archwall", "w": round(w, 2), "d": round(d, 2), "h": round(h, 2),
                                     "span": round(span, 2), "spring": round(max(h - span / 2 - 3.5, 2.0), 2)},
                                    b.x, b.y, z, 1, 1, 1, a + 90, "arch"))
                self.add(b)
                self.placed[t.id] += 1


# --- the plan --------------------------------------------------------------------------------

def wanted(layout: SiteLayout) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for d in layout.districts:
        for mix in d.mix.values():
            for k, v in mix.items():
                out[k] += v
    for o in layout.city.overlays.values():
        for k, v in o.adds.items():
            out[k] += v
    return out


def mass(P, f, rng, green_lines, keep_block) -> list[Building]:
    """Typologies for every block of the plan that `keep_block(i, block)` keeps (False: the
    block stays empty). Line features first, so blocks avoid them. A block merged into a
    neighbour's large building is skipped."""
    T = Typer(P, f, rng, green_lines)
    T.tree = STRtree(P.blocks) if P.blocks else None
    T.features(green_lines, wanted(P.layout))
    for i, b in enumerate(P.blocks):
        if i in T.done:
            continue
        T.done.add(i)          # parks and empty blocks can't be merged into a neighbour either
        district = keep_block(i, b)
        if district is not False:
            T.block(b, i, district)
    assign_materials(T)
    return T.out


def assign_materials(T: Typer) -> None:
    """Pinned material, else one from the district's palette, per (typology, district, tile):
    the slot grouping, so a slot is one material."""
    c = T.c
    cache = {}
    for b in T.out:
        t = T.types[b.typology]
        if t.material:
            b.material = t.material
            continue
        key = (b.typology, b.district, int(math.floor(b.x / c.tile_m)), int(math.floor(b.y / c.tile_m)))
        if key not in cache:
            rule = T.districts.get(b.district)
            pal = {k: v for k, v in (rule.palette.items() if rule else []) if k in T.mats and v > 0}
            if pal:
                ks = sorted(pal)
                p = np.array([pal[k] for k in ks])
                cache[key] = ks[int(T.rng.choice(len(ks), p=p / p.sum()))]
            else:
                cache[key] = None
        b.material = cache[key]


# --- report -----------------------------------------------------------------------------------

def report(P, buildings: list[Building]) -> dict:
    """Typology shares (city, per district), types per urban tile, identical runs along a
    frontage, height spread per block, column share, district diversity; warnings against
    the catalog's thresholds."""
    c = P.layout.city
    ck = c.checks
    built = [b for b in buildings if b.storeys > 0 or b.parts]
    n = len(built) or 1
    # shares by footprint: a row of narrow townhouses is one frontage, not ten buildings
    area = lambda b: max(b.foot, 1.0)  # noqa: E731
    city, count = Counter(), Counter()
    by_d = defaultdict(Counter)
    for b in built:
        city[b.typology] += area(b)
        count[b.typology] += 1
        by_d[b.district][b.typology] += area(b)
    total = sum(city.values()) or 1.0
    warn = []
    for k, v in city.most_common():
        if v / total > ck.max_typology_share_city:
            warn.append(f"{k} covers {v / total:.0%} of the built footprint (max {ck.max_typology_share_city:.0%})")
    div = {}
    for d, cnt in sorted(by_d.items(), key=lambda kv: str(kv[0])):
        tot = sum(cnt.values())
        k, v = cnt.most_common(1)[0]
        many = sum(1 for b in built if b.district == d) >= 20
        if many and v / tot > ck.max_typology_share_district:
            warn.append(f"{k} covers {v / tot:.0%} of district {d} (max {ck.max_typology_share_district:.0%})")
        p = np.array(list(cnt.values()), float) / tot
        div[str(d)] = round(float(-(p * np.log(p)).sum()), 2)
        if many and div[str(d)] < ck.district_diversity_min:
            warn.append(f"district {d} diversity {div[str(d)]} < {ck.district_diversity_min} "
                        f"(about {math.exp(div[str(d)]):.1f} types' worth)")
    tiles, tile_n = defaultdict(set), Counter()
    for b in built:
        key = (math.floor(b.x / c.tile_m), math.floor(b.y / c.tile_m))
        tiles[key].add(b.typology)
        tile_n[key] += 1
    urban = {k: v for k, v in tiles.items() if tile_n[k] >= 25}
    thin = sorted(k for k, v in urban.items() if len(v) < ck.min_types_per_urban_tile)
    if urban and len(thin) / len(urban) > 0.2:
        warn.append(f"{len(thin)} of {len(urban)} urban tiles show fewer than {ck.min_types_per_urban_tile} typologies")
    runs, worst = 0, 0
    by_run = defaultdict(list)
    for b in built:
        if b.run is not None:
            by_run[b.run].append(b)
    for bs in by_run.values():
        cur, prev = 0, None
        for b in bs:
            sig = (b.typology, b.facade, b.roof, b.storeys)
            cur = cur + 1 if sig == prev else 1
            prev = sig
            worst = max(worst, cur)
            if cur == ck.max_identical_run + 1:
                runs += 1
    if runs:
        warn.append(f"{runs} frontages have more than {ck.max_identical_run} identical buildings in a row "
                    f"(longest {worst})")
    blocks = defaultdict(list)
    for b in built:
        if b.storeys > 0:
            blocks[b.block].append(b.h)
    cvs = [float(np.std(h) / np.mean(h)) for h in blocks.values() if len(h) >= 3 and np.mean(h) > 0]
    flat = sum(1 for v in cvs if v < ck.min_height_cv_block)
    if cvs and flat / len(cvs) > 0.25:
        warn.append(f"{flat} of {len(cvs)} blocks have a height spread under {ck.min_height_cv_block} (std / mean)")
    # walkable: every building within 20 m of a street, lane, stair or avenue (measured from
    # its footprint's edge, roughly: centre distance minus half its footprint's side)
    # Hill towns (organic nodes) must be walkable: every building within 20 m of a street,
    # lane or stair, measured from its footprint's edge (roughly: centre distance minus half
    # its side). City-wide the share is reported too; ordinary blocks have inner courts and
    # second rows reached through them.
    organic = [nd for nd in c.nodes if nd.layout == "organic"]       # within 1.6 x their radius
    lines = [w.line for w in P.ways]
    walk, walk_org = 1.0, None
    on = [b for b in built if b.storeys > 0]
    if lines and on:
        tree = STRtree(lines)
        xy = [Point(b.x, b.y) for b in on]
        idx = tree.query_nearest(xy, all_matches=False)[1]
        d = np.array([lines[j].distance(p) for p, j in zip(xy, idx)])
        half = np.array([math.sqrt(max(b.foot, 1.0)) / 2 for b in on])
        ok = d - half <= 20.0
        walk = float(ok.mean())
        sel = np.array([any(math.dist(nd.center, (b.x, b.y)) < 1.6 * nd.radius for nd in organic) for b in on])
        if sel.any():
            walk_org = float(ok[sel].mean())
            if walk_org < ck.min_walkable:
                warn.append(f"hill towns: {walk_org:.0%} of buildings are within 20 m of a street, lane or stair "
                            f"(want >= {ck.min_walkable:.0%})")
    col = sum(1 for b in built if b.columns) / n
    if col > ck.max_column_share:
        warn.append(f"columns on {col:.0%} of buildings (max {ck.max_column_share:.0%})")
    return {"typologies": dict(count.most_common()),
            "share": {k: round(v / total, 3) for k, v in city.most_common()},
            "by_district": {str(d): {k: round(v / sum(cnt.values()), 3) for k, v in cnt.most_common()}
                            for d, cnt in by_d.items()},
            "diversity": div, "urban_tiles": len(urban), "thin_tiles": len(thin),
            "identical_runs_over_max": runs, "longest_identical_run": worst,
            "flat_blocks": flat, "blocks_checked": len(cvs),
            "column_share": round(col, 3),
            "walkable_share": round(walk, 3),
            "walkable_hill_towns": round(walk_org, 3) if walk_org is not None else None,
            "merged_buildings": sum(1 for b in built if b.merged > 1),
            "accents": sum(1 for b in built if b.accent),
            "height_cv_median": round(float(np.median(cvs)), 3) if cvs else None,
            "roofs": dict(Counter(b.roof for b in built if b.roof).most_common()),
            "facades": dict(Counter(b.facade for b in built if b.facade).most_common()),
            "ground": dict(Counter(b.ground for b in built if b.ground).most_common()),
            "materials": dict(Counter(b.material for b in built if b.material).most_common()),
            "warnings": warn}
