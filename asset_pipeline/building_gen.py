"""Generators for procedural buildings, one per catalog form, built from building_parts and a
brief's features (building_proc has the brief, the measures and the build).

TYPE_FEATURES holds what every building of a form gets: the parts its reference sheets show
(from the sheet reviews, 2026-10-10). A brief's `features` override them key by key (dicts merged
one level down), so a feature added here reaches every building of that type.
"""
import copy
import math

import numpy as np

from .building_parts import (FACADE, archivolt, balustrade, box, cornice, facade, height, lifted, merged, outline,
                             pierced, placed, planter, prism, ring, walls_around)

FORMS = ("hall_vault", "cube", "basilica", "podium_tower", "perimeter", "row", "civic_block", "arcade_hall", "campanile")

TYPE_FEATURES = {
    "civic_block": {
        "storey_h": 4.5, "ground_h": 6.8, "parapet": 1.2, "bays": 5, "colonnade": None,
        "front": {"ground": {"kind": "pilotis", "recess": 2.0, "column": "square", "column_d": 0.9},
                  "pattern": ["solid"], "cornice": [[0.3, 0.2]],
                  "recess": [{"x0": 0.3, "x1": 0.7, "depth": 0.0, "pattern": ["loggia"], "balcony": None}],
                  "loggia": {"w": 0.8, "h": 0.55, "sill": 0.15, "depth": 1.6, "balustrade": "glass"}},
        "side": {"ground": {"kind": "plain"}, "pattern": ["window"], "cornice": [[0.3, 0.2]],
                 "window": {"kind": "rect", "w": 0.3, "h": 0.4, "sill": 0.3, "reveal": 0.3, "frame": True}},
    },
    "arcade_hall": {
        "storey_h": 6.5, "bays": 5, "roof_steps": 4, "roof_step_m": 1.6, "roof_rise_m": 0.7,
        "range": None, "masses": None,
        "front": {"ground": {"kind": "arcade", "recess": 3.5, "archivolt": 0.35}, "pattern": ["window"],
                  "window": {"kind": "arch", "w": 0.38, "h": 0.6, "sill": 0.22, "reveal": 0.35, "frame": True},
                  "courses": {"every": 1, "h": 0.3, "proj": 0.12}, "cornice": [[0.4, 0.25], [0.3, 0.45]]},
        "side": {"ground": {"kind": "plain"}, "pattern": ["window"], "cornice": [[0.4, 0.25], [0.3, 0.45]],
                 "window": {"kind": "arch", "w": 0.35, "h": 0.55, "sill": 0.25, "reveal": 0.35, "frame": True}},
    },
    "campanile": {
        "storey_h": 6.5, "belfry_h": 7.0, "cap_h": 3.5, "cap_steps": 5, "belfry_arches": 2, "cap": "pyramid",
        "cap_over": 0.6,
        "shaft": {"ground": {"kind": "plain"}, "pattern": ["slot"], "slot": {"w": 0.1, "h": 0.4, "sill": 0.3},
                  "courses": {"every": 2, "h": 0.25, "proj": 0.1}},
        "belfry": {"ground": {"kind": "plain"}, "pattern": ["window"], "cornice": [[0.35, 0.3], [0.25, 0.5]],
                   "window": {"kind": "arch", "w": 0.6, "h": 0.7, "sill": 0.12, "reveal": 0.4, "frame": False}},
    },
    "hall_vault": {
        "base_h": 0.6, "vault_t": 0.5, "ribs": 0, "end_rim": {"w": 1.3, "proj": 0.6},
        "skylight": {"side": 1, "segs": [10, 12], "mullion_m": 2.2}, "ridge_cap": True,
        "end_screen": {"recess": 2.5, "mezzanine": 0.42, "fins": 1.6, "passage_w": 0.36, "pier": True},
        "stalls": {"rows": 2, "pitch": 3.2, "w": 2.6, "d": 1.4, "canopy": 2.5}, "apron": 2.5,
    },
    "cube": {
        "wall_t": 2.4, "plinth": [[0.45, 2.6], [0.45, 1.4]],
        "niche": {"faces": ["front", "back", "left", "right"], "w": 0.5, "h": 0.75, "rise": 0.2, "depth": 4.5,
                  "mezzanine": 0.46, "steps": 3, "door_w": 0.22, "screen": True, "railing": "rail",
                  "portal": {"kind": "open", "w": 0.3, "h": 7.0}},
        "rounds": None, "joints": {"cols": 4, "rows": 3, "groove": 0.12}, "atrium": 0.34,
    },
    "basilica": {
        "nave": 0.44, "aisle_h": 0.42, "gallery_h": 0.6,
        "aisle": {"ground": {"kind": "blind_arcade", "w": 0.62, "h": 0.8, "archivolt": 0.18}, "bay_m": 3.2,
                  "cornice": [[0.35, 0.2], [0.25, 0.4]]},
        "gallery": {"pattern": ["window"], "window": {"kind": "rect", "w": 0.4, "h": 0.45, "sill": 0.3, "frame": True},
                    "bay_m": 3.2, "recess": 1.5, "cornice": [[0.3, 0.2], [0.25, 0.35]]},
        "nave_wall": {"pattern": ["blind"], "blind": {"w": 0.62, "h": 0.6, "sill": 0.2, "depth": 0.2}, "bay_m": 4.4,
                      "attic": 2.2},
        "front": {"pylons": {"w": 0.13, "h": 0.86, "batter": 0.6}, "door": {"w": 0.42, "h": 0.5, "archivolt": 0.6},
                  "window": {"w": 0.12, "h": 0.12}, "oculus": 0.6, "side_doors": True, "attic_extra": 2.5},
        "apse": True,
    },
    "podium_tower": {
        "podium_storeys": 1, "tower_w": None, "storey_h": 3.4,
        "podium": {"ground": {"kind": "recessed", "recess": 1.5}, "cornice": [[0.4, 0.3]], "parapet": 0.0},
        "tower": {"pattern": ["slot", "window", "slot"],
                  "window": {"kind": "rect", "w": 0.5, "h": 0.6, "sill": 0.25, "reveal": 0.3, "frame": True, "mullions": 1},
                  "slot": {"w": 0.18, "h": 0.78, "sill": 0.1}, "courses": {"every": 2, "h": 0.22, "proj": 0.06},
                  "pilasters": {"w": 0.6, "proj": 0.1}, "cornice": [[0.3, 0.2]], "parapet": 0.0},
        "gardens": {"every": 2, "from": 2, "depth": 1.4, "drape": True, "faces": ["front", "right"]},
        "crown": {"inset": 1.2, "h": 3.6, "roof_over": 1.4}, "tower_faces": None, "corner_rise": 0.0,
    },
    "perimeter": {
        "storey_h": 3.4, "ground_h": 4.2, "wing": 14.0,
        "street": {"ground": {"kind": "arcade"}, "pattern": ["window"], "balcony": None},
        "street_side": None,
        "court": {"pattern": ["window"], "ground": {"kind": "plain"}, "cornice": [[0.3, 0.2]], "parapet": 0.9},
        "top": {"storeys": 1, "setback": 3.0, "facade": {"pattern": ["window"], "window": {"w": 0.55, "h": 0.68},
                "cornice": [[0.3, 0.25]], "parapet": 0.0}, "planters": True, "balustrade": "glass"},
        "accent": None, "court_garden": True, "street_garden": None,
    },
    "row": {
        "storey_h": 3.2, "houses": 4, "steps": [0, 1, -1, 0],
        "house": {"ground": {"kind": "plain"}, "pattern": ["window", "window"],
                  "window": {"kind": "rect", "w": 0.55, "h": 0.66, "sill": 0.18, "frame": True},
                  "balcony": {"kind": "slab", "depth": 1.1, "balustrade": "glass", "every": 1, "on": "window"},
                  "cornice": None, "parapet": 1.0},
        "roof": {"pergola": True, "planters": True}, "house_types": None, "gap": 0.0, "front_garden": None,
        "roof_box": None,
    },
}


