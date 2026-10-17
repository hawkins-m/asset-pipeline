"""Procedural buildings (building_proc): convex pierced walls, generators, brief measures."""
import math
from pathlib import Path

import numpy as np
import pytest

from asset_pipeline import building_gen as bg
from asset_pipeline import building_parts as bparts
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
    parts = [p for p in bparts.pierced(-5, 5, 0, 6, 0, 0.5, ops) if p["mat"] == "wall"]
    assert all(_convex(p["xz"]) for p in parts)
    solid = sum(_area(p["xz"]) for p in parts)
    assert solid == pytest.approx(60 - open_area, rel=0.01)       # curves: 12 segments per half circle
    glass = [p for p in bparts.pierced(-5, 5, 0, 6, 0, 0.5, ops) if p["mat"] == "glass"]
    assert len(glass) == 1 and 0 < glass[0]["y0"] < 0.5


def test_open_openings_have_no_glass():
    ops = [{"kind": "arch", "cx": 0.0, "w": 3.0, "z0": 0.0, "h": 4.0, "glass": False}]
    assert not [p for p in bparts.pierced(-4, 4, 0, 6, 0, 1, ops) if p["mat"] == "glass"]


@pytest.mark.parametrize("form,dims", [("hall_vault", [43, 28, 10.6]), ("cube", [48, 48, 16]),
                                       ("basilica", [87, 50, 23.6]), ("podium_tower", [37, 28, 47]),
                                       ("perimeter", [65, 65, 23.8]), ("row", [30, 14, 13.6])])
def test_generators_stay_near_their_dimensions(form, dims):
    parts = bg.GENERATORS[form](dims, {}, bg.features(form, None))
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
    assert not [p for p in parts if p["kind"] == "prism" and len(p["xz"]) < 3]


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
    m = bp.measure(p, [52.0, 14.0, 13.6], storey_h=3.4)
    assert m["storeys"] == 4
    assert m["bay_m"] == pytest.approx(3.4 * 65 / 57, rel=0.05)          # bay period / storey period x storey height
    assert m["palette"][0]["rgb"] == pytest.approx([200, 190, 175], abs=6)
    assert m["frontality"] > 0.8                                   # an elevation: edges square (window corners aside)
    x0, y0, x1, y1 = m["patch_px"]
    patch = a[y0:y1, x0:x1].reshape(-1, 3)
    assert (patch == (200, 190, 175)).all(1).mean() > 0.95


def test_segmental_openings_and_curved_tops():
    seg = {"kind": "seg", "cx": 0.0, "w": 4.0, "z0": 0.0, "h": 3.0, "rise": 0.8}
    ps = [p for p in bparts.pierced(-4, 4, 0, 5, 0, 0.5, [seg]) if p["mat"] == "wall"]
    R = (4 + 0.64) / 1.6                                           # circle through the springs and the crown
    seg_area = R * R * math.acos((R - 0.8) / R) - (R - 0.8) * math.sqrt(2 * R * 0.8 - 0.64)
    assert sum(_area(p["xz"]) for p in ps) == pytest.approx(40 - (4 * 2.2 + seg_area), rel=0.01)
    vault = bparts.pierced(-3, 3, 0, lambda x: 4 * math.sqrt(max(0, 1 - (x / 3) ** 2)), 0, 0.5, [])
    assert sum(_area(p["xz"]) for p in vault) == pytest.approx(math.pi * 3 * 4 / 2, rel=0.01)


def test_facade_grammar_parts():
    F = bparts.merged(bparts.FACADE, {"storeys": 4, "pattern": ["loggia", "window"],
                                    "balcony": {"kind": "slab", "depth": 1.2, "planters": True, "on": "window"},
                                    "ground": {"kind": "pilotis", "canopy": 2.5, "planters": True},
                                    "pilasters": {"every": 2, "w": 0.5, "proj": 0.12}, "courses": {"every": 2},
                                    "recess": [{"x0": 0.4, "x1": 0.6, "depth": 2.0}], "loggia": {"planters": True}})
    ps = bparts.facade(30.0, F)
    mats = {p["mat"] for p in ps}
    assert {"planting", "frame", "rail_glass", "glass", "trim"} <= mats
    assert min(p["y0"] for p in ps if p["kind"] == "box") <= -2.5 + 1e-6          # the canopy stands out
    H = bparts.height(F)
    returns = [p for p in ps if p["kind"] == "box" and p["y1"] >= 2.0 and p["z1"] - p["z0"] >= H - 1e-6]
    assert len(returns) == 2                                                      # the recess's return walls


