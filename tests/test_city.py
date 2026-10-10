"""City-scale layout mode (asset_pipeline/city.py): plan, pads, spec, shots; a real Blender
build of a small city."""
import json
import math
from pathlib import Path

import numpy as np
import pytest
import shapely.geometry

from asset_pipeline import city, greybox
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import CitySpec, CivicNode, Enclosure, Kit, SiteLayout, TerrainSpec, Town
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
    assert {p.id for p in a.layout.plazas} == {"a-plaza", "a-lagoon", "b-plaza"}
    # no building inside a civic core
    core = city._rings(L.city.nodes[0])["core"]
    assert all(math.dist((bx.x, bx.y), L.city.nodes[0].center) > core - 20 for bx in a.buildings)


def test_rotunda_is_an_ensemble_set_back_behind_a_lagoon_between_two_colonnades():
    L = small_city()
    P = city.plan(L, heights(L))
    plots = {p.id: p for p in P.layout.plots}
    rot, lagoon = plots["a-rotunda"], next(p for p in P.layout.plazas if p.id == "a-lagoon")
    assert lagoon.type == "lagoon"
    # the sea is to the south: the rotunda stands inland of the node centre, the lagoon seaward
    assert rot.center[1] > -250 > lagoon.center[1]
    wings = [plots["a-colonnade-0"], plots["a-colonnade-1"]]
    for w in wings:     # one arc around the lagoon, running through the rotunda
        assert w.type == "colonnade" and w.center == lagoon.center
        assert w.arc[0] == pytest.approx(math.dist(rot.center, lagoon.center))
    # the lagoon's edge stops short of the rotunda
    assert math.dist(rot.center, lagoon.center) - lagoon.radius > rot.size[0] / 2 + 5
    spec = greybox.build_spec(L, heights(L))
    s = next(s for s in spec["slots"] if s["id"] == "a-rotunda")
    arches = [p for p in s["pieces"] if p["prim"]["kind"] == "archwall"]
    assert len(arches) == 8 and all(p["prim"]["spring"] > 0.6 * p["prim"]["span"] for p in arches)
    dome = next(p for p in s["pieces"] if p["piece"] == "dome")
    assert dome["scale"][2] <= 1.0                                  # never taller than a half sphere
    columns = [p for p in s["pieces"] if "column-shaft" in p["piece"]]
    assert len(columns) == 16                                       # paired at the 8 corners
    assert {x["type"] for x in spec["slots"] if x["id"].startswith("a-colonnade")} == {"colonnade"}


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


def walled_city() -> SiteLayout:
    """small_city inside a ring wall, with a town across a bay to the east."""
    L = small_city(enclosure=Enclosure(center=(-200, -250), radius=520, wall_h=10),
                   towns=[Town(id="t", center=(1000, -300), radius=220)])
    L.city.bounds = (-1000, -900, 1400, 600)
    L.terrain.bay_m, L.terrain.bay_lateral_m, L.terrain.bay_width_m = 380, 620, 160
    return L


def test_bay_pulls_the_shore_inland_at_its_lateral_position():
    t = walled_city().terrain
    plain = t.model_copy(update={"bay_m": 0})
    lat = np.array([620.0, -800.0])
    d = greybox.shore_at(plain, lat) - greybox.shore_at(t, lat)
    assert d[0] == pytest.approx(380) and d[1] == pytest.approx(0, abs=1e-6)


