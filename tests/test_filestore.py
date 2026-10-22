"""Shared file store: sheets, views and GLBs stored once by hash; references are independent;
migration de-duplicates; sheets can be deleted (with their uses named); Storage cleans."""
import os

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import filestore, kit_proc, library_entries as le, review, versions
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore, read_json, write_json
from asset_pipeline.stages import s2_refs, s5_3d, sw_library, sw_site
from asset_pipeline.ui import app as ui_app


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("p")
    e = {"id": "gate", "tier": "A", "kind": "structure", "typology": "gate", "part": "gate", "material": None,
         "instances": 2, "districts": {}, "district": "core", "mood": [], "flags": [], "best": None, "share": 0.0,
         "description_auto": "A gate.", "description": None, "size_m": [10, 4, 8], "route_auto": "procedural",
         "slot_ids": ["g0", "g1"]}
    write_json(sw_site.site_dir(s) / "library.json", {"min_share": 0.005, "sources": {}, "mood": {}, "candidates": [],
                                                       "checked": True, "entries": [], "unseen": [e], "surfaces": {}})
    return s


def _sheet(store, unit, name="sheet_000.png", data=b"sheet-a"):
    p = store.root / "refs/library" / unit / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    meta = read_json(p.parent / "meta.json", default=None) or {"plan": "library", "unit": unit, "kind": "object",
                                                               "assets": [unit], "sheets": []}
    meta["sheets"].append({"file": name, "seed": 1})
    write_json(p.parent / "meta.json", meta)
    return p


def test_identical_files_share_one_read_only_object(store):
    a, b = _sheet(store, "gate"), _sheet(store, "arch")
    assert filestore.intern(store, a) and filestore.intern(store, b)
    assert os.path.samefile(a, b) and filestore.is_ref(a) and filestore.is_ref(b)
    s = filestore.stats(store)
    assert s["objects"] == 1 and s["references"] == 2 and s["saved"] == len(b"sheet-a")
    with pytest.raises(PermissionError):           # nothing writes into shared content
        a.write_bytes(b"changed")
    filestore.detach(a)                             # a writer replaces only its own reference
    a.write_bytes(b"changed")
    assert b.read_bytes() == b"sheet-a" and a.read_bytes() == b"changed"
    assert filestore.intern(store, a) and filestore.stats(store)["objects"] == 2
    # files outside the store's kinds stay plain
    j = store.root / "refs/library/gate/meta.json"
    assert filestore.intern(store, j) is None and not filestore.is_ref(j)


def test_migrate_dedupes_copies_and_ignores_the_store_in_git(store):
    (store.root / ".gitignore").write_text("refs/\n")
    big = b"x" * 5000
    for u in ("gate", "gate-copy"):
        _sheet(store, u, data=big)
        g = store.root / f"3d/library/{u}/procedural.glb"
        g.parent.mkdir(parents=True, exist_ok=True)
        g.write_bytes(b"g" * 7000)
        (store.root / f"3d/library/{u}/versions/v1").mkdir(parents=True)
        (store.root / f"3d/library/{u}/versions/v1/procedural.glb").write_bytes(b"g" * 7000)
    r = filestore.migrate(store, log=lambda s: None)
    assert r["stored"] == 6 and r["objects"] == 2 and r["references"] == 6
    assert r["freed"] == 5000 + 3 * 7000
    assert "/files/" in (store.root / ".gitignore").read_text()
    again = filestore.migrate(store, log=lambda s: None)            # safe to repeat
    assert again["stored"] == 0 and again["objects"] == 2
    assert (store.root / ".gitignore").read_text().count("/files/") == 1


def test_duplicate_links_references_and_changes_stay_on_one_entry(store):
    _sheet(store, "gate")
    out = kit_proc.out_dir(store, "gate")
    out.mkdir(parents=True)
    (out / "procedural.glb").write_bytes(b"build-1")
    sw_library.decide(store, "gate", "description")
    before = filestore.stats(store)["bytes"]
    le.duplicate(store, "gate", "gate-b")
    src, dst = store.root / "refs/library/gate/sheet_000.png", store.root / "refs/library/gate-b/sheet_000.png"
    assert os.path.samefile(src, dst)                                # a reference, not a copy
    assert os.path.samefile(out / "procedural.glb", kit_proc.out_dir(store, "gate-b") / "procedural.glb")
    assert filestore.stats(store)["bytes"] == before + len(b"sheet-a") + len(b"build-1")   # stored once each
    # a rebuild of the copy (run_blender detaches, writes, interns) leaves the source's build
    g = kit_proc.out_dir(store, "gate-b") / "procedural.glb"
    filestore.detach(g)
    g.write_bytes(b"build-2")
    filestore.intern(store, g)
    assert (out / "procedural.glb").read_bytes() == b"build-1"
    # deleting the copy's sheet keeps the source's (and the shared file)
    s2_refs.delete_sheets(store, ["refs/library/gate-b/sheet_000.png"])
    assert src.read_bytes() == b"sheet-a" and filestore.stats(store)["unreferenced"] == 0


