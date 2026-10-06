"""Stages 3-5: cutting views, review (choose, usage), TRELLIS jobs and their Stop. No GPU."""
import json
import os
import stat
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from asset_pipeline import review, segment
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import AssetPlan, Dimensions, PlanAsset
from asset_pipeline.stages import s1_plan, s3_views, s5_3d

SCENE = "style/explore/batch_001/scene_000.png"
NAME = "batch_001__scene_000"
W, H = 300, 100


def _asset(id_, name, cat="prop", kit=None):
    return PlanAsset(id=id_, name=name, noun=name, category=cat, kit=kit,
                     dimensions=Dimensions(width=1, depth=1, height=1))


def _sheet(path, xs, snow=False):
    """White sheet with dark blocks at the given x ranges (+ white-ish 'snow' on top)."""
    px = np.full((H, W, 3), 255, np.uint8)
    for x0, x1 in xs:
        px[40:90, x0:x1] = (60, 90, 40)
        if snow:
            px[30:40, x0:x1] = (238, 240, 244)   # near-white, outside SAM's mask below
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(px).save(path)


def _det(x0, x1):
    m = np.zeros((H, W), bool)
    m[40:90, x0:x1] = True
    return segment.Detection(mask=m, bbox=segment._bbox(m), area_frac=float(m.mean()), touches_border=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("demo")
    Image.new("RGB", (8, 8)).save(_mk(store.root / SCENE))
    review.set_star(store, SCENE)
    s1_plan.save(store, NAME, AssetPlan(scene=SCENE, assets=[
        _asset("shrub", "shrub"), _asset("wall-a", "wall a", "structure", kit="wall kit"),
        _asset("wall-b", "wall b", "structure", kit="wall kit"), _asset("paving", "paving", "terrain")]))
    shrub = store.root / f"refs/{NAME}/shrub/sheet_000.png"
    _sheet(shrub, [(10, 60), (120, 170), (230, 290)], snow=True)
    kit = store.root / f"refs/{NAME}/kit-wall-kit/sheet_000.png"
    _sheet(kit, [(150, 280), (20, 100)])
    pave = store.root / f"refs/{NAME}/paving/sheet_000.png"
    _sheet(pave, [(0, 300)])
    for p in (shrub, kit, pave):
        review.set_star(store, p)
    return store


def _mk(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_cut_object_kit_and_skip_material(env, monkeypatch):
    store = env
    found = {"shrub": [_det(230, 290), _det(10, 60), _det(120, 170)],
             "wall a": [_det(150, 280)], "wall b": [_det(20, 100), _det(150, 280)]}
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: found[noun])
    assert s3_views.cut_starred(store, log=lambda m: None) == 5
    shrub = s3_views.views(store, NAME, "shrub")
    assert [(v["position"], v["asked"], v["method"]) for v in shrub] == [
        (1, "front", "sam"), (2, "side", "sam"), (3, "back", "sam")]
    # band crop keeps the near-white "snow" the mask left out
    v1 = np.asarray(Image.open(store.root / shrub[0]["key"]).convert("RGB"))
    assert ((v1 == (238, 240, 244)).all(axis=2)).any()
    # kit: pieces assigned left to right in the order asked (wall a, wall b)
    assert [v["position"] for v in s3_views.views(store, NAME, "wall-a")] == [1]
    assert [v["position"] for v in s3_views.views(store, NAME, "wall-b")] == [2]
    assert s3_views.views(store, NAME, "paving") == []
    assert s3_views.cut_starred(store, log=lambda m: None) == 0       # already cut


def test_recut_replaces_views_and_forgets_their_choice(env, monkeypatch):
    store = env
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [_det(10, 60), _det(120, 170), _det(230, 290)])
    sheet = f"refs/{NAME}/shrub/sheet_000.png"
    s3_views.cut(store, sheet, client=object())
    first = s3_views.views(store, NAME, "shrub")[0]["key"]
    review.choose(store, NAME, "shrub", first)
    review.set_star(store, first)
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [_det(10, 60), _det(120, 170)])
    monkeypatch.setattr(segment, "white_components", lambda p: [])
    views = s3_views.cut(store, sheet, client=object())
    assert len(views) == 2 and all(v["asked"] is None for v in views)  # 2 of 3: no front/side/back
    assert not review.load(store)["stars"].get(first)


def test_white_fallback_when_sam_finds_too_few(env, monkeypatch):
    store = env
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [])
    views = s3_views.cut(store, f"refs/{NAME}/shrub/sheet_000.png", client=object())
    assert [v["method"] for v in views] == ["white"] * 3
    assert [v["bbox"][0] for v in views] == sorted(v["bbox"][0] for v in views)


def test_usage_hero_is_a_hand_edit(env):
    store = env
    plan = s1_plan.set_usage(store, NAME, "shrub", "hero")
    assert plan.assets[0].usage == "hero" and plan.edited is not None
    with pytest.raises(Exception):
        s1_plan.set_usage(store, NAME, "shrub", "legendary")


# --- TRELLIS jobs ----------------------------------------------------------------------

