"""City mode with a typology catalog (asset_pipeline/city_types.py, catalog.py): YAML import,
assignment and variation, the anti-repetition report, slot tags; shot edits."""
import json
import math
from collections import Counter

import numpy as np
import pytest

from asset_pipeline import catalog, city, city_types, greybox
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import District, ShotSpec, Typology

from test_city import heights, small_city

CATALOG = """
typologies:
  - {id: courts, family: housing, form: perimeter, size_m: {width: [40, 80], depth: [12, 15]}, storeys: [4, 6],
     place: [core, avenue, interior], prompt: "courtyard blocks", roofs: [roof_garden, terracotta_hip],
     facades: [punched, arcade_base, loggia_top], ground: [residential, shops]}
  - {id: rows, family: housing, form: row, size_m: {width: [6, 9], depth: [11, 14]}, storeys: [2, 4],
     place: [interior, edge], roofs: [pergola, terracotta_pitch], facades: [render_a, render_b]}
  - {id: bars, family: housing, form: bar, size_m: {width: [40, 80], depth: [11, 13]}, storeys: [4, 6],
     place: [interior, edge, corridor], roofs: [roof_garden], facades: [balconies]}
  - {id: villas, family: housing, form: courtyard, size_m: {width: [16, 24], depth: [16, 24]}, storeys: [1, 2],
     place: [edge], roofs: [flat_white], facades: [blank], columns: accent, cap: {max_share: 0.5}}
  - {id: hall, family: civic, form: hall, size_m: {width: [30, 50], depth: [40, 70]}, storeys: [2, 3],
     place: [core, interior], roofs: [barrel_vault], facades: [tall_arches], cap: {max_count: 2}}
  - {id: tower, family: housing, form: podium_tower, size_m: {width: [18, 24], depth: [18, 24]},
     storeys: [12, 16], place: [crossing, core], roofs: [garden_crown], cap: {max_count: 3}}
  - {id: gate, family: public_realm, form: pavilion, size_m: {width: [20, 30], depth: [8, 12]},
     storeys: [3, 4], place: [node_ring], columns: accent, cap: {max_count: 2}}
  - {id: rotunda, family: civic, form: drum, columns: order, material: warm, ref: refs/landmark.jpg}
materials:
  - {id: stone, words: "pale stone"}
  - {id: brick, words: "warm brick"}
  - {id: warm, words: "rose granite"}
districts:
  a:
    identity: "the civic centre"
    palette: {stone: 3, brick: 1}
    mix: {core: {courts: 3, hall: 1, tower: 0.5}, middle: {courts: 2, rows: 2, bars: 1}, edge: {rows: 2, villas: 2}}
  b:
    identity: "the market quarter"
    palette: {brick: 1}
    columns: none
    height_bias: -1
    mix: {core: {courts: 2, rows: 1}, middle: {rows: 2, bars: 1}, edge: {rows: 1, villas: 1}}
overlays:
  node_ring: {adds: {gate: 1}}
checks: {max_typology_share_city: 0.5}
"""


@pytest.fixture
def typed(tmp_path):
    p = tmp_path / "cat.yaml"
    p.write_text(CATALOG)
    L = catalog.apply(small_city(trees=False), catalog.load(p))
    return L


def test_yaml_import_merges_and_round_trips(typed):
    L = typed
    t = {t.id: t for t in L.city.typologies}
    assert t["courts"].width == (40, 80) and t["villas"].max_share == 0.5 and t["hall"].max_count == 2
    assert city_types.site_of(t["courts"]) == "block" and city_types.site_of(t["gate"]) == "node_gate"
    assert city_types.site_of(t["rotunda"]) == "kit"
    assert {d.id: d.notes for d in L.districts} == {"a": "the civic centre", "b": "the market quarter"}
    assert L.city.checks.max_typology_share_city == 0.5
    again = catalog.apply(L, catalog.yaml.safe_load(catalog.dump(L)))
    assert again == L


def test_unknown_names_are_refused(typed):
    with pytest.raises(ValueError, match="unknown typology nope"):
        catalog.apply(typed, {"districts": {"a": {"mix": {"core": {"nope": 1}}}}})
    with pytest.raises(ValueError, match="unknown material marble"):
        catalog.apply(typed, {"districts": {"a": {"palette": {"marble": 1}}}})


