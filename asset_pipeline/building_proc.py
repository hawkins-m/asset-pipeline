"""Procedural buildings (PLAN.md "Build split"): the library's architectural types built in
Blender at real dimensions from a design brief read off their reference sheet.

The brief (the project's library/kits/<asset>.json, kind "building") holds:
    form      the catalog form: hall_vault | cube | basilica | podium_tower | perimeter | row
    dims      [w, d, h] metres (the entry's size: greybox or catalog)
    params    form parameters (storeys, bays, openings...): every generator has defaults,
              the brief overrides them
    view      the sheet view it was read from (project path), for the maps and the measures
    measured  what `measure` read off the view: palette, plain wall patch, bay and storey
              rhythm (px periods and counts at the brief's size)
Surfaces come from the view: a seamless wall tile from its plainest patch (with normal and
roughness), glass and trim as plain materials in the palette's colours.

Walls are built from convex pieces only (`pierced`): a wall with openings is cut into vertical
slices at every opening edge (and along every curve), and each slice keeps the solid z
intervals between the openings as trapezoid prisms. Arched walls built any other way came out
solid (CLAUDE.md, "Arched walls were solid until 2026-10-08").
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import kit_proc
from .project import ProjectStore, read_json, write_json

FORMS = ("hall_vault", "cube", "basilica", "podium_tower", "perimeter", "row")


# --- walls ------------------------------------------------------------------------------

def _span(o: dict, x: float) -> tuple[float, float] | None:
    """The opening's z interval at x, or None if x is outside it."""
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
    spring = o["z0"] + o["h"] - hw                                       # arch: round head
    return o["z0"], spring + math.sqrt(max(hw * hw - dx * dx, 0.0))


def _edges(o: dict, seg: int = 12) -> list[float]:
    if o["kind"] == "rect":
        return [o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2]
    r = o["r"] if o["kind"] == "round" else o["w"] / 2
    return [o["cx"] - r * math.cos(math.pi * i / seg) for i in range(seg + 1)]


def pierced(x0, x1, z0, z1, y0, y1, openings, mat="wall", glass="glass", inset=None) -> list[dict]:
    """A wall from x0 to x1, z0 to z1, front face at y0, back at y1, with openings (rect:
    cx w z0 h; arch: cx w z0 h, h to the crown; round: cx cz r; any: "glass": False for an
    open one). Returns prism parts plus a glass pane set `inset` behind the front face."""
    xs = sorted({x0, x1, *[min(max(e, x0), x1) for o in openings for e in _edges(o)]})
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
                parts.append({"kind": "prism", "xz": [[xa, lo_a], [xb, lo_b], [xb, sb[0]], [xa, sa[0]]],
                              "y0": y0, "y1": y1, "mat": mat})
            lo_a, lo_b = sa[1], sb[1]
        if z1 - lo_a > 1e-4 or z1 - lo_b > 1e-4:
            parts.append({"kind": "prism", "xz": [[xa, lo_a], [xb, lo_b], [xb, z1], [xa, z1]],
                          "y0": y0, "y1": y1, "mat": mat})
    t = y1 - y0
    gi = min(inset if inset is not None else 0.35 * t, 0.8 * t)
    for o in openings:
        if not o.get("glass", True):
            continue
        poly = _outline(o)
        parts.append({"kind": "prism", "xz": poly, "y0": y0 + gi, "y1": y0 + gi + 0.04, "mat": glass})
    return parts


def _outline(o: dict, seg: int = 12) -> list[list[float]]:
    if o["kind"] == "rect":
        a, b = o["cx"] - o["w"] / 2, o["cx"] + o["w"] / 2
        return [[a, o["z0"]], [b, o["z0"]], [b, o["z0"] + o["h"]], [a, o["z0"] + o["h"]]]
    if o["kind"] == "round":
        return [[o["cx"] + o["r"] * math.cos(2 * math.pi * k / 24), o["cz"] + o["r"] * math.sin(2 * math.pi * k / 24)]
                for k in range(24)]
    hw = o["w"] / 2
    spring = o["z0"] + o["h"] - hw
    arc = [[o["cx"] + hw * math.cos(math.pi * i / seg), spring + hw * math.sin(math.pi * i / seg)] for i in range(seg + 1)]
    return [[o["cx"] - hw, o["z0"]], [o["cx"] + hw, o["z0"]]] + arc[:]


