"""Reusable parts for procedural buildings (building_proc), and the facade grammar that puts
them together from a brief's `features`.

Every part is a dict for scripts/blender_kit.py (box, prism, lathe, shell; "rz" / "at" place it).
Local frame of a facade: x along it (-L/2 .. L/2), its front face at y = 0 facing -Y, the wall's
thickness towards +y (inside), z up from the facade's foot.

Materials named here (building_proc.materials makes them): base / wall / top (storey bands, each
from its own patch of the sheet), trim (cornices, slabs, string courses), frame (window frames,
rails: a dark metal), glass, planting (planters, roof gardens), wood, roof, vault.

Walls with openings are cut into convex prisms (`pierced`): arched walls built any other way came
out solid (docs/notes/world-and-city.md, "Arched walls were solid until 2026-10-08").

Vegetation zones (`zone` parts: no geometry) mark where plants go: planter tops, garden strips,
roof-edge planters, courtyard and terrace beds. The build writes them next to the GLB
(kit_proc.run_blender: <glb>.zones.json); review renders draw proxies on them, and a PCG graph in
Unreal fills them later. The builder itself draws only the ground cover (a flat planting top).
"""
import copy
import math

# --- openings and pierced walls ----------------------------------------------------------
# an opening: rect (cx w z0 h) | arch (cx w z0 h, h to the crown, round head) | seg (cx w z0 h
# rise: a segmental head `rise` high) | round (cx cz r); "glass": False leaves it open.


def _seg_circle(o):
    hw, rise = o["w"] / 2, max(min(o.get("rise", o["w"] / 6), o["w"] / 2), 0.05)
    R = (hw * hw + rise * rise) / (2 * rise)
    return R, o["z0"] + o["h"] - R            # radius, centre z (the crown at z0 + h)


def _pointed(o):
    """A pointed (lancet) head: radius R = `point` x the width (1: equilateral), rise to the crown."""
    hw = o["w"] / 2
    R = max(o.get("point", 1.0) * o["w"], hw)
    return R, math.sqrt(max(R * R - (R - hw) ** 2, 0.0))


def _span(o: dict, x: float) -> tuple[float, float] | None:
    """The opening's z interval at x, or None outside it."""
    dx = x - o["cx"]
    if o["kind"] == "round":
        r = o["r"]
        if abs(dx) > r + 1e-9:
            return None
        d = math.sqrt(max(r * r - dx * dx, 0.0))
        return o["cz"] - d, o["cz"] + d
    hw = o["w"] / 2
    if abs(dx) > hw + 1e-9:
        return None
    if o["kind"] == "rect":
        return o["z0"], o["z0"] + o["h"]
    if o["kind"] == "seg":
        R, cz = _seg_circle(o)
        return o["z0"], cz + math.sqrt(max(R * R - dx * dx, 0.0))
    if o["kind"] == "pointed":                 # two arcs of radius R meeting at the crown (R = w: equilateral)
        R, rise = _pointed(o)
        spring = o["z0"] + o["h"] - rise
        c = o["cx"] + (R - hw if dx <= 0 else hw - R)           # each half's centre lies across the axis
        return o["z0"], spring + math.sqrt(max(R * R - (x - c) ** 2, 0.0))
    spring = o["z0"] + o["h"] - hw
    return o["z0"], spring + math.sqrt(max(hw * hw - dx * dx, 0.0))


def _edges(o: dict, seg: int = 12) -> list[float]:
    if o["kind"] == "rect":
        return [o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2]
    r = o["r"] if o["kind"] == "round" else o["w"] / 2
    if o["kind"] in ("seg", "pointed"):
        return [o["cx"] - r + 2 * r * i / seg for i in range(seg + 1)]
    return [o["cx"] - r * math.cos(math.pi * i / seg) for i in range(seg + 1)]


def outline(o: dict, seg: int = 12) -> list[list[float]]:
    """The opening's outline as a convex polygon in XZ."""
    if o["kind"] == "rect":
        a, b = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2
        return [[a, o["z0"]], [b, o["z0"]], [b, o["z0"] + o["h"]], [a, o["z0"] + o["h"]]]
    if o["kind"] == "round":
        return [[o["cx"] + o["r"] * math.cos(2 * math.pi * k / 24), o["cz"] + o["r"] * math.sin(2 * math.pi * k / 24)]
                for k in range(24)]
    xs = _edges(o, seg)[::-1]
    return [[o["cx"] - o["w"] / 2, o["z0"]], [o["cx"] + o["w"] / 2, o["z0"]]] + [[x, _span(o, x)[1]] for x in xs]


def pierced(x0, x1, z0, z1, y0, y1, openings, mat="wall", glass="glass", inset=None) -> list[dict]:
    """A wall from x0 to x1, z0 to z1, front face at y0, back at y1, with openings. Returns its
    convex prisms plus a glass pane `inset` behind the front face of each glazed opening.
    z1 may be a function of x (a wall under a vault's curve: the end screen of a hall)."""
    top = z1 if callable(z1) else (lambda x, z=z1: z)
    xs = {x0, x1, *[min(max(e, x0), x1) for o in openings for e in _edges(o)]}
    if callable(z1):
        xs |= {x0 + (x1 - x0) * i / 32 for i in range(33)}
    xs = sorted(xs)
    parts = []
    for xa, xb in zip(xs, xs[1:]):
        if xb - xa < 1e-6:
            continue
        xm = (xa + xb) / 2
        act = sorted((o for o in openings if _span(o, xm)), key=lambda o: _span(o, xm)[0])
        lo_a = lo_b = z0
        for o in act:
            sa = _span(o, xa) or _span(o, xm)
            sb = _span(o, xb) or _span(o, xm)
            if sa[0] - lo_a > 1e-4 or sb[0] - lo_b > 1e-4:
                parts.append(prism([[xa, lo_a], [xb, lo_b], [xb, sb[0]], [xa, sa[0]]], y0, y1, mat))
            lo_a, lo_b = sa[1], sb[1]
        ta, tb = top(xa), top(xb)
        if ta - lo_a > 1e-4 or tb - lo_b > 1e-4:
            parts.append(prism([[xa, lo_a], [xb, lo_b], [xb, max(tb, lo_b)], [xa, max(ta, lo_a)]], y0, y1, mat))
    t = y1 - y0
    gi = min(inset if inset is not None else 0.35 * t, 0.85 * t)
    for o in openings:
        if o.get("glass", True):
            parts.append(prism(outline(o), y0 + gi, y0 + gi + 0.04, glass))
    return parts


# --- primitives ---------------------------------------------------------------------------

def box(x0, x1, y0, y1, z0, z1, mat="wall"):
    return {"kind": "box", "x0": min(x0, x1), "x1": max(x0, x1), "y0": min(y0, y1), "y1": max(y0, y1),
            "z0": min(z0, z1), "z1": max(z0, z1), "mat": mat}


def prism(xz, y0, y1, mat="wall"):
    """A convex XZ polygon extruded from y0 to y1 (repeated points dropped: a slice whose top
    meets its bottom at one end is a triangle)."""
    pts = []
    for q in xz:
        if not pts or abs(q[0] - pts[-1][0]) + abs(q[1] - pts[-1][1]) > 1e-6:
            pts.append([float(q[0]), float(q[1])])
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) <= 1e-6:
        pts.pop()
    return {"kind": "prism", "xz": pts, "y0": y0, "y1": y1, "mat": mat}


def placed(parts: list[dict], rz: float = 0.0, at=(0.0, 0.0, 0.0)) -> list[dict]:
    """Place parts (rotate about Z, then move). Parts already placed are composed."""
    c, s = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    for p in parts:
        r0, a0 = p.get("rz", 0.0), p.get("at", [0.0, 0.0, 0.0])
        p["rz"] = (r0 + rz) % 360
        p["at"] = [c * a0[0] - s * a0[1] + at[0], s * a0[0] + c * a0[1] + at[1], a0[2] + at[2]]
    return parts


def lifted(parts: list[dict], dz: float) -> list[dict]:
    return placed(parts, 0.0, (0.0, 0.0, dz))


