"""Variant sets (several entries for one prop type, picked per prop slot), the area panel's form
gaps with their suggested fixes, and kit modules' sizes from their kit decision."""
import pytest
from fastapi.testclient import TestClient

from asset_pipeline import areas, library_entries as le, variants
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import write_json
from asset_pipeline.schema import Enclosure
from asset_pipeline.stages import sw_library, sw_shots, sw_site
from asset_pipeline.ui import app as ui_app

from tests.test_library_entries import _entry, proj  # noqa: F401  (fixture)


def _canopy(i, x):
    return {"name": f"market-0.market:canopy.{i:03d}", "piece": "market:canopy", "mesh": "unit",
            "matrix_local": [[1, 0, 0, x], [0, 1, 0, 0.5 * i], [0, 0, 1, 2.7], [0, 0, 0, 1]]}


@pytest.fixture
def market(proj):            # noqa: F811
    slots = [{"id": "market-0", "type": "market", "district": "market", "typology": None,
              "matrix": [[1, 0, 0, 100], [0, 1, 0, 50], [0, 0, 1, 20], [0, 0, 0, 1]],
              "pieces": [_canopy(i, 3.0 * i) for i in range(60)]},
             {"id": "market-1", "type": "market", "district": "town", "typology": None,
              "matrix": [[1, 0, 0, 900], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
              "pieces": [_canopy(i, 3.0 * i) for i in range(10)]},
             {"id": "wall-0", "type": "wall", "typology": None, "matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
              "pieces": []}]
    write_json(sw_site.site_dir(proj) / "greybox.json", {"slots": slots, "shots": [{"id": "med-a"}]})
    write_json(sw_shots.shot_dir(proj, "med-a") / "ids.json", {"slots": {"market-0": {"frac": 0.3}, "wall-0": {"frac": 0.05}}})
    write_json(sw_library.library_path(proj), {"entries": [
        _entry("canopy-table", sources=[{"shot": "med-a", "share": 0.3}]),
        _entry("stall-b", custom=True, slot_ids=[]), _entry("stall-c", custom=True, slot_ids=[]),
        _entry("market-hut", typology="market hut", part="market hut"),
        _entry("wall", tier="B", kind="structure", typology="wall", part="wall", route_auto="procedural",
               size_m=[1000, 148, 26], slot_ids=["wall-0"]),
        _entry("depot-concrete", tier="C", kind="building", typology="depot", part="depot",
               route_auto="procedural", size_m=[55, 40, 6.8], slot_ids=["wall-0"]),
        _entry("court-house-slate", tier="C", kind="building", typology="court_house", part="court_house",
               route_auto="procedural", size_m=[40, 32.5, 13.6], slot_ids=["wall-0"])], "unseen": []})
    sw_site._greybox_memo.d = None
    areas.define(proj, "west", shots=["med-a"])
    return proj


def test_picks_are_reproducible_follow_the_weights_and_adding_only_moves_slots_to_the_new_member(market):
    variants.save(market, "canopy-table", {"canopy-table": 1, "stall-b": 1})
    a = {r["key"]: r["entry"] for r in variants.assign(market, "canopy-table")}
    assert len(a) == 70 and a == {r["key"]: r["entry"] for r in variants.assign(market, "canopy-table")}
    assert 20 <= sum(e == "stall-b" for e in a.values()) <= 50
    variants.save(market, "canopy-table", {"canopy-table": 1, "stall-b": 1, "stall-c": 2})
    b = {r["key"]: r["entry"] for r in variants.assign(market, "canopy-table")}
    moved = [k for k in a if a[k] != b[k]]
    assert moved and all(b[k] == "stall-c" for k in moved)          # nobody swaps between the old members
    assert 25 <= sum(e == "stall-c" for e in b.values()) <= 45      # about half of 70
    s = variants.summary(market, "canopy-table")
    assert s["slots"] == 70 and sum(m["slots"] for m in s["members"]) == 70
    assert {m["entry"]: m["share"] for m in s["members"]}["stall-c"] == 0.5


def test_areas_scope_and_area_membership(market):
    variants.save(market, "canopy-table", {"canopy-table": 0, "stall-b": 1}, areas=["west"])
    rows = variants.assign(market, "canopy-table")
    assert {r["entry"] for r in rows if r["slot"] == "market-0"} == {"stall-b"}
    assert {r["entry"] for r in rows if r["slot"] == "market-1"} == {"canopy-table"}    # outside the set's areas
    assert {r["slot"] for r in variants.assign(market, "canopy-table", area="west")} == {"market-0"}
    m = areas.members(market, "west")
    assert m["stall-b"] == m["canopy-table"] == 0.3            # members join the base's area
    row = next(r for r in areas.status(market, "west") if r["asset"] == "stall-b")
    assert row["variant_of"] == "canopy-table" and row["route"] == "trellis"
    assert "stall-c" not in m


def test_save_refuses_bad_sets(market):
    with pytest.raises(ValueError, match="prop type of the library plan"):
        variants.save(market, "stall-b", {})
    with pytest.raises(ValueError, match="not a prop"):
        variants.save(market, "canopy-table", {"wall": 1})
    with pytest.raises(ValueError, match="weight"):
        variants.save(market, "canopy-table", {"stall-b": -1})
    with pytest.raises(ValueError, match="above 0"):
        variants.save(market, "canopy-table", {"canopy-table": 0})
    with pytest.raises(ValueError, match="no area"):
        variants.save(market, "canopy-table", {}, areas=["nowhere"])
    variants.save(market, "market-hut", {"stall-c": 1})
    with pytest.raises(ValueError, match="already in the variant set of market-hut"):
        variants.save(market, "canopy-table", {"stall-c": 1})
    assert variants.set_of(market, "stall-c") == "market-hut"
    variants.remove(market, "market-hut")
    assert variants.set_of(market, "stall-c") is None


def test_variant_api(market):
    c = TestClient(ui_app.create_app(JobQueue()))
    base = "/api/projects/p/library/variants"
    r = c.get(f"{base}/canopy-table").json()
    assert r["set"] is None and r["can_base"] and {"stall-b", "stall-c"} <= set(r["candidates"])
    assert not c.get(f"{base}/stall-b").json()["can_base"]
    s = c.post(f"{base}/canopy-table", json={"members": {"canopy-table": 1, "stall-b": 3}}).json()
    assert s["slots"] == 70 and [m["entry"] for m in s["members"]] == ["canopy-table", "stall-b"]
    assert c.get(f"{base}/stall-b").json()["set"]["base"] == "canopy-table"
    assert c.post(f"{base}/canopy-table", json={"members": {"wall": 1}}).status_code == 400
    assert c.post(f"{base}/nope", json={"members": {}}).status_code == 404
    assert c.delete(f"{base}/canopy-table").json() == {"removed": "canopy-table"}
    assert c.get(f"{base}/canopy-table").json()["set"] is None


def test_form_gaps_suggest_a_fix_each(market, monkeypatch):
    monkeypatch.setattr(sw_site, "load_layout", lambda store: type("L", (), {
        "city": type("C", (), {"enclosure": Enclosure(wall_h=14.0, wall_d=6.0), "typologies": []})()})())
    rows = [{"asset": a, "entry": a, "route": "procedural", "brief": False, "status": None, "missing": ["form"]}
            for a in ("wall", "depot-concrete", "court-house-slate")]
    gaps = {g["asset"]: g for g in areas.form_gaps(market, rows)}
    w = gaps["wall"]["fixes"][0]
    assert w["kind"] == "form" and w["form"] == "arcade_wall" and w["size_m"] == [9.0, 6.0, 14.0]
    assert "wall bay" in w["why"]
    m = gaps["depot-concrete"]["fixes"]
    assert m[0] == m[0] | {"kind": "route", "route": "trellis"} and m[1]["form"] == "cube"
    assert gaps["court-house-slate"]["fixes"][0]["form"] == "perimeter"
    c = TestClient(ui_app.create_app(JobQueue()))
    st = c.get("/api/projects/p/areas/status?area=west").json()
    assert "gaps" in st


def test_a_kit_modules_size_comes_from_its_kit_decision(market):
    write_json(sw_library.library_path(market), {"entries": [
        _entry("arcaded-block", tier="A", kind="building", typology="arcaded_block", part="arcaded_block",
               route_auto="procedural", size_m=[43, 23, 35])], "unseen": []})
    d = sw_library.decisions(market)
    d["arcaded-block"] = {"decision": "kit", "pieces": {"bay": {"description": "a bay", "size": [4.5, 16, 20.4]},
                                                       "corner": {"description": "a corner", "size": [16, 16, 20.4]}}}
    write_json(sw_library.decisions_path(market), d)
    info = le.form_info(market, "arcaded-block-corner")
    assert info["size_m"] == [16, 16, 20.4] and info["size_source"] == "kit piece"
    assert info["suggested"] == "arcade_corner" and le.form_info(market, "arcaded-block-bay")["suggested"] == "arcade"
    le.set_form(market, "arcaded-block-bay", "arcade", [4.5, 16, 20.4])
    assert le.form_info(market, "arcaded-block-bay")["form"] == "arcade"