@pytest.mark.skipif(not Path("/snap/bin/blender").exists(), reason="needs Blender")
def test_built_meshes_face_outwards(tmp_path):
    """Every piece is a closed prism or shell with outward normals (positive signed volume).
    Until 2026-10-10 every prism came out inside-out, which Cycles' two-sided shading hid."""
    import json
    import subprocess
    from asset_pipeline import kit_proc
    ps = bg.GENERATORS["cube"]([20, 20, 12], {}, bg.features("cube", None))
    mats = {m: {"uv": "plain", "color": [0.5, 0.5, 0.5], "rough": 0.5} for m in {p["mat"] for p in ps}}
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"name": "t", "out": str(tmp_path / "t.glb"), "parts": ps, "materials": mats}))
    check = tmp_path / "check.py"
    check.write_text("""
import bpy, bmesh, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[sys.argv.index("--") + 1])
o = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
bm = bmesh.new(); bm.from_mesh(o.data)
print("VOL", bm.calc_volume(signed=True))
""")
    subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(kit_proc.SCRIPT), "--", str(job)],
                   check=True, capture_output=True, timeout=300)
    r = subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(check), "--", str(tmp_path / "t.glb")],
                       capture_output=True, text=True, check=True, timeout=300)
    vol = float(next(ln for ln in r.stdout.splitlines() if ln.startswith("VOL")).split()[1])
    assert vol > 0.5 * 20 * 20 * 12 * 0.2                                # solid walls: a good share of the cube, positive


FORM_DIMS = [("cube", [48, 48, 40]), ("basilica", [87, 50, 24]), ("podium_tower", [37, 28, 47]),
             ("perimeter", [45, 45, 24]), ("row", [30, 14, 10]), ("hall_vault", [43, 28, 19.5]),
             ("civic_block", [31, 21, 18.2]), ("arcade_hall", [36, 22, 19.5]), ("campanile", [7, 7, 46])]


@pytest.mark.parametrize("form,dims", FORM_DIMS)
def test_no_coplanar_twins_after_resolving(form, dims):
    """Faces of two parts in one plane, facing the same way, rendered black in Cycles (shadow
    rays hit the twin) and z-fought in Unreal: roof slabs inside walls, attics and accents on the
    court facade, recess returns over the wall beside them (fixed 2026-10-11)."""
    ps = bg.GENERATORS[form](dims, {}, bg.features(form, None))
    bparts.resolve_coincident(ps)
    assert bparts.coincident_area(ps) == []


def test_resolve_keeps_the_trim_in_front_and_moves_only_millimetres():
    wall = bparts.box(-5, 5, 0, 0.5, 0, 10, "wall")
    slab = bparts.box(-5, 5, 0, 8, 9.55, 10, "roof")                  # its front face on the wall's
    coping = bparts.box(-5, 5, 0, 0.6, 9.7, 10.0, "trim")
    ps = [wall, slab, coping]
    assert bparts.coincident_area(ps)
    bparts.resolve_coincident(ps)
    assert bparts.coincident_area(ps) == []
    assert coping["y0"] == 0 and coping["z1"] == 10.0                            # the trim never moves
    assert 0 < slab["y0"] <= 0.004 and 0 < wall["y0"] <= 0.004                   # the others give way, by mm
    assert abs(wall["z1"] - 10) <= 0.006


@pytest.mark.skipif(not Path("/snap/bin/blender").exists(), reason="needs Blender")
def test_every_face_has_a_material(tmp_path):
    """Every part's material is one the build makes, and every face of the built GLB has a
    material slot with a material in it, for every form."""
    import json
    import subprocess
    from asset_pipeline import kit_proc
    names = {"wall", "base", "top", "trim", "vault", "roof", "glass", "rail_glass", "frame", "planting", "wood"}
    check = tmp_path / "check.py"
    check.write_text("""
import bpy, sys, json
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[sys.argv.index("--") + 1])
out = []
for o in bpy.context.scene.objects:
    if o.type == "MESH":
        ms = o.data.materials
        out.append(sum(1 for p in o.data.polygons if p.material_index >= len(ms) or ms[p.material_index] is None))
print("NOMAT", json.dumps(out))
""")
    for form, dims in FORM_DIMS:
        ps = bg.GENERATORS[form](dims, {}, bg.features(form, None))
        used = {p["mat"] for p in ps}
        assert used <= names, f"{form}: parts use materials the build never makes: {used - names}"
        mats = {m: {"uv": "plain", "color": [0.5, 0.5, 0.5], "rough": 0.5} for m in used}
        job = tmp_path / f"{form}.json"
        job.write_text(json.dumps({"name": form, "out": str(tmp_path / f"{form}.glb"), "parts": ps, "materials": mats}))
        subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(kit_proc.SCRIPT), "--", str(job)],
                       check=True, capture_output=True, timeout=300)
        r = subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(check), "--",
                            str(tmp_path / f"{form}.glb")], capture_output=True, text=True, check=True, timeout=300)
        nomat = json.loads(next(ln for ln in r.stdout.splitlines() if ln.startswith("NOMAT"))[6:])
        assert nomat and not any(nomat), f"{form}: faces without a material: {nomat}"