def ring(w, d, t, z0, z1, mat="trim") -> list[dict]:
    """A rectangular band t wide around a w x d outline (leaves the middle open)."""
    return [box(-w / 2, w / 2, -d / 2, -d / 2 + t, z0, z1, mat), box(-w / 2, w / 2, d / 2 - t, d / 2, z0, z1, mat),
            box(-w / 2, -w / 2 + t, -d / 2 + t, d / 2 - t, z0, z1, mat), box(w / 2 - t, w / 2, -d / 2 + t, d / 2 - t, z0, z1, mat)]


def column(x, y, z, h, d, kind="round", mat="trim") -> list[dict]:
    """A plain column: round (base, slight taper, capital) or square (pilotis)."""
    if kind == "square":
        return [box(x - d / 2, x + d / 2, y - d / 2, y + d / 2, z, z + h, mat)]
    r = d / 2
    prof = [(r * 1.25, 0.0), (r * 1.25, 0.12 * d), (r, 0.25 * d), (0.9 * r, h - 0.4 * d), (r * 1.2, h - 0.15 * d),
            (r * 1.2, h)]
    return [{"kind": "lathe", "x": x, "y": y, "z": z, "profile": [list(q) for q in prof], "seg": 16, "mat": mat}]


def zone(x0, x1, y0, y1, z0, z1, veg: str) -> dict:
    """A vegetation zone of type `veg` over a box (no geometry): placed like any part."""
    b = box(x0, x1, y0, y1, z0, z1)
    return b | {"kind": "zone", "veg": veg, "mat": None}


def zone_records(zones: list[dict]) -> list[dict]:
    """Placed zone parts as records in the building's frame: type, centre (x, y, the top z), size
    (along its own x and y, and its depth below the top), yaw (degrees about Z)."""
    out = []
    for p in zones:
        rz = p.get("rz", 0.0)
        at = p.get("at", [0.0, 0.0, 0.0])
        cx, cy = (p["x0"] + p["x1"]) / 2, (p["y0"] + p["y1"]) / 2
        c, s_ = math.cos(math.radians(rz)), math.sin(math.radians(rz))
        out.append({"type": p["veg"], "center": [round(c * cx - s_ * cy + at[0], 3), round(s_ * cx + c * cy + at[1], 3),
                                                 round(p["z1"] + at[2], 3)],
                    "size": [round(p["x1"] - p["x0"], 3), round(p["y1"] - p["y0"], 3), round(p["z1"] - p["z0"], 3)],
                    "yaw": round(rz % 360, 2)})
    return out


def hip_roof(w: float, d: float, z: float, rise: float, over: float = 0.0, t: float = 0.25, mat: str = "roof") -> list[dict]:
    """A hipped roof over a w x d footprint centred at the origin: eaves `over` m past the walls,
    a slab edge t thick, faces rising `rise` m to a ridge along the longer side (a pyramid on a square)."""
    W, D = w / 2 + over, d / 2 + over
    r = min(W, D)
    if W >= D:
        ridge = [[-(W - r), 0.0, z + t + rise], [W - r, 0.0, z + t + rise]]
    else:
        ridge = [[0.0, -(D - r), z + t + rise], [0.0, D - r, z + t + rise]]
    eave = [[sx * W, sy * D, zz] for sx in (-1, 1) for sy in (-1, 1) for zz in (z, z + t)]
    return [{"kind": "hull", "pts": eave + ridge, "mat": mat}]


# planter styles (a brief's `planter`, or a part's own): box (a raised box), trough (low and
# slim), pots (single pots at intervals), hedge (a clipped hedge, no box), bed (flush with the
# ground), none
PLANTER_STYLES = ("box", "trough", "pots", "hedge", "bed", "none")
PLANTER = {"style": "box"}               # the build's default style (building_proc sets it per brief)


def planter(x0, x1, y0, y1, z, h=0.55, green=0.35, seed=0, style=None, veg="planter") -> list[dict]:
    """A planter run in a style (PLANTER_STYLES) with its ground cover, and its vegetation zone
    (the plants come from the zone: proxies in review renders, PCG in Unreal)."""
    st = style or PLANTER["style"]
    if st == "none" or x1 - x0 < 0.3:
        return []
    if st == "hedge":
        hh = h + green
        return [box(x0, x1, y0, y1, z, z + hh, "planting"), zone(x0, x1, y0, y1, z + hh, z + hh, veg)]
    if st == "bed":
        return [box(x0, x1, y0, y1, z, z + 0.06, "planting"), zone(x0, x1, y0, y1, z + 0.06, z + 0.06, veg)]
    if st == "pots":
        n = max(1, int((x1 - x0) / 1.6))
        out = []
        for i in range(n):
            cx = x0 + (x1 - x0) * (i + 0.5) / n
            r = min(0.3, (y1 - y0) / 2)
            cy = (y0 + y1) / 2
            out += [box(cx - r, cx + r, cy - r, cy + r, z, z + 0.6 * h, "trim"),
                    box(cx - r + 0.04, cx + r - 0.04, cy - r + 0.04, cy + r - 0.04, z + 0.6 * h, z + 0.6 * h + 0.03, "planting")]
        return out + [zone(x0, x1, y0, y1, z + 0.6 * h, z + 0.6 * h, veg)]
    if st == "trough":
        h, y1 = 0.6 * h, y0 + max(0.3, 0.6 * (y1 - y0))
    return [box(x0, x1, y0, y1, z, z + h, "trim"),
            box(x0 + 0.05, x1 - 0.05, y0 + 0.05, y1 - 0.05, z + h, z + h + 0.03, "planting"),
            zone(x0 + 0.05, x1 - 0.05, y0 + 0.05, y1 - 0.05, z + h, z + h, veg)]


def drape(x0, x1, y, z, seed=0) -> list[dict]:
    """Planting hanging over a slab edge (hanging gardens): strands of uneven length."""
    out = []
    n = max(1, int((x1 - x0) / 0.7))
    for i in range(n):
        a, b = x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n
        L = 0.5 + 1.6 * (((i * 5 + seed * 11) % 7) / 6)
        out.append(box(a + 0.04, b - 0.04, y - 0.25, y, z - L, z, "planting"))
    return out + [zone(x0, x1, y - 0.25, y, z, z, "hanging")]


def balustrade(x0, x1, y, z, kind="glass", h=1.05) -> list[dict]:
    if kind == "solid":
        return [box(x0, x1, y, y + 0.18, z, z + h, "wall")]
    if kind == "rail":
        return [box(x0, x1, y, y + 0.06, z + h - 0.06, z + h, "frame")] + \
               [box(x, x + 0.04, y, y + 0.04, z, z + h, "frame") for x in _steps(x0, x1, 0.12)]
    # glass: clear panels about 1.5 m wide between slim posts, on a base shoe, under a handrail
    n = max(1, round((x1 - x0) / 1.5))
    pw = (x1 - x0) / n
    out = [box(x0, x1, y, y + 0.08, z, z + 0.1, "frame"),                               # base shoe
           box(x0, x1, y - 0.01, y + 0.08, z + h - 0.05, z + h, "frame")]                # handrail
    out += [box(x0 + i * pw + 0.015, x0 + (i + 1) * pw - 0.015, y + 0.03, y + 0.05, z + 0.1, z + h - 0.05, "rail_glass")
            for i in range(n)]
    out += [box(max(x0, x - 0.02), min(x1, x + 0.02), y + 0.015, y + 0.065, z + 0.1, z + h - 0.05, "frame")
            for x in (x0 + i * pw for i in range(n + 1))]                                 # posts at the joints
    return out


def _steps(a, b, step):
    n = max(1, int((b - a) / step))
    return [a + (b - a) * i / n for i in range(n + 1)]


def cornice(L, z, steps, y=0.0, mat="trim") -> list[dict]:
    """A stepped cornice along a facade: steps [[height, overhang], ...] from the bottom."""
    out = []
    for h, over in steps:
        out.append(box(-L / 2 - over, L / 2 + over, y - over, y + 0.6, z, z + h, mat))
        z += h
    return out