def features(form: str, brief_feats: dict | None) -> dict:
    return merged(TYPE_FEATURES[form], brief_feats)


def _F(over: dict | None, **kw) -> dict:
    """A facade spec: the grammar's defaults, then kw, then the brief's values."""
    return merged(merged(FACADE, kw), over)


def hall_vault(dims, p, f) -> list[dict]:
    """A barrel-vaulted hall along X: a smooth shell rising from a low plinth, thick rims at the
    open ends, recessed end screens (a mezzanine slab, vertical fins above, open passages either
    side of a central pier), a framed glazed strip near the crown, a ridge cap, two rows of stalls
    inside, an apron."""
    w, d, h = dims
    base, t, seg = f["base_h"], f["vault_t"], 18
    a, b = d / 2, h - base                              # the ellipse: half-span, rise
    parts = [box(-w / 2 - f["apron"], w / 2 + f["apron"], -d / 2 - 1.0, d / 2 + 1.0, -0.3, 0.0, "base"),
             box(-w / 2, w / 2, -d / 2 - 0.3, -d / 2 + t, 0.0, base, "base"),
             box(-w / 2, w / 2, d / 2 - t, d / 2 + 0.3, 0.0, base, "base")]

    def arc(k, ra, rb):
        th = math.pi * k / seg
        return [-ra * math.cos(th), base + rb * math.sin(th)]

    sky = f.get("skylight")
    gl = set()
    if sky:
        lo, hi = sky["segs"]
        gl = set(range(lo, hi)) if sky.get("side", 1) > 0 else set(range(seg - hi, seg - lo))
    nb = max(1, round(w / (sky["mullion_m"] if sky else 6.0)))
    for i in range(nb):
        x0, x1 = -w / 2 + w * i / nb, -w / 2 + w * (i + 1) / nb
        for k in range(seg):
            q = [arc(k, a, b), arc(k + 1, a, b), arc(k + 1, a + t, b + t), arc(k, a + t, b + t)]
            if k in gl:
                parts += placed([prism(q, x0 + 0.08, x1 - 0.08, "glass"), prism(q, x0, x0 + 0.08, "frame"),
                                 prism(q, x1 - 0.08, x1, "frame")], 90)
            else:
                parts += placed([prism(q, x0, x1, "vault")], 90)
    if f.get("ridge_cap"):
        parts.append(box(-w / 2, w / 2, -0.5, 0.5, base + b + t - 0.05, base + b + t + 0.3, "trim"))
    for i in range(1, f.get("ribs", 0)):
        xr = -w / 2 + w * i / f["ribs"]
        for k in range(seg):
            q = [arc(k, a - 0.2, b - 0.2), arc(k + 1, a - 0.2, b - 0.2), arc(k + 1, a + t + 0.25, b + t + 0.25),
                 arc(k, a + t + 0.25, b + t + 0.25)]
            parts += placed([prism(q, xr - 0.25, xr + 0.25, "trim")], 90)
    rim, es = f.get("end_rim"), f.get("end_screen")
    for sx in (-1, 1):
        xe = sx * w / 2
        if rim:
            for k in range(seg):
                q = [arc(k, a - 0.15, b - 0.15), arc(k + 1, a - 0.15, b - 0.15), arc(k + 1, a + rim["w"], b + rim["w"]),
                     arc(k, a + rim["w"], b + rim["w"])]
                y0, y1 = (xe - 0.4, xe + rim["proj"]) if sx > 0 else (xe - rim["proj"], xe + 0.4)
                parts += placed([prism(q, y0, y1, "vault")], 90)
        if es:
            xs_ = xe - sx * es["recess"]
            zm = base + es["mezzanine"] * b
            pw = es["passage_w"] * d
            cxs = (-d / 4 - 0.4, d / 4 + 0.4) if es.get("pier") else (0.0,)
            ops = [{"kind": "rect", "cx": cx, "w": pw, "z0": 0.0, "h": zm - 0.4, "glass": False} for cx in cxs]
            screen = pierced(-a, a, 0.0, lambda y: base + b * math.sqrt(max(0.0, 1 - (y / a) ** 2)) + 0.05, 0.0, 0.5,
                             ops, "wall")
            rz = -90 if sx < 0 else 90
            parts += placed(screen, rz, (xs_, 0, 0))
            out = es["recess"] - 0.4
            parts.append(box(xs_, xs_ + sx * out, -a * 0.9, a * 0.9, zm - 0.45, zm, "trim"))
            parts += placed(balustrade(-a * 0.82, a * 0.82, 0.0, zm, "solid", 1.0), rz, (xs_ + sx * out, 0, 0))
            n = int(1.6 * a / es["fins"])
            for i in range(1, n):
                y = -0.8 * a + 1.6 * a * i / n
                zt = base + b * math.sqrt(max(0.0, 1 - (y / a) ** 2)) - 0.3
                if zt > zm + 1.0:
                    parts.append(box(xs_, xs_ + sx * 0.55, y - 0.18, y + 0.18, zm, zt, "wall"))
    st = f.get("stalls")
    if st:
        L = w - 2 * (es["recess"] if es else 0) - 4.0
        n = max(1, int(L / st["pitch"]))
        for r in range(st["rows"]):
            y = (-1 if r % 2 == 0 else 1) * (d / 4)
            back = y + (st["d"] / 2 if y > 0 else -st["d"] / 2)
            for i in range(n):
                x = -L / 2 + st["pitch"] * (i + 0.5)
                sw, sd = st["w"], st["d"]
                parts += [box(x - sw / 2, x + sw / 2, y - sd / 2, y + sd / 2, 0.0, 1.0, "wood"),
                          box(x - sw / 2, x + sw / 2, back - 0.08, back + 0.08, 0.0, st["canopy"], "wood"),
                          box(x - sw / 2 - 0.15, x + sw / 2 + 0.15, y - sd / 2 - 0.4, y + sd / 2 + 0.4, st["canopy"],
                              st["canopy"] + 0.12, "trim")]
    return parts


