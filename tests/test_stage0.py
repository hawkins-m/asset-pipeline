import json
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


def _det(h, w, y0, y1, x0, x1, border=False):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return segment.Detection(mask=m, bbox=segment._bbox(m), area_frac=float(m.mean()),
                             touches_border=border)


def _scene(store):
    scene = s0_style.explore(store, n=1) / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(scene)
    return scene


def test_derive_skips_part_of_an_object_already_taken(env, monkeypatch):
    store, _ = env
    scene = _scene(store)
    whole, lid = _det(48, 64, 10, 30, 10, 30), _det(48, 64, 10, 14, 12, 28)  # lid inside barrel
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [whole, lid])
    out = s0_style.derive(store, scene, ["barrel"], per_noun=3, regenerate=False)
    assert sorted(p.name for p in out.glob("cut_*.png")) == ["cut_barrel_0.png"]


def test_derive_dedupes_across_runs_and_nouns(env, monkeypatch):
    store, _ = env
    scene = _scene(store)
    barrel = _det(48, 64, 10, 30, 10, 30)
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [barrel])
    s0_style.derive(store, scene, ["barrel"], regenerate=False)
    out = s0_style.derive(store, scene, ["flower pot"], regenerate=False)  # later run, same object
    assert sorted(p.name for p in out.glob("cut_*.png")) == ["cut_barrel_0.png"]


def test_rederiving_a_noun_replaces_unstarred_items_and_keeps_starred(env, monkeypatch):
    store, _ = env
    scene = _scene(store)
    a, b, c = _det(48, 64, 5, 15, 5, 15), _det(48, 64, 25, 40, 30, 50), _det(48, 64, 20, 30, 5, 15)
    monkeypatch.setattr(segment, "detect", lambda c_, img, noun, **kw: [a, b])
    out = s0_style.derive(store, scene, ["crate"], per_noun=2, regenerate=False)
    review.set_star(store, out / "cut_crate_1.png")                 # keep b
    monkeypatch.setattr(segment, "detect", lambda c_, img, noun, **kw: [b, c])
    s0_style.derive(store, scene, ["crate"], per_noun=2, regenerate=False)
    # crate_0 (a, unstarred) replaced; crate_1 (b, starred) kept; b not re-cut; c new
    assert sorted(p.name for p in out.glob("cut_*.png")) == ["cut_crate_0.png", "cut_crate_1.png"]
    assert review.load(store)["stars"] == {"style/derive/batch_001__scene_000/cut_crate_1.png": True}
    meta = json.loads((out / "meta.json").read_text())
    assert sorted(m["cut"] for m in meta["items"]) == ["cut_crate_0.png", "cut_crate_1.png"]
    new = np.load(out / "mask_crate_0.npz")["mask"]
    assert segment.iou(new, c.mask) == 1.0                          # crate_0 is now c, unstarred


def test_forget_removes_all_given_stars(env):
    store, _ = env
    batch = s0_style.explore(store, n=3)
    keys = [review.set_star(store, batch / f"scene_00{i}.png") for i in range(3)]
    review.forget(store, keys)
    assert review.load(store)["stars"] == {}


def test_same_scene_filename_in_two_batches_gets_separate_derive_dirs(env, monkeypatch):
    store, _ = env
    a = _scene(store)                                            # batch_001/scene_000.png
    b = s0_style.explore(store, n=1) / "scene_000.png"           # batch_002/scene_000.png
    Image.new("RGB", (64, 48), "gray").save(b)
    barrel = _det(48, 64, 10, 30, 10, 30)                        # same place in both scenes
    monkeypatch.setattr(segment, "detect", lambda c, img, noun, **kw: [barrel])
    out_a = s0_style.derive(store, a, ["barrel"], regenerate=False)
    out_b = s0_style.derive(store, b, ["barrel"], regenerate=False)
    assert (out_a.name, out_b.name) == ("batch_001__scene_000", "batch_002__scene_000")
    # b's barrel isn't a duplicate of a's: different scenes
    assert (out_b / "cut_barrel_0.png").is_file()
    assert json.loads((out_b / "meta.json").read_text())["scene"] == "style/explore/batch_002/scene_000.png"


def test_legacy_stem_named_derive_dir_is_kept_for_its_own_scene(env, monkeypatch):
    store, _ = env
    a = _scene(store)
    b = s0_style.explore(store, n=1) / "scene_000.png"
    Image.new("RGB", (64, 48), "gray").save(b)
    legacy = store.root / s0_style.DERIVE / "scene_000"           # made by the old code for a
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text(json.dumps({"scene": "style/explore/batch_001/scene_000.png",
                                                  "items": []}))
    assert s0_style.derive_dir(store, "style/explore/batch_001/scene_000.png") == legacy
    assert s0_style.derive_dir(store, "style/explore/batch_002/scene_000.png").name == "batch_002__scene_000"