def test_plan_mixes_typologies_with_variation_and_keeps_to_the_checks(typed):
    h = heights(typed)
    P, Q = city.plan(typed, h), city.plan(typed, h)
    assert P.stats == Q.stats                                          # deterministic
    json.dumps(P.stats)
    assert not P.buildings and P.typed                                 # the catalog replaces the legacy types
    rep = P.stats["repetition"]
    assert {"courts", "rows", "villas"} <= set(rep["typologies"]) and len(rep["typologies"]) >= 5
    assert rep["typologies"].get("hall", 0) <= 2 and rep["typologies"].get("gate", 0) <= 2   # caps
    assert rep["longest_identical_run"] <= typed.city.checks.max_identical_run
    assert rep["column_share"] <= 0.1
    assert len(rep["roofs"]) >= 3 and len(rep["facades"]) >= 4
    # district b has no columns and a lower height bias
    b = [x for x in P.typed if x.district == "b"]
    assert b and not any(x.columns for x in b)
    # variation shows in the massing: arcades are recessed ground floors, roofs are shapes
    roles = Counter(p.role for x in P.typed for p in x.parts)
    assert roles["ground"] and roles["roof-garden"] and roles["roof"] and roles["setback"]
    # materials come from the district palette, one per (typology, district, tile)
    assert {x.material for x in P.typed if x.district == "b"} <= {"brick"}


def test_one_type_everywhere_is_reported(typed):
    L = typed.model_copy(deep=True)
    for d in L.districts:
        d.mix = {band: {"courts": 1} for band in ("core", "middle", "edge")}
    L.city.overlays = {}
    rep = city.plan(L, heights(L)).stats["repetition"]
    assert any("courts covers" in w for w in rep["warnings"])
    assert any("diversity" in w for w in rep["warnings"])


def test_slots_carry_typology_and_material_and_share_unit_meshes(typed):
    spec = greybox.build_spec(typed, heights(typed))
    typed_slots = [s for s in spec["slots"] if s.get("typology") in {"courts", "rows", "bars", "villas"}]
    assert typed_slots and all(s["type"] == s["typology"] for s in typed_slots)
    assert {s["material"] for s in typed_slots} <= {"stone", "brick"}
    boxes = {json.dumps(p["prim"], sort_keys=True) for s in typed_slots for p in s["pieces"]
             if p["prim"]["kind"] == "box"}
    assert boxes == {json.dumps(city.UNIT["box"], sort_keys=True)}       # one mesh for every box
    gable = [p for s in typed_slots for p in s["pieces"] if p["prim"]["kind"] == "gable"]
    assert gable and all(p["prim"] == city.UNIT["gable"] for p in gable)
    # the rotunda ensemble is tagged with the landmark's typology and pinned material
    ens = [s for s in spec["slots"] if s.get("typology") == "rotunda"]
    assert {s["type"] for s in ens} == {"rotunda", "colonnade"} and {s["material"] for s in ens} == {"warm"}


def test_shot_nudges_move_in_the_camera_frame():
    s = ShotSpec(id="x", pos=(0, 0, 10), look_at=(0, 100, 10), lens_mm=24, nudge_pos=(2, 1, 5),
                 nudge_target=(0, 3, 0), lens_override=35, tier_override="tight")
    e = s.effective()
    assert e.pos == pytest.approx((2, 5, 11))                  # right = +X, forward = +Y, up = +Z
    assert e.look_at == pytest.approx((0, 100, 13))
    assert (e.lens_mm, e.tier) == (35, "tight") and s.camera_edited()
    assert not ShotSpec(id="y", pos=(0, 0, 0), look_at=(1, 0, 0)).camera_edited()


def test_auto_shots_keep_the_users_edits(tmp_path, monkeypatch):
    from asset_pipeline.stages import sw_site
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("c")
    L = small_city(trees=False)
    path = tmp_path / "layout.json"
    path.write_text(L.model_dump_json())
    sw_site.init(store, path)
    layout = sw_site.load_layout(store)
    layout.shots[0].prompt_append = "golden hour"
    layout.shots[0].nudge_pos = (0, 5, 0)
    sw_site.save_layout(store, layout)
    shots = sw_site.set_city_shots(store)
    assert shots[0].prompt_append == "golden hour" and shots[0].nudge_pos == (0, 5, 0)