def test_a_restored_trellis_result_reorders_only_its_own_entry(store):
    for u in ("gate", "gate-b"):
        out = s5_3d.out_dir(store, "library", u)
        out.mkdir(parents=True)
        for i, stem in enumerate(("a_1024_cascade_s1", "b_1024_cascade_s2")):
            (out / f"{stem}.glb").write_bytes(stem.encode())
            write_json(out / f"{stem}.json", {"seed": i})
            os.utime(out / f"{stem}.json", (1000 + i, 1000 + i))
        filestore.intern_tree(store, out)
        for i, stem in enumerate(("a_1024_cascade_s1", "b_1024_cascade_s2")):
            versions.record_trellis(store, "library", u, {"glb": f"3d/library/{u}/{stem}.glb", "input": "x",
                                                         "seed": i, "mode": "1024_cascade"})
    assert os.path.samefile(store.root / "3d/library/gate/a_1024_cascade_s1.glb",
                            store.root / "3d/library/gate-b/a_1024_cascade_s1.glb")
    versions.restore(store, "gate", 1)                                 # a back on top for gate only
    assert s5_3d.results(store, "library", "gate")[0]["name"] == "a_1024_cascade_s1"
    assert s5_3d.results(store, "library", "gate-b")[0]["name"] == "b_1024_cascade_s2"


def test_delete_sheets_names_uses_and_frees_the_file_only_when_unreferenced(store):
    a = _sheet(store, "gate")
    b = _sheet(store, "gate", "sheet_001.png", b"sheet-b")
    c = _sheet(store, "gate-b", data=b"sheet-b")                       # same content as b, another entry
    filestore.intern_all(store, [a, b, c])
    ka, kb, kc = (p.relative_to(store.root).as_posix() for p in (a, b, c))
    review.set_star(store, ka)
    write_json(kit_proc.spec_path(store, "gate"), {"kind": "building", "tile": {"sheet": kb}})
    vroot = versions.root(store, "gate-b")
    vroot.mkdir(parents=True)
    write_json(vroot / "versions.json", [{"n": 3, "kind": "procedural", "pinned": True, "approved": False,
                                          "files": [], "source": {"sheet": kc}}])
    uses = s2_refs.sheet_uses(store, [ka, kb, kc])
    assert uses[ka] == ["starred"]
    assert uses[kb] == ["the brief of gate (library/kits/gate.json)"]
    assert uses[kc] == ["version v3 of gate-b (procedural, pinned)"]
    with pytest.raises(s2_refs.SheetInUse) as e:
        s2_refs.delete_sheets(store, [kb])
    assert "brief of gate" in str(e.value)
    r = s2_refs.delete_sheets(store, [kb], confirm=True)
    assert r["freed"] == 0 and c.read_bytes() == b"sheet-b"            # gate-b still references the file
    assert not b.exists() and [s["file"] for s in read_json(a.parent / "meta.json")["sheets"]] == ["sheet_000.png"]
    s2_refs.delete_sheets(store, [kc], confirm=True)                    # the last reference: the file goes
    assert filestore.stats(store)["objects"] == 1
    s2_refs.delete_sheets(store, [ka], confirm=True)
    assert ka not in review.load(store)["stars"] and filestore.stats(store)["objects"] == 0
    assert s2_refs._next_index(a.parent) == 2                          # deleted numbers are never reused
    with pytest.raises(ValueError):
        s2_refs.delete_sheets(store, ["views/library/gate/x.png"])


