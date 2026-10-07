"""City-scale layout mode (asset_pipeline/city.py): plan, pads, spec, shots; a real Blender
build of a small city."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from asset_pipeline import city, greybox
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import CitySpec, CivicNode, Kit, SiteLayout, TerrainSpec
from asset_pipeline.stages import sw_site

REAL_BLENDER = Path("/snap/bin/blender")


def small_city(**kw) -> SiteLayout:
    return SiteLayout(
        terrain=TerrainSpec(extent_m=3000, resolution=257, shore_m=700, headland_m=60, hill_height_m=80,
                            hill_from_m=200, hill_to_m=1500, coast_amp_m=60, coast_wavelength_m=1200,
                            city_radius_m=0, z_range=(-60, 200)),
        kits=[Kit(id="k")],
        city=CitySpec(nodes=[CivicNode(id="a", center=(-300, -250), radius=160, role="forum", monument="rotunda",
                                       radials=6, spiral_deg_per_100m=5, weight=1.2, avenue_m=900),
                             CivicNode(id="b", center=(450, 150), radius=110, role="market", radials=5)],
                      bounds=(-1000, -700, 1000, 600), density_falloff_m=450, markets=1, **kw))


def heights(layout):
    return city.apply_pads(layout, greybox.make_terrain(layout.terrain))


def test_plan_is_deterministic_housing_dominates_and_density_falls_off():
    L = small_city()
    h = heights(L)
    a, b = city.plan(L, h), city.plan(L, h)
    assert a.stats == b.stats
    st = a.stats
    json.dumps(st)                                                # JSON-able (written to site/city.json)
    assert st["buildings"] > 200 and st["houses"] > 0 and st["housing_blocks"] > 0
    assert st["footprint_share"]["housing"] >= 0.6 and st["footprint_share"]["monument"] <= 0.03
    assert st["monuments"] == 1 and not st["warnings"]
    # dense mid-rise near the nodes, low-rise further out
    near = [bx.h for bx in a.buildings if min(math.dist((bx.x, bx.y), n.center) for n in L.city.nodes) < 350]
    far = [bx.h for bx in a.buildings if min(math.dist((bx.x, bx.y), n.center) for n in L.city.nodes) > 600]
    assert near and far and np.median(near) > np.median(far) + 3
    # every node's civic core is in the kit system, centred on the node
    assert {p.id for p in a.layout.plazas} == {"a-plaza", "b-plaza"}
    assert next(p for p in a.layout.plots if p.type == "rotunda").center == (-300, -250)
    # no building inside a civic core
    core = city._rings(L.city.nodes[0])["core"]
    assert all(math.dist((bx.x, bx.y), L.city.nodes[0].center) > core - 20 for bx in a.buildings)


def test_pads_level_each_node_and_grade_into_the_terrain():
    L = small_city()
    raw = greybox.make_terrain(L.terrain)
    h = city.apply_pads(L, raw)
    hs = greybox.Heights(h, L.terrain.extent_m)
    nd = L.city.nodes[0]
    a = np.linspace(0, 2 * math.pi, 16, endpoint=False)
    ring = hs.many(nd.center[0] + 0.9 * nd.radius * np.cos(a), nd.center[1] + 0.9 * nd.radius * np.sin(a))
    assert np.ptp(ring) < 0.5                                     # level across the core
    # no wall: the steepest grade around the pad is a slope, not a step
    xs = nd.center[0] + np.linspace(nd.radius, nd.radius + city.PAD_BLEND_M, 40)
    z = hs.many(xs, np.full_like(xs, nd.center[1]))
    assert np.abs(np.diff(z) / np.diff(xs)).max() < 0.35


def test_spec_instances_housing_as_scaled_unit_boxes_and_drapes_streets():
    L = small_city()
    h = heights(L)
    spec = greybox.build_spec(L, h)
    assert spec["city"]["buildings"] > 0
    housing = [p for s in spec["slots"] if s["type"] in ("housing", "houses") for p in s["pieces"]]
    assert housing and all(p["prim"] == greybox.UNIT_BOX and len(p["scale"]) == 3 for p in housing)
    assert len({s["id"] for s in spec["slots"] if s["type"] == "housing"}) < len(housing) / 20  # grouped per tile
    hs = greybox.Heights(h, L.terrain.extent_m)
    way = next(s for s in spec["slots"] if s["id"].startswith("way-a-av"))
    rib = way["pieces"][0]
    assert rib["prim"]["kind"] == "ribbon" and rib["prim"]["w"] == L.city.avenue_w
    ox, oy, oz = way["loc"]
    for x, y, z in rib["prim"]["pts"][::5]:
        wx, wy = ox + rib["loc"][0] + x, oy + rib["loc"][1] + y
        assert abs(oz + rib["loc"][2] + z - hs(wx, wy) - 0.12) < 0.3   # follows the ground
    types = {s["type"] for s in spec["slots"]}
    assert {"avenue", "street", "housing", "park", "market", "rotunda", "stoa", "block"} <= types


def test_city_shots_frame_the_city_and_each_node():
    L = small_city()
    shots = city.shots(L, heights(L))
    ids = [s.id for s in shots]
    assert [s.id for s in shots if s.tier == "wide"] == ["wide-hills", "wide-sea", "wide-aerial"]
    assert {"med-a", "med-a-high", "med-b", "med-b-high"} <= set(ids)
    assert [s.id for s in shots if s.tier == "tight"] == ["tight-a"]           # only the monument node
    sea = next(s for s in shots if s.id == "wide-sea")
    assert sea.pos[1] < -700                                                    # offshore (sea to the south)


@pytest.fixture
def city_site(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("c")
    L = small_city(trees=False)
    path = tmp_path / "layout.json"
    path.write_text(L.model_dump_json())
    sw_site.init(store, path)
    return store


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_real_build_keeps_scale_and_shares_one_housing_mesh(city_site):
    assert sw_site.load_layout(city_site).shots                     # auto-placed at init
    data = sw_site.build(city_site)
    assert (city_site.root / "site/city.json").is_file() and not data["untagged"]
    boxes = [p for s in data["slots"] if s["type"] in ("housing", "houses") for p in s["pieces"]]
    assert len({p["mesh"] for p in boxes}) == 1
    m = np.array(boxes[0]["matrix_local"])
    sx, sy, sz = np.linalg.norm(m[:3, :3], axis=0)
    assert min(sx, sy) > 5 and sz > 5                                # metres, not a unit cube