def test_walled_city_is_dense_inside_a_wall_with_gates_and_a_town_across_the_bay():
    L = walled_city()
    h = heights(L)
    P = city.plan(L, h)
    e, tw = L.city.enclosure, L.city.towns[0]
    f = city.Fields(L, h)
    # nothing built outside the wall but the town; the town has its own district and square
    assert all(math.dist((b.x, b.y), e.center) < e.radius or math.dist((b.x, b.y), tw.center) < 1.2 * tw.radius
               for b in P.buildings)
    assert sum(b.district == "t" for b in P.buildings) > 30
    assert any(sq.contains(shapely.geometry.Point(tw.center)) for sq, _ in P.markets)
    # dense up to the wall: no detached houses inside it
    assert not [b for b in P.buildings if b.kind == "houses" and math.dist((b.x, b.y), e.center) < e.radius]
    # gates on land, each with an avenue in and a road out through it
    assert len(P.gates) == 4
    for k, a in enumerate(P.gates):
        g = shapely.geometry.Point(np.array(e.center) + e.radius * city._dir(a))
        assert {f"gate{k}-av", f"gate{k}-road"} <= {w.id for w in P.ways if w.line.distance(g) < 30}
    # the wall: whole bays of one mesh on the circle, on land, open at the gates
    bays = [w for w in P.walls if w.piece == "wall:bay"]
    assert len({str(w.prim) for w in bays}) == 1
    assert all(abs(math.dist((w.x, w.y), e.center) - e.radius) < 0.5 for w in bays)
    assert all(f.inland(w.x, w.y) >= 15 for w in bays)
    assert all(min(abs(city._angdiff(math.degrees(math.atan2(w.y - e.center[1], w.x - e.center[0])), a))
                   for a in P.gates) * math.pi / 180 * e.radius > e.gate_w / 2 for w in bays)
    # a greenbelt outside, in hole-free pieces (lawns are drawn from their outline)
    belt = [pk for pk in P.parks if math.dist(pk.representative_point().coords[0], e.center) > e.radius]
    assert belt and all(not pk.interiors for pk in P.parks)
    spec = greybox.build_spec(L, h)
    assert {"wall", "gate"} <= {s["type"] for s in spec["slots"]}


def test_walled_city_wides_frame_the_circle_and_the_aerial_is_a_high_oblique():
    from asset_pipeline.stages import s0_frames
    L = walled_city()
    shots = {s.id: s for s in city.shots(L, heights(L))}
    a = shots["wide-aerial"]
    d = np.array(a.look_at) - np.array(a.pos)
    pitch = math.degrees(math.atan2(d[2], math.hypot(d[0], d[1])))
    assert pitch == pytest.approx(-L.city.aerial_pitch, abs=0.5)
    # the whole circle fits the 24 mm frame: its near and far edges too
    dist, R, p = np.linalg.norm(d), L.city.enclosure.radius, math.radians(L.city.aerial_pitch)
    vh = math.atan(36 * 768 / 1344 / 2 / a.lens_mm)
    X, H = dist * math.cos(p), dist * math.sin(p)
    assert math.atan2(H, X - R) <= p + vh and math.atan2(H, X + R) >= p - vh
    fwd = d / np.linalg.norm(d)
    m = [[0, 0, -fwd[0]], [0, 0, -fwd[1]], [0, 0, -fwd[2]]]       # a camera looks down its local -Z
    assert s0_frames.view_of({"matrix": m, "tier": "wide"}) == "aerial"
    assert shots["wide-sea"].pos[1] < -900 and shots["wide-hills"].pos[1] > 600


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


def hill_city(**node):
    L = small_city()
    L.city.nodes[1] = L.city.nodes[1].model_copy(update={"role": "hill", "monument": "temple", "layout": "organic",
                                                         "radius": 160.0, **node})
    return L


def test_organic_node_has_no_rings_keeps_its_slope_and_is_walkable():
    L = hill_city()
    h = heights(L)
    P = city.plan(L, h)
    b = L.city.nodes[1]
    assert not [r for r in P.layout.rings if r.district == "b"]
    assert not [r for r in P.layout.rows if r.district == "b"]
    assert not [p for p in P.layout.plots if p.district == "b" and p.type == "stoa"]
    assert not [w for w in P.ways if w.id == "b-outer"]
    temple = next(p for p in P.layout.plots if p.id == "b-temple")
    sx, sy = city.summit(b, greybox.Heights(h, L.terrain.extent_m).many)
    assert math.dist(temple.center, (sx, sy)) < 15                        # on the summit
    # only the summit plaza is levelled: the slope stays across the rest of the node
    raw = greybox.Heights(greybox.make_terrain(L.terrain), L.terrain.extent_m)
    padded = greybox.Heights(h, L.terrain.extent_m)
    a = np.linspace(0, 2 * math.pi, 16, endpoint=False)
    ring = (b.center[0] + 0.9 * b.radius * np.cos(a), b.center[1] + 0.9 * b.radius * np.sin(a))
    far = np.hypot(ring[0] - sx, ring[1] - sy) > city.organic_plaza_r(b) * 1.15 + city.ORGANIC_PAD_BLEND_M
    assert far.sum() >= 8 and np.allclose(raw.many(*ring)[far], padded.many(*ring)[far], atol=0.5)
    assert np.ptp(padded.many(*ring)) > 10                                 # a real slope, not a terrace
    lanes = [w for w in P.ways if w.district == "b" and w.kind in ("lane", "stair")]
    assert len({w.id.split("-")[1] for w in lanes if "lane" in w.id}) >= 3
    assert P.stats["repetition"] is None or P.stats["repetition"]["walkable_share"] >= 0.95