def cube(dims, p, f) -> list[dict]:
    """A monolithic cube: deep walls with panel joints; on each listed face one great segmental
    niche (a mezzanine slab across it, steps up into it, a door and a lattice-screened arched
    window in its back wall); optional round openings; a stepped plinth; a skylit atrium."""
    w, d, h = dims
    t = f["wall_t"]
    z = 0.0
    parts = []
    for hh, over in f["plinth"]:
        parts.append(box(-w / 2 - over, w / 2 + over, -d / 2 - over, d / 2 + over, z, z + hh, "base"))
        z += hh
    zp, H = z, h - z
    ni = f.get("niche")
    portals = {}
    for side, rz, length, at in (("front", 0, w, (0, -d / 2, zp)), ("back", 180, w, (0, d / 2, zp)),
                                 ("left", -90, d - 2 * t, (-w / 2, 0, zp)), ("right", 90, d - 2 * t, (w / 2, 0, zp))):
        ops, extra = [], []
        if ni and side in ni["faces"]:
            nw = ni["w"] * (w if side in ("front", "back") else d)
            nh = ni["h"] * H
            o = {"kind": "seg", "cx": 0.0, "w": nw, "z0": 0.0, "h": nh, "rise": ni["rise"] * nw, "glass": False}
            ops.append(o)
            dep = ni["depth"]
            head = outline(o)[2:]
            for (xa, za), (xb, zb) in zip(head, head[1:]):            # the niche's ceiling, under its head
                extra.append(prism([[xa, za], [xb, zb], [xb, zb + 0.6], [xa, za + 0.6]], t, dep, "wall"))
            extra += [box(-nw / 2 - 0.6, -nw / 2, t, dep, 0.0, nh, "wall"), box(nw / 2, nw / 2 + 0.6, t, dep, 0.0, nh, "wall")]
            zm = ni["mezzanine"] * nh
            po = ni.get("portal") or {"kind": "door"}
            pw = po.get("w", 0.3) * nw
            ph = min(po.get("h", 7.0), zm - 0.8)
            if po["kind"] in ("open", "glazed"):         # a tall portal under the mezzanine
                back_ops = [{"kind": "rect", "cx": 0.0, "w": pw, "z0": 0.0, "h": ph, "glass": po["kind"] == "glazed"}]
            else:                                        # doors stay human: at most 2.6 x 3.0 m (a double door)
                back_ops = [{"kind": "rect", "cx": 0.0, "w": min(ni["door_w"] * nw, 2.6), "z0": 0.0, "h": min(zm - 0.6, 3.0)}]
            if ni.get("screen"):
                back_ops.append({"kind": "arch", "cx": 0.0, "w": 0.4 * nw, "z0": zm + 0.8, "h": 0.6 * (nh - zm)})
            extra += pierced(-nw / 2, nw / 2, 0.0, lambda x, o=o: max(_head(o, x), 0.0), dep, dep + 0.6, back_ops,
                             "wall", inset=0.3)
            if ni.get("screen"):
                so = back_ops[-1]
                for k in range(1, 6):
                    xx = -so["w"] / 2 + so["w"] * k / 6
                    extra.append(box(xx - 0.06, xx + 0.06, dep + 0.05, dep + 0.2, so["z0"], so["z0"] + so["h"] - 0.3, "frame"))
                for k in range(1, 5):
                    zz = so["z0"] + (so["h"] - so["w"] / 2) * k / 5
                    extra.append(box(-so["w"] / 2, so["w"] / 2, dep + 0.05, dep + 0.2, zz - 0.06, zz + 0.06, "frame"))
            if po["kind"] == "glazed":                   # mullions, a transom at door height, two door leaves
                y_ = dep + 0.3
                n_m = max(2, round(pw / 1.5))
                for k in range(n_m + 1):
                    xx = -pw / 2 + pw * k / n_m
                    extra.append(box(xx - 0.07, xx + 0.07, y_ - 0.08, y_ + 0.08, 0.0, ph, "frame"))
                for zz in (2.7, ph - 0.1):
                    extra.append(box(-pw / 2, pw / 2, y_ - 0.08, y_ + 0.08, zz - 0.08, zz + 0.08, "frame"))
                for sx in (-1, 1):
                    extra += [box(sx * 0.02, sx * 1.22, y_ - 0.12, y_ - 0.06, 0.0, 2.6, "frame")]
            extra.append(box(-nw / 2, nw / 2, -0.8, dep, zm - 0.35, zm, "trim"))       # the mezzanine slab
            extra += balustrade(-nw / 2 + 0.2, nw / 2 - 0.2, -0.75, zm, ni.get("railing", "rail"), 1.05)
            portals[side] = (pw, ph, dep + 0.6)
            if ni.get("steps"):                  # a flight from the ground up to the plinth top, in front of the niche,
                over = max(o_ for _, o_ in f["plinth"])          # at human scale (15 cm rise, 38 cm tread); no step
                y_edge = -min(o_ for _, o_ in f["plinth"])       # shares a plane with a plinth tier
                n_st = max(1, round(zp / 0.15))
                rise = zp / n_st
                for k in range(1, n_st):
                    top = -rise * k
                    if any(abs((top + zp) - zz) < 0.02 for zz in np.cumsum([h_ for h_, _ in f["plinth"]])):
                        top -= 0.01
                    extra.append(box(-nw / 2 + 0.3, nw / 2 - 0.3, y_edge - 0.38 * k, y_edge - 0.38 * (k - 1), -zp, top, "base"))
                del over
        if f.get("rounds"):
            r = f["rounds"]["r"] * H
            for sx in (-1, 1):
                ops.append({"kind": "round", "cx": sx * f["rounds"]["x"] * length, "cz": f["rounds"]["z"] * H, "r": r})
        wall = pierced(-length / 2, length / 2, 0.0, H, 0.0, t, ops, "wall", inset=0.8 * t)
        jt = f.get("joints")
        if jt:                                          # panel joints: so many panels across and up each face
            rows_ = jt.get("rows") or max(1, int(H / jt.get("panel_h", 3.2)))
            g_ = jt.get("groove", 0.12) / 2
            for k in range(1, rows_):
                zz = H * k / rows_
                wall.append(box(-length / 2, length / 2, -0.03, 0.0, zz - g_, zz + g_, "frame"))
            nj = jt.get("cols") or max(1, int(length / jt.get("panel_w", 4.0)))
            for k in range(1, nj):
                xx = -length / 2 + k * length / nj
                z_from = 0.0
                for o in ops:
                    if o["kind"] != "seg":
                        continue
                    hw_ = o["w"] / 2
                    if abs(abs(xx - o["cx"]) - hw_) < 0.7:          # on a jamb: just outside it, the full height
                        xx = o["cx"] + (hw_ + g_ + 0.12) * (1 if xx >= o["cx"] else -1)
                    elif abs(xx - o["cx"]) < hw_:                   # over the niche: down to its head
                        z_from = max(z_from, _head(o, xx) + 0.8)
                if z_from < H - 0.5:
                    wall.append(box(xx - g_, xx + g_, -0.03, 0.0, z_from, H, "frame"))
        parts += placed(wall + extra, rz, at)
    if ni and (ni.get("portal") or {}).get("kind") == "open":
        # an open passage runs through to the opposite face's niche (daylight at its end); where the
        # two cross, one ceiling covers the crossing and the walls stop at its edges
        pairs = [(("front", "back"), "y"), (("left", "right"), "x")]
        cross = {ax: portals[a_][0] for (a_, b_), ax in pairs if a_ in portals and b_ in portals}
        for (a_, b_), ax in pairs:
            if a_ not in portals or b_ not in portals:
                continue
            pw, ph, rec = portals[a_]
            half = (d / 2 if ax == "y" else w / 2) - rec
            other = cross.get("x" if ax == "y" else "y")
            runs = [(-half, -other / 2), (other / 2, half)] if other else [(-half, half)]
            for r0, r1 in runs:
                for sx in (-1, 1):
                    xa, xb = sx * pw / 2, sx * (pw / 2 + 0.5)
                    parts.append(box(xa, xb, r0, r1, zp, zp + ph, "wall") if ax == "y" else box(r0, r1, xa, xb, zp, zp + ph, "wall"))
                parts.append(box(-pw / 2 - 0.5, pw / 2 + 0.5, r0, r1, zp + ph, zp + ph + 0.6, "wall") if ax == "y"
                             else box(r0, r1, -pw / 2 - 0.5, pw / 2 + 0.5, zp + ph, zp + ph + 0.6, "wall"))
            if other and ax == "y":
                parts.append(box(-pw / 2 - 0.5, pw / 2 + 0.5, -other / 2, other / 2, zp + ph + 0.01, zp + ph + 0.61, "wall"))
    a = f["atrium"] * min(w, d)
    for x0, x1, y0, y1 in ((-w / 2, w / 2, -d / 2, -a / 2), (-w / 2, w / 2, a / 2, d / 2),
                           (-w / 2, -a / 2, -a / 2, a / 2), (a / 2, w / 2, -a / 2, a / 2)):
        parts.append(box(x0, x1, y0, y1, h - 0.8, h, "roof"))
    parts.append(box(-a / 2, a / 2, -a / 2, a / 2, h - 0.3, h - 0.2, "glass"))
    parts += ring(w, d, 0.6, h, h + 0.5, "trim")
    return parts


