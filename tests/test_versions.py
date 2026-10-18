"""Asset version bank: recording, retention, pin, approve, restore, compare, storage, migration and
the API. Blender and TRELLIS are monkeypatched; projects are temporary and neutral."""
import os
import time

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
        (out / "procedural.glb").write_bytes(b"glb" * 1000)
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
    with pytest.raises(PermissionError):
        versions.delete(store, "gate", 2)
    versions.unpin(store, "gate", 2)
    assert not (d2 / "procedural.glb").exists()
    versions.delete(store, "gate", 2)
    assert [x["n"] for x in versions.load(store, "gate")] == [1] and not d2.exists()


def test_restore_without_a_glb_rebuilds_and_with_one_copies_it_back(store):
    _build(store)                                                    # v1: w 10, guidance "taller arch"
    versions.pin(store, "gate", 1)                                   # current: GLB kept
    _build(store, params={"w": 14.0}, guidance="other")             # v2
    assert read_json(kit_proc.spec_path(store, "gate"))["params"]["w"] == 14.0
    glb = kit_proc.out_dir(store, "gate").joinpath("procedural.glb")
    filestore.detach(glb)                          # what a build does before writing it
    glb.write_bytes(b"newer")
    r = versions.restore(store, "gate", 1)
    assert r == {"version": 1, "rebuilt": False}                     # pinned GLB: no rebuild
    assert read_json(kit_proc.spec_path(store, "gate"))["guidance"] == "taller arch"
    assert kit_proc.out_dir(store, "gate").joinpath("procedural.glb").read_bytes() == b"glb" * 1000
    assert versions.current(store, "gate") == 1
    versions.unpin(store, "gate", 1)
    called = []
    monkey = lambda s, a, log=print: (called.append(a), {"version": 9})[1]    # noqa: E731
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
        stem = "prop_1024_cascade_s7"
        for suf, data in ((".glb", b"x"), (".json", b"{}"), ("_input.png", b"x")):
            (out / f"{stem}{suf}").write_bytes(data)
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