def archivolt(o: dict, width: float, y0: float, proj: float, mat="trim", seg=12) -> list[dict]:
    """A band around an arched (round or segmental) opening's head, standing proud of the wall."""
    pts = outline(o, seg)[2:]
    out = []
    cx = o["cx"]
    if o["kind"] == "seg":
        R, cz = _seg_circle(o)
    else:
        R, cz = o["w"] / 2, o["z0"] + o["h"] - o["w"] / 2
    for (xa, za), (xb, zb) in zip(pts, pts[1:]):
        def out_pt(x, z):
            vx, vz = x - cx, z - cz
            n = math.hypot(vx, vz) or 1.0
            return [x + vx / n * width, z + vz / n * width]
        q = [[xa, za], out_pt(xa, za), out_pt(xb, zb), [xb, zb]]
        out.append(prism(q[::-1] if (q[1][0] - q[0][0]) * (q[2][1] - q[1][1]) - (q[1][1] - q[0][1]) * (q[2][0] - q[1][0]) < 0 else q,
                         y0 - proj, y0, mat))
    return out


def frame(o: dict, y: float, t: float = 0.07, mullions: int = 0, mat: str = "frame") -> list[dict]:
    """A window frame (and mullions) inside a rect opening, at depth y (mat "wood": timber)."""
    if o["kind"] != "rect":
        return []
    a, b, z0, z1 = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2, o["z0"], o["z0"] + o["h"]
    out = [box(a, b, y, y + 0.06, z0, z0 + t, mat), box(a, b, y, y + 0.06, z1 - t, z1, mat),
           box(a, a + t, y, y + 0.06, z0 + t, z1 - t, mat), box(b - t, b, y, y + 0.06, z0 + t, z1 - t, mat)]
    for k in range(1, mullions + 1):
        x = a + (b - a) * k / (mullions + 1)
        out.append(box(x - t / 2, x + t / 2, y, y + 0.06, z0 + t, z1 - t, mat))
    return out


# --- the facade grammar ---------------------------------------------------------------------

FACADE = {
    "storeys": 4, "storey_h": 3.4, "ground_h": None, "bay_m": 4.5, "thickness": 0.5,
    "pattern": ["window"],              # bay kinds, cycled across each segment: window | slot | loggia | blind | curtain | solid
    # window kind: rect | arch | seg | pointed ("point": the head's radius over the width, 1 = equilateral);
    # frame_mat: "frame" (dark metal) or "wood" (timber, also on plain ground windows)
    "window": {"kind": "rect", "w": 0.45, "h": 0.55, "sill": 0.28, "reveal": 0.25, "frame": True, "mullions": 0,
               "rise": None, "archivolt": 0.0, "frame_mat": "frame", "frame_t": 0.07},
    "slot": {"w": 0.16, "h": 0.72, "sill": 0.12},
    # + "jamb" (m), "doors": "sliding", "frame" (back window framed like the windows), "lining" (the
    # jambs' material, e.g. "wood"); parapet_h: the balustrade's (or solid parapet's) height, m
    "loggia": {"w": 0.72, "h": 0.78, "sill": 0.0, "depth": 1.8, "balustrade": "glass", "parapet_h": 1.05},
    "blind": {"kind": "rect", "w": 0.6, "h": 0.72, "sill": 0.14, "depth": 0.14},
    # curtain-wall bays: glazing over the storey's height between the slabs, mullions every
    # mullion_m, a transom at that share of the height (0: none)
    "curtain": {"w": 0.96, "mullion_m": 1.5, "transom": 0.0, "frame_mat": "frame"},
    # plain | shop | sparse | arcade | pilotis | recessed | blind_arcade (pilotis / recessed: "recess" m,
    # "door_w" / "door_h" of the back wall's doors as shares of a bay). On wall-based kinds (plain, shop,
    # sparse) a layout replaces the kind's own doors: door_count doors door_width_m wide, evenly
    # spaced; an entrance entrance_m wide at entrance_x (share of the length from the centre, -0.5..0.5);
    # door_leaf: wood | glass. canopy: a slab that deep over the ground storey (any kind).
    "ground": {"kind": "plain", "door_count": 0, "door_width_m": 1.2, "door_height_m": 2.6, "door_leaf": "wood",
               "entrance_m": 0.0, "entrance_x": 0.0, "entrance_height_m": 3.2, "canopy": 0.0},
    "balcony": None,                    # {"kind": slab|band, "depth", "balustrade", "planters", "drape", "every", "on", "from"}
    "pilasters": None,                  # {"every": bays, "w", "proj", "from_ground", "profile": pier | strip | fin}
    "courses": None,                    # {"every": storeys, "h", "proj"}
    "panels": None,                     # panel joints: {"panel_w", "panel_h" (m; 0: the storey), "groove" (m)}
    "cornice": [[0.45, 0.3]],
    "parapet": 0.9,
    "recess": [],                       # [{"x0": frac, "x1": frac, "depth": m, "pattern": [...]}]: set-back segments
    # opening proportions per upper storey (first upper storey first): width and height factors
    "open_w": [], "open_h": [],
    # a fixed number of openings per facade (0: one per bay), spacing m between centres (0: spread evenly)
    "count": 0, "spacing": 0.0,
    "mat": {"ground": "base", "upper": "wall"},
}
# keys a generator sets per facade (storeys, heights...): never filled in from the grammar's
# defaults when a brief form shows a facade (form_features)
FACADE_KW = ("storeys", "storey_h", "ground_h", "bay_m", "thickness", "parapet", "cornice", "mat")


def merged(base: dict, over: dict | None) -> dict:
    """A deep copy of base with over's keys on top (dicts merged one level down)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = ({**out[k], **v} if isinstance(out.get(k), dict) and isinstance(v, dict) else copy.deepcopy(v))
    return out


def levels(F: dict) -> list[tuple[float, float]]:
    """(z0, height) of each storey."""
    g = F["ground_h"] or F["storey_h"]
    return [(0.0, g)] + [(g + i * F["storey_h"], F["storey_h"]) for i in range(F["storeys"] - 1)]


def height(F: dict) -> float:
    z, h = levels(F)[-1]
    return z + h


def _segments(L: float, F: dict) -> list[dict]:
    """The facade's segments along x: the recesses and the plain runs between them."""
    cuts = sorted(F["recess"], key=lambda r: r["x0"])
    out, x = [], -L / 2
    for r in cuts:
        a, b = -L / 2 + r["x0"] * L, -L / 2 + r["x1"] * L
        if a - x > 0.3:
            out.append({"x0": x, "x1": a, "depth": 0.0, "pattern": F["pattern"]})
        out.append({"x0": a, "x1": b, "depth": r["depth"], "pattern": r.get("pattern", F["pattern"]),
                    "balcony": r.get("balcony", "inherit")})
        x = b
    if L / 2 - x > 0.3:
        out.append({"x0": x, "x1": L / 2, "depth": 0.0, "pattern": F["pattern"]})
    return out


def bays(x0: float, x1: float, F: dict, pat: list[str]) -> list[tuple[float, float, str]]:
    """The bays of a run from x0 to x1 as (centre, width, kind). Uniform bays of about bay_m
    with the pattern cycled; with `pattern_w` (relative widths per pattern entry) whole cycles of
    the pattern, each bay as wide as its weight (a wide loggia bay, a narrow slot bay)."""
    L = x1 - x0
    wts = F.get("pattern_w")
    if not wts or len(wts) != len(pat):
        nb = max(1, round(L / F["bay_m"]))
        bw = L / nb
        return [(x0 + bw * (b + 0.5), bw, pat[b % len(pat)]) for b in range(nb)]
    cyc = max(1, round(L / (F["bay_m"] * len(pat))))
    unit = L / (cyc * sum(wts))
    out, x = [], x0
    for _ in range(cyc):
        for k, wt in zip(pat, wts):
            out.append((x + wt * unit / 2, wt * unit, k))
            x += wt * unit
    return out


