"""Project size on disk (toolbar and Properties): categories, every inode counted once (the
shared store and hard-linked UE backups), unreferenced objects as Other, the drive's space, and
a background cache that measures at most once per interval."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import disk_usage, filestore
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore
from asset_pipeline.ui import app as ui_app


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    monkeypatch.setenv("AP_UE_PROJECTS", str(tmp_path / "unreal"))
    monkeypatch.setenv("AP_UE_BACKUPS", str(tmp_path / "backups"))
    return ProjectStore.create("p")


def _write(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(n))
    return path


def _blocks(p):
    return os.lstat(p).st_blocks * 512


def test_categories():
    c = disk_usage.category
    assert c("refs/library/gate/sheet_000.png") == "sheets"
    assert c("views/library/gate/view_0.png") == "views"
    assert c("3d/library/gate/versions/v1/procedural.glb") == "models"
    assert c("3d/library/gate/review/round_1/front.png") == "models"
    assert c("3d/library/gate/procedural/facade_base.png") == "textures"
    assert c("export/look/x/textures/a.png") == "textures"
    assert c("export/assets/gate/SM_gate.glb") == "models"
    assert c("frames/med-x/frame_000.png") == c("shots/med-x/depth.png") == "frames"
    assert c("site/greybox.blend") == c("plan/library.json") == c("export/terrain/far.glb") == "greybox"
    assert c("style/moodboard/a.png") == c(".git/objects/aa/bb") == c("runs.jsonl") == "other"


def test_measure_counts_shared_and_linked_files_once(store):
    a = _write(store.root / "refs/library/gate/sheet_000.png", 20000)
    b = _write(store.root / "refs/library/arch/sheet_000.png", 0)
    b.write_bytes(a.read_bytes())
    filestore.intern_all(store, [a, b])                         # one object, two references
    g = _write(store.root / "3d/library/gate/procedural.glb", 50000)
    filestore.intern(store, g)
    orphan = _write(filestore.object_path(store, "ab" * 32, ".png"), 9000)
    _write(store.root / "frames/s/frame_000.png", 30000)
    ue = _write(store.root.parent.parent / "unreal/p/Content/a.uasset", 40000)
    snap = store.root.parent.parent / "backups/p/20261018-000000/Content/a.uasset"
    snap.parent.mkdir(parents=True)
    os.link(ue, snap)                                           # rsync --link-dest: same inode
    r = disk_usage.measure(store)
    cats = {c["key"]: c for c in r["categories"]}
    assert cats["sheets"]["bytes"] == _blocks(a) and cats["sheets"]["files"] == 1
    assert cats["models"]["bytes"] == _blocks(g)
    assert cats["frames"]["files"] == 1
    assert cats["unreal"]["files"] == 1 and cats["unreal"]["bytes"] == _blocks(ue)
    assert r["unreal"]["project"]["bytes"] == _blocks(ue) and r["unreal"]["backups"]["bytes"] == 0
    assert r["unreferenced"] == 1 and r["unreferenced_bytes"] == _blocks(orphan)
    assert cats["other"]["bytes"] >= _blocks(orphan)            # project.json and the orphan
    assert r["total"] == sum(c["bytes"] for c in r["categories"])
    assert r["saved"] == 20000                                  # the second sheet is a link
    d = r["drive"]
    assert d["total"] >= d["used"] > 0 and d["free"] > 0 and os.path.ismount(d["path"])


def test_cache_measures_in_the_background_at_most_once_per_interval(store, monkeypatch):
    calls = []
    monkeypatch.setattr(disk_usage, "measure", lambda s: calls.append(1) or {"total": len(calls)})
    c = disk_usage.Cache(lambda slug: store, interval=0.5)

    def settle():
        for _ in range(200):
            r = c.get("p")
            if not r["pending"]:
                return r
            time.sleep(0.01)
        raise AssertionError("never finished")

    first = c.get("p")
    assert first["result"] is None and first["pending"]
    assert settle()["result"] == {"total": 1}
    assert c.get("p", dirty=True)["pending"]                    # a finished job: once the interval is up
    assert len(calls) == 1
    assert c.get("p", dirty=True)["result"] == {"total": 1}     # the old result meanwhile, one timer
    assert settle()["result"] == {"total": 2} and len(calls) == 2
    assert c.get("p")["pending"] is False                       # inside the interval: nothing new


def test_disk_api(store):
    _write(store.root / "refs/library/gate/sheet_000.png", 5000)
    client = TestClient(ui_app.create_app(JobQueue()))
    for _ in range(200):
        r = client.get("/api/projects/p/disk").json()
        if r["result"] and not r["pending"]:
            break
        time.sleep(0.01)
    assert [c["label"] for c in r["result"]["categories"]] == [lb for _, lb in disk_usage.CATEGORIES]
    assert r["result"]["total"] > 0 and r["error"] is None
    assert client.get("/api/projects/nope/disk").status_code == 404
