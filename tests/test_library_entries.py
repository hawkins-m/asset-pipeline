"""Library › Entries: create, duplicate, rename, describe; Brief & build / Cut views next steps."""
import json

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import kit_proc, library_review as lr, library_entries as le, review
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore, read_json, write_json
from asset_pipeline.stages import s1_plan, s2_refs, sw_library, sw_site
from asset_pipeline.ui import app as ui_app


def _entry(eid, **kw):
    return {"id": eid, "tier": "D", "kind": "prop", "typology": "canopy table", "part": "canopy table", "material": None,
            "instances": 3, "districts": {}, "district": "core", "mood": [], "flags": [], "best": None, "share": 0.0,
            "description_auto": "An open market stand.", "description": None, "size_m": [4.4, 3.2, 2.7],
            "route_auto": "trellis", "slot_ids": ["s1"]} | kw


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("p")
    write_json(sw_site.site_dir(store) / "library.json", {
        "min_share": 0.005, "sources": {}, "mood": {}, "candidates": [], "checked": True,
        "entries": [], "unseen": [_entry("canopy-table"), _entry("hall-a", tier="B", kind="building",
                                                                  route_auto="procedural", part="hall")],
        "surfaces": {}})
    return store


def _populate(store, eid="canopy-table"):
    """Sheets, views, brief, build, TRELLIS result and a review round, all naming the id."""
    r = store.root
    sd = r / f"refs/library/{eid}"
    sd.mkdir(parents=True)
    (sd / "sheet_000.png").write_bytes(b"png")
    write_json(sd / "meta.json", {"plan": "library", "unit": eid, "kind": "object", "assets": [eid],
                                  "sheets": [{"file": "sheet_000.png", "seed": 1}]})
    vd = r / f"views/library/{eid}"
    vd.mkdir(parents=True)
    (vd / "sheet_000_v1.png").write_bytes(b"png")
    write_json(vd / "meta.json", {"plan": "library", "unit": eid, "views": [
        {"file": "sheet_000_v1.png", "sheet": f"refs/library/{eid}/sheet_000.png", "asset": eid}]})
    write_json(kit_proc.spec_path(store, eid), {"kind": "building", "form": "cube", "dims": [4, 3, 3],
                                                "sheet": f"refs/library/{eid}/sheet_000.png",
                                                "view": f"views/library/{eid}/sheet_000_v1.png", "params": {}})
    od = kit_proc.out_dir(store, eid)
    (od / "review/round_1").mkdir(parents=True)
    (od / "procedural.glb").write_bytes(b"glb")
    (od / "trellis_001.glb").write_bytes(b"glb")
    write_json(od / "trellis_001.json", {"input": f"views/library/{eid}/sheet_000_v1.png", "glb_faces": 9})
    (od / "review/round_1/threeq.png").write_bytes(b"png")
    (r / f"cleanup/library/{eid}").mkdir(parents=True)
    (r / f"cleanup/library/{eid}/clean.glb").write_bytes(b"glb")
    review.set_star(store, f"refs/library/{eid}/sheet_000.png")
    review.choose(store, "library", eid, f"views/library/{eid}/sheet_000_v1.png")


def test_create_flows_into_units(proj):
    e = le.create(proj, "stone-counter", name="Stone counter", description="A long stone counter.",
                  kind="prop", size_m=[3, 1, 1])
    assert e["custom"] and e["route"] == "trellis" and e["decision"] == "description"
    data = sw_library.load(proj)
    assert "stone-counter" in {u["id"] for u in data["unseen"]}
    # a re-plan's file is untouched, and the custom file survives it
    assert "stone-counter" not in {u["id"] for u in read_json(sw_library.library_path(proj))["unseen"]}
    keys = [u.key for u in s2_refs.units(proj, "library")]
    assert "stone-counter" in keys
    a = next(a for a in s1_plan.load(proj, "library").assets if a.id == "stone-counter")
    assert a.description == "A long stone counter." and a.name == "Stone counter" and a.dimensions.width == 3
    with pytest.raises(le.EntryExists):
        le.create(proj, "stone-counter")
    with pytest.raises(ValueError):
        le.create(proj, "Bad Id")
    b = le.create(proj, "gatehouse", kind="building")
    assert b["route"] == "procedural" and b["tier"] == "B"