def placed(parts: list[dict], rz: float = 0.0, at=(0.0, 0.0, 0.0)) -> list[dict]:
    for p in parts:
        p["rz"], p["at"] = rz, list(at)
    return parts


def box(x0, x1, y0, y1, z0, z1, mat="wall"):
    return {"kind": "box", "x0": x0, "x1": x1, "y0": y0, "y1": y1, "z0": z0, "z1": z1, "mat": mat}


def grid(length: float, z0: float, storeys: int, storey_h: float, bays: int, win_w: float = 0.45,
         win_h: float = 0.55, sill: float = 0.28, kind: str = "rect", skip_ground: bool = False) -> list[dict]:
    """Window openings for a facade `length` wide: `bays` per storey, centred in each bay,
    win_w / win_h as fractions of the bay width / storey height."""
    bw = length / bays
    out = []
    for s in range(1 if skip_ground else 0, storeys):
        zb = z0 + s * storey_h
        for b in range(bays):
            cx = -length / 2 + bw * (b + 0.5)
            w, h = win_w * bw, win_h * storey_h
            if kind == "arch":
                h = max(h, w / 2 + 0.3)
            out.append({"kind": kind, "cx": cx, "w": w, "z0": zb + sill * storey_h, "h": h})
    return out


def ground(length: float, storey_h: float, bays: int, kind: str) -> list[dict]:
    """Ground-storey openings: arcade (round arches, shopfront glass deep inside), shop (tall
    glazed fronts), or plain (windows and a door every few bays)."""
    bw = length / bays
    out = []
    for b in range(bays):
        cx = -length / 2 + bw * (b + 0.5)
        if kind == "arcade":
            out.append({"kind": "arch", "cx": cx, "w": 0.68 * bw, "z0": 0.0, "h": 0.9 * storey_h})
        elif kind == "shop":
            out.append({"kind": "rect", "cx": cx, "w": 0.7 * bw, "z0": 0.25, "h": 0.72 * storey_h})
        else:
            door = b % 3 == 1
            out.append({"kind": "rect", "cx": cx, "w": (0.36 if door else 0.45) * bw,
                        "z0": 0.0 if door else 0.3 * storey_h, "h": (0.8 if door else 0.5) * storey_h})
    return out


def facade(length, height, storeys, storey_h, bays, thickness, ground_kind="plain", top_kind=None,
           win=None, mat="wall") -> list[dict]:
    """One facade wall along X with its front face at y = 0 (local), openings and floor bands."""
    win = win or {}
    ops = ground(length, storey_h, bays, ground_kind) if ground_kind else []
    upper = storeys - (1 if top_kind == "loggia" else 0)
    ops += grid(length, 0.0, upper, storey_h, bays, skip_ground=bool(ground_kind), **win)
    if top_kind == "loggia":                      # top floor: wide open loggias, glass deep inside
        zb = (storeys - 1) * storey_h
        bw = length / bays
        ops += [{"kind": "rect", "cx": -length / 2 + bw * (b + 0.5), "w": 0.78 * bw, "z0": zb + 0.15 * storey_h,
                 "h": 0.72 * storey_h} for b in range(bays)]
    parts = pierced(-length / 2, length / 2, 0.0, height, 0.0, thickness, ops, mat,
                    inset=0.6 * thickness if top_kind == "loggia" else None)
    for s in range(1, storeys):                   # thin floor bands: the storey rhythm
        z = s * storey_h - 0.1
        parts.append(box(-length / 2, length / 2, -0.06, 0.0, z - 0.12, z + 0.06, "trim"))
    return parts


def cornice(w, d, z, h=0.6, over=0.45, mat="trim", parapet=0.9) -> list[dict]:
    out = _ring(w + 2 * over, d + 2 * over, over + 1.0, z, z + h, mat)
    if parapet:
        t = 0.3
        for x0, x1, y0, y1 in ((-w / 2, w / 2, -d / 2, -d / 2 + t), (-w / 2, w / 2, d / 2 - t, d / 2),
                               (-w / 2, -w / 2 + t, -d / 2, d / 2), (w / 2 - t, w / 2, -d / 2, d / 2)):
            out.append(box(x0, x1, y0, y1, z + h, z + h + parapet, "wall"))
    return out