def test_organic_reroll_changes_the_lanes():
    L1, L2 = hill_city(seed=0), hill_city(seed=5)
    h = heights(L1)
    f = city.Fields(L1, h)
    ends = lambda L: sorted(round(w.line.coords[-1][0]) for w in city.ways(city.civic_layout(L, f), f)  # noqa: E731
                            if w.district == "b")
    assert ends(L1) != ends(L2)


def test_quad_centre_and_rare_stoas():
    L = small_city()
    L.city.nodes[1] = L.city.nodes[1].model_copy(update={"centre": "quad"})
    L.city.nodes[0] = L.city.nodes[0].model_copy(update={"stoa_share": 0.2, "civic_ring": "plain"})
    h = heights(L)
    P = city.plan(L, h)
    cl = [p for p in P.layout.plots if p.type == "cloister"]
    assert len(cl) == 4 and next(p for p in P.layout.plazas if p.id == "b-plaza").type == "court"
    assert len([p for p in P.layout.plots if p.type == "stoa" and p.district == "a"]) == 1
    assert next(r for r in P.layout.rows if r.id == "a-civic").variant == "plain"
    med = next(s for s in city.shots(L, h) if s.id == "med-b")
    half = city.QUAD_HALF * city._rings(L.city.nodes[1])["plaza"]
    cx, cy = L.city.nodes[1].center
    rel = [abs(c) for c in greybox._rot(med.pos[0] - cx, med.pos[1] - cy, -L.city.nodes[1].rot)]
    assert max(rel) < half and min(rel) > 0.3 * half                       # in the court, near a corner
    assert math.dist(med.pos[:2], med.look_at[:2]) > half                  # looking along the court
    assert next(s for s in greybox.build_spec(L, h)["slots"] if s["id"] == "plaza-b-plaza")["type"] == "plaza"
    spec = greybox.build_spec(L, h)
    s = next(s for s in spec["slots"] if s["id"] == "b-cloister-0")
    arc = next(p for p in s["pieces"] if p["prim"]["kind"] == "arcade")
    # the arcade is on the court side of the range: nearer the node's centre than the range's middle
    wx, wy = greybox._rot(arc["loc"][0], arc["loc"][1], s["rot_z"])
    assert math.dist((s["loc"][0] + wx, s["loc"][1] + wy), L.city.nodes[1].center) < \
        math.dist(s["loc"][:2], L.city.nodes[1].center)
    assert arc["prim"]["n"] >= 3 and 0.85 < arc["scale"][0] < 1.15
    plain = next(s for s in spec["slots"] if s["id"].startswith("a-civic-"))
    assert not [p for p in plain["pieces"] if "pier" in p["piece"]]


def test_way_ids_are_unique():
    L = hill_city()
    P = city.plan(L, heights(L))
    ids = [w.id for w in P.ways]
    assert len(ids) == len(set(ids))


def forum_city() -> SiteLayout:
    L = small_city()
    L.city.nodes[1] = L.city.nodes[1].model_copy(update={"centre": "forum", "radius": 140, "radials": 6, "rot": 10})
    return L


def _local(nd, x, y):
    return greybox._rot(x - nd.center[0], y - nd.center[1], -nd.rot)