def test_duplicate_copies_everything_but_review_status(proj):
    _populate(proj)
    sw_library.decide(proj, "canopy-table", "description")
    sw_library.describe(proj, "canopy-table", "My own words.")
    sw_library.set_route(proj, "canopy-table", "procedural")
    lr.set_status(proj, "canopy-table", "approved", "good")
    e = le.duplicate(proj, "canopy-table", "stone-counter", name="Stone counter")
    assert e["custom"] and e["description"] == "My own words." and e["route"] == "procedural" and e["decision"] == "description"
    r = proj.root
    assert (r / "refs/library/stone-counter/sheet_000.png").is_file()
    assert (r / "refs/library/canopy-table/sheet_000.png").is_file()          # the original is untouched
    meta = json.loads((r / "refs/library/stone-counter/meta.json").read_text())
    assert meta["unit"] == "stone-counter" and meta["assets"] == ["stone-counter"]
    vm = json.loads((r / "views/library/stone-counter/meta.json").read_text())
    assert vm["views"][0]["asset"] == "stone-counter" and vm["views"][0]["sheet"] == "refs/library/stone-counter/sheet_000.png"
    brief = read_json(kit_proc.spec_path(proj, "stone-counter"))
    assert brief["sheet"] == "refs/library/stone-counter/sheet_000.png" and "canopy-table" not in json.dumps(brief)
    assert read_json(kit_proc.spec_path(proj, "canopy-table"))["sheet"] == "refs/library/canopy-table/sheet_000.png"
    od = kit_proc.out_dir(proj, "stone-counter")
    assert (od / "procedural.glb").is_file() and (od / "trellis_001.glb").is_file() and (od / "review/round_1/threeq.png").is_file()
    assert json.loads((od / "trellis_001.json").read_text())["input"] == "views/library/stone-counter/sheet_000_v1.png"
    assert (r / "cleanup/library/stone-counter/clean.glb").is_file()
    # stars and the chosen view follow
    assert "refs/library/stone-counter/sheet_000.png" in review.starred(proj)
    assert review.chosen(proj, "library", "stone-counter") == "views/library/stone-counter/sheet_000_v1.png"
    # the review status is not copied; the original keeps its
    assert lr.get(proj, "stone-counter")["status"] is None and lr.get(proj, "stone-counter")["history"] == []
    assert lr.get(proj, "canopy-table")["status"] == "approved"
    # it is a unit of its own, with its sheets
    u = next(u for u in s2_refs.units(proj, "library") if u.key == "stone-counter")
    assert s2_refs.sheets(proj, u) == ["refs/library/stone-counter/sheet_000.png"]
    with pytest.raises(le.EntryExists):
        le.duplicate(proj, "canopy-table", "stone-counter")


def test_duplicate_refuses_existing_files_without_overwrite(proj):
    _populate(proj)
    (proj.root / "refs/library/stone-counter").mkdir(parents=True)
    with pytest.raises(le.EntryExists):
        le.duplicate(proj, "canopy-table", "stone-counter")
    le.duplicate(proj, "canopy-table", "stone-counter", overwrite=True)
    assert (proj.root / "refs/library/stone-counter/sheet_000.png").is_file()


def test_rename_custom_moves_files_and_plan_entries_only_rename_the_name(proj):
    _populate(proj)
    sw_library.decide(proj, "canopy-table", "description")
    le.duplicate(proj, "canopy-table", "stone-counter")
    lr.set_status(proj, "stone-counter", "needs_changes", "x")
    e = le.rename(proj, "stone-counter", "stone-bench", name="Stone bench")
    assert e["id"] == "stone-bench" and e["name"] == "Stone bench"
    r = proj.root
    assert not (r / "refs/library/stone-counter").exists() and (r / "refs/library/stone-bench/sheet_000.png").is_file()
    assert not kit_proc.spec_path(proj, "stone-counter").exists()
    assert "stone-counter" not in json.dumps(read_json(kit_proc.spec_path(proj, "stone-bench")))
    assert "stone-counter" not in sw_library.custom(proj) and "stone-bench" in sw_library.custom(proj)
    assert lr.get(proj, "stone-bench")["status"] == "needs_changes"          # a rename keeps the review
    assert "refs/library/stone-bench/sheet_000.png" in review.starred(proj)
    assert [u.key for u in s2_refs.units(proj, "library")].count("stone-bench") == 1
    # a plan-derived entry: name only
    with pytest.raises(ValueError, match="comes from the library plan"):
        le.rename(proj, "canopy-table", "other")
    assert le.rename(proj, "canopy-table", name="Market stand")["name"] == "Market stand"
    assert (r / "refs/library/canopy-table").is_dir()


def test_api_entries_and_description(proj):
    c = TestClient(ui_app.create_app(JobQueue()))
    base = "/api/projects/p/library/entries"
    assert c.post(base, json={"id": "bench", "name": "Bench", "kind": "prop"}).status_code == 200
    assert c.post(base, json={"id": "bench"}).status_code == 409
    assert c.post(base, json={"id": "Bad Id"}).status_code == 400
    assert c.post(f"{base}/bench/duplicate", json={"to": "bench-2"}).json()["custom"]
    assert c.post(f"{base}/bench/duplicate", json={"to": "bench-2"}).status_code == 409
    assert c.post(f"{base}/hall-a/rename", json={"to": "x"}).status_code == 400
    assert c.post(f"{base}/bench-2/rename", json={"to": "bench-3"}).json()["id"] == "bench-3"
    assert c.post(f"{base}/nope/duplicate", json={"to": "y"}).status_code == 404
    d = c.post(f"{base}/bench/description", json={"description": "Mine."}).json()
    assert d["description"] == "Mine."
    assert c.post(f"{base}/bench/description", json={"description": None}).json()["description"] is None
    ids = {e["id"] for e in c.get(base).json()}
    assert {"bench", "bench-3", "canopy-table", "hall-a"} <= ids
    lib = c.get("/api/projects/p/library").json()
    assert "bench" in {u["id"] for u in lib["plan"]["unseen"]}


