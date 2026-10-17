"""Builder parts and brief fields added for the self-serve asset loop (2026-10-16): vegetation
zones and planter styles, pointed arches, the ground layout (doors, entrance, door leaves),
per-storey opening proportions, opening counts, curtain walls, panel joints, pilaster profiles,
hip roofs, annexes, garden wall length, and the brief form's expanded features."""
import json
import math
import subprocess
from pathlib import Path

import pytest

from asset_pipeline import building_gen as bg
from asset_pipeline import building_parts as bparts
from asset_pipeline import building_proc as bp


def _F(**over):
    return bparts.merged(bparts.FACADE, over)


def _upper_ops(ps, z0, z1, mat="glass"):
    """Glass panes of the openings between z0 and z1 (one pane per glazed opening)."""
    return [p for p in ps if p["mat"] == mat and p["kind"] == "prism" and z0 <= min(z for _, z in p["xz"]) < z1]


# --- vegetation zones and planters ---------------------------------------------------------

def test_every_planter_style_marks_its_zone():
    for st in bparts.PLANTER_STYLES:
        ps = bparts.planter(0.0, 4.0, 0.0, 0.8, 10.0, style=st, veg="balcony")
        zones = [p for p in ps if p["kind"] == "zone"]
        if st == "none":
            assert ps == []
            continue
        assert len(zones) == 1 and zones[0]["veg"] == "balcony", st
        assert zones[0]["x1"] - zones[0]["x0"] > 3.0, st
        assert not [p for p in ps if p["mat"] == "planting" and p["z1"] - p["z0"] > 0.1 and st != "hedge"], \
            f"{st}: plants are the zone's (proxies, PCG), the builder draws only ground cover"


def test_zone_records_follow_placement():
    z = bparts.placed([bparts.zone(0.0, 2.0, 0.0, 1.0, 3.0, 3.5, "roof_edge")], 90, (10.0, 0.0, 5.0))
    r = bparts.zone_records(z)[0]
    assert r["type"] == "roof_edge" and r["yaw"] == 90.0
    assert r["center"] == pytest.approx([10.0 - 0.5, 1.0, 8.5])      # (1, 0.5) turned 90 deg, then moved
    assert r["size"] == pytest.approx([2.0, 1.0, 0.5])


@pytest.mark.parametrize("form,dims", [("perimeter", [45, 45, 24]), ("row", [30, 14, 10]), ("podium_tower", [37, 28, 47])])
def test_generators_mark_vegetation_zones(form, dims):
    ps = bp.generate(form, dims, {}, bg.features(form, None))
    types = {p["veg"] for p in ps if p["kind"] == "zone"}
    assert types, f"{form}: no vegetation zones"
    if form == "perimeter":
        assert {"roof_edge", "court_bed"} <= types


def test_brief_planter_style_reaches_every_planter():
    f = bg.features("perimeter", {"planter": "pots"})
    ps = bp.generate("perimeter", [45, 45, 24], {}, f)
    assert bparts.PLANTER["style"] == "box"                         # reset after the build
    pots = [p for p in ps if p["mat"] == "trim" and p["kind"] == "box" and abs((p["x1"] - p["x0"]) - 0.6) < 1e-6]
    assert pots


def test_street_garden_length_and_height():
    def walls(sg):
        f = bg.features("perimeter", {"street_garden": sg})
        return [p for p in bg.perimeter([44, 44, 15], {}, f) if p["kind"] == "box" and p["mat"] == "base" and p["z0"] == 0.0
                and abs(p["z1"] - sg["h"]) < 1e-9 and abs((p["y1"] - p["y0"]) - 0.3) < 1e-9]
    full = walls({"depth": 3.2, "h": 1.2, "gap": 8.0})
    half = walls({"depth": 3.2, "h": 1.6, "gap": 8.0, "length": 0.5})
    run = lambda ws: sum(p["x1"] - p["x0"] for p in ws)
    assert len(full) == 4 and len(half) == 4
    assert run(half) < 0.6 * run(full)


