"""Areas: membership from the shots' id passes (and the plan's source shots for props), the
status rows and next steps, batch skips (approved, unchanged builds), the checklist that keeps
the artist's text."""
import pytest

from asset_pipeline import areas, kit_proc, library_review
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.stages import sw_library, sw_shots, sw_site

BRIEF = {"kind": "building", "form": "cube", "dims": [40.0, 40.0, 36.0], "params": {}, "features": {}, "measured": {}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("demo")
    site = sw_site.site_dir(s)
    write_json(site / "greybox.json", {"slots": [{"id": "hall-1", "typology": None, "type": "hall"},
                                                 {"id": "tile-a", "typology": "row_house", "material": "brick", "type": "row"},
                                                 {"id": "market-0", "typology": None, "type": "market"}],
                                       "shots": [{"id": "med-a"}, {"id": "med-b"}]})
    write_json(sw_shots.shot_dir(s, "med-a") / "ids.json", {"slots": {"hall-1": {"frac": 0.05}, "tile-a": {"frac": 0.002},
                                                                       "market-0": {"frac": 0.3}}})
    write_json(sw_shots.shot_dir(s, "med-b") / "ids.json", {"slots": {"hall-1": {"frac": 0.0001}}})
    write_json(sw_library.library_path(s), {"entries": [
        {"id": "hall", "tier": "B", "kind": "building", "slot_ids": ["hall-1"], "districts": {"north": 1}, "route_auto": "procedural"},
        {"id": "row-house-brick", "tier": "C", "kind": "building", "slot_ids": ["tile-a"], "districts": {"south": 3},
         "route_auto": "procedural"},
        {"id": "stall", "tier": "D", "kind": "prop", "slot_ids": [], "sources": [{"shot": "med-a", "share": 0.3}],
         "districts": {"north": 9}, "route_auto": "trellis"},
        {"id": "pine", "tier": "D", "kind": "vegetation", "slot_ids": ["hall-1"]}], "unseen": []})
    write_json(sw_library.decisions_path(s), {"hall": {"decision": "reference"}, "stall": {"decision": "source"}})
    write_json(kit_proc.spec_path(s, "hall"), BRIEF)
    sw_site._greybox_memo.d = None
    monkeypatch.setattr(library_review, "entry_of", lambda store, asset: None)
    return s


def test_members_and_next_steps(store):
    areas.define(store, "a", shots=["med-a", "med-b"])
    m = areas.members(store, "a")
    assert m == {"hall": 0.05, "row-house-brick": 0.002, "stall": 0.3, "pine": 0.05}
    rows = {r["asset"]: r for r in areas.status(store, "a")}
    assert "pine" not in rows                                   # trees: PCG
    assert rows["hall"]["next"].startswith("build:")            # a brief, never built
    assert rows["row-house-brick"]["next"].startswith("decide")
    assert rows["stall"]["route"] == "trellis" and "sheets" in rows["stall"]["next"]
    assert set(areas.members(store, district="south")) == {"row-house-brick"}
    with pytest.raises(KeyError, match="ap area define"):
        areas.members(store, "nope")
    with pytest.raises(ValueError, match="no shot"):
        areas.define(store, "b", shots=["med-x"])


def test_fresh_builds_and_approved_assets(store):
    areas.define(store, "a", shots=["med-a"])
    write_json(kit_proc.out_dir(store, "hall") / "procedural.json", {"brief_sha": areas.brief_sha(store, "hall")})
    (kit_proc.out_dir(store, "hall") / "procedural.glb").write_bytes(b"x")
    r = next(r for r in areas.status(store, "a") if r["asset"] == "hall")
    assert r["fresh"] and r["next"].startswith("review")
    write_json(kit_proc.spec_path(store, "hall"), BRIEF | {"dims": [41.0, 40.0, 36.0]})        # an edited brief: stale
    assert not next(r for r in areas.status(store, "a") if r["asset"] == "hall")["fresh"]
    library_review.set_status(store, "hall", "approved", "")
    assert next(r for r in areas.status(store, "a") if r["asset"] == "hall")["next"] == "done (approved)"


def test_checklist_keeps_the_artists_text(store):
    areas.define(store, "a", shots=["med-a"])
    p = areas.checklist(store, "a")
    txt = p.read_text().replace("(a) existing brief fields", "(a) existing brief fields\n\n- hall: loggias too wide (a)")
    p.write_text(txt)
    areas.checklist(store, "a")
    out = p.read_text()
    assert "- hall: loggias too wide (a)" in out and out.count(areas.CHECK_BEGIN) == 1 and "| hall |" in out


def test_material_types_are_checked(store, monkeypatch):
    monkeypatch.setattr(library_review.sw_site, "load_layout", lambda store: type("L", (), {"city": None, "shots": []})())
    v = library_review.validate(store, "hall", BRIEF | {"material_types": {"base": "granite"}})
    assert any("material type base" in e for e in v["errors"])
    assert not library_review.validate(store, "hall", BRIEF | {"material_types": {"base": "brick"}, "surface": "auto"})["errors"]