def test_refs_next_step_and_brief_build_job(proj, monkeypatch):
    _populate(proj)
    sw_library.decide(proj, "canopy-table", "description")
    sw_library.decide(proj, "hall-a", "description")
    sw_library.asset_plan(proj)
    c = TestClient(ui_app.create_app(JobQueue()))
    rows = {u["key"]: u for u in c.get("/api/projects/p/refs").json()}
    assert rows["canopy-table"]["next"]["route"] == "trellis" and rows["canopy-table"]["next"]["views"] == 1
    assert rows["hall-a"]["next"]["route"] == "procedural" and not rows["hall-a"]["next"]["has_brief"]
    # TRELLIS entries can't brief
    r = c.post("/api/projects/p/library/asset/canopy-table/brief_build", json={"sheet": "refs/library/canopy-table/sheet_000.png"})
    assert r.status_code == 400
    calls = []
    monkeypatch.setattr(le, "brief_from_sheet", lambda store, a, sh, view="front", **kw: calls.append(("brief", a, sh)))
    monkeypatch.setattr(lr, "rebuild", lambda store, a, log=print: calls.append(("rebuild", a)) or {"faces": 5})
    sheet = "refs/library/canopy-table/sheet_000.png"       # any existing image
    j = c.post("/api/projects/p/library/asset/hall-a/brief_build", json={"sheet": sheet}).json()
    assert j["kind"] == "library.brief_build" and j["lane"] == "gpu0"
    import time
    for _ in range(100):
        st = c.get(f"/api/jobs/{j['id']}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert [x[0] for x in calls] == ["brief", "rebuild"]
    # with a brief present and no rebrief: just the rebuild
    write_json(kit_proc.spec_path(proj, "hall-a"), {"kind": "building", "form": "cube", "dims": [1, 1, 1]})
    calls.clear()
    j = c.post("/api/projects/p/library/asset/hall-a/brief_build", json={"sheet": sheet}).json()
    for _ in range(100):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [("rebuild", "hall-a")]


def test_a_duplicate_briefs_with_its_catalog_form_not_a_cube(proj, monkeypatch):
    """A copy of a plan entry keeps its typology, so its brief uses the catalog form (a vaulted
    hall stays a vault), found when the UI walkthrough re-briefed a duplicated market hall."""
    from types import SimpleNamespace
    from asset_pipeline import building_proc
    le.duplicate(proj, "hall-a", "hall-copy")
    typ = SimpleNamespace(id=le._all_entries(proj)["hall-copy"]["typology"], form="vault_hall")
    monkeypatch.setattr(sw_site, "load_layout", lambda s: SimpleNamespace(city=SimpleNamespace(typologies=[typ])))
    monkeypatch.setitem(building_proc.FORM_OF, "vault_hall", "hall_vault")
    got = {}
    monkeypatch.setattr(building_proc, "brief", lambda store, e, sheet, view, form, dims, params: got.update(form=form) or {})
    le.brief_from_sheet(proj, "hall-copy", "refs/library/hall-copy/sheet_000.png", view="0")
    assert got["form"] == "hall_vault"
    le.create(proj, "plain-box", kind="building")
    le.brief_from_sheet(proj, "plain-box", "refs/library/plain-box/sheet_000.png", view="0")
    assert got["form"] == "cube"                                   # a new entry of your own has no catalog form


def test_brief_and_build_sets_an_approved_asset_back_to_needs_review(proj, monkeypatch):
    _populate(proj, "hall-a")
    lr.set_status(proj, "hall-a", "approved", "looked right")
    monkeypatch.setattr(le, "brief_from_sheet", lambda *a, **k: {})
    monkeypatch.setattr(lr, "rebuild", lambda store, asset, log=None: {"round": 1})
    sw_library.set_route(proj, "hall-a", "procedural")
    c = TestClient(ui_app.create_app(JobQueue()))
    job = c.post("/api/projects/p/library/asset/hall-a/brief_build",
                 json={"sheet": "refs/library/hall-a/sheet_000.png", "rebrief": True}).json()
    for _ in range(300):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
    assert j["status"] == "done", j
    assert lr.get(proj, "hall-a")["status"] == "needs_review"
