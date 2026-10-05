import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from asset_pipeline import review, segment
from asset_pipeline.imagegen.base import GenResult
from asset_pipeline.project import ProjectStore
from asset_pipeline.stages import s0_style


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate(self, req, out_dir, prefix="img"):
        self.requests.append(req)
        out_dir.mkdir(parents=True, exist_ok=True)
        res = []
        for i in range(req.n):
            p = out_dir / f"{prefix}_{i:03d}.png"
            Image.new("RGB", (req.width // 16, req.height // 16), (40 * i, 100, 150)).save(p)
            res.append(GenResult(path=p, seed=req.seed, backend=self.name, meta={"batch_index": i}))
        return res


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    backend = FakeBackend()
    monkeypatch.setattr(s0_style, "backend_for", lambda stage, project=None: backend)
    store = ProjectStore.create("demo", brief="a misty harbour")
    return store, backend


def _fake_detect(scene_size=(64, 48), same_for_all_nouns=False):
    seen = []

    def detect(client, image, noun, **kw):
        if noun not in seen:
            seen.append(noun)
        y0 = 4 if same_for_all_nouns else 4 + 12 * seen.index(noun)  # distinct rows per noun
        w, h = scene_size
        dets = []
        for x0, touches in [(10, False), (30, False), (0, True)]:  # last one touches the edge
            m = np.zeros((h, w), bool)
            m[y0:y0 + 8, x0:x0 + 8] = True
            dets.append(segment.Detection(mask=m, bbox=segment._bbox(m), area_frac=float(m.mean()),
                                          touches_border=touches))
        return dets
    return detect


def test_explore_batches_and_names(env):
    store, backend = env
    out = s0_style.explore(store, n=6, seed=10, batch_size=4)
    assert out.name == "batch_001"
    assert sorted(p.name for p in out.glob("scene_*.png")) == [f"scene_{i:03d}.png" for i in range(6)]
    assert not list(out.glob("tmp*"))
    assert [r.n for r in backend.requests] == [4, 2]
    assert [r.seed for r in backend.requests] == [10, 14]
    assert "a misty harbour" in backend.requests[0].prompt
    assert s0_style.explore(store, n=1).name == "batch_002"


def test_explore_requires_a_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("nobrief")
    with pytest.raises(ValueError, match="brief"):
        s0_style.explore(store)


def test_derive_cuts_skips_border_and_redraws_from_cutout(env, monkeypatch):
    store, backend = env
    batch = s0_style.explore(store, n=1)
    scene = batch / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(scene)
    review.set_star(store, scene)
    monkeypatch.setattr(segment, "detect", _fake_detect())
    out = s0_style.derive(store, scene, ["boat", "red house"], per_noun=5, style_text="flat colours")
    names = sorted(p.name for p in out.glob("*.png"))
    # 2 non-border detections per noun, each with a cutout and a redraw
    assert names == ["cut_boat_0.png", "cut_boat_1.png", "cut_red-house_0.png", "cut_red-house_1.png",
                     "obj_boat_0.png", "obj_boat_1.png", "obj_red-house_0.png", "obj_red-house_1.png"]
    redraws = backend.requests[1:]
    assert all(r.anchor.images[0].name.startswith("cut_") for r in redraws)
    assert all(r.anchor.strength == s0_style.DERIVE_STRENGTH for r in redraws)
    assert redraws[0].anchor.style_text == "flat colours"
    assert "a single boat" in redraws[0].prompt


def test_save_anchor_from_starred_derived(env, monkeypatch):
    store, _ = env
    scene = s0_style.explore(store, n=1) / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(scene)
    monkeypatch.setattr(segment, "detect", _fake_detect())
    out = s0_style.derive(store, scene, ["boat"], per_noun=2)
    with pytest.raises(ValueError, match="star"):
        s0_style.save_anchor(store)
    review.set_star(store, out / "obj_boat_0.png")
    review.set_star(store, out / "cut_boat_1.png")
    anchor = s0_style.save_anchor(store, strength=0.1, style_text="ink wash")
    assert len(anchor.images) == 2 and all(p.is_file() for p in anchor.images)
    assert store.load().anchor.style_text == "ink wash"
    # style_text None keeps the existing text
    assert s0_style.save_anchor(store).style_text == "ink wash"
    assert (store.root / "style/anchor/anchor.json").is_file()


def test_review_rejects_escapes_and_missing(env):
    store, _ = env
    with pytest.raises(ValueError):
        review.set_star(store, "../outside.png")
    with pytest.raises(FileNotFoundError):
        review.set_star(store, "style/nope.png")


def test_ui_api_flow(env, monkeypatch):
    store, _ = env
    from asset_pipeline.ui.app import create_app
    client = TestClient(create_app())
    assert "demo" in client.get("/api/projects").json()
    job = client.post("/api/projects/demo/style/explore", json={"n": 2}).json()
    for _ in range(100):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert j["status"] == "done", j
    data = client.get("/api/projects/demo").json()
    images = data["explore"][0]["images"]
    assert len(images) == 2
    assert client.get(f"/files/demo/{images[0]}").status_code == 200
    r = client.post("/api/projects/demo/star", json={"path": images[0]})
    assert r.status_code == 200
    assert client.get("/api/projects/demo").json()["stars"] == {images[0]: True}
    # path traversal is refused
    assert client.get("/files/demo/../../etc/passwd").status_code == 404
    assert client.get("/files/demo/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert client.post("/api/projects/demo/star", json={"path": "../x.png"}).status_code == 400
    assert client.post("/api/projects", json={"slug": "Bad Slug"}).status_code == 400


def test_derive_skips_same_object_found_under_another_noun(env, monkeypatch):
    store, backend = env
    scene = s0_style.explore(store, n=1) / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(scene)
    monkeypatch.setattr(segment, "detect", _fake_detect(same_for_all_nouns=True))
    out = s0_style.derive(store, scene, ["barrel", "flower pot"], per_noun=5, regenerate=False)
    assert sorted(p.name for p in out.glob("*.png")) == ["cut_barrel_0.png", "cut_barrel_1.png"]


def test_save_anchor_falls_back_to_derive_style_text(env, monkeypatch):
    store, _ = env
    scene = s0_style.explore(store, n=1) / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(scene)
    monkeypatch.setattr(segment, "detect", _fake_detect())
    out = s0_style.derive(store, scene, ["boat"], per_noun=1, style_text="painterly")
    review.set_star(store, out / "obj_boat_0.png")
    assert s0_style.save_anchor(store).style_text == "painterly"