def test_forum_square_porticoes_gates_on_the_radials_and_a_stall_grid():
    L = forum_city()
    nd = L.city.nodes[1]
    h = heights(L)
    P = city.plan(L, h)
    plots = [p for p in P.layout.plots if p.district == "b"]
    por = [p for p in plots if p.type == "portico"]
    gates = [p for p in plots if p.type == "forum_gate"]
    assert len(por) == 4 and len(gates) == 6 and [p.type for p in plots].count("fountain") == 1
    assert all(p.typology == city.FORUM_TYPOLOGY for p in plots)
    # the ranges' outer corners stay inside the core; no ring road or civic ring row there
    cl, cw = city.forum_dims(nd)
    assert math.hypot(cl / 2 + city.FORUM_RANGE_M, cw / 2 + city.FORUM_RANGE_M) < 0.7 * nd.radius + 6
    assert not [r for r in P.layout.rings if r.district == "b"] and not [r for r in P.layout.rows if r.district == "b"]
    # every gate stands where a radial comes in, and the radial starts outside the ranges
    angles = sorted((math.degrees(math.atan2(g.center[1] - nd.center[1], g.center[0] - nd.center[0])) - nd.rot) % 360
                    for g in gates)
    assert np.allclose(angles, [0, 60, 120, 180, 240, 300], atol=0.5)
    rads = [r for r in P.layout.radials if r.id.startswith("b-r")]
    assert all(r.r_from >= cw / 2 + city.FORUM_RANGE_M - 0.01 for r in rads)
    # each range's gaps sit where its gates are
    spec = greybox.build_spec(L, h)
    slots = {s["id"]: s for s in spec["slots"]}
    for p in por:
        assert len(p.gaps) == {"long": 2, "short": 1}["long" if "long" in p.id else "short"]
        cols = [q for q in slots[p.id]["pieces"] if "column-shaft" in q["piece"]]
        assert cols and all(abs(q["loc"][0] - gx) > gw / 2 for q in cols for gx, gw in p.gaps)
        # the colonnade faces the court: its columns are on the centre's side of the range
        assert all(q["loc"][1] < 0 for q in cols)
    # stalls: in the court, clear of the fountain, small, of every kind, sharing meshes
    court, stalls = next((sq, st) for sq, st in P.markets if sq.contains(shapely.geometry.Point(nd.center)))
    assert {b.kind for b in stalls} == {"hut", "tent", "table"} and len(stalls) > 50
    assert all(court.contains(shapely.geometry.Point(b.x, b.y)) for b in stalls)
    assert all(math.dist((b.x, b.y), nd.center) > city.FORUM_FOUNTAIN_M / 2 + 4 for b in stalls)
    assert max(b.h for b in stalls) < 3.0
    mk = next(s for s in spec["slots"] if s["type"] == "market" and math.dist(s["loc"][:2], nd.center) < 1)
    huts = [q for q in mk["pieces"] if q["piece"] == "market:hut"]
    assert len({json.dumps(q["prim"], sort_keys=True) for q in huts}) == 1
    # olive rows along the long sides, inside the court
    olives = [t for t in P.trees if court.contains(shapely.geometry.Point(t[0], t[1]))]
    assert olives and {t[5] for t in olives} == {"olive"}
    # the eye-level shot stands inside the court; the raised one looks down into it
    shots = {s.id: s for s in city.shots(L, h)}
    x, y = _local(nd, *shots["med-b"].pos[:2])
    assert abs(x) < cl / 2 and abs(y) < cw / 2
    hi = shots["med-b-high"]
    down = math.degrees(math.atan2(hi.pos[2] - hi.look_at[2], math.dist(hi.pos[:2], hi.look_at[:2])))
    assert 20 < down < 35


def test_forum_leaves_the_rest_of_the_city_alone():
    L0 = small_city()
    L1 = forum_city()
    L0.city.nodes[1] = L1.city.nodes[1].model_copy(update={"centre": "auto"})
    h = heights(L0)
    a = {s["id"]: s for s in greybox.build_spec(L0, h)["slots"]}
    b = {s["id"]: s for s in greybox.build_spec(L1, h)["slots"]}
    same = [k for k in set(a) & set(b) if a[k]["district"] != "b" and not k.startswith("radial-b")
            and json.dumps(a[k], sort_keys=True) == json.dumps(b[k], sort_keys=True)]
    others = [k for k in set(a) & set(b) if a[k]["district"] != "b" and not k.startswith("radial-b")]
    assert len(same) >= len(others) - 2      # at most a park or two the forum's olive rows join


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_forum_builds_in_blender(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("f")
    path = tmp_path / "layout.json"
    path.write_text(forum_city().model_copy(update={"city": forum_city().city.model_copy(update={"trees": False})})
                    .model_dump_json())
    sw_site.init(store, path)
    data = sw_site.build(store)
    got = {s["id"]: s for s in data["slots"]}
    assert len(got["b-gate-0"]["pieces"]) == 2 and len(got["b-portico-longp"]["pieces"]) > 100
