"""Asset version bank: recording, retention, pin, approve, restore, compare, storage, migration and
the API. Blender and TRELLIS are monkeypatched; projects are temporary and neutral."""
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import filestore, kit_proc, library_review as lr, versions
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore, read_json, write_json
from asset_pipeline.stages import s5_3d
from asset_pipeline.ui import app as ui_app

BRIEF = {"kind": "arch_gate", "form": "cube", "dims": [10.0, 10.0, 9.0], "params": {"w": 10.0},
         "sheet": "refs/s.png", "view": "views/s.png", "position": 1, "guidance": "taller arch"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("demo")
    write_json(kit_proc.spec_path(s, "gate"), BRIEF)
    monkeypatch.setattr(lr, "entry_of", lambda store, asset: None)

    def fake_build(store, asset, log=print):
        out = kit_proc.out_dir(store, asset)
        out.mkdir(parents=True, exist_ok=True)
        filestore.detach(out / "procedural.glb")          # as kit_proc.run_blender does
        spec = {k: v for k, v in read_json(kit_proc.spec_path(store, asset)).items() if k != "guidance"}
        (out / "procedural.glb").write_bytes(b"glb" * 1000 + json.dumps(spec, sort_keys=True).encode())  # like a real build: the brief decides the bytes
        filestore.intern(store, out / "procedural.glb")
        res = {"faces": 12}
        from asset_pipeline.areas import brief_sha
        write_json(out / "procedural.json", res | {"brief_sha": brief_sha(store, asset)})
        return res | {"brief_sha": brief_sha(store, asset)}
    monkeypatch.setattr(kit_proc, "build", fake_build)
    return s


def _build(store, asset="gate", **brief):
    if brief:
        write_json(kit_proc.spec_path(store, asset), read_json(kit_proc.spec_path(store, asset)) | brief)
    return kit_proc.build_any(store, asset, log=lambda s: None)


def test_every_build_is_a_version_with_its_brief_and_guidance(store):
    r = _build(store)
    assert r["version"] == 1
    _build(store, guidance="wider piers", params={"w": 12.0})
    rows = versions.load(store, "gate")
    assert [x["n"] for x in rows] == [1, 2] and all(x["kind"] == "procedural" for x in rows)
    assert versions.get(store, "gate", 2)["source"]["guidance"] == "wider piers"
    d = versions.vdir(store, "gate", 1)
    assert read_json(d / "brief.json")["guidance"] == "taller arch"
    assert not (d / "procedural.glb").exists()                       # no GLB unless pinned
    assert versions.current(store, "gate") == 2


def test_a_build_from_before_the_bank_becomes_v1_first(store):
    out = kit_proc.out_dir(store, "gate")
    out.mkdir(parents=True)
    (out / "procedural.glb").write_bytes(b"old")
    _build(store)
    assert [x["n"] for x in versions.load(store, "gate")] == [1, 2]


def test_thumbnails_come_from_the_rebuild_renders(store, monkeypatch):
    def fake_render(store, asset, glb, out, mode, form, extra_views=None, figure=None):
        from PIL import Image
        pre = "preview_" if mode == "quick" else ""
        for v in ("sheet", "threeq"):
            Image.new("RGB", (8, 8), "white").save(out / f"{pre}{v}.png")
    monkeypatch.setattr(lr, "render", fake_render)
    res = lr.rebuild(store, "gate")
    row = versions.get(store, "gate", res["version"])
    assert row["thumb"] and (store.root / row["thumb"]).is_file() and row["bytes"] > 0


def test_retention_keeps_last_k_and_never_pinned_approved_or_current(store):
    for i in range(6):
        _build(store, params={"w": 10.0 + i})
    versions.pin(store, "gate", 1)
    lr.set_status(store, "gate", "approved", "")                  # v6 is current
    assert versions.get(store, "gate", 6)["approved"]
    dry = versions.prune(store, "gate", dry_run=True)
    assert dry["remove"] == [2, 3] and dry["keep"] == 3 and dry["bytes"] > 0
    assert len(versions.load(store, "gate")) == 6                  # a dry run removes nothing
    versions.set_keep(store, 1)
    assert versions.prune(store, "gate")["remove"] == [2, 3, 4, 5]       # 1 (pinned) and 6 (approved, current) stay
    versions.prune(store, "gate", dry_run=False)
    assert [x["n"] for x in versions.load(store, "gate")] == [1, 6]
    assert not versions.vdir(store, "gate", 3).exists()
    with pytest.raises(ValueError):
        versions.set_keep(store, 0)
    assert read_json(store.project_file)["versions_keep"] == 1 and store.load().versions_keep == 1


def test_approval_follows_the_status(store):
    _build(store)
    _build(store, params={"w": 11.0})
    lr.set_status(store, "gate", "approved", "")
    assert [x["approved"] for x in versions.load(store, "gate")] == [False, True]
    with pytest.raises(ValueError):
        versions.delete(store, "gate", 2)
    lr.set_status(store, "gate", "needs_changes", "x")
    assert not any(x["approved"] for x in versions.load(store, "gate"))
    lr.set_status(store, "gate", "approved", "")
    lr.save_kit(store, "gate", {"w": 15.0})                            # editing an approved brief clears the mark
    assert not any(x["approved"] for x in versions.load(store, "gate"))


def test_pin_keeps_the_glb_unpin_removes_it_and_delete_asks_for_pinned(store):
    _build(store)
    _build(store, params={"w": 11.0})
    versions.pin(store, "gate", 2)
    d2 = versions.vdir(store, "gate", 2)
    assert (d2 / "procedural.glb").is_file() and versions.get(store, "gate", 2)["bytes"] > 3000
    versions.pin(store, "gate", 1)                                  # an older build: protected, but no GLB to keep
    assert not (versions.vdir(store, "gate", 1) / "procedural.glb").exists()
    versions.set_current(store, "gate", 1)                          # the Active version can't be deleted
    with pytest.raises(PermissionError):
        versions.delete(store, "gate", 2)
    versions.unpin(store, "gate", 2)
    assert not (d2 / "procedural.glb").exists()
    versions.delete(store, "gate", 2)
    assert [x["n"] for x in versions.load(store, "gate")] == [1] and not d2.exists()


def test_restore_without_a_glb_rebuilds_and_with_one_copies_it_back(store):
    _build(store)                                                    # v1: w 10, guidance "taller arch"
    versions.pin(store, "gate", 1)                                   # current: GLB kept
    v1_bytes = (versions.vdir(store, "gate", 1) / "procedural.glb").read_bytes()
    _build(store, params={"w": 14.0}, guidance="other")             # v2
    assert read_json(kit_proc.spec_path(store, "gate"))["params"]["w"] == 14.0
    glb = kit_proc.out_dir(store, "gate").joinpath("procedural.glb")
    filestore.detach(glb)                          # what a build does before writing it
    glb.write_bytes(b"newer")
    r = versions.restore(store, "gate", 1)
    assert r["version"] == 1 and not r["rebuilt"] and r["current"] == 1  # pinned GLB: no rebuild
    assert r["glb"] == "3d/library/gate/procedural.glb"
    assert read_json(kit_proc.spec_path(store, "gate"))["guidance"] == "taller arch"
    assert kit_proc.out_dir(store, "gate").joinpath("procedural.glb").read_bytes() == v1_bytes
    assert versions.current(store, "gate") == 1
    versions.unpin(store, "gate", 1)
    called = []
    monkey = lambda s, a, log=print: (called.append(a), _build(s, a))[1]    # noqa: E731
    lr_rebuild = lr.rebuild
    lr.rebuild = monkey
    try:
        r = versions.restore(store, "gate", 2)
    finally:
        lr.rebuild = lr_rebuild
    assert called == ["gate"] and r["rebuilt"] and read_json(kit_proc.spec_path(store, "gate"))["params"]["w"] == 14.0


def test_compare_lists_source_and_brief_differences(store):
    _build(store)
    _build(store, guidance="wider", params={"w": 12.0})
    c = versions.compare(store, "gate", 1, 2)
    assert {"field": "guidance", "a": "taller arch", "b": "wider"} in c["source_diff"]
    assert {"path": "params.w", "a": 10.0, "b": 12.0} in c["brief_diff"]


def _fake_trellis(store, asset="prop", seeds=(1, 2, 3)):
    out = s5_3d.out_dir(store, "library", asset)
    out.mkdir(parents=True, exist_ok=True)
    t = time.time() - 100
    for i, sd in enumerate(seeds):
        stem = f"v_1024_cascade_s{sd}"
        for suf, data in ((".glb", b"x" * 500), (".json", b"{}"), ("_input.png", b"png")):
            p = out / f"{stem}{suf}"
            p.write_bytes(data)
            os.utime(p, (t + i, t + i))
    return out


def test_trellis_results_migrate_oldest_first_and_restore_makes_one_newest(store):
    _fake_trellis(store)
    assert versions.migrate_asset(store, "prop") == 3
    rows = versions.load(store, "prop")
    assert [r["source"]["seed"] for r in rows] == [1, 2, 3] and rows[0]["kind"] == "trellis"
    assert versions.current(store, "prop") == 3
    before = [r["name"] for r in s5_3d.results(store, "library", "prop")]
    versions.restore(store, "prop", 1)
    after = [r["name"] for r in s5_3d.results(store, "library", "prop")]
    assert sorted(before) == sorted(after) and after[0].endswith("_s1")      # listing intact, v1 is now the newest
    assert versions.current(store, "prop") == 1
    versions.delete(store, "prop", 2)
    assert not any("_s2" in r["name"] for r in s5_3d.results(store, "library", "prop"))


def test_trellis_run_records_a_version(store, monkeypatch):
    out = s5_3d.out_dir(store, "library", "prop")
    out.mkdir(parents=True)
    view = "views/prop.png"
    (store.root / "views").mkdir()
    (store.root / view).write_bytes(b"png")

    class P:
        returncode = 0
        pid = 1

        def poll(self):
            return 0

    def popen(cmd, **kw):
        stem, to = "prop_1024_cascade_s7", Path(cmd[cmd.index("--out-dir") + 1])
        for suf, data in ((".glb", b"x"), (".json", b"{}"), ("_input.png", b"x")):
            (to / f"{stem}{suf}").write_bytes(data)
        return P()
    monkeypatch.setattr(s5_3d.subprocess, "Popen", popen)
    monkeypatch.setattr(s5_3d.review, "chosen", lambda *a: view)
    monkeypatch.setattr(s5_3d.review, "rel", lambda store, v: v)
    rec = s5_3d.run(store, "library", "prop", seed=7)
    rows = versions.load(store, "prop")
    assert len(rows) == 1 and rows[0]["source"]["seed"] == 7 and rows[0]["files"][0] == rec["glb"]
    s5_3d.run(store, "library", "prop", seed=7)                       # same files rewritten: still one version
    assert len(versions.load(store, "prop")) == 1


def test_migrate_is_idempotent_and_makes_v1_of_every_build(store):
    _fake_trellis(store, "prop", seeds=(4,))
    out = kit_proc.out_dir(store, "gate")
    out.mkdir(parents=True)
    (out / "procedural.glb").write_bytes(b"g")
    write_json(out / "procedural.json", {"faces": 3})
    write_json(kit_proc.spec_path(store, "idle"), BRIEF)               # a brief with no build: nothing to migrate
    r = versions.migrate(store)
    assert r["detail"] == {"gate": 1, "prop": 1}
    assert versions.migrate(store)["versions"] == 0
    assert [x["n"] for x in versions.load(store, "gate")] == [1]


def test_storage_totals_and_delete_unpinned(store):
    for i in range(3):
        _build(store, params={"w": 10.0 + i})
    versions.pin(store, "gate", 1)
    s = versions.storage(store)
    a = s["assets"][0]
    assert a["asset"] == "gate" and a["versions"] == 3 and a["pinned"] == 1
    assert a["current_bytes"] >= 3000 and s["total"] == a["versions_bytes"] + a["current_bytes"]
    assert a["removable"] == 1                                         # v2; v1 pinned, v3 current
    r = versions.storage_prune(store)
    assert r["removed"] == 1 and [x["n"] for x in versions.load(store, "gate")] == [1, 3]


def test_api_routes(store, monkeypatch):
    for i in range(5):
        _build(store, params={"w": 10.0 + i})
    c = TestClient(ui_app.create_app(JobQueue()))
    U = "/api/projects/demo/library/asset/gate/versions"
    d = c.get(U).json()
    assert [v["n"] for v in d["versions"]] == [5, 4, 3, 2, 1] and d["keep"] == 3 and d["prunable"] == [1, 2]
    assert d["versions"][0]["current"]
    assert c.get(U + "/compare", params={"a": 1, "b": 5}).json()["brief_diff"]
    assert c.post(U + "/2/pin").json()["pinned"]
    r = c.delete(U + "/2")
    assert r.status_code == 409 and "pinned" in r.json()["detail"]
    assert c.delete(U + "/2", params={"confirm_pinned": True}).status_code == 200
    assert c.delete(U + "/99").status_code == 404
    assert c.post(U + "/prune", json={"dry_run": True}).json()["remove"] == [1]
    assert len(c.get(U).json()["versions"]) == 4
    c.post(U + "/prune", json={"dry_run": False})
    assert [v["n"] for v in c.get(U).json()["versions"]] == [5, 4, 3]
    assert c.put("/api/projects/demo/settings", json={"versions_keep": 0}).status_code == 400
    assert c.put("/api/projects/demo/settings", json={"versions_keep": 2}).json()["versions_keep"] == 2
    assert c.get("/api/projects/demo/settings").json()["versions_keep"] == 2
    st = c.get("/api/projects/demo/storage").json()
    assert st["assets"][0]["asset"] == "gate" and st["removable"] == 2
    assert c.post("/api/projects/demo/storage/prune", json={"dry_run": True}).json()["removed"] == 2
    assert c.post("/api/projects/demo/storage/prune", json={"asset": "gate"}).json()["removed"] == 2
    monkeypatch.setattr(versions, "restore", lambda s, a, n, log=print: {"version": n, "rebuilt": False})
    j = c.post(U + "/5/restore").json()
    assert j["kind"] == "library.restore"
    for _ in range(300):
        if c.get(f"/api/jobs/{j['id']}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert c.get(f"/api/jobs/{j['id']}").json()["status"] == "done"


def test_ui_scripts_are_wired_and_free_of_commands():
    import re
    from asset_pipeline.ui import app as app_mod
    from pathlib import Path
    st = Path(app_mod.__file__).parent / "static"
    assert 'src="/static/versions.js"' in (st / "index.html").read_text()
    js = (st / "versions.js").read_text()
    for needle in ('"Storage…"', '"Settings…"', "library.restore", "ver-compare", "st-prune-all", "set-keep", "version-compare-dialog"):
        assert needle in js, needle
    assert not re.search(r"\bap [a-z]+ |--[a-z]", js)
    assert "versionStrip(" in (st / "library_review.js").read_text() and "versionStrip(" in (st / "review.js").read_text()


def test_trellis_source_is_relative_and_names_the_sheet(tmp_path, monkeypatch):
    from asset_pipeline import versions
    from asset_pipeline.project import ProjectStore, write_json
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("p")
    vd = store.root / "views/library/a"
    vd.mkdir(parents=True)
    (vd / "sheet_002_v1.png").write_bytes(b"png")
    write_json(vd / "meta.json", {"views": [{"file": "sheet_002_v1.png", "sheet": "refs/library/a/sheet_002.png"}]})
    od = store.root / "3d/library/a"
    od.mkdir(parents=True)
    (od / "r_1024_cascade_s7.glb").write_bytes(b"glb")
    (od / "r_1024_cascade_s7_input.png").write_bytes(b"png")
    row = versions.record_trellis(store, "library", "a", {"glb": "3d/library/a/r_1024_cascade_s7.glb",
                                  "input": "3d/library/a/r_1024_cascade_s7_input.png", "seed": 7,
                                  "view": str(vd / "sheet_002_v1.png"), "mode": "1024_cascade"})
    assert row["source"]["view"] == "views/library/a/sheet_002_v1.png"
    assert row["source"]["sheet"] == "refs/library/a/sheet_002.png"
    (store.root / "3d/library/_keep-copy").mkdir()
    assert versions.assets_of(store) == ["a"]


# --- "Use this version": either kind becomes the version in use, the route follows -------------

def _library_entry(store, eid="gate", route=None):
    from asset_pipeline.stages import sw_library, sw_site
    e = {"id": eid, "tier": "A", "kind": "structure", "typology": "gate", "part": "gate", "material": None,
         "instances": 0, "districts": {}, "district": "core", "mood": [], "flags": [], "best": None, "share": 0.0,
         "description_auto": "A gate.", "description": None, "size_m": [10, 4, 8], "route_auto": "procedural",
         "slot_ids": []}
    write_json(sw_site.site_dir(store) / "library.json", {"min_share": 0.005, "sources": {}, "mood": {}, "candidates": [],
                                                          "checked": True, "entries": [], "unseen": [e], "surfaces": {}})
    if route:
        sw_library.set_route(store, eid, route)


def _trellis_run(store, asset="gate", stem="sheet_006_v2_1024_cascade_s42"):
    out = kit_proc.out_dir(store, asset)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.glb").write_bytes(b"trellis-glb" * 100)
    write_json(out / f"{stem}.json", {"seed": 42, "glb_faces": 934000, "peak_vram_gb": 9.23})
    (out / f"{stem}_input.png").write_bytes(b"png")
    filestore.intern(store, out / f"{stem}.glb")
    return versions.record_trellis(store, "library", asset, {"glb": f"3d/library/{asset}/{stem}.glb",
                                                             "input": f"3d/library/{asset}/{stem}_input.png",
                                                             "seed": 42, "mode": "1024_cascade"})


def test_using_a_trellis_version_beats_the_brief_and_switches_the_route(store, monkeypatch):
    """The bug: with a brief on disk, the procedural version stayed current whatever was restored."""
    from asset_pipeline.stages import s6_cleanup, sw_library
    _library_entry(store)                                  # route procedural (auto)
    _build(store)                                          # v1 procedural, in use
    versions.pin(store, "gate", 1)                         # keeps its GLB
    t = _trellis_run(store)                                # v2 TRELLIS: not the route, so not in use
    assert versions.current(store, "gate") == 1
    r = versions.restore(store, "gate", t["n"])
    assert r["current"] == t["n"] == versions.current(store, "gate")
    assert r["route"] == {"from": "procedural", "to": "trellis"}
    assert sw_library.decisions(store)["gate"]["route"] == "trellis"
    assert r["glb"] == "3d/library/gate/sheet_006_v2_1024_cascade_s42.glb"
    assert versions.listing(store, "gate")["current"] == t["n"]          # what the strip reads
    # export: cleanup takes the version in use, not the newest file
    seen = {}

    def fake_blender(script, args, log, what, exe=None, blend=None):
        if "--in" not in args:                         # review renders of a rebuild: not this check
            return
        seen["in"] = args[args.index("--in") + 1]
        rep = args[args.index("--report") + 1]
        write_json(rep, {"output_faces": 1, "warnings": []})
    monkeypatch.setattr(s6_cleanup.blender, "run", fake_blender)
    monkeypatch.setattr(s6_cleanup, "_asset", lambda *a: __import__("types").SimpleNamespace(
        dimensions=__import__("types").SimpleNamespace(width=10, depth=4, height=8), usage="cine", category="structure"))
    s6_cleanup.run(store, "library", "gate")
    assert str(seen["in"]).endswith("sheet_006_v2_1024_cascade_s42.glb")
    # and back: using the procedural version switches the route back
    r = versions.restore(store, "gate", 1)
    assert r["current"] == 1 and r["route"] == {"from": "trellis", "to": "procedural"}
    assert versions.current_glb(store, "gate").name == "procedural.glb"


def test_a_new_build_is_used_only_on_its_route_and_a_failed_switch_raises(store):
    _library_entry(store, route="trellis")
    _build(store)                                          # v1 procedural on a TRELLIS-route entry
    t = _trellis_run(store)                                # v2 TRELLIS: matches the route, in use
    assert versions.current(store, "gate") == t["n"]
    _build(store, guidance="again")                         # v3 procedural: not the route
    assert versions.current(store, "gate") == t["n"]
    (store.root / t["files"][0]).unlink()                  # its model gone: can't be used
    with pytest.raises(FileNotFoundError):
        versions.restore(store, "gate", t["n"])


def test_cards_are_the_versions_with_a_model_in_use_first(store):
    _library_entry(store, route="trellis")
    _build(store)                                          # v1 procedural: the build on disk is its model
    t1 = _trellis_run(store, stem="a_1024_cascade_s1")     # v2
    t2 = _trellis_run(store, stem="b_1024_cascade_s2")     # v3, in use
    out = kit_proc.out_dir(store, "gate")                  # an orphan TRELLIS output: becomes a version
    (out / "old_1024_cascade_s7.glb").write_bytes(b"old")
    write_json(out / "old_1024_cascade_s7.json", {"seed": 7})
    os.utime(out / "old_1024_cascade_s7.json", (1000, 1000))
    (out / "stray.glb").write_bytes(b"stray")              # a GLB no version can take: listed as loose
    versions.restore(store, "gate", t1["n"])
    c = versions.cards(store, "gate")
    assert [x["n"] for x in c][0] == t1["n"] and c[0]["current"] and not any(x["current"] for x in c[1:])
    assert {x["n"] for x in c} == {1, t1["n"], t2["n"], 4}             # v4 adopted
    assert versions.current(store, "gate") == t1["n"]                    # adopting didn't switch
    assert c[0]["faces"] == 934000 and c[0]["file_bytes"] == 1100 and c[0]["peak_vram_gb"] == 9.23
    assert [r["file"] for r in versions.loose_files(store)] == ["3d/library/gate/stray.glb"]
    assert versions.remove_loose(store)["removed"] == 1 and not (out / "stray.glb").exists()
    versions.delete(store, "gate", t2["n"])
    assert t2["n"] not in {x["n"] for x in versions.cards(store, "gate")}


def test_review_api_serves_version_cards_and_the_version_in_use(store):
    from fastapi.testclient import TestClient
    from asset_pipeline.jobs import JobQueue
    from asset_pipeline.stages import sw_library
    from asset_pipeline.ui import app as ui_app
    _library_entry(store, route="trellis")
    sw_library.decide(store, "gate", "description")
    sw_library.asset_plan(store)
    _build(store)
    t = _trellis_run(store)
    client = TestClient(ui_app.create_app(JobQueue()))
    a = next(x for x in client.get("/api/projects/demo/review").json()["assets"] if x["asset"]["id"] == "gate")
    assert a["current"]["n"] == t["n"] and a["current"]["kind"] == "trellis"
    assert a["results"][0]["n"] == t["n"] and a["results"][0]["current"]


def test_switching_back_to_the_procedural_build_on_disk_needs_no_rebuild(store, monkeypatch):
    _library_entry(store)
    _build(store)                                          # v1, its build stays on disk (not pinned)
    t = _trellis_run(store)
    versions.restore(store, "gate", t["n"])
    assert {c["n"] for c in versions.cards(store, "gate")} == {1, t["n"]}   # v1 keeps its card
    monkeypatch.setattr(lr, "rebuild", lambda *a, **k: pytest.fail("rebuilt"))
    r = versions.restore(store, "gate", 1)
    assert not r["rebuilt"] and r["current"] == 1 and r["glb"] == "3d/library/gate/procedural.glb"


def test_a_failed_trellis_rerun_keeps_the_old_versions_files(store, monkeypatch):
    """Make 3D with the same view, mode and seed reuses its files: TRELLIS writes into a staging
    dir, so a failed run leaves the version it would replace untouched."""
    out = s5_3d.out_dir(store, "library", "prop")
    out.mkdir(parents=True)
    view = "views/prop.png"
    (store.root / "views").mkdir()
    (store.root / view).write_bytes(b"png")
    fail = {"on": False}

    class P:
        pid = 1

        def __init__(self):
            self.returncode = 1 if fail["on"] else 0

        def poll(self):
            return self.returncode

    def popen(cmd, **kw):
        stem, to = "prop_1024_cascade_s7", Path(cmd[cmd.index("--out-dir") + 1])
        (to / f"{stem}_input.png").write_bytes(b"new input")       # TRELLIS writes the input first
        if not fail["on"]:
            for suf, data in ((".glb", b"model"), (".json", b"{}")):
                (to / f"{stem}{suf}").write_bytes(data)
        return P()
    monkeypatch.setattr(s5_3d.subprocess, "Popen", popen)
    monkeypatch.setattr(s5_3d.review, "chosen", lambda *a: view)
    monkeypatch.setattr(s5_3d.review, "rel", lambda store, v: v)
    rec = s5_3d.run(store, "library", "prop", seed=7)
    glb = store.root / rec["glb"]
    before = {p.name: p.read_bytes() for p in out.glob("prop_*")}
    fail["on"] = True
    with pytest.raises(RuntimeError):
        s5_3d.run(store, "library", "prop", seed=7)
    assert {p.name: p.read_bytes() for p in out.glob("prop_*")} == before and glb.is_file()
    assert not (out / "_staging").exists() and len(versions.load(store, "prop")) == 1


def test_the_active_version_is_never_pruned_or_deleted(store):
    for i in range(5):
        _build(store, params={"w": 10.0 + i})
    versions.set_current(store, "gate", 1)                        # an old version is Active
    versions.set_keep(store, 1)
    assert 1 not in versions.prune(store, "gate")["remove"]
    assert 1 not in versions.removable(store, "gate")["remove"]
    with pytest.raises(ValueError, match="Active"):
        versions.delete(store, "gate", 1)


def _fake_reroll_deps(store, monkeypatch, fail=False):
    from asset_pipeline import brief_guide, building_proc, library_entries
    monkeypatch.setattr(building_proc, "read_band_materials", lambda *a, **k: False)
    monkeypatch.setattr(lr, "render", lambda store, asset, glb, out, mode, form, **k: out.mkdir(parents=True, exist_ok=True))
    (store.root / "refs").mkdir(exist_ok=True)
    (store.root / "refs/s.png").write_bytes(b"png")

    def propose(store, asset, sheet, guidance, view="front"):
        if fail:
            raise ValueError("the vision model did not answer")
        return {"edits": [{"path": "params.w", "before": 10.0, "after": 14.0, "why": "wider"}], "dropped": [], "brief_made": False}
    monkeypatch.setattr(brief_guide, "propose", propose)
    monkeypatch.setattr(brief_guide, "apply", lambda store, asset, edits, guidance: write_json(
        kit_proc.spec_path(store, asset), read_json(kit_proc.spec_path(store, asset)) | {"params": {"w": 14.0}, "guidance": guidance}))
    monkeypatch.setattr(library_entries, "brief_from_sheet", lambda *a, **k: None)


def test_reroll_adds_a_version_that_is_not_active_and_puts_the_build_back(store, monkeypatch):
    _fake_reroll_deps(store, monkeypatch)
    _build(store)
    lr.set_status(store, "gate", "approved", "")
    out = kit_proc.out_dir(store, "gate")
    glb_before, brief_before = (out / "procedural.glb").read_bytes(), read_json(kit_proc.spec_path(store, "gate"))
    res = lr.reroll(store, "gate", "wider")
    assert res["version"] == 2 and versions.current(store, "gate") == 1
    assert read_json(kit_proc.spec_path(store, "gate")) == brief_before
    assert (out / "procedural.glb").read_bytes() == glb_before and not (out / "_reroll_backup").exists()
    assert lr.get(store, "gate")["status"] == "approved" and versions.get(store, "gate", 1)["approved"]
    v2 = versions.get(store, "gate", 2)
    assert v2["source"]["guidance"] == "wider" and "procedural.glb" in v2["files"]
    assert versions.version_glb(store, "gate", v2) == versions.vdir(store, "gate", 2) / "procedural.glb"
    assert versions.current_glb(store, "gate") == out / "procedural.glb"
    versions.restore(store, "gate", 2, log=lambda *a: None)              # Use this version: its own GLB, no rebuild
    assert versions.current(store, "gate") == 2 and read_json(kit_proc.spec_path(store, "gate"))["params"]["w"] == 14.0


def test_a_reroll_version_says_what_changed_in_the_brief(store, monkeypatch):
    _fake_reroll_deps(store, monkeypatch)
    _build(store)
    res = lr.reroll(store, "gate", "wider")
    assert versions.get(store, "gate", 2)["changes"] == res["changes"] == "Brief: params.w 10.0 → 14.0."
    assert res["message"].startswith("v2: Brief: params.w")


def test_a_reroll_identical_to_an_existing_version_adds_none_and_says_why(store, monkeypatch):
    from asset_pipeline import brief_guide
    _fake_reroll_deps(store, monkeypatch)
    _build(store)
    monkeypatch.setattr(brief_guide, "propose", lambda *a, **k: {"edits": [], "dropped": [], "brief_made": False,
                                                                 "unsupported": ["rounded corners"]})
    monkeypatch.setattr(brief_guide, "apply", lambda store, asset, edits, guidance: None)
    res = lr.reroll(store, "gate", "rounded corners")
    assert res["version"] is None and res["unchanged"] and res["same_as"] == 1
    assert res["message"] == ("Nothing changed: the build is identical to v1 (the brief came out the same). "
                              "No setting for: rounded corners.")
    assert [r["n"] for r in versions.load(store, "gate")] == [1] and versions.current(store, "gate") == 1
    assert not versions.vdir(store, "gate", 2).exists()


def test_change_line_names_what_has_no_setting():
    assert lr.change_line([], ["smooth shading"]) == "Brief unchanged. No setting for: smooth shading."
    assert lr.change_line(["shading auto → smooth"], []) == "Brief: shading auto → smooth."


def test_a_failed_reroll_adds_nothing_and_changes_nothing(store, monkeypatch):
    _fake_reroll_deps(store, monkeypatch, fail=True)
    _build(store)
    brief_before = read_json(kit_proc.spec_path(store, "gate"))
    with pytest.raises(ValueError):
        lr.reroll(store, "gate", "wider")
    assert [r["n"] for r in versions.load(store, "gate")] == [1] and versions.current(store, "gate") == 1
    assert read_json(kit_proc.spec_path(store, "gate")) == brief_before