def _head(o, x):
    from .building_parts import _span
    s = _span(o, x)
    return s[1] if s else o["z0"] + o["h"] - o.get("rise", 0)


def basilica(dims, p, f) -> list[dict]:
    """A civic basilica along X in three tiers: aisles with a dense blind arcade, a set-back
    gallery storey with square windows, a tall nave with blind panels and a panelled attic band;
    an entrance front at -X with battered pylons, a tall central block, an arched door with an
    archivolt, a small window and an oculus, arched side doors; an apse with a half dome at +X."""
    w, d, h = dims
    nw = f["nave"] * d
    ha, hg = f["aisle_h"] * h, f["gallery_h"] * h
    apse_r = nw / 2 if f.get("apse") else 0.0
    L = w - apse_r
    x0 = -w / 2
    xc = x0 + L / 2
    parts = []
    gr = f["gallery"]["recess"]
    Fa = _F(f["aisle"], storeys=1, ground_h=ha, thickness=0.9, parapet=0.0)
    Fg = _F({k: v for k, v in f["gallery"].items() if k != "recess"}, storeys=1, ground_h=hg - ha - 0.6, thickness=0.6,
            parapet=0.0)
    Fg["ground"] = {"kind": "plain"}
    attic = f["nave_wall"]["attic"]
    Fn = _F({k: v for k, v in f["nave_wall"].items() if k != "attic"}, storeys=2, ground_h=hg,
            storey_h=h - hg - attic, thickness=0.9, parapet=0.0, cornice=None)
    Fn["ground"] = {"kind": "plain"}
    for side in (-1, 1):
        rz = 0 if side < 0 else 180
        parts += placed(facade(L, Fa), rz, (xc, side * d / 2, 0))
        parts.append(box(x0, x0 + L, side * d / 2, side * (nw / 2), ha - 0.5, ha, "roof"))
        gal = [q for q in facade(L, Fg) if not (q["kind"] == "prism" and max(z for _, z in q["xz"]) <= Fg["ground_h"] + 1e-6)]
        parts += placed(lifted(gal, ha + 0.6 - Fg["ground_h"]), rz, (xc, side * (d / 2 - gr), 0))
        parts.append(box(x0, x0 + L, side * (d / 2 - gr), side * (nw / 2), hg - 0.5, hg, "roof"))
        nave = [q for q in facade(L, Fn) if not (q["kind"] == "prism" and max(z for _, z in q["xz"]) <= hg + 1e-6)]
        nave += [box(-L / 2, L / 2, 0.0, 0.9, h - attic, h, "wall")]
        for k in range(int(L / 6)):                       # the attic band's recessed panels
            xx = -L / 2 + 6 * (k + 0.5)
            nave.append(box(xx - 2.2, xx + 2.2, -0.08, 0.0, h - attic + 0.4, h - 0.4, "trim"))
        nave += cornice(L, h, [[0.35, 0.25], [0.3, 0.5]])
        parts += placed(nave, rz, (xc, side * nw / 2, 0))
    parts.append(box(x0, x0 + L, -nw / 2, nw / 2, h - 0.8, h, "roof"))
    fr = f["front"]
    hf = h + fr["attic_extra"]
    di, ni_ = d / 2 - 0.9, nw / 2
    door = {"kind": "arch", "cx": 0.0, "w": fr["door"]["w"] * nw, "z0": 0.0, "h": fr["door"]["h"] * h}
    win = {"kind": "arch", "cx": 0.0, "w": max(1.0, fr["window"]["w"] * nw), "z0": door["h"] + 2.0,
           "h": fr["window"]["h"] * h + 1.2}
    ocu = {"kind": "round", "cx": 0.0, "cz": win["z0"] + win["h"] + 1.6, "r": fr["oculus"]}
    front = pierced(-ni_, ni_, 0.0, hf, 0.0, 2.0, [door, win, ocu], "wall", inset=1.4)
    if fr["door"].get("archivolt"):
        front += archivolt(door, fr["door"]["archivolt"], 0.0, 0.25)
    front += cornice(nw, hf, [[0.4, 0.3], [0.3, 0.5]])
    for sx in (-1, 1):
        a_, b_ = sorted((sx * ni_, sx * di))
        ops = [{"kind": "arch", "cx": (a_ + b_) / 2, "w": 0.3 * (b_ - a_), "z0": 0.0, "h": 0.55 * ha}] if fr.get("side_doors") else []
        front += pierced(a_, b_, 0.0, ha, 0.6, 1.5, ops, "wall", inset=0.6)
        a2, b2 = sorted((sx * ni_, sx * (di - gr)))
        front += pierced(a2, b2, ha, hg, 0.6 + gr, 1.5 + gr, [], "wall")
    py = fr["pylons"]
    pw, bt = py["w"] * d, py["batter"]
    for sx in (-1, 1):
        xa = sx * (ni_ + pw / 2)
        front.append(prism([[xa - pw / 2, 0.0], [xa + pw / 2, 0.0], [xa + pw / 2 - bt, py["h"] * h],
                            [xa - pw / 2 + bt, py["h"] * h]], -1.2, 2.0, "wall"))
        front.append(box(xa - pw / 2 + bt - 0.2, xa + pw / 2 - bt + 0.2, -1.4, 2.2, py["h"] * h, py["h"] * h + 0.6, "trim"))
    parts += placed(front, -90, (x0, 0, 0))
    back = pierced(-di, di, 0.0, ha, 0.0, 0.9, [], "wall") + pierced(-ni_, ni_, ha, h, 0.0, 0.9, [], "wall")
    parts += placed(back, 90, (x0 + L, 0, 0))
    if apse_r:
        hh = 0.8 * h
        parts.append({"kind": "shell", "x": x0 + L, "y": 0.0, "z": 0.0, "a0": -90, "a1": 90, "seg": 24, "mat": "wall",
                      "rows": [[apse_r, 0.0, apse_r - 0.9, 0.0], [apse_r, hh * 0.62, apse_r - 0.9, hh * 0.62]]})
        rows = [[apse_r * math.cos(fi), hh * 0.62 + apse_r * 0.7 * math.sin(fi), (apse_r - 0.9) * math.cos(fi),
                 hh * 0.62 + (apse_r - 0.9) * 0.7 * math.sin(fi)] for fi in np.linspace(0, math.radians(84), 9)]
        parts.append({"kind": "shell", "x": x0 + L, "y": 0.0, "z": 0.0, "a0": -90, "a1": 90, "seg": 24, "mat": "roof",
                      "rows": rows})
    return parts