def walls_around(w, d, h, storeys, storey_h, bays_w, bays_d, t, **kw) -> list[dict]:
    """Four facades around a w x d footprint (front at -Y), thickness inwards."""
    parts = []
    for rz, length, nb, at in ((0, w, bays_w, (0, -d / 2, 0)), (180, w, bays_w, (0, d / 2, 0)),
                               (-90, d, bays_d, (-w / 2, 0, 0)), (90, d, bays_d, (w / 2, 0, 0))):
        # the side walls run between the front and back walls (no coplanar end faces)
        parts += placed(facade(length - (2 * t if rz in (90, -90) else 0), h, storeys, storey_h, nb, t, **kw),
                        rz, at)
    return parts


# --- generators -------------------------------------------------------------------------

def _n(length: float, bay_m: float) -> int:
    return max(1, round(length / bay_m))


def hall_vault(dims, p) -> list[dict]:
    """A barrel-vaulted hall: the vault along X over low side walls, ribs every bay, glazed
    crown panels in alternating bays, open arched ends, a floor slab."""
    w, d, h = dims
    base = p.get("base_h", 0.22 * h)
    t = p.get("vault_t", 0.6)
    bay = p.get("bay_m", 6.0)
    seg = 16
    nb = _n(w, bay)
    bw = w / nb
    a, b = d / 2, h - base                                                  # ellipse half-width, rise
    parts = [box(-w / 2, w / 2, -d / 2, d / 2, -0.3, 0.0, "roof")]
    for y0 in (-d / 2, d / 2 - 0.8):                                         # low side walls with doors
        ops = [{"kind": "rect", "cx": -w / 2 + bw * (i + 0.5), "w": 0.4 * bw, "z0": 0.0, "h": min(0.85 * base, 3.2),
                "glass": True} for i in range(nb) if i % 2 == 0]
        parts += pierced(-w / 2, w / 2, 0.0, base, y0, y0 + 0.8, ops, "wall")
    glass_seg = set(range(seg // 2 - p.get("crown_glass", 2), seg // 2 + p.get("crown_glass", 2)))
    for i in range(nb):
        x0, x1 = -w / 2 + bw * i, -w / 2 + bw * (i + 1)
        for k in range(seg):
            th0, th1 = math.pi * k / seg, math.pi * (k + 1) / seg
            q = [[-a * math.cos(th0), base + b * math.sin(th0)], [-a * math.cos(th1), base + b * math.sin(th1)],
                 [-(a + t) * math.cos(th1), base + (b + t) * math.sin(th1)],
                 [-(a + t) * math.cos(th0), base + (b + t) * math.sin(th0)]]
            mat = "glass" if (k in glass_seg and i % 2 == 1) else "vault"
            parts += placed([{"kind": "prism", "xz": q[::-1], "y0": x0, "y1": x1, "mat": mat}], 90)
    for xr in [-w / 2 + bw * i for i in range(nb + 1)]:                    # ribs
        rt = 0.9 if abs(abs(xr) - w / 2) < 1e-6 else 0.45
        for k in range(seg):
            th0, th1 = math.pi * k / seg, math.pi * (k + 1) / seg
            q = [[-(a - 0.25) * math.cos(th0), base + (b - 0.25) * math.sin(th0)],
                 [-(a - 0.25) * math.cos(th1), base + (b - 0.25) * math.sin(th1)],
                 [-(a + t + 0.3) * math.cos(th1), base + (b + t + 0.3) * math.sin(th1)],
                 [-(a + t + 0.3) * math.cos(th0), base + (b + t + 0.3) * math.sin(th0)]]
            parts += placed([{"kind": "prism", "xz": q[::-1], "y0": xr - rt / 2, "y1": xr + rt / 2, "mat": "trim"}], 90)
    return parts


def cube(dims, p) -> list[dict]:
    """A monolithic cube: thick walls pierced by a few huge openings per face (a great arch
    at the ground, round openings above), a plinth with steps, a roof with a skylit atrium."""
    w, d, h = dims
    t = p.get("wall_t", 2.2)
    plinth = p.get("plinth_h", 1.2)
    parts = [box(-w / 2 - 2.5, w / 2 + 2.5, -d / 2 - 2.5, d / 2 + 2.5, 0, plinth / 2, "wall"),
             box(-w / 2 - 1.2, w / 2 + 1.2, -d / 2 - 1.2, d / 2 + 1.2, plinth / 2, plinth, "wall")]
    H = h - plinth
    for rz, length, at in ((0, w, (0, -d / 2, plinth)), (180, w, (0, d / 2, plinth)),
                           (-90, d, (-w / 2, 0, plinth)), (90, d, (w / 2, 0, plinth))):
        L = length - (2 * t if rz in (90, -90) else 0)
        # one great arch in the middle, a great round opening either side (sized to the face's
        # height so a squat cube keeps solid wall between them)
        aw = p.get("arch_w", 0.26) * length
        ah = min(p.get("arch_h", 0.78) * H, H - 1.5)
        ops = [{"kind": "arch", "cx": 0.0, "w": min(aw, 2 * (ah - 0.5)), "z0": 0.0, "h": ah}]
        r = min(p.get("round_r", 0.13) * length, 0.36 * H)
        for sx in (-1, 1):
            ops.append({"kind": "round", "cx": sx * p.get("round_x", 0.3) * length, "cz": p.get("round_z", 0.5) * H, "r": r})
        parts += placed(pierced(-L / 2, L / 2, 0.0, H, 0.0, t, ops, "wall", inset=0.75 * t), rz, at)
    roof_z = h
    a = p.get("atrium", 0.36) * min(w, d)
    for x0, x1, y0, y1 in ((-w / 2, w / 2, -d / 2, -a / 2), (-w / 2, w / 2, a / 2, d / 2),
                           (-w / 2, -a / 2, -a / 2, a / 2), (a / 2, w / 2, -a / 2, a / 2)):
        parts.append(box(x0, x1, y0, y1, roof_z - 0.8, roof_z, "roof"))
    parts.append(box(-a / 2, a / 2, -a / 2, a / 2, roof_z - 0.3, roof_z - 0.2, "glass"))
    parts += _ring(w + 0.4, d + 0.4, 1.2, roof_z, roof_z + 0.35)
    return parts


def _ring(w, d, t, z0, z1, mat="trim") -> list[dict]:
    """A rectangular band t wide around a w x d outline (a cornice that leaves the roof open)."""
    return [box(-w / 2, w / 2, -d / 2, -d / 2 + t, z0, z1, mat), box(-w / 2, w / 2, d / 2 - t, d / 2, z0, z1, mat),
            box(-w / 2, -w / 2 + t, -d / 2 + t, d / 2 - t, z0, z1, mat), box(w / 2 - t, w / 2, -d / 2 + t, d / 2 - t, z0, z1, mat)]


def basilica(dims, p) -> list[dict]:
    """A civic basilica along X: tall nave with a clerestory, lower side aisles with arcaded
    glazed walls, an entrance front with a great arch at -X, an apse with a half dome at +X."""
    w, d, h = dims
    st = p.get("storey_h", 5.0)
    nave_w = p.get("nave", 0.42) * d
    aisle_h = p.get("aisle", 0.55) * h
    bay = p.get("bay_m", 5.5)
    t = 1.0
    apse_r = nave_w / 2
    L = w - apse_r                                                       # hall length without the apse
    x0 = -w / 2
    xc = x0 + L / 2
    nb = _n(L, bay)
    parts = []
    for side in (-1, 1):                                                 # aisles: arcaded walls
        y_out = side * d / 2
        ops = ground(L, aisle_h * 0.82, nb, "arcade")
        ops += [{"kind": "rect", "cx": o["cx"], "w": 0.3 * L / nb, "z0": aisle_h * 0.8, "h": aisle_h * 0.12} for o in ops]
        parts += placed(pierced(-L / 2, L / 2, 0.0, aisle_h, 0.0, t, ops, "wall", inset=0.8), 0 if side < 0 else 180,
                        (xc, y_out, 0))
        parts.append(box(x0, x0 + L, min(y_out, side * nave_w / 2), max(y_out, side * nave_w / 2),
                         aisle_h - 0.6, aisle_h, "roof"))
    for side in (-1, 1):                                                 # nave walls: clerestory
        ops = [{"kind": "arch", "cx": o, "w": 0.4 * L / nb, "z0": aisle_h + 1.0, "h": (h - aisle_h) * 0.6}
               for o in [-L / 2 + L / nb * (i + 0.5) for i in range(nb)]]
        parts += placed(pierced(-L / 2, L / 2, 0.0, h, 0.0, t, ops, "wall"), 0 if side < 0 else 180,
                        (xc, side * nave_w / 2, 0))
    parts.append(box(x0, x0 + L, -nave_w / 2, nave_w / 2, h - 0.8, h, "roof"))
    for side in (-1, 1):
        parts.append(box(x0 - 0.3, x0 + L + 0.3, side * nave_w / 2 - 0.5, side * nave_w / 2 + 0.5, h, h + 0.7, "trim"))
        parts.append(box(x0 - 0.3, x0 + L + 0.3, side * d / 2 - 0.4, side * d / 2 + 0.4, aisle_h, aisle_h + 0.5, "trim"))
    # entrance front (faces -X): a great arch in the nave, doors in the aisles
    fops = [{"kind": "arch", "cx": 0.0, "w": 0.5 * nave_w, "z0": 0.0, "h": 0.62 * h}]
    for sx in (-1, 1):
        fops.append({"kind": "arch", "cx": sx * (nave_w / 2 + (d - nave_w) / 4), "w": 0.36 * (d - nave_w) / 2,
                     "z0": 0.0, "h": 0.6 * aisle_h})
    di, ni = d / 2 - t, nave_w / 2 - t          # between the aisle / nave walls: no coplanar faces
    front = pierced(-di, di, 0.0, aisle_h, 0.0, 1.6, [o for o in fops if o["cx"] != 0.0], "wall", inset=1.2)
    front += pierced(-ni, ni, aisle_h, h + 1.5, 0.0, 1.6, [], "wall")
    front += pierced(-ni, ni, 0.0, aisle_h, 0.0, 1.6, [fops[0]], "wall", inset=1.2)
    parts += placed(front, -90, (x0, 0, 0))
    back = pierced(-di, di, 0.0, aisle_h, 0.0, t, [], "wall") + pierced(-ni, ni, aisle_h, h, 0.0, t, [], "wall")
    parts += placed(back, 90, (x0 + L, 0, 0))
    hh = 0.8 * h                                                         # apse: half drum + half dome
    parts.append({"kind": "shell", "x": x0 + L, "y": 0.0, "z": 0.0, "a0": -90, "a1": 90, "seg": 24, "mat": "wall",
                  "rows": [[apse_r, 0.0, apse_r - t, 0.0], [apse_r, hh * 0.62, apse_r - t, hh * 0.62]]})
    rows = [[apse_r * math.cos(f), hh * 0.62 + apse_r * 0.7 * math.sin(f), (apse_r - t) * math.cos(f),
             hh * 0.62 + (apse_r - t) * 0.7 * math.sin(f)] for f in np.linspace(0, math.radians(84), 9)]
    parts.append({"kind": "shell", "x": x0 + L, "y": 0.0, "z": 0.0, "a0": -90, "a1": 90, "seg": 24, "mat": "roof",
                  "rows": rows})
    return parts


def podium_tower(dims, p) -> list[dict]:
    """A slender tower on a podium: a 3-storey podium (shopfronts, windows), a square tower
    with punched windows and a stack of loggias on its front, a setback crown."""
    w, d, h = dims
    st = p.get("storey_h", 3.4)
    pod_n = p.get("podium_storeys", 3)
    tw = p.get("tower_w", min(22.0, 0.75 * d))
    crown = p.get("crown_storeys", 2)
    bay = p.get("bay_m", 3.6)
    pod_h = pod_n * st
    n = max(4, round((h - pod_h) / st))
    tower_h = n * st
    parts = walls_around(w, d, pod_h, pod_n, st, _n(w, 6.0), _n(d, 6.0), 0.5, ground_kind="shop")
    parts += [box(-w / 2, w / 2, -d / 2, d / 2, pod_h - 0.5, pod_h, "roof")] + cornice(w, d, pod_h, 0.5, 0.3, parapet=1.0)
    body_n = n - crown
    body = walls_around(tw, tw, body_n * st, body_n, st, _n(tw, bay), _n(tw, bay), 0.45, ground_kind=None,
                        win={"win_w": 0.42, "win_h": 0.58})
    parts += [dict(q, at=[q["at"][0], q["at"][1], q["at"][2] + pod_h]) for q in body]
    # loggia stack on the front: every `loggia_every` storeys a deep recess across the face
    ev = p.get("loggia_every", 3)
    for s in range(1, body_n, ev):
        z = pod_h + s * st
        parts.append(box(-tw / 2 + 1.0, tw / 2 - 1.0, -tw / 2 - 1.4, -tw / 2, z - 0.25, z, "trim"))   # balcony slab
        parts.append(box(-tw / 2 + 1.0, tw / 2 - 1.0, -tw / 2 - 1.4, -tw / 2 - 1.3, z, z + 1.0, "glass"))
    ci = p.get("crown_inset", 2.0)
    cw = tw - 2 * ci
    zc = pod_h + body_n * st
    cr = walls_around(cw, cw, crown * st, crown, st, _n(cw, bay), _n(cw, bay), 0.4, ground_kind=None,
                      win={"win_w": 0.6, "win_h": 0.7})
    parts += [dict(q, at=[q["at"][0], q["at"][1], q["at"][2] + zc]) for q in cr]
    parts += [box(-tw / 2, tw / 2, -tw / 2, tw / 2, zc - 0.4, zc, "roof")] + cornice(tw, tw, zc, 0.4, 0.3, parapet=1.1)
    parts += [box(-cw / 2, cw / 2, -cw / 2, cw / 2, zc + crown * st - 0.4, zc + crown * st, "roof")]
    parts += cornice(cw, cw, zc + crown * st, 0.5, 0.4, parapet=0)
    return parts


def perimeter(dims, p) -> list[dict]:
    """A perimeter block: four wings `wing` deep around a courtyard, street facades with a
    ground arcade (or shops) and a loggia top floor, plainer courtyard facades, flat roofs."""
    w, d, h = dims
    st = p.get("storey_h", 3.4)
    n = max(2, round(h / st))
    h = n * st
    wing = p.get("wing", 14.0)
    bay = p.get("bay_m", 4.5)
    t = 0.5
    g = p.get("ground", "arcade")
    top = p.get("top", "loggia")
    parts = walls_around(w, d, h, n, st, _n(w, bay), _n(d, bay), t, ground_kind=g, top_kind=top,
                         win={"win_w": p.get("win_w", 0.4), "win_h": p.get("win_h", 0.55)})
    cw, cd = w - 2 * wing, d - 2 * wing
    court = walls_around(cw, cd, h, n, st, _n(cw, bay), _n(cd, bay), t, ground_kind="plain",
                         win={"win_w": 0.4, "win_h": 0.5})
    for q in court:                                   # same places, turned to face the courtyard
        q["rz"] = (q["rz"] + 180) % 360
    parts += court
    for x0, x1, y0, y1 in ((-w / 2, w / 2, -d / 2, -cd / 2), (-w / 2, w / 2, cd / 2, d / 2),
                           (-w / 2, -cw / 2, -cd / 2, cd / 2), (cw / 2, w / 2, -cd / 2, cd / 2)):
        parts.append(box(x0, x1, y0, y1, h - 0.5, h, "roof"))
    parts += cornice(w, d, h, 0.55, 0.45, parapet=0.9)
    parts.append(box(-cw / 2, cw / 2, -cd / 2, cd / 2, -0.2, 0.15, "garden"))
    if p.get("pergola", True):                        # roof garden pergolas on the street wings
        for x in np.arange(-w / 2 + 4, w / 2 - 3, 3.0):
            for y in (-d / 2 + 3.0, -d / 2 + wing - 3.0):
                parts.append(box(x - 0.12, x + 0.12, y - 0.12, y + 0.12, h + 0.0, h + 2.6, "trim"))
            parts.append(box(x - 0.08, x + 0.08, -d / 2 + 2.6, -d / 2 + wing - 2.6, h + 2.6, h + 2.8, "trim"))
    return parts


def row(dims, p) -> list[dict]:
    """A row of narrow attached houses along X: each its own storey count (stepping by one),
    a door and two window bays, small balconies, a parapet roof terrace with a pergola."""
    w, d, h = dims
    st = p.get("storey_h", 3.4)
    k = p.get("houses", 4)
    hw = w / k
    base_n = max(2, round(h / st))
    steps = p.get("steps", [0, 1, -1, 0])
    parts = []
    for i in range(k):
        n = max(2, base_n + steps[i % len(steps)])
        hh = n * st
        cx = -w / 2 + hw * (i + 0.5)
        ops = [{"kind": "rect", "cx": -hw / 4, "w": 1.2, "z0": 0.0, "h": 2.6},
               {"kind": "rect", "cx": hw / 4, "w": 0.4 * hw / 2 * 2, "z0": 0.9, "h": 1.6}]
        ops += grid(hw, 0.0, n, st, 2, win_w=0.5, win_h=0.62, sill=0.2, skip_ground=True)
        parts += placed(pierced(-hw / 2, hw / 2, 0.0, hh, 0.0, 0.4, ops, "wall"), 0, (cx, -d / 2, 0))
        parts += placed(pierced(-hw / 2, hw / 2, 0.0, hh, 0.0, 0.4, grid(hw, 0.0, n, st, 2, win_w=0.4, win_h=0.5), "wall"),
                        180, (cx, d / 2, 0))
        for sx in (-1, 1):                            # party walls (shared): solid
            parts.append(box(cx + sx * hw / 2 - 0.15, cx + sx * hw / 2 + 0.15, -d / 2, d / 2, 0, hh + 1.0, "wall"))
        parts.append(box(cx - hw / 2, cx + hw / 2, -d / 2, d / 2, hh - 0.4, hh, "roof"))
        parts.append(box(cx - hw / 2, cx + hw / 2, -d / 2 - 0.12, -d / 2 + 0.3, hh, hh + 1.0, "wall"))   # parapet
        parts.append(box(cx - hw / 2, cx + hw / 2, d / 2 - 0.3, d / 2 + 0.12, hh, hh + 1.0, "wall"))
        for s in range(1, n):                         # balconies on the upper floors
            z = s * st
            parts.append(box(cx, cx + hw / 2 - 0.4, -d / 2 - 1.1, -d / 2, z - 0.18, z, "trim"))
            parts.append(box(cx, cx + hw / 2 - 0.4, -d / 2 - 1.1, -d / 2 - 1.04, z, z + 1.0, "trim"))
        for x in (cx - hw / 2 + 0.6, cx + hw / 2 - 0.6):            # roof pergola
            for y in (-d / 2 + 1.5, -d / 2 + 5.5):
                parts.append(box(x - 0.1, x + 0.1, y - 0.1, y + 0.1, hh, hh + 2.4, "trim"))
        parts.append(box(cx - hw / 2 + 0.5, cx + hw / 2 - 0.5, -d / 2 + 1.4, -d / 2 + 5.6, hh + 2.4, hh + 2.55, "trim"))
    return parts


GENERATORS = {"hall_vault": hall_vault, "cube": cube, "basilica": basilica, "podium_tower": podium_tower,
              "perimeter": perimeter, "row": row}


# --- the brief: measures from the sheet view ---------------------------------------------

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
            "lum": round(float(c[i] @ [0.299, 0.587, 0.114]), 1)} for i in range(k)]
    return sorted(out, key=lambda x: -x["share"])