def count_bays(L: float, F: dict, pat: list[str]) -> list[tuple[float, float, str]] | None:
    """With F["count"]: that many openings centred on the facade, `spacing` m apart (or spread
    evenly), their kinds cycling through the pattern's openings (its "solid" entries are skipped:
    the count is what you see); None otherwise."""
    n = int(F.get("count") or 0)
    if n <= 0:
        return None
    sp = F.get("spacing") or L / n
    sp = min(sp, L / n)
    kinds = [k for k in pat if k != "solid"] or ["window"]
    return [((i - (n - 1) / 2) * sp, sp, kinds[i % len(kinds)]) for i in range(n)]


def _bay_openings(kind, cx, bw, z, sh, F):
    """Openings (and extra parts) of one bay of one upper storey."""
    extra = []
    if kind == "solid":
        return [], extra
    if kind == "slot":
        s = F["slot"]
        return [{"kind": "rect", "cx": cx, "w": max(0.45, s["w"] * bw), "z0": z + s["sill"] * sh, "h": s["h"] * sh}], extra
    if kind == "loggia":
        s = F["loggia"]
        return [{"kind": "rect", "cx": cx, "w": s["w"] * bw, "z0": z + s["sill"] * sh + 0.15, "h": s["h"] * sh,
                 "glass": False, "loggia": True}], extra
    if kind == "curtain":
        c = F["curtain"]
        return [{"kind": "rect", "cx": cx, "w": c["w"] * bw, "z0": z + 0.05, "h": sh - 0.45, "curtain": True}], extra
    if kind == "blind":
        s = F["blind"]
        o = {"kind": s.get("kind", "rect"), "cx": cx, "w": s["w"] * bw, "z0": z + s["sill"] * sh, "h": s["h"] * sh,
             "glass": False, "blind": True}
        return [o], extra
    w = F["window"]
    o = {"kind": w["kind"], "cx": cx, "w": w["w"] * bw, "z0": z + w["sill"] * sh, "h": w["h"] * sh}
    if w["kind"] == "seg":
        o["rise"] = w.get("rise") or o["w"] / 5
    if w["kind"] == "arch":
        o["h"] = max(o["h"], o["w"] / 2 + 0.3)
    if w["kind"] == "pointed":
        o["point"] = w.get("point", 1.0)
        o["h"] = max(o["h"], _pointed(o)[1] + 0.3)
    return [o], extra


def _ground_openings(g, x0, x1, nb, h, F, seg_i):
    bw = (x1 - x0) / nb
    out = []
    for b in range(nb):
        cx = x0 + bw * (b + 0.5)
        k = g["kind"]
        if k in ("arcade", "blind_arcade"):
            prof = g.get("arch", "arch")
            o = {"kind": prof, "cx": cx, "w": g.get("w", 0.66) * bw, "z0": 0.0, "h": g.get("h", 0.88) * h,
                 "glass": False, "blind": k == "blind_arcade"}
            if prof == "seg":
                o["rise"] = o["w"] / 4
            if prof == "pointed":
                o["point"] = g.get("point", 1.0)
            out.append(o)
        elif k == "shop":
            out.append({"kind": "rect", "cx": cx, "w": 0.72 * bw, "z0": 0.25, "h": 0.74 * h})
        elif k == "sparse":                           # mostly solid, a few big dark openings
            if b % g.get("every", 4) == g.get("every", 4) // 2:
                out.append({"kind": "rect", "cx": cx, "w": g.get("w", 0.85) * bw, "z0": 0.0, "h": g.get("h", 0.8) * h})
        elif k == "plain":
            door = (b + seg_i) % 3 == 1
            out.append({"kind": "rect", "cx": cx, "w": (0.34 if door else 0.45) * bw, "z0": 0.0 if door else 0.3 * h,
                        "h": (0.78 if door else 0.5) * h})
    return out


def _ground_layout(L: float, gh: float, g: dict) -> list[dict]:
    """The ground storey's doors and entrance set in the brief (door_count, entrance_m): see FACADE."""
    out = []
    ew = g.get("entrance_m") or 0.0
    if ew > 0:
        out.append({"kind": "rect", "cx": max(-0.5, min(0.5, g.get("entrance_x", 0.0))) * L, "w": min(ew, L - 1.0),
                    "z0": 0.0, "h": min(g.get("entrance_height_m", 3.2), gh - 0.3), "entrance": True})
    n = int(g.get("door_count") or 0)
    for i in range(n):
        cx = -L / 2 + L * (i + 0.5) / n
        dw = g.get("door_width_m", 1.2)
        if any(abs(cx - o["cx"]) < (o["w"] + dw) / 2 + 0.3 for o in out):
            continue                                   # no door inside the entrance
        out.append({"kind": "rect", "cx": cx, "w": dw, "z0": 0.0, "h": min(g.get("door_height_m", 2.6), gh - 0.3),
                    "glass": g.get("door_leaf", "wood") == "glass", "door": True})
    return out