def podium_tower(dims, p, f) -> list[dict]:
    """A slender tower on a low base: the base recessed and glazed, the tower's faces in their
    bay pattern (slot windows either side of framed windows), stone courses every few storeys,
    corner strips, hanging gardens (planted balconies, planting draping over their edges) on
    alternating storeys of the listed faces, a glass crown under an overhanging roof."""
    w, d, h = dims
    st = p.get("storey_h", f["storey_h"])
    tw = f["tower_w"] or min(22.0, 0.78 * d)
    Fp = _F(f["podium"], storeys=f["podium_storeys"], storey_h=st, ground_h=st + 0.8, bay_m=6.0, thickness=0.5)
    ph = height(Fp)
    parts = walls_around(w, d, {"front": Fp})
    parts += [box(-w / 2, w / 2, -d / 2, d / 2, ph - 0.4, ph, "roof")] + ring(w + 0.6, d + 0.6, 1.3, ph, ph + 0.35, "trim")
    cr = f.get("crown")
    n = max(4, round((h - ph - (cr["h"] if cr else 0)) / st))
    Ft = _F(f["tower"], storeys=n, storey_h=st, ground_h=st, bay_m=p.get("bay_m", 3.4), thickness=0.45)
    Ft["ground"] = {"kind": "plain"}
    Ft["pilasters"] = None
    gd = f.get("gardens")
    faces = {}
    for side in ("front", "back", "left", "right"):
        F = merged(Ft, (f.get("tower_faces") or {}).get(side))
        if gd and side in gd["faces"]:
            F["balcony"] = {"kind": "slab", "depth": gd["depth"], "balustrade": "glass", "planters": True,
                            "drape": gd.get("drape"), "every": gd["every"], "from": gd["from"], "on": "window"}
        faces[side] = F
    parts += lifted(walls_around(tw, tw, faces), ph)
    th = ph + height(Ft)
    pil = f["tower"].get("pilasters")
    if pil:                                              # corner strips, rising above the roof if asked
        pw, pj = pil["w"], pil["proj"]
        zt = th + f.get("corner_rise", 0.0)
        for sx in (-1, 1):
            for sy in (-1, 1):
                parts.append(box(sx * tw / 2, sx * (tw / 2 - pw), sy * tw / 2, sy * (tw / 2 + pj), ph, zt, "wall"))
                parts.append(box(sx * tw / 2, sx * (tw / 2 + pj), sy * tw / 2, sy * (tw / 2 - pw), ph, zt, "wall"))
    parts.append(box(-tw / 2, tw / 2, -tw / 2, tw / 2, th - 0.4, th, "roof"))
    if cr:
        cw = tw - 2 * cr["inset"]
        e = cw / 2
        parts += [box(-e, e, -e, -e + 0.06, th, th + cr["h"], "glass"), box(-e, e, e - 0.06, e, th, th + cr["h"], "glass"),
                  box(-e, -e + 0.06, -e, e, th, th + cr["h"], "glass"), box(e - 0.06, e, -e, e, th, th + cr["h"], "glass")]
        for k in range(9):
            u = -e + cw * k / 8
            for xa, xb, ya, yb in ((u - 0.05, u + 0.05, -e - 0.05, -e + 0.1), (u - 0.05, u + 0.05, e - 0.1, e + 0.05),
                                   (-e - 0.05, -e + 0.1, u - 0.05, u + 0.05), (e - 0.1, e + 0.05, u - 0.05, u + 0.05)):
                parts.append(box(xa, xb, ya, yb, th, th + cr["h"], "frame"))
        ro = cr["roof_over"]
        parts.append(box(-e - ro, e + ro, -e - ro, e + ro, th + cr["h"], th + cr["h"] + 0.35, "frame"))
        for rz, at in ((0, (0, -tw / 2, th)), (180, (0, tw / 2, th)), (-90, (-tw / 2, 0, th)), (90, (tw / 2, 0, th))):
            parts += placed(balustrade(-tw / 2 + 0.2, tw / 2 - 0.2, 0.1, 0.0, "glass")
                            + planter(-tw / 2 + 0.8, tw / 2 - 0.8, 0.35, 0.95, 0.0, seed=rz % 5), rz, at)
    return parts