def test_mark_user_edits_records_changed_fields():
    old = [District(id="a", notes="x"), District(id="b")]
    new = [District(id="a", notes="y"), District(id="b"), District(id="c", notes="z")]
    out = {d.id: d.user_fields for d in catalog.mark_user_edits(old, new)}
    assert out == {"a": ["notes"], "b": [], "c": ["id", "notes"]}
    # later saves keep the marks
    again = catalog.mark_user_edits(new, [d.model_copy() for d in new])
    assert again[0].user_fields == ["notes"]


def test_typology_fields_roundtrip_in_layout_json(typed):
    t = Typology.model_validate(typed.city.typologies[0].model_dump())
    assert t == typed.city.typologies[0]
    assert math.isfinite(np.mean([1]))


def test_height_field_drifts_within_a_band_and_accents_rise_at_crossings(typed):
    from asset_pipeline.schema import Variation
    L = typed.model_copy(deep=True)
    L.city.variation = Variation(storeys_jitter=0, avenue_bonus=0, height_noise=2.5, height_noise_scale_m=400,
                                 accent_chance=1.0, accent_storeys=4)
    P = city.plan(L, heights(L))
    courts = [b for b in P.typed if b.typology == "courts" and not b.accent]
    # same typology, no jitter: storeys still spread, by place (the field), beyond the 4-6 range
    assert max(b.storeys for b in courts) - min(b.storeys for b in courts) >= 4
    acc = [b for b in P.typed if b.accent]
    assert acc and all(b.family in ("housing", "mixed") for b in acc)
    T = city_types.Typer(P, city.Fields(L, heights(L)), np.random.default_rng(0), [])
    assert all(T.near_crossing(b.x, b.y) for b in acc)
    a, b = T.height_field("a", 0, 0), T.height_field("a", 1, 1)
    assert abs(a - b) < 0.05 and T.height_field("a", 0, 0) != T.height_field("b", 0, 0)   # smooth; per district
    assert P.stats["repetition"]["accents"] == len(acc)


def test_corner_bonus_raises_perimeter_corners(typed):
    from asset_pipeline.schema import Variation
    L = typed.model_copy(deep=True)
    L.city.variation = Variation(storeys_jitter=0, avenue_bonus=0, step_back_top=0)
    h = heights(L)
    base = city.plan(L, h).stats["repetition"]["height_cv_median"]
    L.city.variation.corner_bonus = 2
    assert city.plan(L, h).stats["repetition"]["height_cv_median"] > base


def test_big_types_merge_neighbouring_blocks_and_drop_their_streets(typed):
    L = typed.model_copy(deep=True)
    for t in L.city.typologies:
        if t.id == "hall":
            t.merge_chance, t.max_count, t.width, t.depth = 1.0, 3, (16, 22), (16, 22)
    for d in L.districts:
        d.mix["core"] = {"hall": 5, "courts": 1}
    h = heights(L)
    P = city.plan(L, h)
    merged = [b for b in P.typed if b.merged > 1]
    assert merged and all(b.typology == "hall" and 2 <= b.merged <= 4 for b in merged)
    assert max(p.w * p.d for b in merged for p in b.parts if p.role == "mass") > 22 * 22   # past the 22 x 22 range
    for d in L.districts:            # a district can switch merging off
        d.merge_chance = 0.0
    assert not [b for b in city.plan(L, h).typed if b.merged > 1]
    # no street is left running through a merged building
    streets = [w.line for w in P.ways if w.kind == "street"]
    for b in merged:
        for p in b.parts:
            if p.role == "mass":
                foot = city_types._rect(p.x, p.y, p.w - 2, p.d - 2, p.rot)
                assert not any(ln.intersects(foot) for ln in streets)


def test_aerial_previews_frame_the_whole_city():
    from asset_pipeline.stages import sw_shots
    L = small_city()
    cams = sw_shots.preview_cameras(L)
    x0, y0, x1, y1 = L.city.bounds
    for c in cams:
        assert c["look_at"][:2] == [(x0 + x1) / 2, (y0 + y1) / 2]
        p, t = np.array(c["pos"]), np.array(c["look_at"])
        half = math.hypot(x1 - x0, y1 - y0) / 2
        # the city's half-diagonal fits half the horizontal field of view
        assert half / np.linalg.norm(p - t) <= math.tan(math.atan(18 / c["lens_mm"])) + 1e-6
