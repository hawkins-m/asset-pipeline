"""Reusable parts for procedural buildings (building_proc), and the facade grammar that puts
them together from a brief's `features`.

Every part is a dict for scripts/blender_kit.py (box, prism, lathe, shell; "rz" / "at" place it).
Local frame of a facade: x along it (-L/2 .. L/2), its front face at y = 0 facing -Y, the wall's
thickness towards +y (inside), z up from the facade's foot.

Materials named here (building_proc.materials makes them): base / wall / top (storey bands, each
from its own patch of the sheet), trim (cornices, slabs, string courses), frame (window frames,
rails: a dark metal), glass, planting (planters, roof gardens), wood, roof, vault.

Walls with openings are cut into convex prisms (`pierced`): arched walls built any other way came
out solid (CLAUDE.md, "Arched walls were solid until 2026-10-08").
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
    spring = o["z0"] + o["h"] - hw
    return o["z0"], spring + math.sqrt(max(hw * hw - dx * dx, 0.0))


def _edges(o: dict, seg: int = 12) -> list[float]:
    if o["kind"] == "rect":
        return [o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2]
    r = o["r"] if o["kind"] == "round" else o["w"] / 2
    if o["kind"] == "seg":
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


def planter(x0, x1, y0, y1, z, h=0.55, green=0.35, seed=0) -> list[dict]:
    """A planter box with planting standing out of it in uneven clumps (shrubs, grasses)."""
    out = [box(x0, x1, y0, y1, z, z + h, "trim")]
    n = max(1, int((x1 - x0) / 0.9))
    for i in range(n):
        a, b = x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n
        hh = green * (0.6 + 0.8 * (((i * 7 + seed * 3) % 5) / 4))
        out.append(box(a + 0.05, b - 0.05, y0 + 0.06, y1 - 0.06, z + h, z + h + hh, "planting"))
    return out


def drape(x0, x1, y, z, seed=0) -> list[dict]:
    """Planting hanging over a slab edge (hanging gardens): strands of uneven length."""
    out = []
    n = max(1, int((x1 - x0) / 0.7))
    for i in range(n):
        a, b = x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n
        L = 0.5 + 1.6 * (((i * 5 + seed * 11) % 7) / 6)
        out.append(box(a + 0.04, b - 0.04, y - 0.25, y, z - L, z, "planting"))
    return out


def balustrade(x0, x1, y, z, kind="glass", h=1.05) -> list[dict]:
    if kind == "solid":
        return [box(x0, x1, y, y + 0.18, z, z + h, "wall")]
    if kind == "rail":
        return [box(x0, x1, y, y + 0.06, z + h - 0.06, z + h, "frame")] + \
               [box(x, x + 0.04, y, y + 0.04, z, z + h, "frame") for x in _steps(x0, x1, 0.12)]
    return [box(x0, x1, y + 0.02, y + 0.05, z, z + h, "rail_glass"), box(x0, x1, y, y + 0.07, z + h - 0.05, z + h, "frame")]


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


def frame(o: dict, y: float, t: float = 0.07, mullions: int = 0) -> list[dict]:
    """A window frame (and mullions) inside a rect opening, at depth y."""
    if o["kind"] != "rect":
        return []
    a, b, z0, z1 = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2, o["z0"], o["z0"] + o["h"]
    out = [box(a, b, y, y + 0.06, z0, z0 + t, "frame"), box(a, b, y, y + 0.06, z1 - t, z1, "frame"),
           box(a, a + t, y, y + 0.06, z0, z1, "frame"), box(b - t, b, y, y + 0.06, z0, z1, "frame")]
    for k in range(1, mullions + 1):
        x = a + (b - a) * k / (mullions + 1)
        out.append(box(x - t / 2, x + t / 2, y, y + 0.06, z0, z1, "frame"))
    return out


# --- the facade grammar ---------------------------------------------------------------------

FACADE = {
    "storeys": 4, "storey_h": 3.4, "ground_h": None, "bay_m": 4.5, "thickness": 0.5,
    "pattern": ["window"],              # bay kinds, cycled across each segment: window | slot | loggia | blind | solid
    "window": {"kind": "rect", "w": 0.45, "h": 0.55, "sill": 0.28, "reveal": 0.25, "frame": True, "mullions": 0,
               "rise": None, "archivolt": 0.0},
    "slot": {"w": 0.16, "h": 0.72, "sill": 0.12},
    "loggia": {"w": 0.72, "h": 0.78, "sill": 0.0, "depth": 1.8, "balustrade": "glass"},
    "blind": {"kind": "rect", "w": 0.6, "h": 0.72, "sill": 0.14, "depth": 0.14},
    "ground": {"kind": "plain"},        # plain | shop | sparse | arcade | pilotis | recessed | blind_arcade
                                        # (pilotis / recessed: "recess" m, "door_w" / "door_h" of the back wall's doors)
    "balcony": None,                    # {"kind": slab|band, "depth", "balustrade", "planters", "drape", "every", "on", "from"}
    "pilasters": None,                  # {"every": bays, "w", "proj", "from_ground"}
    "courses": None,                    # {"every": storeys, "h", "proj"}
    "cornice": [[0.45, 0.3]],
    "parapet": 0.9,
    "recess": [],                       # [{"x0": frac, "x1": frac, "depth": m, "pattern": [...]}]: set-back segments
    "mat": {"ground": "base", "upper": "wall"},
}


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
    for si, sg in enumerate(segs):
        x0, x1, dy = sg["x0"], sg["x1"], sg["depth"]
        nb = max(1, round((x1 - x0) / F["bay_m"]))
        bw = (x1 - x0) / nb
        # ground storey
        gk = g["kind"]
        if gk in ("pilotis", "recessed"):
            rec = g.get("recess", 3.0)
            parts += pierced(x0, x1, 0.0, gh, dy + rec, dy + rec + t,
                             [{"kind": "rect", "cx": x0 + bw * (b + 0.5), "w": g.get("door_w", 0.8) * bw, "z0": 0.0,
                               "h": g.get("door_h", 0.82) * gh} for b in range(nb)], F["mat"]["ground"], inset=0.15)
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
            ops = _ground_openings(g, x0, x1, nb, gh, F, si)
            parts += pierced(x0, x1, 0.0, gh, dy, dy + t, ops, F["mat"]["ground"],
                             inset=0.3 if gk == "shop" else (0.8 * t if gk == "sparse" else None))
            for o in ops:
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
        # upper storeys
        pat = sg["pattern"]
        for s, (z, sh) in enumerate(lv[1:], start=1):
            ops = []
            for cx, bwi, kind in bays(x0, x1, F, pat):
                o, _ = _bay_openings(kind, cx, bwi, z, sh, F)
                ops += o
            win = F["window"]
            parts += pierced(x0, x1, z, z + sh, dy, dy + t, ops, F["mat"]["upper"], inset=win["reveal"])
            for o in ops:
                if o.get("loggia"):
                    lg = F["loggia"]
                    a, b_, zb, zt = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2, o["z0"], o["z0"] + o["h"]
                    dep = lg["depth"]
                    parts += [box(a, a + 0.15, dy + t, dy + dep, zb, zt, F["mat"]["upper"]),
                              box(b_ - 0.15, b_, dy + t, dy + dep, zb, zt, F["mat"]["upper"])]
                    parts += pierced(a, b_, zb, zt, dy + dep, dy + dep + 0.2,
                                     [{"kind": "rect", "cx": o["cx"], "w": 0.8 * o["w"], "z0": zb, "h": 0.9 * (zt - zb)}],
                                     F["mat"]["upper"], inset=0.05)
                    parts += balustrade(a, b_, dy + 0.02, zb, lg["balustrade"])
                    if lg.get("planters"):                  # planted loggias (hanging gardens)
                        parts += planter(a + 0.2, b_ - 0.2, dy + 0.25, dy + 0.85, zb, h=0.5, seed=int(s * 3 + o["cx"]))
                elif o.get("blind"):
                    parts.append(prism(outline(o), dy + F["blind"]["depth"], dy + t, F["mat"]["upper"]))
                else:
                    if win.get("frame") and o["kind"] == "rect":
                        parts += frame(o, dy + win["reveal"] - 0.03, mullions=win.get("mullions", 0))
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
        # set-back segment: return walls at its ends
        if dy > 0.01:
            for xe in (x0, x1):
                parts.append(box(xe - t / 2, xe + t / 2, 0.0, dy + t, 0.0, H, F["mat"]["upper"]))
    # pilasters, courses, cornice, parapet
    pil = F["pilasters"]
    if pil:                                              # on the bay boundaries: every `every` bays
        bb = bays(-L / 2, L / 2, F, F["pattern"])
        edges = [-L / 2] + [cx + bwi / 2 for cx, bwi, _ in bb]
        z0 = 0.0 if pil.get("from_ground", False) else lv[0][1]
        for b in range(0, len(edges), pil.get("every", 3)):
            x = min(max(edges[b], -L / 2 + pil["w"] / 2), L / 2 - pil["w"] / 2)
            parts.append(box(x - pil["w"] / 2, x + pil["w"] / 2, -pil.get("proj", 0.12), 0.0, z0, H,
                             pil.get("mat", F["mat"]["upper"])))
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
