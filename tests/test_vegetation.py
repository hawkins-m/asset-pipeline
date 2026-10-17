"""Vegetation data: street-tree slots (their own random stream: the city plan never moves) and
the export of every vegetation point for Unreal PCG."""
import math

import pytest

from asset_pipeline import kit_proc, vegetation
from asset_pipeline.project import ProjectStore, read_json, write_json


@pytest.fixture
def city_store(tmp_path, monkeypatch):
    from asset_pipeline.stages import sw_site
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("demo")
    sw_site.save_layout(s, sw_site.starter_city())
    return s


def test_street_tree_slots_leave_the_plan_alone(city_store):
    from asset_pipeline import city
    from asset_pipeline.stages import sw_site
    L = sw_site.load_layout(city_store)
    before = city.plan(L, sw_site.plan_terrain(city_store, L))
    rec = vegetation.street_tree_slots(city_store)
    after = city.plan(L, sw_site.plan_terrain(city_store, L))
    assert [(b.x, b.y) for b in before.buildings] == [(b.x, b.y) for b in after.buildings]
    assert before.trees == after.trees
    slots = rec["street_trees"]
    assert rec["counts"]["street"] > 0 and len({s["id"] for s in slots}) == len(slots)
    for s in slots[:200]:                                   # clear of the plan's buildings
        for b in before.buildings:
            if abs(s["x"] - b.x) < b.w and abs(s["y"] - b.y) < b.w:
                a = math.radians(b.rot)
                u = (s["x"] - b.x) * math.cos(a) + (s["y"] - b.y) * math.sin(a)
                v = -(s["x"] - b.x) * math.sin(a) + (s["y"] - b.y) * math.cos(a)
                assert not (abs(u) < b.w / 2 and abs(v) < b.d / 2)
    assert vegetation.street_tree_slots(city_store)["street_trees"] == slots          # deterministic


def test_street_trees_follow_their_settings(city_store):
    from asset_pipeline.stages import sw_site
    n = vegetation.street_tree_slots(city_store)["counts"]["street"]
    L = sw_site.load_layout(city_store)
    L.city.street_trees.spacing_m = 24.0
    L.city.street_trees.plazas = False
    sw_site.save_layout(city_store, L)
    rec = vegetation.street_tree_slots(city_store)
    assert rec["counts"]["plaza"] == 0 and rec["counts"]["street"] < 0.7 * n


def test_export_points_place_instance_zones(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("demo")
    glb = s.root / "3d/library/insula-a/instances/i1.glb"
    glb.parent.mkdir(parents=True)
    glb.write_bytes(b"glb")
    write_json(glb.parent / "index.json", [{"key": "i1", "glb": "3d/library/insula-a/instances/i1.glb",
                                            "x": 100.0, "y": 50.0, "z": 10.0, "yaw": 90.0}])
    write_json(kit_proc.zones_path(glb), {"zones": [{"type": "court_bed", "center": [2.0, 0.0, 0.25], "size": [4, 3, 0], "yaw": 0.0}]})
    rec = vegetation.export_points(s, s.root / "export/vegetation.json")
    p = rec["points"][0]
    assert p["type"] == "court_bed" and (p["x"], p["y"], p["z"]) == pytest.approx((100.0, 52.0, 10.25))
    assert p["ue"]["loc"] == pytest.approx([10000.0, -5200.0, 1025.0])               # cm, Y mirrored
    assert read_json(s.root / "export/vegetation.json")["counts"] == {"court_bed": 1}