FEATURED = [
    ("arcade_hall", [36, 22, 19.5], {"range": {"h": 12.5, "d": 7.0, "arch_h": 9.0}, "front": {"pattern": ["solid"]},
                                     "masses": [{"x": [0.6, 0.94], "y": [0.0, 0.85], "h": 1.0},
                                                {"x": [0.3, 0.56], "y": [0.15, 0.8], "h": 0.8}]}),
    ("campanile", [10, 10, 46], {"cap": "flat", "cap_h": 1.0, "cap_over": 0.7}),
    ("civic_block", [31, 21, 18.2], {"ground_h": 7.5, "front": {
        "ground": {"kind": "pilotis", "recess": 3.5, "column": "square", "column_d": 1.0},
        "recess": [{"x0": 0.35, "x1": 0.75, "depth": 0.0, "pattern": ["loggia"], "balcony": None}],
        "loggia": {"w": 0.86, "h": 0.5, "sill": 0.03, "depth": 2.5, "balustrade": "glass"}}}),
]


def test_projecting_segment_is_closed_by_returns_and_a_cap():
    # a projecting bay (negative recess depth) had no return walls: a dark slot down each side
    for ground in ({"kind": "plain"}, {"kind": "pilotis", "recess": 1.6}):
        F = bparts.merged(bparts.FACADE, {"storeys": 5, "ground": ground,
                                        "recess": [{"x0": 0.4, "x1": 0.6, "depth": -1.4}]})
        ps = bparts.facade(30.0, F)
        H, t, x0, x1 = bparts.height(F), F["thickness"], -3.0, 3.0
        ret = [p for p in ps if p["kind"] == "box" and p["y0"] <= -1.4 + t + 1e-6 and p["y1"] >= -1e-6
               and p["z1"] >= H - 1e-6 and p["x1"] - p["x0"] <= t + 1e-6]
        assert sorted(round(p["x0"], 3) for p in ret) == [x0, round(x1 - t, 3)]
        assert any(p["mat"] == "roof" and p["x0"] <= x0 + t + 1e-6 and p["x1"] >= x1 - t - 1e-6
                   and p["z1"] >= H - 1e-6 for p in ps)
        bparts.resolve_coincident(ps)
        assert bparts.coincident_area(ps) == []


def test_glass_balustrade_is_framed_clear_panels():
    ps = bparts.balustrade(0.0, 6.0, 0.0, 3.0, "glass")
    glass = [p for p in ps if p["mat"] == "rail_glass"]
    assert len(glass) == 4 and all(p["x1"] - p["x0"] < 1.6 for p in glass)        # panels, not one slab
    top = max(p["z1"] for p in ps)
    assert any(p["mat"] == "frame" and p["z1"] == top and p["x1"] - p["x0"] == 6.0 for p in ps)   # handrail
    assert bp._plain_materials({"measured": {"palette": [{"rgb": [50, 50, 50], "lum": 50}]}})["rail_glass"]["alpha"] < 1


def test_timber_frames_and_loggia_lining_are_opt_in():
    base = {"storeys": 3, "pattern": ["loggia", "window"], "loggia": {"balustrade": "rail"}}
    assert "wood" not in {p["mat"] for p in bparts.facade(20.0, bparts.merged(bparts.FACADE, base))}
    F = bparts.merged(bparts.FACADE, {**base, "window": {"frame_mat": "wood"},
                                      "loggia": {"balustrade": "rail", "frame": True, "lining": "wood"}})
    ps = bparts.facade(20.0, F)
    wood = [p for p in ps if p["mat"] == "wood"]
    assert wood and not [p for p in ps if p["mat"] == "frame" and p["z1"] - p["z0"] > 1.2]   # no metal frames left
    bparts.resolve_coincident(ps)
    assert bparts.coincident_area(ps) == []


@pytest.mark.parametrize("form,dims,feats", FEATURED)
def test_featured_variants_have_no_coplanar_twins(form, dims, feats):
    ps = bg.GENERATORS[form](dims, {}, bg.features(form, feats))
    bparts.resolve_coincident(ps)
    assert bparts.coincident_area(ps) == []


def _zmax(parts, mat=None):
    return max(p["z1"] + p.get("at", [0, 0, 0])[2] for p in parts if p["kind"] == "box" and (mat is None or p["mat"] == mat))


def test_arcade_range_is_one_storey_of_tall_arches_with_masses_behind():
    f = bg.features("arcade_hall", FEATURED[0][2])
    ps = bg.arcade_hall([36, 22, 19.5], {}, f)
    roofs = [p for p in ps if p["mat"] == "roof"]
    assert len(roofs) == 1 + 2                                         # flat slabs: the range's and each mass's
    assert _zmax(ps) == pytest.approx(19.5, abs=1.5)                   # the tallest mass
    front = [p for p in ps if p.get("at", [0, 0, 0])[1] < -10 and p["kind"] == "box"]
    assert front and max(p["z1"] + p.get("at", [0, 0, 0])[2] for p in front) < 14.5   # the range stays low


def test_campanile_flat_cap_is_stone_and_overhangs():
    ps = bg.campanile([10, 10, 46], {}, bg.features("campanile", {"cap": "flat", "cap_h": 1.0, "cap_over": 0.7}))
    assert len([p for p in ps if p["mat"] == "roof"]) == 1            # the slab inside the walls: no pyramid
    cap = max((p for p in ps if p["kind"] == "box"), key=lambda p: p["z1"])
    assert cap["mat"] == "top" and cap["x1"] - cap["x0"] == pytest.approx(11.4)