# --- openings --------------------------------------------------------------------------------

def test_pointed_arch_is_convex_and_reaches_its_crown():
    o = {"kind": "pointed", "cx": 0.0, "w": 2.0, "z0": 0.0, "h": 4.0}
    poly = bparts.outline(o)
    zs = [z for _, z in poly]
    assert max(zs) == pytest.approx(4.0, abs=1e-6)
    assert bparts._span(o, 0.9)[1] < 4.0 and bparts._span(o, -0.9)[1] == pytest.approx(bparts._span(o, 0.9)[1])
    wall = bparts.pierced(-3, 3, 0, 6, 0, 0.5, [o])
    assert all(len(p["xz"]) >= 3 for p in wall if p["kind"] == "prism")


def test_ground_layout_doors_entrance_and_leaves():
    F = _F(storeys=3, ground={"kind": "plain", "door_count": 3, "entrance_m": 6.0, "entrance_x": 0.0, "door_leaf": "wood"})
    ps = bparts.facade(30.0, F)
    leaves = [p for p in ps if p["mat"] == "wood"]
    assert len(leaves) == 2                                    # the middle door would sit in the entrance
    gh = bparts.levels(F)[0][1]
    ground_glass = _upper_ops(ps, -0.01, 0.01)
    assert any(max(x for x, _ in p["xz"]) - min(x for x, _ in p["xz"]) >= 5.9 for p in ground_glass)    # the entrance
    assert not [p for p in ps if p["mat"] == "glass" and min(z for _, z in p["xz"]) == 0.0 and
                max(x for x, _ in p["xz"]) - min(x for x, _ in p["xz"]) < 2.0]          # doors are leaves, not glass
    assert gh > 0


def test_opening_proportions_per_storey():
    base = _F(storeys=4, storey_h=3.0, ground_h=3.0, pattern=["window"])
    F = _F(storeys=4, storey_h=3.0, ground_h=3.0, pattern=["window"], open_w=[1.0, 1.5], open_h=[1.0, 1.0, 0.5])
    def sizes(ps, s):
        z0 = 3.0 * s
        return sorted(round(max(x for x, _ in p["xz"]) - min(x for x, _ in p["xz"]), 3) for p in _upper_ops(ps, z0, z0 + 3.0)), \
            sorted(round(max(z for _, z in p["xz"]) - min(z for _, z in p["xz"]), 3) for p in _upper_ops(ps, z0, z0 + 3.0))
    a, b = bparts.facade(20.0, base), bparts.facade(20.0, F)
    assert sizes(b, 1) == sizes(a, 1)
    assert sizes(b, 2)[0][0] == pytest.approx(1.5 * sizes(a, 2)[0][0])
    assert sizes(b, 3)[1][0] == pytest.approx(0.5 * sizes(a, 3)[1][0])


def test_opening_count_and_spacing():
    F = _F(storeys=2, pattern=["window", "solid"], count=4, spacing=6.0, ground={"kind": "plain"})
    ps = bparts.facade(40.0, F)
    zs = bparts.levels(F)
    upper = _upper_ops(ps, zs[1][0], zs[1][0] + zs[1][1])
    ground = _upper_ops(ps, 0.0, zs[0][1])
    centres = sorted(round((max(x for x, _ in p["xz"]) + min(x for x, _ in p["xz"])) / 2, 3) for p in upper)
    assert centres == [-9.0, -3.0, 3.0, 9.0]
    assert len(ground) == 4                                    # the ground follows the count (no doors)