def test_storage_api_reports_sharing_cleans_and_deletes_sheets(store):
    a, b = _sheet(store, "gate"), _sheet(store, "arch")
    filestore.intern_all(store, [a, b])
    orphan = filestore.object_path(store, "ab" * 32, ".png")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    client = TestClient(ui_app.create_app(JobQueue()))
    f = client.get("/api/projects/p/storage").json()["files"]
    assert f["saved"] == len(b"sheet-a") and f["unreferenced"] == 1 and f["unreferenced_bytes"] == 6
    assert client.post("/api/projects/p/storage/clean", json={"dry_run": True}).json()["removed"] == 1 and orphan.exists()
    assert client.post("/api/projects/p/storage/clean", json={}).json() == {"removed": 1, "bytes": 6}
    assert not orphan.exists() and a.read_bytes() == b"sheet-a"
    review.set_star(store, "refs/library/gate/sheet_000.png")
    u = client.post("/api/projects/p/refs/sheets/uses", json={"sheets": ["refs/library/gate/sheet_000.png"]}).json()
    assert u["uses"]["refs/library/gate/sheet_000.png"] == ["starred"]
    r = client.post("/api/projects/p/refs/sheets/delete", json={"sheets": ["refs/library/gate/sheet_000.png"]})
    assert r.status_code == 409 and "starred" in r.json()["detail"]
    r = client.post("/api/projects/p/refs/sheets/delete", json={"sheets": ["refs/library/gate/sheet_000.png"], "confirm": True})
    assert r.status_code == 200 and not a.exists() and b.read_bytes() == b"sheet-a"


def _png(path, colour="white", size=(60, 40), box=None):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, colour)
    if box:
        ImageDraw.Draw(im).rectangle(box, fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


def test_save_image_never_writes_through_a_stored_file(store):
    """A stored view is a read-only hard link to the store's object: saving new content drops
    that link and writes a fresh file; the object (and every other reference) keeps its bytes."""
    from PIL import Image
    v = _png(store.root / "views/library/gate/sheet_001_p0.png", box=(10, 10, 30, 30))
    obj = filestore.intern(store, v)
    other = store.root / "views/library/arch/sheet_001_p0.png"
    other.parent.mkdir(parents=True)
    os.link(obj, other)                                         # a second entry uses the same content
    before = obj.read_bytes()
    assert not filestore.save_image(store, Image.open(v).convert("RGB"), v)        # same content: untouched
    assert os.path.samefile(v, obj)
    assert filestore.save_image(store, Image.new("RGB", (60, 40), "red"), v)
    assert obj.read_bytes() == before and other.read_bytes() == before and not os.path.samefile(v, obj)
    assert filestore.is_ref(v)                                  # the new content is stored too


def test_detach_drops_any_hard_link_even_a_writable_one(store):
    a = store.root / "views/library/gate/a.png"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"x")
    b = store.root / "elsewhere.png"
    os.link(a, b)
    filestore.detach(a)
    assert not a.exists() and b.read_bytes() == b"x"


def test_cut_view_over_a_stored_view_leaves_the_sheet_and_store_unchanged(store):
    from asset_pipeline import building_proc
    sheet = _png(store.root / "refs/library/gate/sheet_001.png", size=(300, 100), box=(20, 20, 70, 90))
    filestore.intern(store, sheet)
    first = building_proc.cut_view(store, "refs/library/gate/sheet_001.png", 0, "gate")
    view = store.root / first
    filestore.intern(store, view)
    sums = {p: p.read_bytes() for p in (sheet, view)}
    assert view.stat().st_nlink == 2 and not os.access(view, os.W_OK)
    assert building_proc.cut_view(store, "refs/library/gate/sheet_001.png", 0, "gate") == first   # was PermissionError
    assert {p: p.read_bytes() for p in (sheet, view)} == sums


def test_band_crops_write_fresh_files_over_stored_ones(store, tmp_path):
    from asset_pipeline import material_lib
    view = _png(store.root / "views/library/gate/sheet_001_p0.png", size=(80, 120), box=(10, 10, 70, 110))
    out = store.root / "views/library/gate"
    band = _png(out / "band_wall.png", "green")
    obj = filestore.intern(store, band)
    keep = obj.read_bytes()
    material_lib.band_crops(view, {"bbox_px": [10, 10, 70, 110], "storeys": 3, "storey_px": 33}, out)
    assert obj.read_bytes() == keep


def test_a_failed_write_says_which_file_and_why(store):
    v = _png(store.root / "views/library/gate/sheet_001_p0.png")
    filestore.intern(store, v)
    try:
        v.open("wb")
    except PermissionError as e:
        msg = filestore.write_error_text(e, store.root.parent)
    assert msg.startswith("Couldn't write p/views/library/gate/sheet_001_p0.png: it is a read-only shared copy")
    assert "Traceback" not in msg