def perimeter(dims, p, f) -> list[dict]:
    """A perimeter block: four wings around a courtyard. Street facades (front/back and sides
    can differ) and courtyard facades in the grammar; recessed segments; a set-back top storey
    ring behind a glazed terrace with planters; an accent volume rising above the middle of the
    street fronts; a planted courtyard."""
    w, d, h = dims
    st = p.get("storey_h", f["storey_h"])
    gh = f["ground_h"]
    top = f.get("top")
    k = top["storeys"] if top else 0
    n = max(2, round((h - gh) / st) + 1)
    n_main = n - k
    wing = f["wing"]
    bay = p.get("bay_m", 4.5)
    Fs = _F(f["street"], storeys=n_main, storey_h=st, ground_h=gh, bay_m=bay)
    Fside = _F(f["street_side"], storeys=n_main, storey_h=st, ground_h=gh, bay_m=bay) if f.get("street_side") else Fs
    if top:
        Fs["parapet"] = 0.0
        Fside["parapet"] = 0.0
    parts = walls_around(w, d, {"front": Fs, "back": Fs, "left": Fside, "right": Fside})
    H = height(Fs)
    cw, cd = w - 2 * wing, d - 2 * wing
    Fc = _F(f["court"], storeys=n, storey_h=st, ground_h=gh, bay_m=bay)
    t = Fc["thickness"]
    for rz, length, at in ((180, cw + 2 * t, (0, -cd / 2, 0)), (0, cw + 2 * t, (0, cd / 2, 0)),
                           (90, cd, (-cw / 2, 0, 0)), (-90, cd, (cw / 2, 0, 0))):
        parts += placed(facade(length, Fc), rz, at)
    for x0, x1, y0, y1 in ((-w / 2, w / 2, -d / 2, -cd / 2 - t), (-w / 2, w / 2, cd / 2 + t, d / 2),
                           (-w / 2, -cw / 2 - t, -cd / 2 - t, cd / 2 + t), (cw / 2 + t, w / 2, -cd / 2 - t, cd / 2 + t)):
        parts.append(box(x0, x1, y0, y1, H - 0.45, H, "roof"))
    if top:
        s = top["setback"]
        Ft = _F(top["facade"], storeys=k, storey_h=st, ground_h=st, bay_m=bay)
        Ft["ground"] = {"kind": "plain"}
        Ft["mat"] = {"ground": "top", "upper": "top"}
        Ht = H + height(Ft)
        c = top.get("corners")
        span = top.get("span")
        if span:                                         # an attic over the middle of the street fronts only;
            aw_ = span * w                               # the wings end in planted roof terraces
            ad_ = wing - s
            for sy, rz in ((-1, 0), (1, 180)):
                ya = sy * (d / 2 - s - ad_ / 2)
                Fat = _F(top["facade"], storeys=k, storey_h=st, ground_h=st, bay_m=bay)
                Fat["ground"] = {"kind": "plain"}
                Fat["mat"] = {"ground": "top", "upper": "top"}
                vol = walls_around(aw_, ad_, {"front": Fat})
                parts += placed(lifted(vol, H), rz, (0, ya, 0))
                parts.append(box(-aw_ / 2, aw_ / 2, ya - ad_ / 2, ya + ad_ / 2, Ht - 0.45, Ht, "roof"))
                parts += placed(ring(aw_ + 0.4, ad_ + 0.4, 0.5, Ht, Ht + 0.3, "trim"), 0, (0, ya, 0))
            for i, (x0, x1, y0, y1) in enumerate(((-w / 2 + 2, -aw_ / 2 - 2, -d / 2 + 2, -d / 2 + wing - 2),
                                                  (aw_ / 2 + 2, w / 2 - 2, -d / 2 + 2, -d / 2 + wing - 2),
                                                  (-w / 2 + 2, -aw_ / 2 - 2, d / 2 - wing + 2, d / 2 - 2),
                                                  (aw_ / 2 + 2, w / 2 - 2, d / 2 - wing + 2, d / 2 - 2))):
                if x1 - x0 > 2:                          # terrace beds with shrubs between paths
                    parts.append(box(x0, x1, y0 + 1.5, y1 - 1.5, H, H + 0.35, "planting"))
                    parts += planter(x0, x1, y0, y0 + 0.9, H, h=0.5, green=0.6, seed=i)
        elif c:                                          # attic volumes at the corners only
            for sx in (-1, 1):
                for sy in (-1, 1):
                    cx_, cy_ = sx * (w / 2 - s - c / 2), sy * (d / 2 - s - c / 2)
                    parts += placed(lifted(walls_around(c, c, {"front": Ft}), H), 0, (cx_, cy_, 0))
                    parts.append(box(cx_ - c / 2, cx_ + c / 2, cy_ - c / 2, cy_ + c / 2, Ht - 0.45, Ht, "roof"))
                    parts += placed(ring(c + 0.4, c + 0.4, 0.5, Ht, Ht + 0.3, "trim"), 0, (cx_, cy_, 0))
        else:
            parts += lifted(walls_around(w - 2 * s, d - 2 * s, {"front": Ft}), H)
            for x0, x1, y0, y1 in ((-w / 2 + s, w / 2 - s, -d / 2 + s, -cd / 2 - t), (-w / 2 + s, w / 2 - s, cd / 2 + t, d / 2 - s),
                                   (-w / 2 + s, -cw / 2 - t, -cd / 2 - t, cd / 2 + t), (cw / 2 + t, w / 2 - s, -cd / 2 - t, cd / 2 + t)):
                parts.append(box(x0, x1, y0, y1, Ht - 0.45, Ht, "roof"))
            parts += ring(w - 2 * s + 0.5, d - 2 * s + 0.5, 0.7, Ht, Ht + 0.3, "trim")
        for rz, length, at in ((0, w, (0, -d / 2, H)), (180, w, (0, d / 2, H)), (-90, d, (-w / 2, 0, H)), (90, d, (w / 2, 0, H))):
            edge = balustrade(-length / 2 + 0.3, length / 2 - 0.3, 0.05, 0.0, top.get("balustrade", "glass"))
            if top.get("planters"):
                edge += planter(-length / 2 + 1.0, length / 2 - 1.0, 0.4, 1.2, 0.0, seed=int(rz) % 7)
            parts += placed(edge, rz, at)
    acc = f.get("accent")
    if acc:
        aw = acc["w"] * w
        rec = acc.get("recess", 0.0)
        ad = wing - rec
        Fa = _F(acc.get("facade"), storeys=n + acc["extra"], storey_h=st, ground_h=gh, bay_m=acc.get("bay_m", bay))
        Fa["ground"] = merged(Fs["ground"], (acc.get("facade") or {}).get("ground"))
        Fsolid = _F({"pattern": ["solid"]}, storeys=n + acc["extra"], storey_h=st, ground_h=gh, bay_m=bay, cornice=None,
                    parapet=0.0)
        Fsolid["ground"] = {"kind": "plain"}
        for sy, rz in ((-1, 0), (1, 180)):
            vol = walls_around(aw, ad, {"front": Fa, "back": Fsolid, "left": Fsolid, "right": Fsolid})
            ya = sy * (d / 2 - rec - ad / 2)
            parts += placed(vol, rz, (0, ya, 0))
            Ha = height(Fa)
            parts.append(box(-aw / 2, aw / 2, ya - ad / 2, ya + ad / 2, Ha - 0.45, Ha, "roof"))
    sg = f.get("street_garden")
    if sg:                                               # low planted garden walls in front of the street fronts
        for rz, length, at in ((0, w, (0, -d / 2, 0)), (180, w, (0, d / 2, 0))):
            g = []
            for a_, b_ in ((-length / 2 + 2, -sg.get("gap", 6) / 2), (sg.get("gap", 6) / 2, length / 2 - 2)):
                g.append(box(a_, b_, -sg["depth"] - 0.3, -sg["depth"], 0.0, sg["h"], "base"))
                g += planter(a_ + 0.3, b_ - 0.3, -sg["depth"] + 0.1, -0.6, 0.0, h=0.4, green=0.5, seed=int(a_) % 5)
            parts += placed(g, rz, at)
    if f.get("court_garden"):
        parts.append(box(-cw / 2, cw / 2, -cd / 2, cd / 2, -0.2, 0.05, "base"))
        for i, (x0, x1, y0, y1) in enumerate(((-cw / 2 + 2, -2, -cd / 2 + 2, -2), (2, cw / 2 - 2, -cd / 2 + 2, -2),
                                              (-cw / 2 + 2, -2, 2, cd / 2 - 2), (2, cw / 2 - 2, 2, cd / 2 - 2))):
            parts.append(box(x0, x1, y0, y1, 0.05, 0.25, "planting"))
            parts += planter(x0, x1, y0, y0 + 0.8, 0.05, h=0.45, seed=i)
    return parts