def test_curtain_wall_bays_have_mullions():
    F = _F(storeys=2, storey_h=3.6, pattern=["curtain"], bay_m=6.0, curtain={"w": 0.96, "mullion_m": 1.5, "transom": 0.3,
                                                                              "frame_mat": "frame"})
    ps = bparts.facade(12.0, F)
    z1 = bparts.levels(F)[1][0]
    glass = _upper_ops(ps, z1, z1 + 3.6)
    assert len(glass) == 2 and all(max(z for _, z in p["xz"]) - min(z for _, z in p["xz"]) > 3.0 for p in glass)
    vert = [p for p in ps if p["mat"] == "frame" and p["z0"] >= z1 and p["z1"] - p["z0"] > 2.5 and p["x1"] - p["x0"] < 0.1]
    assert len(vert) >= 2 * 3                                  # 6 m bays: 3 mullions plus the jambs, each


def test_panel_joints_stop_at_openings_and_have_no_twins():
    F = _F(storeys=3, pattern=["window"], panels={"panel_w": 1.5, "panel_h": 0.0, "groove": 0.03})
    ps = bparts.facade(18.0, F)
    joints = [p for p in ps if p["mat"] == "joint"]
    assert joints
    glass = [p for p in ps if p["mat"] == "glass"]
    for j in joints:                                           # no groove crosses an opening
        for g_ in glass:
            xs, zs = [x for x, _ in g_["xz"]], [z for _, z in g_["xz"]]
            assert not (j["x0"] < max(xs) - 0.01 and j["x1"] > min(xs) + 0.01 and j["z0"] < max(zs) - 0.01 and j["z1"] > min(zs) + 0.01)
    bparts.resolve_coincident(ps)
    assert bparts.coincident_area(ps) == []


@pytest.mark.parametrize("profile", ["pier", "strip", "fin"])
def test_pilaster_profiles(profile):
    F = _F(storeys=3, pilasters={"every": 2, "w": 0.6, "proj": 0.5, "profile": profile})
    ps = bparts.facade(20.0, F)
    pil = [p for p in ps if p["kind"] == "box" and p["y1"] == 0.0 and p["y0"] < -0.01 and p["mat"] == "wall"]
    assert pil
    depth = -min(p["y0"] for p in pil)
    width = max(p["x1"] - p["x0"] for p in pil)
    if profile == "strip":
        assert depth <= 0.06 and any(p["mat"] == "joint" for p in ps)
    elif profile == "fin":
        assert width <= 0.3 + 1e-9 and depth == pytest.approx(0.5)
    else:
        assert width == pytest.approx(0.6) and depth == pytest.approx(0.5)


def test_loggia_parapet_height():
    F = _F(storeys=2, pattern=["loggia"], loggia={"balustrade": "solid", "parapet_h": 0.6})
    solid = [p for p in bparts.facade(10.0, F) if p["kind"] == "box" and p["mat"] == "wall" and abs((p["y1"] - p["y0"]) - 0.18) < 1e-9]
    assert solid and all(p["z1"] - p["z0"] == pytest.approx(0.6) for p in solid)


# --- roofs, annexes ------------------------------------------------------------------------

def test_hip_roof_points():
    pts = bparts.hip_roof(10.0, 6.0, 20.0, 2.0, 0.5)[0]["pts"]
    assert max(p[2] for p in pts) == pytest.approx(22.25)
    ridge = [p for p in pts if p[2] > 22]
    assert len(ridge) == 2 and abs(ridge[0][0] - ridge[1][0]) == pytest.approx(10.0 + 1.0 - 6.0 - 1.0)


