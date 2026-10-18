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
    monkeypatch.setattr(building_proc, "brief", lambda store, e, sheet, view, form, dims, params, feats=None: got.update(form=form) or {})
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


def test_entry_without_a_catalog_form_asks_for_one_in_app_terms(proj, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(sw_site, "load_layout", lambda s: SimpleNamespace(city=SimpleNamespace(typologies=[])))
    le.create(proj, "forum-portico", name="forum portico", kind="building", route="procedural",
              description="One range of an enclosed forum: two storeys of colonnades", size_m=[73, 25, 14])
    d = sw_library.decisions(proj)
    d["forum-portico"].pop("form", None)
    write_json(sw_library.decisions_path(proj), d)
    custom = read_json(sw_site.site_dir(proj) / "library_custom.json")
    for group in ("entries", "unseen"):
        g = custom.get(group) or {}
        for e in (g.values() if isinstance(g, dict) else g):
            e.pop("custom", None)                     # behave like a plan entry: no cube fallback
    write_json(sw_site.site_dir(proj) / "library_custom.json", custom)
    info = le.form_info(proj, "forum-portico")
    assert info["form"] is None and info["suggested"] == "arcade_range" and info["size_m"] == [73.0, 25.0, 14.0]
    with pytest.raises(le.FormNeeded) as e:
        le.brief_from_sheet(proj, "forum-portico", "refs/library/forum-portico/sheet_000.png", view="0")
    assert "--" not in str(e.value) and "Form" in str(e.value) and "One-storey colonnade range" in str(e.value)
    got = {}
    from asset_pipeline import building_proc
    monkeypatch.setattr(building_proc, "brief", lambda store, a, sheet, view, form, dims, params=None, feats=None:
                        got.update(form=form, dims=dims, feats=feats) or {})
    le.set_form(proj, "forum-portico", "arcade_range")
    assert le.form_info(proj, "forum-portico")["source"] == "yours"
    le.brief_from_sheet(proj, "forum-portico", "refs/library/forum-portico/sheet_000.png", view="0")
    assert got["form"] == "arcade_hall" and got["feats"]["range"]["h"] > 0 and got["dims"] == [73.0, 25.0, 14.0]
    with pytest.raises(ValueError):
        le.set_form(proj, "forum-portico", "nonsense")
    with pytest.raises(ValueError, match="above 0"):
        le.set_form(proj, "forum-portico", "arcade_range", [10, 0, 5])


def test_forms_api_and_brief_build_keep_the_dialogs_choice(proj, monkeypatch):
    _populate(proj, "hall-a")
    sw_library.set_route(proj, "hall-a", "procedural")
    kit_proc.spec_path(proj, "hall-a").unlink()
    seen = {}
    monkeypatch.setattr(le, "brief_from_sheet", lambda store, a, sheet, view="front", form=None, dims=None, **k: seen.update(form=form, dims=dims))
    monkeypatch.setattr(lr, "rebuild", lambda store, asset, log=None: {"round": 1})
    c = TestClient(ui_app.create_app(JobQueue()))
    forms = c.get("/api/forms").json()
    assert any(f["id"] == "arcade_range" and f["desc"] for f in forms) and all(f["label"] for f in forms)
    assert c.get("/api/projects/p/library/entries/hall-a/form").json()["suggested"]
    assert c.post("/api/projects/p/library/entries/hall-a/form", json={"form": "civic_block", "size_m": [20, 10, 9]}).json()["source"] == "yours"
    job = c.post("/api/projects/p/library/asset/hall-a/brief_build",
                 json={"sheet": "refs/library/hall-a/sheet_000.png", "form": "arcade_range", "dims": [70, 24, 14]}).json()
    for _ in range(300):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
    assert j["status"] == "done", j
    assert seen == {"form": "arcade_range", "dims": [70.0, 24.0, 14.0]}
    assert le.form_info(proj, "hall-a")["form"] == "arcade_range" and le.form_info(proj, "hall-a")["size_m"] == [70.0, 24.0, 14.0]
    assert c.get("/api/projects/p/library/entries/nope/form").status_code == 404
    nx = le.next_step(proj, "library", "hall-a")
    assert nx["form"]["form"] == "arcade_range"


def test_colonnade_preset_fits_the_size_and_a_rebrief_keeps_only_your_edits(proj, monkeypatch):
    from asset_pipeline import building_gen, building_proc
    f = building_gen.preset_for("arcade_range", [73, 25, 14.3])
    assert f["bays"] == 16 and f["range"]["h"] == 14.3 and f["range"]["arch_h"] == round(0.75 * 14.3, 2)
    assert building_gen.FORM_CHOICE["arcade_range"]["features"]["range"]["h"] == 7.0     # the table itself unchanged
    _populate(proj, "hall-a")
    writes = []

    def fake_brief(store, a, sheet, view, form, dims, params=None, feats=None):
        rec = {"kind": "building", "form": form, "dims": dims, "features": feats or {}}
        write_json(kit_proc.spec_path(store, a), rec)
        writes.append(rec)
        return rec
    monkeypatch.setattr(building_proc, "brief", fake_brief)
    le.brief_from_sheet(proj, "hall-a", "refs/library/hall-a/sheet_000.png", view="0", form="arcade_range", dims=[73, 25, 14.3])
    rec = read_json(kit_proc.spec_path(proj, "hall-a"))
    assert rec["form_choice"] == "arcade_range" and rec["features"]["bays"] == 16
    rec["features"]["range"]["parapet"] = 1.4                       # the artist's edit in the form
    write_json(kit_proc.spec_path(proj, "hall-a"), rec)
    le.brief_from_sheet(proj, "hall-a", "refs/library/hall-a/sheet_000.png", view="0", form="arcade_range", dims=[40, 20, 9])
    f2 = writes[-1]["features"]
    assert f2["bays"] == 9 and f2["range"]["h"] == 9.0             # untouched preset values follow the new size
    assert f2["range"]["parapet"] == 1.4                            # the edit stays


# --- duplicates own no slots, are independent; names follow the entry; rename reaches the rest --

def test_a_duplicate_owns_no_slots_and_the_source_keeps_them(proj):
    data = read_json(sw_library.library_path(proj))
    data["unseen"][0] |= {"slot_ids": ["s1", "s2", "s3"], "instances": 3, "districts": {"core": 3}}
    write_json(sw_library.library_path(proj), data)
    sw_library.decide(proj, "canopy-table", "description")
    le.duplicate(proj, "canopy-table", "stone-counter")
    ents = {e["id"]: e for e in sw_library.load(proj)["unseen"]}
    assert ents["stone-counter"]["slot_ids"] == [] and ents["stone-counter"]["instances"] == 0
    assert ents["stone-counter"]["districts"] == {}
    assert ents["canopy-table"]["slot_ids"] == ["s1", "s2", "s3"] and ents["canopy-table"]["instances"] == 3
    # renaming the duplicate doesn't take any either
    le.rename(proj, "stone-counter", "stone-bench")
    ents = {e["id"]: e for e in sw_library.load(proj)["unseen"]}
    assert ents["stone-bench"]["slot_ids"] == [] and ents["canopy-table"]["slot_ids"] == ["s1", "s2", "s3"]


def test_names_come_from_the_entry_everywhere(proj):
    sw_library.decide(proj, "canopy-table", "description")
    le.duplicate(proj, "canopy-table", "portico")
    plan_name = lambda a: next(x.name for x in s1_plan.load(proj, "library").assets if x.id == a)  # noqa: E731
    assert plan_name("portico") == "canopy table copy"
    le.rename(proj, "portico", name="Portico gate")            # name only: the plan follows (Views and 3D)
    assert plan_name("portico") == "Portico gate"
    assert sw_library.asset_names(proj)["portico"] == "Portico gate"
    unit = next(u for u in s2_refs.units(proj, "library") if u.key == "portico")
    assert unit.title == "Portico gate"
    # kit modules carry the entry's current name
    d = sw_library.decisions(proj)
    d["hall-a"] = {"decision": "kit", "name": "Great hall", "pieces": {"bay": {"size": [4, 1, 6], "description": "a bay"}}}
    write_json(sw_library.decisions_path(proj), d)
    sw_library.asset_plan(proj)
    assert sw_library.asset_names(proj)["hall-a-bay"] == "Great hall bay" == plan_name("hall-a-bay")
    # and the UI gets the map with the library data
    client = TestClient(ui_app.create_app(JobQueue()))
    assert client.get("/api/projects/p/library").json()["names"]["portico"] == "Portico gate"


def _png(path, boxes=((10, 20, 50, 80), (70, 20, 110, 80), (130, 20, 170, 80))):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (180, 100), "white")
    dr = ImageDraw.Draw(im)
    for b in boxes:
        dr.rectangle(b, fill=(90, 60, 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _tree(store, eid):
    """{project-relative path: bytes} of every file an entry owns."""
    out = {}
    for p in le.paths(store, eid).values():
        files = [p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []
        out |= {f.relative_to(store.root).as_posix(): f.read_bytes() for f in files if f.is_file()}
    return out


def test_a_duplicate_is_independent_of_its_source(proj, monkeypatch):
    """Rerolling sheets, cutting views, Make 3D and deleting versions on the copy leave every
    file of the source as it was (and the copy shares no file, link or path with it)."""
    import os
    import sys

    import numpy as np
    from PIL import Image

    from asset_pipeline import segment, versions
    from asset_pipeline.imagegen.base import GenResult
    from asset_pipeline.stages import s3_views, s5_3d
    _populate(proj)
    r = proj.root
    _png(r / "refs/library/canopy-table/sheet_000.png")
    sw_library.decide(proj, "canopy-table", "description")
    # a TRELLIS version of the source, so the copy's version index lists files
    od = kit_proc.out_dir(proj, "canopy-table")
    (od / "trellis_001_input.png").write_bytes(b"png")
    versions.record_trellis(proj, "library", "canopy-table", {
        "glb": "3d/library/canopy-table/trellis_001.glb", "input": "3d/library/canopy-table/trellis_001_input.png",
        "view": "views/library/canopy-table/sheet_000_v1.png", "seed": 42, "mode": "1024_cascade"})
    le.duplicate(proj, "canopy-table", "portico-gate", name="Portico gate")
    before = _tree(proj, "canopy-table")
    copy = _tree(proj, "portico-gate")
    # nothing shared: no symlink or hard link, and no file of the copy names the source
    for k in copy:
        f = r / k
        assert not f.is_symlink() and os.stat(f).st_nlink == 1, k
        if f.suffix == ".json":
            assert "canopy-table" not in f.read_text(), k
    assert all(f.startswith("3d/library/portico-gate/") for v in versions.load(proj, "portico-gate")
               for f in v["files"] if v["kind"] == "trellis")

    # reroll: two new sheets for the copy only
    class FakeBackend:
        name = "fake"

        def generate(self, req, d, prefix="tmp"):
            for i in range(req.n):
                p = d / f"{prefix}_{i}.png"
                _png(p)
                yield GenResult(path=p, seed=7, backend="fake", meta={})
    monkeypatch.setattr(s2_refs, "backend_for", lambda *a, **k: FakeBackend())
    unit = next(u for u in s2_refs.units(proj, "library") if u.key == "portico-gate")
    new = s2_refs.generate(proj, unit, n=2)
    assert all("refs/library/portico-gate/" in p.as_posix() for p in new)

    # Cut views of the copy's sheet
    def detect(client, path, noun, max_dets=6):
        im = np.array(Image.open(path).convert("L")) < 200
        out = []
        for x0, x1 in ((10, 51), (70, 111), (130, 171)):
            m = np.zeros_like(im)
            m[20:81, x0:x1] = True
            out.append(segment.Detection(mask=m, bbox=(x0, 20, x1, 81), area_frac=float(m.mean()), touches_border=False))
        return out
    monkeypatch.setattr(s3_views.segment, "detect", detect)
    monkeypatch.setattr(s3_views, "connect", lambda: None)
    views = s3_views.cut(proj, "refs/library/portico-gate/sheet_000.png")
    assert len(views) == 3 and all(v["asset"] == "portico-gate" for v in views)

    # Make 3D on the copy (a stand-in for scripts/trellis that writes what it writes)
    fake = r / "fake_trellis.py"
    fake.write_text("import json, sys, pathlib\n"
                    "a = sys.argv; img = pathlib.Path(a[1]); out = pathlib.Path(a[a.index('--out-dir') + 1])\n"
                    "stem = f\"{img.stem[:24]}_{a[a.index('--type') + 1]}_s{a[a.index('--seed') + 1]}\"\n"
                    "(out / f'{stem}.glb').write_bytes(b'glb'); (out / f'{stem}_input.png').write_bytes(b'png')\n"
                    "(out / f'{stem}.json').write_text(json.dumps({'seed': int(a[a.index('--seed') + 1]), 'glb_faces': 1}))\n")
    fake.chmod(0o755)
    wrapper = r / "trellis"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(s5_3d, "TRELLIS", wrapper)
    s5_3d.run(proj, "library", "portico-gate", view="views/library/portico-gate/sheet_000_v1.png", seed=5)
    assert any(x["name"] == "sheet_000_v1_1024_cascade_s5" for x in s5_3d.results(proj, "library", "portico-gate"))

    # delete every version of the copy
    for v in versions.load(proj, "portico-gate"):
        versions.delete(proj, "portico-gate", v["n"], confirm_pinned=True)
    assert versions.load(proj, "portico-gate") == []

    assert _tree(proj, "canopy-table") == before                  # the source, byte for byte
    assert [v["n"] for v in versions.load(proj, "canopy-table")] == [1]
    assert review.chosen(proj, "library", "canopy-table") == "views/library/canopy-table/sheet_000_v1.png"
    assert not (r / "views/library/canopy-table/sheet_000_v2.png").exists()

    # and the other way round: deleting the source's version leaves the copy's files alone
    copy_now = _tree(proj, "portico-gate")
    versions.delete(proj, "canopy-table", 1, confirm_pinned=True)
    assert _tree(proj, "portico-gate") == copy_now


def test_rename_carries_variant_sets_and_checklists_and_lists_what_unreal_keeps(proj, tmp_path, monkeypatch):
    monkeypatch.setenv("AP_UE_PROJECTS", str(tmp_path / "ue"))
    sw_library.decide(proj, "canopy-table", "description")
    le.duplicate(proj, "canopy-table", "stall-b")
    d = sw_library.decisions(proj)
    d["_variants"] = {"canopy-table": {"members": {"canopy-table": 1.0, "stall-b": 2.0}, "areas": [], "seed": 0}}
    write_json(sw_library.decisions_path(proj), d)
    (proj.root / "areas").mkdir()
    (proj.root / "areas/market.md").write_text("| stall-b | D |\n\nGaps: stall-b needs a roof; stall-bb is another.\n")
    content = tmp_path / "ue/p/Content/AP"
    (content / "Preview/stall-b").mkdir(parents=True)
    (content / "LookTest/med-market/builds").mkdir(parents=True)
    (content / "LookTest/med-market/builds/SM_stallmb_m10_4.uasset").write_bytes(b"x")
    (content / "LookTest/med-market/builds/SM_canopymtable_m1_2.uasset").write_bytes(b"x")
    write_json(sw_site.site_dir(proj) / "ue_layout.json", {"actors": [{"label": "AP_Preview_stall-b"}, {"label": "AP_core_s1"}]})
    e = le.rename(proj, "stall-b", "stall-oak")
    assert sw_library.decisions(proj)["_variants"]["canopy-table"]["members"] == {"canopy-table": 1.0, "stall-oak": 2.0}
    assert (proj.root / "areas/market.md").read_text() == "| stall-oak | D |\n\nGaps: stall-oak needs a roof; stall-bb is another.\n"
    assert "areas/market.md" in e["updated"]
    nu = "\n".join(e["not_updated"])
    assert "/Game/AP/Preview/stall-b/" in nu and "AP_Preview_stall-b" in nu
    assert "look scene med-market: 1 build mesh(es) SM_stallmb_m10_4" in nu and "canopymtable" not in nu
    assert "AP_core_s1" not in nu
    assert len(e["not_updated"]) == 3
    # through the API, the same lists come back
    client = TestClient(ui_app.create_app(JobQueue()))
    res = client.post("/api/projects/p/library/entries/stall-oak/rename", json={"to": "stall-elm"}).json()
    assert res["id"] == "stall-elm" and res["not_updated"] == [] and "areas/market.md" in res["updated"]