def row(dims, p, f) -> list[dict]:
    """A row of narrow attached houses along X: each its own storey count (stepping), a street
    facade in the grammar (door, windows, balconies), a plainer back, party walls standing above
    the roofs, a parapet roof terrace with planters and a pergola."""
    w, d, h = dims
    st = p.get("storey_h", f["storey_h"])
    k = f["houses"]
    hw = w / k
    base_n = max(2, round(h / st))
    steps = f["steps"]
    parts = []
    gap = f.get("gap", 0.0)
    types = f.get("house_types") or [{}]
    for i in range(k):
        n = max(2, base_n + steps[i % len(steps)])
        house = merged(f["house"], types[i % len(types)])
        fw = hw - gap - 0.3                              # the house's own street front
        Fh = _F(house, storeys=n, storey_h=st, bay_m=fw / len(house["pattern"]))
        hh = height(Fh)
        cx = -w / 2 + hw * (i + 0.5)
        parts += placed(facade(fw, Fh), 0, (cx, -d / 2, 0))
        Fb = _F({"pattern": ["window"], "window": Fh["window"], "mat": Fh["mat"]}, storeys=n, storey_h=st,
                bay_m=fw / 2, cornice=None, parapet=1.0)
        parts += placed(facade(fw, Fb), 180, (cx, d / 2, 0))
        ex = fw / 2 + 0.15
        for sx in (-1, 1):                               # party (or, with a gap, gable) walls
            parts.append(box(cx + sx * ex - 0.15, cx + sx * ex + 0.15, -d / 2, d / 2, 0, hh + 1.0, "wall"))
        parts.append(box(cx - ex, cx + ex, -d / 2 + 0.5, d / 2 - 0.5, hh - 0.4, hh, "roof"))
        fg = f.get("front_garden")
        if fg:                                           # a low wall with a gate opening and planting behind it
            gx = cx - fw / 4
            parts += [box(cx - hw / 2 + 0.1, gx - 0.7, -d / 2 - fg["depth"] - 0.25, -d / 2 - fg["depth"], 0, fg["h"], "base"),
                      box(gx + 0.7, cx + hw / 2 - 0.1, -d / 2 - fg["depth"] - 0.25, -d / 2 - fg["depth"], 0, fg["h"], "base")]
            parts += planter(gx + 0.9, cx + hw / 2 - 0.4, -d / 2 - fg["depth"] + 0.05, -d / 2 - 0.4, 0.0, h=0.3, green=0.6, seed=i)
        rb = f.get("roof_box")
        if rb:                                           # the stair box on the roof terrace
            bx = cx + (fw / 2 - rb["w"] / 2 - 0.3) * (1 if i % 2 else -1)
            parts += [box(bx - rb["w"] / 2, bx + rb["w"] / 2, d / 2 - 1.0 - rb["d"], d / 2 - 1.0, hh, hh + rb["h"], "wall")]
        rf = f["roof"]
        if rf.get("planters"):
            parts += planter(cx - hw / 2 + 0.5, cx + hw / 2 - 0.5, -d / 2 + 0.6, -d / 2 + 1.3, hh)
        if rf.get("pergola"):
            for x in (cx - hw / 2 + 0.6, cx + hw / 2 - 0.6):
                for y in (-d / 2 + 2.0, -d / 2 + 6.0):
                    parts.append(box(x - 0.08, x + 0.08, y - 0.08, y + 0.08, hh, hh + 2.4, "frame"))
            for j in range(9):
                y = -d / 2 + 2.0 + 4.0 * j / 8
                parts.append(box(cx - hw / 2 + 0.5, cx + hw / 2 - 0.5, y - 0.04, y + 0.04, hh + 2.4, hh + 2.52, "wood"))
    return parts


def _roof_slab(w, d, t, z, mat="roof"):
    """A flat roof slab inside the walls (its edges on their inner faces: no coplanar twins)."""
    return box(-w / 2 + t, w / 2 - t, -d / 2 + t, d / 2 - t, z - 0.45, z, mat)


def _projecting_colonnade(w, d, gh, c) -> list[dict]:
    """A square-pier colonnade standing proj metres in front of the facade (local y = -d/2), extend
    metres past each end, under a flat ledge (ledge_h thick, its top at gh); the corridor's
    floor-to-soffit stays open. bays: pier spacing over the whole run; or "piers": the measured
    left edges of the piers (metres from the centre, read off a frame) for an uneven run;
    "infill": bay indices closed by a stone panel 0.4 m behind the piers' faces."""
    proj, ext, pw, lh = c.get("proj", 1.5), c.get("extend", 0.0), c.get("pier", 1.0), c.get("ledge_h", 1.0)
    L, y0 = w + 2 * ext, -d / 2 - proj
    parts = [box(-L / 2, L / 2, y0, -d / 2, gh - lh, gh, "base")]                       # the ledge
    # piers: evenly over the run, or at measured positions (their left edges, metres from the centre)
    xs = [x0 + pw / 2 for x0 in c["piers"]] if c.get("piers") else \
        [-L / 2 + pw / 2 + (L - pw) * b / c["bays"] for b in range(c["bays"] + 1)]
    for x in xs:
        parts.append(box(x - pw / 2, x + pw / 2, y0, y0 + pw, 0.0, gh - lh, "base"))
    for i in c.get("infill") or []:            # closed bays: a stone panel set just behind the piers' faces
        if 0 <= i < len(xs) - 1:
            parts.append(box(xs[i] + pw / 2, xs[i + 1] - pw / 2, y0 + 0.4, y0 + 0.8, 0.0, gh - lh, "base"))
    return parts


def civic_block(dims, p, f) -> list[dict]:
    """A civic block: a deep colonnade across the front (square piers, the wall set back behind
    them), one tall upper storey of plain wall with a centred loggia, a flat parapet; plainer
    sides and back with small windows. With `colonnade` ({"bays", "proj", "depth", "extend",
    "pier", "ledge_h"}): the piers stand proj metres in front of the upper face under a flat
    ledge, and the ground wall is set back to a corridor `depth` metres deep from the piers,
    with full-height glass doors (the front's ground "door_w" / "door_h")."""
    w, d, h = dims
    gh, par = f["ground_h"], f["parapet"]
    up = max(3.0, h - gh - par)
    c = f.get("colonnade")
    if c:
        nb = c["bays"]
        g = merged((f.get("front") or {}).get("ground") or {}, {"kind": "recessed", "recess": c.get("depth", 3.0) - c.get("proj", 1.5)})
        Ff = _F(merged(f.get("front"), {"ground": g}), storeys=2, ground_h=gh, storey_h=up, bay_m=w / nb, parapet=par)
        Fs = _F(f.get("side"), storeys=2, ground_h=gh, storey_h=up, bay_m=f.get("side_bay_m", 6.0), parapet=par)
        parts = walls_around(w, d, {"front": Ff, "back": Fs, "left": Fs, "right": Fs})
        parts.append(_roof_slab(w, d, Ff["thickness"], gh + up))
        return parts + _projecting_colonnade(w, d, gh, c)
    nb = f["bays"]
    Ff = _F(f.get("front"), storeys=2, ground_h=gh, storey_h=up, bay_m=w / nb, parapet=par)
    Fs = _F(f.get("side"), storeys=2, ground_h=gh, storey_h=up, bay_m=f.get("side_bay_m", 6.0), parapet=par)
    parts = walls_around(w, d, {"front": Ff, "back": Fs, "left": Fs, "right": Fs})
    parts.append(_roof_slab(w, d, Ff["thickness"], gh + up))
    return parts