def _minus(a: float, b: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The interval a..b minus the cut intervals."""
    out = [(a, b)]
    for c0, c1 in cuts:
        nxt = []
        for x0, x1 in out:
            if c1 <= x0 or c0 >= x1:
                nxt.append((x0, x1))
                continue
            if c0 > x0:
                nxt.append((x0, c0))
            if c1 < x1:
                nxt.append((c1, x1))
        out = nxt
    return [(x0, x1) for x0, x1 in out if x1 - x0 > 0.05]


def _joints(x0: float, x1: float, dy: float, L: float, bands: list[tuple[float, float, list]], jt: dict) -> list[dict]:
    """Panel joints over a wall run: shallow grooves on a grid (panel_w along the facade from its
    left end, panel_h up each storey band), stopping at the openings."""
    g, pw = jt.get("groove", 0.03), max(0.5, jt.get("panel_w", 3.0))
    out = []
    for z, sh, ops in bands:
        ph = jt.get("panel_h") or sh
        rows = max(1, round(sh / ph))
        zs = [z + sh * k / rows for k in range(rows + 1)]
        for zz in zs[1:-1] + ([z] if z > 0 else []):      # horizontal joints (not at the foot of the wall)
            cuts = [(o["cx"] - o["w"] / 2 - 0.05, o["cx"] + o["w"] / 2 + 0.05) for o in ops
                    if _span(o, o["cx"]) and _span(o, o["cx"])[0] - 0.05 <= zz <= _span(o, o["cx"])[1] + 0.05]
            for a, b in _minus(x0, x1, cuts):
                out.append(box(a, b, dy - 0.006, dy, zz - g / 2, zz + g / 2, "joint"))
        k0 = math.ceil((x0 + L / 2) / pw - 1e-6)
        for k in range(k0, int((x1 + L / 2) / pw) + 1):
            x = -L / 2 + k * pw
            if not x0 + 0.1 < x < x1 - 0.1:
                continue
            for za, zb in zip(zs, zs[1:]):
                cuts = []
                for o in ops:
                    sp = _span(o, x) or (_span(o, o["cx"]) if abs(x - o["cx"]) <= o["w"] / 2 + 0.05 else None)
                    if sp:
                        cuts.append((sp[0] - 0.05, sp[1] + 0.05))
                for a, b in _minus(za + g / 2, zb - g / 2, cuts):
                    out.append(box(x - g / 2, x + g / 2, dy - 0.006, dy, a, b, "joint"))
    return out


def facade(L: float, F: dict) -> list[dict]:
    """One facade from the grammar: storey bands in their materials, the bay pattern per segment
    (windows with reveals and frames, slots, loggias, blind panels), the ground storey kind,
    balconies with planters, pilasters, string courses, a stepped cornice and a parapet."""
    t = F["thickness"]
    lv = levels(F)
    H = height(F)
    g = F["ground"]
    gh = lv[0][1]
    parts = []
    segs = _segments(L, F)
    win = F["window"]
    fmat, ft = win.get("frame_mat", "frame"), win.get("frame_t", 0.07)
    layout = _ground_layout(L, gh, g) if g["kind"] in ("plain", "shop", "sparse") else []
    counted = count_bays(L, F, F["pattern"])
    for si, sg in enumerate(segs):
        x0, x1, dy = sg["x0"], sg["x1"], sg["depth"]
        nb = max(1, round((x1 - x0) / F["bay_m"]))
        bw = (x1 - x0) / nb
        bands = []                                       # (z, height, openings) per storey: the panel joints
        # ground storey
        gk = g["kind"]
        if gk in ("pilotis", "recessed"):
            rec = g.get("recess", 3.0)
            doors = [{"kind": "rect", "cx": x0 + bw * (b + 0.5), "w": g.get("door_w", 0.8) * bw, "z0": 0.0,
                      "h": g.get("door_h", 0.82) * gh} for b in range(nb)]
            parts += pierced(x0, x1, 0.0, gh, dy + rec, dy + rec + t, doors, F["mat"]["ground"], inset=0.15)
            if g.get("door_frames"):                       # framed glass doors in the stone back wall
                for o in doors:
                    parts += frame(o, dy + rec + 0.12, mullions=g.get("door_mullions", 1))
            if gk == "pilotis":
                for b in range(nb + 1):
                    if 0 < b < nb or si == 0 or si == len(segs) - 1:
                        parts += column(x0 + bw * b, dy + 0.4, 0.0, gh, g.get("column_d", 0.45), g.get("column", "square"),
                                        F["mat"]["ground"])
            parts.append(box(x0, x1, dy, dy + rec, gh - 0.35, gh, "trim"))          # soffit
            can = g.get("canopy", 0.0)
            if can:
                parts.append(box(x0, x1, dy - can, dy, gh - 0.35, gh, "trim"))
                parts += balustrade(x0, x1, dy - can, gh, "glass")
                if g.get("planters"):
                    parts += planter(x0 + 0.3, x1 - 0.3, dy - can + 0.1, dy - can + 0.9, gh, seed=si)
        else:
            if counted and gk == "plain":                # a fixed count: windows at the counted bays only
                ops = [{"kind": "rect", "cx": cx, "w": win["w"] * bwi, "z0": 0.3 * gh, "h": 0.5 * gh}
                       for cx, bwi, kind in counted if x0 <= cx < x1 and kind != "solid"]
            else:
                ops = _ground_openings(g, x0, x1, nb, gh, F, si)
            if layout:                                   # the brief's doors and entrance replace the kind's own
                mine = [o for o in layout if x0 <= o["cx"] < x1]
                ops = [o for o in ops if not any(abs(o["cx"] - q["cx"]) < (o["w"] + q["w"]) / 2 + 0.3 for q in layout)]
                if g.get("door_count"):                  # the layout's doors are the doors: the kind's become windows
                    ops = [o if o["z0"] > 0 else o | {"z0": 0.3 * gh, "h": 0.5 * gh} for o in ops]
                ops += mine
            parts += pierced(x0, x1, 0.0, gh, dy, dy + t, ops, F["mat"]["ground"],
                             inset=0.3 if gk == "shop" else (0.8 * t if gk == "sparse" else None))
            if gk in ("plain", "shop", "sparse"):
                bands.append((0.0, gh, ops))
            for o in ops:
                if o.get("door") and not o.get("glass", True):          # a door leaf, set back in the reveal
                    parts.append(prism(outline(o), dy + 0.6 * t, dy + 0.6 * t + 0.06, g.get("door_leaf", "wood")))
                    continue
                if o.get("entrance"):                    # a glazed entrance: framed, mullions about 1.5 m apart
                    parts += frame(o, dy + 0.3 * t, t=0.08, mullions=max(1, round(o["w"] / 1.5)) - 1, mat=fmat)
                    continue
                if gk == "plain" and fmat != "frame" and o["z0"] > 0:         # timber frames: ground windows too
                    parts += frame(o, dy + t / 2 - 0.03, t=ft, mullions=win.get("mullions", 0), mat=fmat)
                if o.get("blind"):
                    parts.append(prism(outline(o), dy + 0.25, dy + t, F["mat"]["ground"]))
                elif gk == "arcade":                   # the arcade's walk and the shopfront behind it
                    rec = g.get("recess", 3.0)
                    parts += pierced(o["cx"] - bw / 2, o["cx"] + bw / 2, 0.0, gh, dy + rec, dy + rec + 0.3,
                                     [{"kind": "rect", "cx": o["cx"], "w": 0.7 * bw, "z0": 0.0, "h": 0.7 * gh}],
                                     F["mat"]["ground"], inset=0.1)
                    parts.append(box(o["cx"] - bw / 2, o["cx"] + bw / 2, dy + t, dy + rec, gh - 0.3, gh, "trim"))
                if g.get("archivolt") and o["kind"] in ("arch", "seg"):
                    parts += archivolt(o, g["archivolt"], dy, 0.08)
            can = g.get("canopy", 0.0)
            if can:                                      # a cantilevered slab over the ground storey
                parts.append(box(x0, x1, dy - can, dy, gh - 0.3, gh, "trim"))
        # upper storeys
        pat = sg["pattern"]
        for s, (z, sh) in enumerate(lv[1:], start=1):
            ops = []
            row = [b for b in counted if x0 <= b[0] < x1] if counted else bays(x0, x1, F, pat)
            for cx, bwi, kind in row:
                o, _ = _bay_openings(kind, cx, bwi, z, sh, F)
                ops += o
            fw, fh = F.get("open_w") or [], F.get("open_h") or []
            for o in ops:                                # this storey's opening proportions
                if s - 1 < len(fw) and fw[s - 1]:
                    o["w"] *= fw[s - 1]
                if s - 1 < len(fh) and fh[s - 1]:
                    o["h"] = min(o["h"] * fh[s - 1], z + sh - 0.2 - o["z0"])
            parts += pierced(x0, x1, z, z + sh, dy, dy + t, ops, F["mat"]["upper"], inset=win["reveal"])
            bands.append((z, sh, ops))
            for o in ops:
                if o.get("loggia"):
                    lg = F["loggia"]
                    a, b_, zb, zt = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2, o["z0"], o["z0"] + o["h"]
                    dep, jt = lg["depth"], lg.get("jamb", 0.15)
                    lm = lg.get("lining") or F["mat"]["upper"]
                    parts += [box(a, a + jt, dy + t, dy + dep, zb, zt, lm),
                              box(b_ - jt, b_, dy + t, dy + dep, zb, zt, lm)]
                    if lg.get("doors") == "sliding":    # full-width glass sliding doors at the back
                        door = {"kind": "rect", "cx": o["cx"], "w": o["w"] - 2 * jt - 0.1, "z0": zb, "h": 0.94 * (zt - zb)}
                        parts += pierced(a + jt, b_ - jt, zb, zt, dy + dep, dy + dep + 0.2, [door], F["mat"]["upper"], inset=0.05)
                        parts += frame(door, dy + dep - 0.03, mullions=lg.get("mullions", 2), mat=fmat)
                    else:
                        back = {"kind": "rect", "cx": o["cx"], "w": 0.8 * o["w"], "z0": zb, "h": 0.9 * (zt - zb)}
                        parts += pierced(a, b_, zb, zt, dy + dep, dy + dep + 0.2, [back], F["mat"]["upper"], inset=0.05)
                        if lg.get("frame"):
                            parts += frame(back, dy + dep + 0.02, t=ft, mullions=lg.get("mullions", 1), mat=fmat)
                    parts += balustrade(a, b_, dy + 0.02, zb, lg["balustrade"], h=lg.get("parapet_h", 1.05))
                    if lg.get("planters"):                  # planted loggias (hanging gardens)
                        parts += planter(a + 0.2, b_ - 0.2, dy + 0.25, dy + 0.85, zb, h=0.5, seed=int(s * 3 + o["cx"]))
                elif o.get("blind"):
                    parts.append(prism(outline(o), dy + F["blind"]["depth"], dy + t, F["mat"]["upper"]))
                elif o.get("curtain"):                   # curtain wall: mullions, an optional transom
                    c = F["curtain"]
                    parts += frame(o, dy + win["reveal"] - 0.03, t=0.06, mullions=max(1, round(o["w"] / c["mullion_m"])) - 1,
                                   mat=c.get("frame_mat", "frame"))
                    if c.get("transom"):
                        zt_ = o["z0"] + c["transom"] * o["h"]
                        parts.append(box(o["cx"] - o["w"] / 2 + 0.06, o["cx"] + o["w"] / 2 - 0.06, dy + win["reveal"] - 0.03,
                                         dy + win["reveal"] + 0.03, zt_ - 0.03, zt_ + 0.03, c.get("frame_mat", "frame")))
                else:
                    if win.get("frame") and o["kind"] == "rect":
                        parts += frame(o, dy + win["reveal"] - 0.03, t=ft, mullions=win.get("mullions", 0), mat=fmat)
                    if win.get("archivolt") and o["kind"] in ("arch", "seg"):
                        parts += archivolt(o, win["archivolt"], dy, 0.06)
                    parts.append(box(o["cx"] - o["w"] / 2 - 0.08, o["cx"] + o["w"] / 2 + 0.08, dy - 0.06, dy + 0.1,
                                     o["z0"] - 0.08, o["z0"], "trim"))          # sill
            # balconies
            bal = F["balcony"] if sg.get("balcony", "inherit") == "inherit" else sg["balcony"]
            if bal and s >= bal.get("from", 1) and (s - bal.get("from", 1)) % bal.get("every", 1) == 0:
                dep = bal.get("depth", 1.4)
                on = bal.get("on", "window")
                spans = [(x0, x1)] if bal.get("kind") == "band" else \
                        [(o["cx"] - o["w"] / 2 - 0.3, o["cx"] + o["w"] / 2 + 0.3) for o in ops
                         if on == "all" or (on == "window" and not o.get("loggia") and not o.get("blind"))
                         or (on == "loggia" and o.get("loggia"))]
                for k, (a, b_) in enumerate(spans):
                    parts.append(box(a, b_, dy - dep, dy, z - 0.2, z, "trim"))
                    parts += balustrade(a, b_, dy - dep, z, bal.get("balustrade", "glass"))
                    if bal.get("planters"):
                        parts += planter(a + 0.1, min(b_ - 0.1, a + max(1.2, 0.45 * (b_ - a))), dy - dep + 0.1, dy - dep + 0.6,
                                         z, h=0.45, seed=s * 7 + k)
                    if bal.get("drape"):
                        parts += drape(a + 0.1, a + 0.45 * (b_ - a), dy - dep, z - 0.2, seed=s * 3 + k)
        if F.get("panels"):
            parts += _joints(x0, x1, dy, L, bands, F["panels"])
        # set-back segment: return walls at its ends
        if dy > 0.01:
            for xe in (x0, x1):
                parts.append(box(xe - t / 2, xe + t / 2, 0.0, dy + t, 0.0, H, F["mat"]["upper"]))
        # projecting segment: returns from its wall back to the facade line and a cap over the gap
        # (open until 2026-10-16: a dark slot down each side of every projecting bay)
        elif dy + t < -0.01:
            zr = gh if gk in ("pilotis", "recessed") else 0.0        # the soffit closes the ground storey
            parts += [box(x0, x0 + t, dy + t, 0.0, zr, H, F["mat"]["upper"]),
                      box(x1 - t, x1, dy + t, 0.0, zr, H, F["mat"]["upper"]),
                      box(x0 + t, x1 - t, dy + t, 0.0, H - 0.45, H, "roof")]
    # pilasters, courses, cornice, parapet
    pil = F["pilasters"]
    if pil:                                              # on the bay boundaries: every `every` bays
        bb = bays(-L / 2, L / 2, F, F["pattern"])
        edges = [-L / 2] + [cx + bwi / 2 for cx, bwi, _ in bb]
        z0 = 0.0 if pil.get("from_ground", False) else lv[0][1]
        prof = pil.get("profile", "pier")
        pw_ = min(pil["w"], 0.3) if prof == "fin" else pil["w"]
        pj = pil.get("proj", 0.12)
        for b in range(0, len(edges), pil.get("every", 3)):
            x = min(max(edges[b], -L / 2 + pw_ / 2), L / 2 - pw_ / 2)
            if prof == "strip":                          # a flat strip with a fine shadow line either side
                parts.append(box(x - pw_ / 2, x + pw_ / 2, -min(pj, 0.06), 0.0, z0, H, pil.get("mat", F["mat"]["upper"])))
                for e in (x - pw_ / 2 - 0.04, x + pw_ / 2):
                    if -L / 2 < e and e + 0.04 < L / 2:
                        parts.append(box(e, e + 0.04, -0.006, 0.0, z0, H, "joint"))
            else:                                        # a projecting pier, or a thin deep fin
                parts.append(box(x - pw_ / 2, x + pw_ / 2, -pj, 0.0, z0, H, pil.get("mat", F["mat"]["upper"])))
    cs = F["courses"]
    if cs:
        for s in range(cs.get("every", 1), F["storeys"], cs.get("every", 1)):
            z = lv[s][0]
            parts.append(box(-L / 2, L / 2, -cs.get("proj", 0.06), 0.0, z - cs.get("h", 0.18), z, "trim"))
    if F["cornice"]:
        parts += cornice(L, H, F["cornice"])
    if F["parapet"]:
        z = H + sum(h for h, _ in (F["cornice"] or []))
        parts.append(box(-L / 2, L / 2, 0.0, 0.3, z, z + F["parapet"], "trim"))          # coping: smooth, not the wall stone
    return parts


# --- the arcade: arches on square piers, a walk behind, an optional gallery above ---------------

# an arcade along a face (metres unless noted): piers `pier_w` square at `bay_m` centres, arches
# (round | seg: a segmental head `rise` x the opening's width high) with their crown at `arch_h`,
# an impost moulding at the springing ({"h", "proj"}, or None), an archivolt band (m, 0: none);
# the arcade storey is `h` high; the walk runs `depth` from the piers' front face to the back
# wall (`back`: shop fronts or a wall with doors); `gallery` (GALLERY or None) over it
ARCADE = {"bay_m": 4.5, "pier_w": 0.9, "arch_h": 5.2, "arch": "round", "rise": 0.3, "h": 6.5, "depth": 4.0,
          "back": "shop", "impost": {"h": 0.3, "proj": 0.1}, "archivolt": 0.0, "gallery": None,
          "cornice": [[0.35, 0.25]], "parapet": 0.9}
# the gallery storeys over an arcade: an open loggia (arched or square openings, a balustrade in
# each, a walk `depth` m deep behind) or a wall with glazed windows; one opening per arcade bay,
# on the arches' axes. storeys 0: as many as the building's height takes. w, h, sill: shares of
# the bay and the storey. grid: stone pilasters on the pier axes and a band at each floor
# ({"w", "proj"} m, or None)
GALLERY = {"kind": "loggia", "storeys": 1, "storey_h": 4.5, "arch": "round", "rise": 0.3, "w": 0.62, "h": 0.8,
           "sill": 0.0, "balustrade": "rail", "balustrade_h": 1.0, "depth": 3.0, "frame": True, "mullions": 1,
           "grid": None}
GALLERY_GRID = {"w": 0.5, "proj": 0.12}


def arcade_bays(L: float, A: dict, ends=(True, True)) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """(piers as (x0, x1), openings as (centre, width), the bay pitch) of an arcade run from -L/2
    to L/2. An end without a pier (False) continues a neighbouring run's pier just outside it (a
    corner pier), so the bays keep their rhythm round the corner."""
    pw = A["pier_w"]
    xa = -L / 2 - (0.0 if ends[0] else pw)
    xb = L / 2 + (0.0 if ends[1] else pw)
    n = max(1, round((xb - xa - pw) / A["bay_m"]))
    s = (xb - xa - pw) / n
    piers = [(xa + i * s, xa + i * s + pw) for i in range(n + 1)]
    piers = [(max(a, -L / 2), min(b, L / 2)) for a, b in piers if b > -L / 2 + 1e-6 and a < L / 2 - 1e-6]
    return piers, [(xa + i * s + pw + (s - pw) / 2, s - pw) for i in range(n)], s


def _arch_opening(kind: str, cx: float, w: float, z0: float, h: float, rise: float, top: float, glass=False) -> dict:
    """An opening with a round (arch), segmental (seg) or square (rect) head, its crown at z0 + h
    but no higher than `top`; a round head is kept at least a half-circle plus 0.2 m tall."""
    k = {"round": "arch", "seg": "seg", "rect": "rect"}.get(kind, kind)
    o = {"kind": k, "cx": cx, "w": w, "z0": z0, "h": h, "glass": glass}
    if k == "arch":
        o["h"] = max(h, w / 2 + 0.2)
    if k == "seg":
        o["rise"] = max(0.05, min(rise * w, w / 2))
    o["h"] = min(o["h"], top - z0)
    return o


def spring(o: dict) -> float:
    """The springing height of an arched opening (where its head starts)."""
    if o["kind"] == "arch":
        return o["z0"] + o["h"] - o["w"] / 2
    if o["kind"] == "seg":
        return o["z0"] + o["h"] - o["rise"]
    return o["z0"] + o["h"]


def arcade_run(L: float, A: dict, ends=(True, True), end_walls=(False, False), end_t: float = 0.5,
               behind=None, ceiling=None, gallery_storeys: int = 0, gallery_behind=None, gallery_ceiling=None,
               top_ceiling: bool = True, gallery_t: float = 0.5, mat: str = "base") -> tuple[list[dict], float]:
    """An arcade along a face, in the facade frame (front at y = 0, inside towards +y), and the
    gallery storeys over it. Returns (parts, height to the top of the last storey).
    ends: a pier at each end (False: the neighbouring run's corner pier stands there);
    end_walls: a wall end_t thick across the walk at that end, in the neighbouring face's plane,
    pierced by an arch like the arcade's; behind / ceiling: the x interval of the walk's back
    wall and of its ceiling (default: the whole run); gallery_*: the same for a loggia gallery."""
    pw, h, dep = A["pier_w"], A["h"], A["depth"]
    t = 0.5
    piers, bays_, s = arcade_bays(L, A, ends)
    ops = [_arch_opening(A.get("arch", "round"), cx, w, 0.0, A["arch_h"], A.get("rise", 0.3), h - 0.3) for cx, w in bays_]
    parts = pierced(-L / 2, L / 2, 0.0, h, 0.0, pw, ops, mat)
    im = A.get("impost")
    if im and ops:                                      # a moulding round each pier at the springing
        zs, ih, pj = spring(ops[0]), im.get("h", 0.3), im.get("proj", 0.1)
        for a, b in piers:
            parts.append(box(a - pj, b + pj, -pj, pw + pj, zs - ih, zs, "trim"))
    if A.get("archivolt"):
        for o in ops:
            if o["kind"] in ("arch", "seg"):
                parts += archivolt(o, A["archivolt"], 0.0, 0.08)
    bx0, bx1 = behind or (-L / 2, L / 2)
    cx0, cx1 = ceiling or (-L / 2 + (end_t if end_walls[0] else 0.0), L / 2 - (end_t if end_walls[1] else 0.0))
    if dep > pw + 0.3:
        parts.append(box(cx0, cx1, pw, dep, h - 0.35, h, "trim"))          # the walk's ceiling
        back = []
        for cx, w in bays_:                             # the back wall's openings, on the arches' axes
            if not bx0 + 0.3 < cx < bx1 - 0.3:
                continue
            if A.get("back", "shop") == "shop":
                back.append({"kind": "rect", "cx": cx, "w": min(0.8 * w, bx1 - bx0 - 0.6), "z0": 0.25,
                             "h": min(0.8 * spring(ops[0]), h - 1.0)})
            else:
                back.append({"kind": "rect", "cx": cx, "w": min(1.6, 0.5 * w), "z0": 0.0, "h": min(3.0, h - 1.0),
                             "glass": False})
        parts += pierced(bx0, bx1, 0.0, h, dep, dep + t, back, mat, inset=0.15)
        for side, xe in ((0, -L / 2), (1, L / 2)):     # walls across the walk's ends, with an arch through
            if not end_walls[side]:
                continue
            ew = min(dep - pw - 1.2, ops[0]["w"] if ops else dep)
            if ew < 0.8:
                wall = pierced(pw, dep, 0.0, h, -end_t if side == 0 else 0.0, 0.0 if side == 0 else end_t, [], mat)
            else:
                o = _arch_opening(A.get("arch", "round"), (pw + dep) / 2, ew, 0.0, A["arch_h"], A.get("rise", 0.3), h - 0.3)
                wall = pierced(pw, dep, 0.0, h, -end_t if side == 0 else 0.0, 0.0 if side == 0 else end_t, [o], mat)
            parts += placed(wall, 90, (xe, 0.0, 0.0))
    H = h
    G = A.get("gallery")
    if G and gallery_storeys > 0:
        gs, gt = G["storey_h"], gallery_t
        gd = min(G.get("depth", 3.0), dep) if G.get("kind", "loggia") == "loggia" else 0.0
        gb0, gb1 = gallery_behind or (bx0, bx1)
        gc0, gc1 = gallery_ceiling or (cx0, cx1)
        bal, bh = G.get("balustrade", "rail"), G.get("balustrade_h", 1.0)
        for k in range(gallery_storeys):
            z = h + k * gs
            gops = [_arch_opening(G.get("arch", "round"), cx, G["w"] * s, z + G.get("sill", 0.0) * gs, G["h"] * gs,
                                  G.get("rise", 0.3), z + gs - 0.3, glass=G.get("kind") == "windowed") for cx, _ in bays_]
            if G.get("kind", "loggia") == "loggia":
                parts += pierced(-L / 2, L / 2, z, z + gs, 0.0, gt, gops, "wall")
                if gd > gt + 0.3:
                    doors = [{"kind": "rect", "cx": o["cx"], "w": min(1.4, 0.5 * o["w"]), "z0": z, "h": min(2.6, gs - 0.6)}
                             for o in gops if gb0 + 0.3 < o["cx"] < gb1 - 0.3]
                    parts += pierced(gb0, gb1, z, z + gs, gd, gd + 0.3, doors, "wall", inset=0.1)
                    if top_ceiling or k < gallery_storeys - 1:
                        parts.append(box(gc0, gc1, gt, gd, z + gs - 0.3, z + gs, "trim"))
            else:
                parts += pierced(-L / 2, L / 2, z, z + gs, 0.0, gt, gops, "wall", inset=0.25)
                for o in gops:
                    if G.get("frame", True) and o["kind"] == "rect":
                        parts += frame(o, 0.22, mullions=G.get("mullions", 1))
                    parts.append(box(o["cx"] - o["w"] / 2 - 0.08, o["cx"] + o["w"] / 2 + 0.08, -0.06, 0.1, o["z0"] - 0.08,
                                     o["z0"], "trim"))                           # sill
            if bal != "none" and bh > 0:               # a balustrade in each opening
                for o in gops:
                    a, b = o["cx"] - o["w"] / 2 + 0.02, o["cx"] + o["w"] / 2 - 0.02
                    parts += balustrade(a, b, 0.06 if G.get("kind") == "windowed" else 0.1, o["z0"], bal, bh)
        H = h + gallery_storeys * gs
        gr = G.get("grid")
        if gr:                                         # stone pilasters on the pier axes, a band at each floor
            gw, gp = gr.get("w", 0.5), gr.get("proj", 0.12)
            for a, b in piers:
                c = min(max((a + b) / 2, -L / 2 + gw / 2), L / 2 - gw / 2)
                parts.append(box(c - gw / 2, c + gw / 2, -gp, 0.05, h, H, "trim"))
            for k in range(gallery_storeys):
                z = h + k * gs
                parts.append(box(-L / 2, L / 2, -gp, 0.05, z, z + 0.25, "trim"))
    if A.get("cornice"):
        parts += cornice(L, H, A["cornice"])
    if A.get("parapet"):
        z = H + sum(c[0] for c in (A.get("cornice") or []))
        parts.append(box(-L / 2, L / 2, 0.0, 0.3, z, z + A["parapet"], "trim"))
    return parts, H


def top_of(p: dict) -> float:
    """A part's highest z in its own frame plus its placement (zones and hulls included)."""
    dz = (p.get("at") or [0.0, 0.0, 0.0])[2]
    k = p["kind"]
    if k in ("box", "zone"):
        return p["z1"] + dz
    if k == "prism":
        return max(z for _, z in p["xz"]) + dz
    if k == "lathe":
        return p["z"] + max(z for _, z in p["profile"]) + dz
    if k == "hull":
        return max(q[2] for q in p["pts"]) + dz
    if k == "shell":
        return p["z"] + max(max(r[1], r[3]) for r in p["rows"]) + dz
    return math.inf


def above(parts: list[dict], z: float) -> list[dict]:
    """The parts that reach above z (a facade's upper storeys without its ground storey)."""
    return [p for p in parts if top_of(p) > z + 1e-6]


def walls_around(w: float, d: float, Fs: dict, fronts=None) -> list[dict]:
    """Facades around a w x d footprint (front at -Y), thickness inwards; side walls run between
    the front and back walls (no coplanar end faces). Fs: {"front", "back", "left", "right"}
    facade specs (missing ones take "front")."""
    parts = []
    t = Fs["front"]["thickness"]
    for side, rz, length, at in (("front", 0, w, (0, -d / 2, 0)), ("back", 180, w, (0, d / 2, 0)),
                                 ("left", -90, d - 2 * t, (-w / 2, 0, 0)), ("right", 90, d - 2 * t, (w / 2, 0, 0))):
        F = Fs.get(side) or Fs["front"]
        if F is None:
            continue
        parts += placed(facade(length, F), rz, at)
    return parts


# --- checks ------------------------------------------------------------------------------

def _faces(p: dict):
    """A box's or prism's axis-aligned faces after placement: (axis, sign, offset, rect in the
    other two axes, handle). The handle says how to pull that face back (`_pull`). Turns that
    aren't multiples of 90 degrees, and the slanted faces of convex pieces, are skipped."""
    rz = round(p.get("rz", 0.0)) % 360
    if rz % 90 or p["kind"] not in ("box", "prism"):
        return []
    at = p.get("at", [0.0, 0.0, 0.0])
    if p["kind"] == "box":
        xz = [(p["x0"], p["z0"]), (p["x1"], p["z0"]), (p["x1"], p["z1"]), (p["x0"], p["z1"])]
    else:
        xz = [tuple(v) for v in p["xz"]]
    if len(xz) < 3:
        return []
    xs, zs = [v[0] for v in xz], [v[1] for v in xz]
    rect = len(xz) == 4 and len({round(x, 6) for x in xs}) == 2 and len({round(z, 6) for z in zs}) == 2
    y0, y1 = p["y0"], p["y1"]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    cx, cz = sum(xs) / len(xs), sum(zs) / len(zs)
    loc = []                                   # (axis, sign, offset, (a0, a1, b0, b1), handle) in the local frame
    if rect:                                   # front and back faces only when they're rectangles
        loc += [(1, -1, y0, (x0, x1, z0, z1), ("y0",)), (1, 1, y1, (x0, x1, z0, z1), ("y1",))]
    for k, ((xa, za), (xb, zb)) in enumerate(zip(xz, xz[1:] + xz[:1])):
        if abs(za - zb) < 1e-6:                # horizontal edge: a top or bottom face
            loc.append((2, 1 if za > cz else -1, za, (min(xa, xb), max(xa, xb), y0, y1), ("edge", k, 1)))
        elif abs(xa - xb) < 1e-6:              # vertical edge: an end face
            loc.append((0, 1 if xa > cx else -1, xa, (y0, y1, min(za, zb), max(za, zb)), ("edge", k, 0)))
    c, s = round(math.cos(math.radians(rz))), round(math.sin(math.radians(rz)))
    out = []
    for ax, sg, off, (a0, a1, b0, b1), h in loc:
        if ax == 2:                            # horizontal: rotate the xy rect
            pts = [(c * x - s * y + at[0], s * x + c * y + at[1]) for x in (a0, a1) for y in (b0, b1)]
            out.append((2, sg, round(off + at[2], 3), (min(q[0] for q in pts), max(q[0] for q in pts),
                                                         min(q[1] for q in pts), max(q[1] for q in pts)), h))
            continue
        n = (sg, 0) if ax == 0 else (0, sg)    # vertical: the normal and the face's horizontal span turn
        n = (c * n[0] - s * n[1], s * n[0] + c * n[1])
        ends = [(off, a0), (off, a1)] if ax == 0 else [(a0, off), (a1, off)]
        pts = [(c * x - s * y + at[0], s * x + c * y + at[1]) for x, y in ends]
        wax = 0 if abs(n[0]) else 1
        hs = [q[1 - wax] for q in pts]
        out.append((wax, n[wax], round(pts[0][wax], 3), (min(hs), max(hs), b0 + at[2], b1 + at[2]), h))
    return out


def _twins(parts: list[dict], tol: float):
    groups = {}
    for i, p in enumerate(parts):
        for ax, sg, off, r, h in _faces(p):
            groups.setdefault((ax, sg, off), []).append((r, i, h))
    for fs in groups.values():
        fs.sort(key=lambda f: f[0])
        for k, (r, i, h) in enumerate(fs):
            for r2, j, h2 in fs[k + 1:]:
                if r2[0] >= r[1] - tol:
                    break
                if i == j:
                    continue
                w, hh = min(r[1], r2[1]) - max(r[0], r2[0]), min(r[3], r2[3]) - max(r[2], r2[2])
                if w > tol and hh > tol:
                    yield w * hh, (i, h), (j, h2)


def coincident_area(parts: list[dict], tol: float = 0.01) -> list[tuple[float, int, int]]:
    """Faces of different parts that lie in one plane, face the same way and overlap: [(m2, i, j)]
    by area. Such twins render black in Cycles (a shadow ray leaving one hits the other) and
    z-fight in Unreal."""
    return sorted(((round(a, 3), fi[0], fj[0]) for a, fi, fj in _twins(parts, tol)), reverse=True)


def _pull(p: dict, h: tuple, eps: float) -> None:
    """Move one face of a part inwards by eps."""
    if h[0] in ("y0", "y1"):
        p[h[0]] += eps if h[0] == "y0" else -eps
        return
    _, k, ax = h
    if p["kind"] == "box":                     # edges of the box's xz outline: bottom, right, top, left
        key = {0: "z0", 1: "x1", 2: "z1", 3: "x0"}[k]
        p[key] += eps if key in ("z0", "x0") else -eps
        return
    xz = p["xz"]
    n = len(xz)
    cx, cz = sum(v[0] for v in xz) / n, sum(v[1] for v in xz) / n
    for q in (xz[k], xz[(k + 1) % n]):
        c = cz if ax == 1 else cx
        q[ax] += eps if q[ax] < c else -eps


# which surface shows where two coincide: copings and slab edges over walls over roof slabs;
# among equals the part built later (attics, accents, returns) wins
PRIORITY = {"roof": 0, "vault": 0, "wall": 1, "base": 1, "top": 1, "trim": 2}


def resolve_coincident(parts: list[dict], eps: float = 0.002, tol: float = 0.01, passes: int = 8) -> int:
    """Pull back the losing face of every coincident twin by eps (2 mm: invisible, but enough
    for Cycles' shadow rays and Unreal's depth buffer to tell the two apart), until none are
    left: where three coincide, the two losers meet again 2 mm back and the next pass parts them.
    Returns how many faces moved. Parts are changed in place."""
    moved = 0
    for _ in range(passes):
        pulled = set()
        for _, (i, hi), (j, hj) in list(_twins(parts, tol)):
            ri = (PRIORITY.get(parts[i]["mat"], 3), i)
            rj = (PRIORITY.get(parts[j]["mat"], 3), j)
            lose = (i, hi) if ri < rj else (j, hj)
            if lose not in pulled:
                pulled.add(lose)
                _pull(parts[lose[0]], lose[1], eps)
        if not pulled:
            break
        moved += len(pulled)
    return moved
