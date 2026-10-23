"""Ground lookup, grounding and the placement check on synthetic projects: one on a slope, one on
flat ground. No real project data."""
import numpy as np
import pytest
from PIL import Image

from asset_pipeline import city_library as cl, greybox, placement_check as pc, ue_coords
from asset_pipeline.ground import Ground
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.schema import UnrealSettings
from asset_pipeline.stages import sw_site

EXTENT, SLOPE = 200.0, 0.1               # a 200 m tile rising 10 cm per metre towards +X


def slope_heights(n=65):
    x = np.linspace(-EXTENT / 2, EXTENT / 2, n)
    return np.tile(10.0 + SLOPE * x, (n, 1))     # every row the same: height grows with x


@pytest.fixture
def sloped(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("slope-demo")
    site = sw_site.site_dir(s)
    site.mkdir(parents=True, exist_ok=True)
    z = (0.0, 40.0)
    Image.fromarray(greybox.to_png16(slope_heights(), z)).save(site / "terrain.png")
    write_json(site / "terrain.json", {"extent_m": EXTENT, "z_range": list(z), "rows": "north (+Y) -> south"})
    return s


@pytest.fixture
def flat(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    return ProjectStore.create("flat-demo")


def test_the_ground_rules_on_a_slope(sloped):
    g = Ground.from_project(sloped)
    assert g.height(0, 0) == pytest.approx(10.0, abs=0.01) and g.height(50, 30) == pytest.approx(15.0, abs=0.01)
    assert g.under(0, 0, 20, 10, rule="lowest") == pytest.approx(9.0, abs=0.01)
    assert g.under(0, 0, 20, 10, rule="highest") == pytest.approx(11.0, abs=0.01)
    assert g.under(0, 0, 20, 10, rule="mean") == pytest.approx(10.0, abs=0.01)
    assert g.under(0, 0, 20, 10, rule="centre") == pytest.approx(10.0, abs=0.01)
    assert g.under(0, 0, 20, 10, yaw=90.0, rule="lowest") == pytest.approx(9.5, abs=0.01)   # turned: 10 m across x
    with pytest.raises(ValueError):
        g.under(0, 0, 1, 1, rule="sideways")


def test_a_project_without_terrain_stands_on_flat_ground(flat):
    g = Ground.from_project(flat)
    assert g.height(123.0, -45.0) == 0.0 and g.under(5, 5, 30, 30, 17.0) == 0.0


def test_grounding_puts_the_base_on_the_lowest_ground_under_the_outline(sloped):
    g = Ground.from_project(sloped)
    lo, hi = [-5.0, -5.0, 0.0], [5.0, 5.0, 8.0]               # a 10 m model, base at its origin
    W = cl.world_matrix({"x": 40.0, "y": 0.0, "z": 99.0, "yaw": 0.0})
    out = cl.grounded(W, lo, hi, g, "lowest")
    assert out[2, 3] == pytest.approx(10.0 + SLOPE * 35.0, abs=0.01)        # its uphill side is in the slope
    assert cl.grounded(W, lo, hi, g, "highest")[2, 3] == pytest.approx(10.0 + SLOPE * 45.0, abs=0.01)
    off = [-5.0, -5.0, -2.0], [5.0, 5.0, 6.0]                 # a model whose base is 2 m below its origin
    assert cl.grounded(W, *off, g, "lowest")[2, 3] == pytest.approx(10.0 + SLOPE * 35.0 + 2.0, abs=0.01)


def _case(g, key, x, y, dz=0.0, dyaw=0.0, slot="s1", parent=None):
    """A manifest row and the actor Unreal reports for it: placed dz off the ground (manifest and
    actor alike: a wrong height), then turned by dyaw in Unreal alone (moved)."""
    lo, hi = [-5.0, -5.0, 0.0], [5.0, 5.0, 8.0]
    W = cl.grounded(cl.world_matrix({"x": x, "y": y, "z": 0.0, "yaw": 0.0}), lo, hi, g, "lowest")
    W[2, 3] += dz
    t = ue_coords.to_ue(W)
    row = {"key": key, "asset": f"lib_{key}", "entry": "hall" if key != "k_box" else "box", "parent": parent, "world": t}
    actor = {"key": key, "loc": list(t["loc"]), "rot": [0.0, 0.0, t["rot"][2] + dyaw], "scale": [1.0, 1.0, 1.0]}
    return slot, row, actor, {"id": f"lib_{key}", "bounds": [lo, hi]}


def _run(g, cases, instances=None):
    slots = {}
    for slot, row, _, _ in cases:
        slots.setdefault(slot, []).append(row)
    man = {"slots": [{"id": s, "library": r} for s, r in slots.items()], "library_assets": [c[3] for c in cases]}
    bounds = {"s1": [[-50, -50, 0], [50, 50, 30]], "s2": [[60, 60, 0], [90, 90, 30]]}
    return pc.check(man, [c[2] for c in cases], instances or {}, bounds, g, UnrealSettings())


def test_the_check_finds_sunk_floating_moved_off_slot_and_overlapping_builds(sloped):
    g = Ground.from_project(sloped)
    cases = [_case(g, "k_ok", 0, 0), _case(g, "k_sunk", 20, 0, dz=-1.0), _case(g, "k_float", -20, 0, dz=0.8),
             _case(g, "k_turned", 0, 20, dyaw=5.0), _case(g, "k_box", 30, -30, slot="s2"),
             _case(g, "k_kid", 30, -30, slot="s2", parent="k_box")]
    left = {"s1": [[2000.0, 0.0, 1200.0]]}                  # a greybox piece left at (20, 0): inside k_sunk
    res = _run(g, cases, left)
    assert not res["ok"] and res["checked"] == 6
    assert res["issues"] == {"sunk": ["k_sunk"], "overlap": ["k_sunk"], "floating": ["k_float"],
                             "moved": ["k_turned"], "off_slot": ["k_box"]}           # the attached piece is exempt
    assert res["per_entry"]["hall"] == {"sunk": 1, "overlap": 1, "floating": 1, "moved": 1}
    text = pc.lines(res)
    assert text[0] == "placement: 1 moved, 1 sunk, 1 floating, 1 off slot, 1 overlap of 6 library actors"
    assert "  box: 1 off slot" in text


def test_a_flat_project_built_right_reports_placement_ok(flat):
    g = Ground.from_project(flat)
    res = _run(g, [_case(g, "k_a", 0, 0), _case(g, "k_b", 20, 20)])
    assert res["ok"] and pc.lines(res) == ["placement OK (2 library actors)"]


def test_tolerances_come_from_the_project_settings(flat):
    g = Ground.from_project(flat)
    case = _case(g, "k_a", 0, 0, dz=0.5)
    man = {"slots": [{"id": "s1", "library": [case[1]]}], "library_assets": [case[3]]}
    strict = pc.check(man, [case[2]], {}, {}, g, UnrealSettings())
    loose = pc.check(man, [case[2]], {}, {}, g, UnrealSettings(floating_m=1.0))
    assert strict["issues"] == {"floating": ["k_a"]} and loose["ok"]