def _arcade_range(dims, p, f) -> list[dict]:
    """A long single-storey arcade range across the front (f["range"]: {"h", "d", "arch_h",
    "parapet"}, metres) with plain flat-roofed masses set back behind it (f["masses"]: [{"x":
    [f0, f1] of the width, "y": [f0, f1] of the depth behind the range, "h": fraction of the
    building's height}]). The masses' walls stop short of each other and of the range's back
    wall (no coplanar twins)."""
    w, d, h = dims
    rg, t = f["range"], 0.5
    rh, rd = rg["h"], min(rg["d"], d)
    par = rg.get("parapet", 0.9)
    g = merged((f.get("front") or {}).get("ground") or {}, {"h": min(0.95, rg["arch_h"] / rh)})
    Ff = _F(merged(f.get("front"), {"ground": g}), storeys=1, ground_h=rh, storey_h=rh, bay_m=w / f["bays"],
            parapet=par, thickness=t)
    Fs = _F(f.get("side"), storeys=1, ground_h=rh, storey_h=rh, bay_m=f.get("side_bay_m", 6.0), parapet=par,
            thickness=t)
    Fs["ground"] = {"kind": "plain"}
    parts = placed(walls_around(w, rd, {"front": Ff, "back": Fs, "left": Fs, "right": Fs}), 0, (0, -d / 2 + rd / 2, 0))
    parts.append(placed([_roof_slab(w, rd, t, rh)], 0, (0, -d / 2 + rd / 2, 0))[0])
    behind = d - rd
    for m in f.get("masses") or []:
        x0, x1 = (-w / 2 + v * w for v in m["x"])
        y0, y1 = (-d / 2 + rd + 0.3 + v * (behind - 0.3) for v in m["y"])
        mw, md, mh = x1 - x0, y1 - y0, max(rh + 2.0, m["h"] * h)
        if mw < 3 or md < 3:
            continue
        st = f.get("mass_storey_h", 4.5)
        n = max(1, round(mh / st))
        Fm = _F(m.get("facade") or f.get("mass_facade") or {"pattern": ["window", "solid"], "cornice": [[0.3, 0.2]],
                "window": {"kind": "rect", "w": 0.35, "h": 0.4, "sill": 0.35, "reveal": 0.3, "frame": False}},
                storeys=n, ground_h=mh - st * (n - 1), storey_h=st, bay_m=4.0, parapet=m.get("parapet", 0.6),
                thickness=t)
        Fm["ground"] = {"kind": "plain"}
        parts += placed(walls_around(mw, md, {"front": Fm}), 0, ((x0 + x1) / 2, (y0 + y1) / 2, 0))
        parts.append(placed([_roof_slab(mw, md, t, height(Fm))], 0, ((x0 + x1) / 2, (y0 + y1) / 2, 0))[0])
    return parts


def arcade_hall(dims, p, f) -> list[dict]:
    """A civic hall: a round-arched arcade across the front (a walk behind it), tall upper
    storeys with arched windows, cornice and a low hipped roof of stepped slabs. With `range`:
    a single-storey arcade range with flat-roofed masses set back behind it (_arcade_range)."""
    if f.get("range"):
        return _arcade_range(dims, p, f)
    w, d, h = dims
    st = p.get("storey_h", f["storey_h"])
    n = max(2, round(h / st))
    Ff = _F(f.get("front"), storeys=n, ground_h=st, storey_h=st, bay_m=w / f["bays"], parapet=0.0)
    Fs = _F(f.get("side"), storeys=n, ground_h=st, storey_h=st, bay_m=f.get("side_bay_m", 6.0), parapet=0.0)
    parts = walls_around(w, d, {"front": Ff, "back": Fs, "left": Fs, "right": Fs})
    H = height(Ff)
    parts.append(_roof_slab(w, d, Ff["thickness"], H))
    z, k = H, 0
    while k < f["roof_steps"]:                     # a low hipped roof: stepped slabs closing in
        inset = (k + 1) * f["roof_step_m"]
        if w - 2 * inset < 2 or d - 2 * inset < 2:
            break
        parts.append(box(-w / 2 + inset, w / 2 - inset, -d / 2 + inset, d / 2 - inset, z, z + f["roof_rise_m"], "roof"))
        z += f["roof_rise_m"]
        k += 1
    return parts


def campanile(dims, p, f) -> list[dict]:
    """A bell tower: a plain square shaft with slit windows, a string course, an open belfry
    of round arches on every face, a cornice and a pyramid cap of stepped slabs (cap "flat": an
    overhanging stone slab, cap_over metres past the walls)."""
    w, d, h = dims
    bh, cap = f["belfry_h"], f["cap_h"]
    shaft = h - bh - cap
    st = shaft / max(1, round(shaft / f["storey_h"]))
    Fsh = _F(f.get("shaft"), storeys=round(shaft / st), ground_h=st, storey_h=st, bay_m=w, parapet=0.0, cornice=None)
    Fb = _F(f.get("belfry"), storeys=1, ground_h=bh, storey_h=bh, bay_m=w / f["belfry_arches"], parapet=0.0)
    parts = walls_around(w, d, {"front": Fsh})
    parts += lifted(walls_around(w, d, {"front": Fb}), shaft)
    parts.append(_roof_slab(w, d, Fb["thickness"], shaft + bh))
    if f.get("cap") == "flat":                     # a flat stone cap: an overhanging slab, no roof
        ov = f.get("cap_over", 0.6)
        z = shaft + bh
        parts.append(box(-w / 2 - ov, w / 2 + ov, -d / 2 - ov, d / 2 + ov, z, z + min(cap, 1.2), "top"))
        return parts
    z, n = shaft + bh, f["cap_steps"]
    for k in range(n):
        inset = (k + 1) * (min(w, d) / 2 - 0.3) / n
        parts.append(box(-w / 2 + inset, w / 2 - inset, -d / 2 + inset, d / 2 - inset, z, z + cap / n, "roof"))
        z += cap / n
    return parts


GENERATORS = {"hall_vault": hall_vault, "cube": cube, "basilica": basilica, "podium_tower": podium_tower,
              "perimeter": perimeter, "row": row, "civic_block": civic_block, "arcade_hall": arcade_hall,
              "campanile": campanile}


# Starting values for optional parts that a form's defaults leave off (None): what the review
# form fills in when such a part is switched on.
TEMPLATES = {
    "accent": {"w": 0.16, "extra": 1, "recess": 2.0, "facade": {"pattern": ["loggia"]}},
    "street_garden": {"depth": 3.0, "h": 1.2, "gap": 6.0},
    "street_side": {"pattern": ["window"]},
    "top": {"storeys": 1, "setback": 3.0, "planters": True, "balustrade": "glass", "facade": {"pattern": ["window"]}},
    "gardens": {"every": 2, "from": 2, "depth": 1.4, "drape": True, "faces": ["front"]},
    "crown": {"inset": 1.2, "h": 3.6, "roof_over": 1.4},
    "rounds": {"r": 0.13, "x": 0.3, "z": 0.5},
    "front_garden": {"depth": 2.0, "h": 1.0},
    "roof_box": {"w": 2.6, "d": 3.0, "h": 2.6},
    "balcony": {"kind": "slab", "depth": 1.3, "balustrade": "glass", "every": 1, "on": "window"},
    "pilasters": {"every": 2, "w": 0.6, "proj": 0.2},
    "courses": {"every": 2, "h": 0.2, "proj": 0.06},
    "tower_faces": {"front": {"pattern": ["loggia", "solid"]}},
    "house_types": [{}],
    "end_screen": {"recess": 2.5, "mezzanine": 0.42, "fins": 1.6, "passage_w": 0.36, "pier": True},
    "stalls": {"rows": 2, "pitch": 3.2, "w": 2.6, "d": 1.4, "canopy": 2.5},
    "skylight": {"side": 1, "segs": [10, 12], "mullion_m": 2.2},
    "end_rim": {"w": 1.3, "proj": 0.6},
    "niche": {"faces": ["front"], "w": 0.5, "h": 0.75, "rise": 0.2, "depth": 4.5, "mezzanine": 0.46, "steps": 3,
              "door_w": 0.22, "screen": True},
    "joints": {"cols": 4, "rows": 3, "groove": 0.12},
}
