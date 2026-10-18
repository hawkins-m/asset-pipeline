"""Stage 2 (reference sheets) with a fake image backend. No ComfyUI."""
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from asset_pipeline import review
from asset_pipeline.imagegen.base import GenResult
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import AssetPlan, Dimensions, PlanAsset, StyleAnchor
from asset_pipeline.stages import s1_plan, s2_refs

SCENE = "style/explore/batch_001/scene_000.png"
NAME = "batch_001__scene_000"


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate(self, req, out_dir, prefix="img"):
        self.requests.append(req)
        res = []
        for i in range(req.n):
            p = out_dir / f"{prefix}_{i:03d}.png"
            Image.new("RGB", (req.width // 32, req.height // 32), "white").save(p)
            res.append(GenResult(path=p, seed=req.seed or 1, backend=self.name,
                                 meta={"batch_index": i, "prompt": req.prompt}))
        return res


def _asset(id_, name, cat="prop", kit=None, include=True, dims=(1, 1, 1)):
    return PlanAsset(id=id_, name=name, category=cat, description=f"a {name}.", kit=kit,
                     include=include, dimensions=Dimensions(width=dims[0], depth=dims[1], height=dims[2]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("demo")
    p = store.load()
    p.anchor = StyleAnchor(images=[tmp_path / "a.png"], strength=0.06, style_text="painterly")
    store.save(p)
    scene = store.root / SCENE
    scene.parent.mkdir(parents=True)
    Image.new("RGB", (64, 36)).save(scene)
    review.set_star(store, SCENE)
    plan = AssetPlan(scene=SCENE, assets=[
        _asset("barrel", "barrel", dims=(0.6, 0.6, 0.9)),
        _asset("wall-straight", "straight wall", "structure", kit="Stone Wall Kit", dims=(2, 0.5, 1.5)),
        _asset("wall-corner", "corner wall", "structure", kit="stone wall kit", dims=(1, 1, 1.5)),
        _asset("lone", "lone gate", "structure", kit="gate kit"),
        _asset("paving", "cobblestones", "terrain", dims=(20, 20, 0.1)),
        _asset("cart", "cart", include=False),
    ])
    s1_plan.save(store, NAME, plan)
    backend = FakeBackend()
    monkeypatch.setattr(s2_refs, "backend_for", lambda stage, project=None: backend)
    return store, backend


def test_units_group_kits_and_terrain(env):
    store, _ = env
    us = s2_refs.units(store)
    assert [(u.key, u.kind) for u in us] == [("barrel", "object"), ("kit-stone-wall-kit", "kit"),
                                             ("lone", "object"), ("paving", "material")]
    assert [a.id for a in us[1].assets] == ["wall-straight", "wall-corner"]   # cart excluded


def test_sizes_are_flux_friendly():
    for cells, aspect in [(3, 0.5), (3, 1.6), (2, 1.0), (6, 1.6)]:
        w, h = s2_refs._size(cells, aspect)
        assert w % 64 == 0 and h % 64 == 0 and h >= 512
        assert 0.8e6 < w * h < 1.5e6
    w, h = s2_refs._size(3, 0.75)                                  # a barrel: ~2.5:1 strip
    assert 2.2 < w / h < 2.8


def test_requests_per_kind(env):
    store, _ = env
    barrel, kit, _, paving = s2_refs.units(store)
    anchor = store.load().anchor
    prompt, w, h, a = s2_refs.request(barrel, anchor)
    assert "one barrel" in prompt and "pure white" in prompt and a is anchor and w > h
    prompt, w, h, a = s2_refs.request(kit, anchor)
    assert "2 matching pieces of one Stone Wall Kit" in prompt
    assert "1. straight wall" in prompt and "2. corner wall" in prompt
    prompt, w, h, a = s2_refs.request(paving, anchor)
    assert "Top-down" in prompt and (w, h) == (1024, 1024)
    assert a.images == [] and a.style_text == "painterly"          # style text, no object images


def test_generate_numbers_sheets_and_records_meta(env):
    store, backend = env
    barrel = s2_refs.units(store)[0]
    s2_refs.generate(store, barrel, n=2, seed=4)
    s2_refs.generate(store, barrel, n=1)
    assert s2_refs.sheets(store, barrel) == [f"refs/{NAME}/barrel/sheet_00{i}.png" for i in range(3)]
    meta = json.loads((barrel.dir(store) / "meta.json").read_text())
    assert [s["file"] for s in meta["sheets"]] == ["sheet_000.png", "sheet_001.png", "sheet_002.png"]
    assert meta["kind"] == "object" and meta["sheets"][0]["seed"] == 4 and meta["sheets"][0]["anchor"]
    assert not list(barrel.dir(store).glob("tmp*"))


def test_generate_missing_skips_units_with_sheets(env):
    store, backend = env
    s2_refs.generate(store, s2_refs.units(store)[0], n=1)
    made = s2_refs.generate_missing(store, n=1, log=lambda m: None)
    assert len(made) == 3 and len(backend.requests) == 4
    assert s2_refs.generate_missing(store, n=1, log=lambda m: None) == []


def test_ui_lists_and_generates(env):
    from asset_pipeline.ui import app as ui_app
    store, _ = env
    client = TestClient(ui_app.create_app(JobQueue()))
    units = client.get("/api/projects/demo/refs").json()
    assert [u["key"] for u in units] == ["barrel", "kit-stone-wall-kit", "lone", "paving"]
    assert units[1]["title"] == "Stone Wall Kit" and units[0]["sheets"] == []
    job = client.post("/api/projects/demo/refs/generate", json={"unit": "barrel", "n": 2}).json()
    for _ in range(200):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
    assert j["status"] == "done" and j["result"] == 2
    assert len(client.get("/api/projects/demo/refs").json()[0]["sheets"]) == 2
    assert client.post("/api/projects/demo/refs/generate", json={"unit": "nope"}).status_code == 404


def _wait_job(client, job):
    for _ in range(300):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            return j
    return j


def test_guidance_steers_one_batch_and_is_saved_with_its_sheets(env):
    store, backend = env
    barrel = s2_refs.units(store)[0]
    g = "slatted timber counter boxes under one shared timber pergola, no stone."
    s2_refs.generate(store, barrel, n=2, guidance=g)
    s2_refs.generate(store, barrel, n=1)                                # the next batch: no guidance
    p1, p2 = backend.requests[0].prompt, backend.requests[1].prompt
    assert "a barrel. slatted timber counter boxes under one shared timber pergola, no stone" in p1
    assert "pergola" not in p2
    sm = s2_refs.sheet_meta(store, barrel)
    assert [m.get("guidance") for m in sm.values()] == [g.rstrip("."), g.rstrip("."), None]
    kit = s2_refs.units(store)[1]
    assert "no mortar" in s2_refs.request(kit, None, guidance="no mortar")[0]       # kits and
    assert "wet look" in s2_refs.request(s2_refs.units(store)[3], None, guidance="wet look")[0]   # materials too
    with pytest.raises(ValueError, match="under 500"):
        s2_refs.clean_guidance("x" * 501)


def test_ui_reroll_with_guidance_and_the_field_starts_from_the_last_one(env):
    from asset_pipeline.ui import app as ui_app
    store, backend = env
    client = TestClient(ui_app.create_app(JobQueue()))
    job = client.post("/api/projects/demo/refs/generate",
                      json={"plan": NAME, "unit": "barrel", "n": 1, "guidance": "  oak staves,\n iron hoops "}).json()
    assert job["tag"]["guidance"] == "oak staves, iron hoops"
    assert _wait_job(client, job)["status"] == "done"
    assert "a barrel. oak staves, iron hoops" in backend.requests[-1].prompt
    u = client.get("/api/projects/demo/refs").json()[0]
    assert u["guidance"] == "oak staves, iron hoops"
    assert u["sheet_meta"][u["sheets"][0]]["guidance"] == "oak staves, iron hoops"
    assert client.post("/api/projects/demo/refs/generate", json={"guidance": "x"}).status_code == 400   # needs a unit
    assert client.post("/api/projects/demo/refs/generate",
                       json={"unit": "barrel", "guidance": "x" * 600}).status_code == 400


def test_more_sheets_with_comfyui_down_offers_to_start_it(env, monkeypatch):
    """The "+2 more" path with ComfyUI down: a 409 comfy_down the UI turns into "Start
    ComfyUI and run" (tests/test_ui_errors.py checks the browser side)."""
    from asset_pipeline.comfy import service
    from asset_pipeline.ui import app as ui_app
    gate = service.Gate(ready_fn=lambda: False)
    client = TestClient(ui_app.create_app(JobQueue(gates={"comfy": gate}), comfy_gate=gate))
    r = client.post("/api/projects/demo/refs/generate", json={"plan": NAME, "unit": "barrel", "n": 2})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "comfy_down" and isinstance(d["message"], str) and "ComfyUI" in d["message"]