FAKE_TRELLIS = r"""#!/usr/bin/env bash
# Mimics scripts/trellis: GPU claim lock, output naming, optional hang.
img="$1"; shift
while [ $# -gt 0 ]; do case "$1" in --type) mode=$2; shift;; --seed) seed=$2; shift;; --out-dir) out=$2; shift;; esac; shift; done
lock="$AP_ROOT/logs/trellis-gpu${TRELLIS_GPU:-0}-$$.lock"; mkdir -p "$AP_ROOT/logs"; : > "$lock"; trap 'rm -f "$lock"' EXIT
stem="$(basename "${img%.*}" | cut -c1-24)_${mode}_s${seed}"
if [ -n "$FAKE_TRELLIS_HANG" ]; then sleep 60 & wait; fi
echo glb > "$out/$stem.glb"; echo png > "$out/${stem}_input.png"
echo '{"glb_faces": 1000, "glb_vertices": 500, "raw_faces": 2000, "raw_vertices": 1000, "peak_vram_gb": 4.7}' > "$out/$stem.json"
"""


@pytest.fixture
def fake_trellis(tmp_path, monkeypatch):
    script = tmp_path / "fake_trellis"
    script.write_text(FAKE_TRELLIS)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(s5_3d, "TRELLIS", script)
    return script


def _chosen_view(store, monkeypatch):
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [_det(10, 60), _det(120, 170), _det(230, 290)])
    s3_views.cut(store, f"refs/{NAME}/shrub/sheet_000.png", client=object())
    view = s3_views.views(store, NAME, "shrub")[1]["key"]
    review.choose(store, NAME, "shrub", view)
    return view


def test_trellis_run_and_results(env, fake_trellis, monkeypatch):
    store = env
    with pytest.raises(ValueError, match="choose a view"):
        s5_3d.run(store, NAME, "shrub")
    view = _chosen_view(store, monkeypatch)
    rec = s5_3d.run(store, NAME, "shrub", mode="512")
    assert rec["view"] == view and rec["glb"] == f"3d/{NAME}/shrub/sheet_000_v2_512_s42.glb"
    [res] = s5_3d.results(store, NAME, "shrub")
    assert res["faces"] == 1000 and res["raw_ratio"] == 2.0
    assert not list((store.root.parents[1] / "logs").glob("trellis-gpu*.lock"))


def test_stop_kills_trellis_and_releases_gpu(env, fake_trellis, monkeypatch):
    store = env
    _chosen_view(store, monkeypatch)
    monkeypatch.setenv("FAKE_TRELLIS_HANG", "1")
    q = JobQueue()
    job = q.submit("3d.trellis", "demo", lambda: s5_3d.run(store, NAME, "shrub"), lane="gpu0")
    logs = store.root.parents[1] / "logs"
    end = time.time() + 10
    while not list(logs.glob("trellis-gpu0-*.lock")) and time.time() < end:
        time.sleep(0.05)
    [lock] = list(logs.glob("trellis-gpu0-*.lock"))
    pid = int(lock.stem.rsplit("-", 1)[1])
    t = time.time()
    q.cancel(job.id)
    while q.get(job.id).status == "running" and time.time() - t < 20:
        time.sleep(0.05)
    assert q.get(job.id).status == "canceled" and time.time() - t < 10
    assert not lock.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert s5_3d.results(store, NAME, "shrub") == []


def test_review_api(env, fake_trellis, monkeypatch):
    from asset_pipeline.ui import app as ui_app
    store = env
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [_det(10, 60), _det(120, 170), _det(230, 290)])
    client = TestClient(ui_app.create_app(JobQueue()))
    r = client.get("/api/projects/demo/review").json()
    assert sorted(r["pending_sheets"]) == [f"refs/{NAME}/kit-wall-kit/sheet_000.png", f"refs/{NAME}/shrub/sheet_000.png"]
    assert [a["asset"]["id"] for a in r["assets"]] == ["shrub", "wall-a", "wall-b"]   # no terrain
    assert client.post("/api/projects/demo/3d", json={"plan": NAME, "asset": "shrub"}).status_code == 400
    s3_views.cut(store, f"refs/{NAME}/shrub/sheet_000.png", client=object())
    view = client.get("/api/projects/demo/review").json()["assets"][0]["views"][0]["key"]
    assert client.post("/api/projects/demo/review/choose", json={"plan": NAME, "asset": "shrub", "view": view}).json() == {"chosen": view}
    assert client.post("/api/projects/demo/review/usage", json={"plan": NAME, "asset": "shrub", "usage": "hero"}).status_code == 200
    assert client.post("/api/projects/demo/review/usage", json={"plan": NAME, "asset": "shrub", "usage": "x"}).status_code == 422
    job = client.post("/api/projects/demo/3d", json={"plan": NAME, "asset": "shrub", "mode": "512"}).json()
    assert job["lane"] == "gpu0" and job["tag"] == {"plan": NAME, "asset": "shrub"}
    end = time.time() + 10
    while client.get(f"/api/jobs/{job['id']}").json()["status"] not in ("done", "error") and time.time() < end:
        time.sleep(0.05)
    a = client.get("/api/projects/demo/review").json()["assets"][0]
    assert a["asset"]["usage"] == "hero" and a["chosen"] == view and len(a["results"]) == 1