def plain_patch(a: np.ndarray, mask: np.ndarray, size: int = 48) -> list[int] | None:
    """The plainest square patch of building surface (lowest detail, typical tone, not a
    shadow): [x0, y0, x1, y1] in the view's pixels. Tries smaller squares when no clean one of
    `size` fits between the openings."""
    lum = a @ np.array([0.299, 0.587, 0.114])
    med = float(np.median(lum[mask]))
    H, W = mask.shape
    found = None
    for s in (size, size * 2 // 3, size // 2):
        s = min(s, H // 3, W // 3)
        best, bb = None, None
        for y in range(0, H - s, max(2, s // 8)):
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


def rhythm(a: np.ndarray, mask: np.ndarray, axis: int) -> float | None:
    """The dominant period (px) of the openings along an axis (1 = across: bays, 0 = down:
    storeys): the strongest autocorrelation peak of the facade's darkness profile (windows are
    dark), so a window's two edges don't count as two bays."""
    lum = a @ np.array([0.299, 0.587, 0.114])
    dark = np.where(mask, 255.0 - lum, 0.0)
    cnt = mask.sum(0 if axis == 1 else 1).astype(np.float64)
    prof = dark.sum(0 if axis == 1 else 1) / np.maximum(cnt, 1)
    prof = prof[cnt > 0.3 * cnt.max()]
    n = len(prof)
    if n < 40:
        return None
    prof = prof - ndimage.uniform_filter1d(prof, max(9, n // 4))
    ac = np.correlate(prof, prof, "full")[n - 1:]
    ac /= ac[0] + 1e-9
    peaks = [i for i in range(6, n // 2) if ac[i] > ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > 0.15]
    return float(max(peaks, key=lambda i: ac[i])) if peaks else None


def measure(view: Path, dims) -> dict:
    """What the sheet view says: palette, the plainest patch, and the bay / storey rhythm as
    counts at the brief's size (the view's width standing for the longer facade)."""
    a = np.asarray(Image.open(view).convert("RGB")).astype(np.float64)
    m = _object_mask(a)
    ys, xs = np.nonzero(m)
    out = {"palette": palette(a, m), "patch_px": plain_patch(a, m)}
    if len(xs):
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        sub = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
        bp, sp = rhythm(a[sub], m[sub], 1), rhythm(a[sub], m[sub], 0)
        bays = round(bw / bp) if bp else None
        storeys = round(bh / sp) if sp else None
        # only plausible rhythms count (three-quarter views and fine texture give noise)
        if bays and not 2.5 <= dims[0] / bays <= 15.0:
            bays = None
        if storeys and not 2.6 <= dims[2] / storeys <= 7.0:
            storeys = None
        out |= {"bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
                "bay_px": bp, "storey_px": sp, "bays": bays, "storeys": storeys,
                "bay_m": round(float(dims[0] / bays), 2) if bays else None,
                "storey_h": round(float(dims[2] / storeys), 2) if storeys else None}
    return out


def _plain(rgb, rough, metal=0.0):
    return {"uv": "plain", "color": [(c / 255.0) ** 2.2 for c in rgb], "rough": rough, "metal": metal}


def materials(store: ProjectStore, brief: dict, maps: Path) -> dict:
    """Wall and roof tiles from the view's plain patch; glass, trim and garden as plain
    materials in the palette's colours (glass: the darkest; trim: the dominant colour, darker,
    unless the brief names one, e.g. a bronze)."""
    view = store.root / brief["view"]
    meas = brief["measured"]
    x0, y0, x1, y1 = brief.get("patch_px") or meas["patch_px"]
    wall = kit_proc.tile_maps(view.parent, {"sheet": view.name, "box_px": [x0, y0, x1, y1]}, maps)
    pal = sorted(meas["palette"], key=lambda c: c["lum"])
    glass = brief.get("glass_rgb") or [min(int(c * 0.8), 255) for c in pal[0]["rgb"]]
    main = meas["palette"][0]["rgb"]                  # the dominant colour: stone trim reads as the wall's, darker
    trim = brief.get("trim_rgb") or [int(c * 0.78) for c in main]
    tm = brief.get("tile_m", 4.0)
    return {"wall": {**wall, "uv": "tile", "tile_m": tm}, "vault": {**wall, "uv": "tile", "tile_m": tm * 1.5},
            "roof": {**wall, "uv": "tile", "tile_m": tm * 2}, "glass": _plain(glass, 0.06, 0.0),
            "trim": _plain(trim, 0.45, brief.get("trim_metal", 0.0)), "garden": _plain([86, 104, 64], 0.9)}


def build(store: ProjectStore, asset: str, log=print) -> dict:
    """Measure (once) and build an asset's procedural building from its brief."""
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
    parts = GENERATORS[brief["form"]](brief["dims"], sheet | brief.get("params", {}))
    return kit_proc.run_blender(store, asset, parts, mats, d / "procedural.glb", maps, log=log,
                                extra={"form": brief["form"]})


def cut_view(store: ProjectStore, sheet: str, position: int, asset: str) -> str:
    """One of a sheet's three views (0 left, 1 middle, 2 right) as the brief's view: the
    building's bounds inside that third, on white (views/library/<asset>/<sheet>_p<pos>.png)."""
    im = Image.open(store.root / sheet).convert("RGB")
    a = np.asarray(im).astype(np.float64)
    W = im.width
    x0, x1 = W * position // 3, W * (position + 1) // 3
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


# catalog form -> generator (the forms with one so far)
FORM_OF = {"hall": "hall_vault", "cube": "cube", "podium_tower": "podium_tower", "tower": "podium_tower",
           "perimeter": "perimeter", "row": "row"}


def brief(store: ProjectStore, asset: str, sheet: str, position: int, form: str, dims, params=None) -> dict:
    """Write an asset's building brief from one view of its sheet (measured now)."""
    if form not in FORMS:
        raise ValueError(f"form must be one of {FORMS}")
    v = cut_view(store, sheet, position, asset)
    b = {"kind": "building", "form": form, "dims": [float(x) for x in dims], "sheet": sheet, "position": position,
         "view": v, "params": params or {}, "measured": measure(store.root / v, dims)}
    write_json(kit_proc.spec_path(store, asset), b)
    return b

