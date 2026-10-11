"""Procedural buildings (building_proc): convex pierced walls, generators, brief measures."""
import math

import numpy as np
import pytest

from asset_pipeline import building_proc as bp


def _area(poly):
    return 0.5 * abs(sum(x0 * z1 - x1 * z0 for (x0, z0), (x1, z1) in zip(poly, poly[1:] + poly[:1])))


def _convex(poly):
    s = []
    n = len(poly)
    for i in range(n):
        (ax, az), (bx, bz), (cx, cz) = poly[i], poly[(i + 1) % n], poly[(i + 2) % n]
        c = (bx - ax) * (cz - bz) - (bz - az) * (cx - bx)
        if abs(c) > 1e-9:
            s.append(c > 0)
    return all(s) or not any(s)


@pytest.mark.parametrize("ops,open_area", [
    ([{"kind": "rect", "cx": 0.0, "w": 2.0, "z0": 1.0, "h": 2.0}], 4.0),
    ([{"kind": "arch", "cx": -2.0, "w": 2.0, "z0": 0.0, "h": 3.0}], 2.0 * 2.0 + math.pi / 2),
    ([{"kind": "round", "cx": 2.0, "cz": 3.0, "r": 1.0}], math.pi),
])
def test_pierced_wall_covers_the_wall_minus_its_openings_with_convex_pieces(ops, open_area):
    parts = [p for p in bp.pierced(-5, 5, 0, 6, 0, 0.5, ops) if p["mat"] == "wall"]
    assert all(_convex(p["xz"]) for p in parts)
    solid = sum(_area(p["xz"]) for p in parts)
    assert solid == pytest.approx(60 - open_area, rel=0.01)       # curves: 12 segments per half circle
    glass = [p for p in bp.pierced(-5, 5, 0, 6, 0, 0.5, ops) if p["mat"] == "glass"]
    assert len(glass) == 1 and 0 < glass[0]["y0"] < 0.5


def test_open_openings_have_no_glass():
    ops = [{"kind": "arch", "cx": 0.0, "w": 3.0, "z0": 0.0, "h": 4.0, "glass": False}]
    assert not [p for p in bp.pierced(-4, 4, 0, 6, 0, 1, ops) if p["mat"] == "glass"]


@pytest.mark.parametrize("form,dims", [("hall_vault", [43, 28, 10.6]), ("cube", [48, 48, 16]),
                                       ("basilica", [87, 50, 23.6]), ("podium_tower", [37, 28, 47]),
                                       ("perimeter", [65, 65, 23.8]), ("row", [30, 14, 13.6])])
def test_generators_stay_near_their_dimensions(form, dims):
    parts = bp.GENERATORS[form](dims, {})
    pts = []
    for p in parts:
        rz, at = math.radians(p.get("rz", 0)), p.get("at", [0, 0, 0])
        if p["kind"] == "box":
            corners = [(x, y, z) for x in (p["x0"], p["x1"]) for y in (p["y0"], p["y1"]) for z in (p["z0"], p["z1"])]
        elif p["kind"] == "prism":
            corners = [(x, y, z) for x, z in p["xz"] for y in (p["y0"], p["y1"])]
        else:
            continue
        for x, y, z in corners:
            pts.append((math.cos(rz) * x - math.sin(rz) * y + at[0], math.sin(rz) * x + math.cos(rz) * y + at[1], z + at[2]))
    a = np.array(pts)
    size = a.max(0) - a.min(0)
    w, d, h = dims
    assert size[0] == pytest.approx(w, abs=0.15 * w + 2.5) and size[1] == pytest.approx(d, abs=0.15 * d + 3.0)
    assert size[2] == pytest.approx(h, abs=0.2 * h + 4.0)        # roofs: parapets, pergolas, crowns
    assert {"wall"} <= {p["mat"] for p in parts}


def test_measure_finds_bays_palette_and_a_plain_patch(tmp_path):
    from PIL import Image
    a = np.full((300, 600, 3), 255, np.uint8)
    a[50:280, 40:560] = (200, 190, 175)                           # a facade
    for i in range(8):                                            # 8 dark windows per row, 4 rows
        for j in range(4):
            x, y = 40 + 65 * i + 20, 50 + 57 * j + 15
            a[y:y + 30, x:x + 25] = (40, 45, 50)
    p = tmp_path / "v.png"
    Image.fromarray(a).save(p)
    m = bp.measure(p, [52.0, 14.0, 13.6])
    assert m["bays"] == 8 and m["storeys"] == 4
    assert m["palette"][0]["rgb"] == pytest.approx([200, 190, 175], abs=6)
    x0, y0, x1, y1 = m["patch_px"]
    patch = a[y0:y1, x0:x1].reshape(-1, 3)
    assert (patch == (200, 190, 175)).all(1).mean() > 0.95