def test_annexes_stand_outside_the_footprint_on_their_side():
    f = bg.features("civic_block", {"annexes": [{"side": "left", "at": 0.2, "w": 8.0, "d": 6.0, "h": 4.0},
                                                {"side": "front", "w": 10.0, "d": 4.0, "h": 3.5, "canopy": 2.0}]})
    w, d = 31, 21
    ann = bg.annexes([w, d, 18.2], f)
    assert ann

    def world_x(p):
        rz, at = math.radians(p.get("rz", 0)), p.get("at", [0, 0, 0])
        cx, cy = (p["x0"] + p["x1"]) / 2, (p["y0"] + p["y1"]) / 2
        return math.cos(rz) * cx - math.sin(rz) * cy + at[0], math.sin(rz) * cx + math.cos(rz) * cy + at[1]
    xs = [world_x(p) for p in ann if p["kind"] == "box"]
    assert any(x < -w / 2 for x, _ in xs) and any(y < -d / 2 for _, y in xs)
    assert all(not (-w / 2 + 0.5 < x < w / 2 - 0.5 and -d / 2 + 0.5 < y < d / 2 - 0.5) for x, y in xs)
    with pytest.raises(ValueError, match="annex side"):
        bg.annexes([w, d, 18], bg.features("civic_block", {"annexes": [{"side": "up"}]}))


@pytest.mark.skipif(not Path("/snap/bin/blender").exists(), reason="needs Blender")
def test_hip_roof_builds_closed_and_outward(tmp_path):
    from asset_pipeline import kit_proc
    ps = bg.GENERATORS["campanile"]([8, 8, 30], {}, bg.features("campanile", {"cap": "hip", "cap_h": 2.5, "cap_over": 0.8}))
    hull = [p for p in ps if p["kind"] == "hull"]
    assert len(hull) == 1
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"name": "t", "out": str(tmp_path / "t.glb"), "parts": hull,
                               "materials": {"roof": {"uv": "plain", "color": [0.5, 0.3, 0.2], "rough": 0.6}}}))
    check = tmp_path / "check.py"
    check.write_text("""
import bpy, bmesh, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[sys.argv.index("--") + 1])
o = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
bm = bmesh.new(); bm.from_mesh(o.data)
print("VOL", bm.calc_volume(signed=True), len(bm.faces))
""")
    subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(kit_proc.SCRIPT), "--", str(job)],
                   check=True, capture_output=True, timeout=300)
    r = subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(check), "--", str(tmp_path / "t.glb")],
                       capture_output=True, text=True, check=True, timeout=300)
    vol = float(next(ln for ln in r.stdout.splitlines() if ln.startswith("VOL")).split()[1])
    W = 8 + 1.6
    assert vol == pytest.approx(W * W * 0.25 + W * W * 2.5 / 3, rel=0.02)     # slab edge + pyramid, outward


# --- the brief form: expanded features, deep merge, round trip -------------------------------

def test_form_features_show_the_grammar_and_keep_generator_keys_out():
    f = bg.form_features("perimeter")
    assert f["street"]["window"]["frame_mat"] == "frame" and f["street"]["ground"]["door_count"] == 0
    assert "count" in f["top"]["facade"] and "open_w" in f["court"]
    for key in bparts.FACADE_KW:
        assert key not in f["street"] or key in bg.TYPE_FEATURES["perimeter"]["street"], key
    assert f["planter"] == "box" and f["annexes"] == []


def test_brief_round_trip_stores_only_the_difference():
    from asset_pipeline import library_review as lr
    shown = bg.form_features("perimeter")
    shown["street"]["window"]["w"] = 0.5
    shown["top"]["facade"]["count"] = 4
    d = lr.diff_features("perimeter", shown)
    assert d == {"street": {"window": {"w": 0.5}}, "top": {"facade": {"count": 4}}}
    f = bg.features("perimeter", d)
    assert f["street"]["ground"] == bg.TYPE_FEATURES["perimeter"]["street"]["ground"]     # deep merge keeps the rest
    assert f["top"]["facade"]["parapet"] == 0.0


def test_enum_values_are_checked():
    from asset_pipeline import library_review as lr
    errs = lr._walk_enums(bg.features("perimeter", {"street": {"window": {"kind": "oval"}}, "planter": "vase"}))
    assert any("window.kind" in e for e in errs) and any("planter" in e for e in errs)
    assert not lr._walk_enums(bg.features("cube", None))         # a niche portal's kind is its own list
