"""Stage 1 (scene -> asset plan) with a scripted LLM. No GPU, no network, no paid calls."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import numpy as np

from asset_pipeline import review, segment
from asset_pipeline.jobs import JobQueue
from asset_pipeline.paid import PaidAPIBlocked
from asset_pipeline.project import ProjectStore
from asset_pipeline.stages import s1_plan

ANALYSIS = {
    "summary": "An alpine market square.",
    "scale_notes": "door ~2 m",
    "assets": [
        {"name": "Market Stall", "noun": "market stall", "category": "structure", "description": "timber stall, red awning",
         "count": 3, "width_m": 3, "depth_m": 2, "height_m": 2.8, "kit": None,
         "placement": "around the square", "bbox_2d": [100, 200, 400, 700]},
        {"name": "market stall", "noun": "stall", "category": "structure", "description": "smaller stall",
         "count": 1, "width_m": 2, "depth_m": 1.5, "height_m": 2.5, "kit": "  ",
         "placement": "left", "bbox_2d": [500, 500, 400, 600]},          # inverted box
        {"name": "barrel", "noun": "barrel", "category": "prop", "description": "oak barrel, iron hoops",
         "count": 4, "width_m": 0.6, "depth_m": 0.6, "height_m": 0.9, "kit": None,
         "placement": "by the stalls", "bbox_2d": [0, 900, 50, 1200]},   # clamped to 1000
        {"name": "stone wall", "noun": "wall", "category": "structure", "description": "dry stone",
         "count": 6, "width_m": 4, "depth_m": 0.5, "height_m": 1.2, "kit": "stone wall kit",
         "placement": "edge"},
    ],
    "relations": [
        {"subject": "barrel", "relation": "next to", "object": "Market Stall"},
        {"subject": "barrel", "relation": "under", "object": "a cart"},      # unknown: dropped
        {"subject": "Stone-Wall", "relation": "behind", "object": "barrel"},  # slug match
    ],
}


class ScriptedLLM:
    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def json_text(self, system, prompt, images, schema):
        self.calls.append({"system": system, "prompt": prompt, "images": images, "schema": schema})
        return self.replies.pop(0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    monkeypatch.delenv("AP_ALLOW_PAID_APIS", raising=False)
    s = ProjectStore.create("demo", brief="alpine market")
    for batch in ("batch_001", "batch_002"):
        p = s.root / "style/explore" / batch / "scene_000.png"
        p.parent.mkdir(parents=True)
        Image.new("RGB", (64, 36), "gray").save(p)
    review.set_star(s, "style/explore/batch_001/scene_000.png")
    return s


SCENE = "style/explore/batch_001/scene_000.png"


def test_analyze_builds_plan(store):
    llm = ScriptedLLM(json.dumps(ANALYSIS))
    plan = s1_plan.analyze(store, SCENE, llm=llm)
    assert "alpine market" in llm.calls[0]["prompt"]
    assert llm.calls[0]["images"] == [store.root / SCENE]
    assert [a.id for a in plan.assets] == ["market-stall", "market-stall-2", "barrel", "stone-wall"]
    assert plan.assets[0].bbox == [0.1, 0.2, 0.4, 0.7]
    assert plan.assets[1].bbox is None and plan.assets[1].kit is None
    assert plan.assets[2].bbox == [0.0, 0.9, 0.05, 1.0]
    assert plan.assets[3].kit == "stone wall kit"
    assert plan.assets[0].dimensions.height == 2.8
    assert [(r.subject, r.object) for r in plan.relations] == [("barrel", "market-stall"),
                                                                ("stone-wall", "barrel")]
    saved = json.loads((store.root / "plan/batch_001__scene_000.json").read_text())
    assert saved["llm"] == "scripted" and saved["edited"] is None
    assert json.loads((store.root / "runs.jsonl").read_text().splitlines()[-1])["stage"] == "plan.analyze"


def test_invalid_answer_is_retried_with_errors(store):
    bad = dict(ANALYSIS, assets=[dict(ANALYSIS["assets"][2], height_m=0)])
    llm = ScriptedLLM(json.dumps(bad), json.dumps(ANALYSIS))
    s1_plan.analyze(store, SCENE, llm=llm)
    assert len(llm.calls) == 2 and "height_m" in llm.calls[1]["prompt"]


def test_plan_names_dont_collide_across_batches(store):
    assert s1_plan.plan_name("style/explore/batch_001/scene_000.png") == "batch_001__scene_000"
    assert s1_plan.plan_name("style/explore/batch_002/scene_000.png") == "batch_002__scene_000"
    assert s1_plan.plan_name("scenes/my town.png") == "my town"
    with pytest.raises(ValueError):
        s1_plan.plan_file(store, "../project")


def test_edited_plan_needs_force_and_keeps_prev(store):
    name = "batch_001__scene_000"
    plan = s1_plan.analyze(store, SCENE, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    plan.assets[0].name = "big stall"
    s1_plan.save(store, name, plan, edited=True)
    with pytest.raises(s1_plan.PlanExists):
        s1_plan.analyze(store, SCENE, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    s1_plan.analyze(store, SCENE, force=True, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    prev = json.loads((store.root / f"plan/{name}.prev.json").read_text())
    assert prev["assets"][0]["name"] == "big stall"
    assert s1_plan.load(store, name).edited is None


def test_save_assigns_ids_and_drops_dangling_relations(store):
    name = "batch_001__scene_000"
    plan = s1_plan.analyze(store, SCENE, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    plan.assets = [a for a in plan.assets if a.id != "barrel"]
    plan.assets.append(plan.assets[0].model_copy(update={"id": "", "name": "Market stall"}))
    s1_plan.save(store, name, plan, edited=True)
    out = s1_plan.load(store, name)
    assert [a.id for a in out.assets][-1] == "market-stall-3"
    assert out.relations == [] and out.edited is not None


def test_paid_llm_is_blocked(store):
    p = store.load()
    p.llm = "claude"
    store.save(p)
    with pytest.raises(PaidAPIBlocked):
        s1_plan.analyze(store, SCENE)
    assert not (store.root / "plan").exists()


def test_scenes_lists_starred_and_imported(store, tmp_path):
    outside = tmp_path / "town.png"
    Image.new("RGB", (8, 8)).save(outside)
    assert s1_plan.import_scene(store, outside) == "scenes/town.png"
    assert s1_plan.import_scene(store, outside) == "scenes/town_1.png"
    assert s1_plan.scenes(store) == [SCENE, "scenes/town.png", "scenes/town_1.png"]
    with pytest.raises(ValueError):
        s1_plan.import_scene(store, tmp_path / "x.png", data=b"not an image")


# --- UI API ----------------------------------------------------------------------------

def _wait(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        import time
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_ui_plan_flow(store, monkeypatch):
    from asset_pipeline.ui import app as ui_app
    llm = ScriptedLLM(json.dumps(ANALYSIS))
    real = s1_plan.analyze
    monkeypatch.setattr(s1_plan, "analyze", lambda st, sc, force=False: real(st, sc, force, llm=llm))
    refined = []
    monkeypatch.setattr(s1_plan, "refine_boxes", lambda st, name: refined.append(name))
    client = TestClient(ui_app.create_app(JobQueue()))
    url = "/api/projects/demo/plans"

    listed = client.get(url).json()
    assert listed == [{"name": "batch_001__scene_000", "scene": SCENE, "plan": None}]

    job = client.post(f"{url}/analyze", json={"scene": SCENE}).json()
    assert _wait(client, job["id"])["result"] == "batch_001__scene_000"
    assert refined == ["batch_001__scene_000"]                  # analyse is followed by SAM boxes
    plan = client.get(url).json()[0]["plan"]
    assert len(plan["assets"]) == 4

    plan["assets"][0]["dimensions"]["width"] = 3.5
    plan["scene"] = "../elsewhere.png"                       # ignored: the server keeps its own
    saved = client.put(f"{url}/batch_001__scene_000", json=plan).json()
    assert saved["assets"][0]["dimensions"]["width"] == 3.5 and saved["scene"] == SCENE
    assert saved["edited"]

    plan["assets"][0]["dimensions"]["width"] = 0
    bad = client.put(f"{url}/batch_001__scene_000", json=plan)
    assert bad.status_code == 422

    assert client.post(f"{url}/analyze", json={"scene": SCENE}).status_code == 409
    plan["assets"][0]["dimensions"]["width"] = 3.5
    assert client.put(f"{url}/nope", json=plan).status_code == 404

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    r = client.post("/api/projects/demo/scenes", files={"file": ("../evil name.png", buf.getvalue(), "image/png")})
    assert r.json() == {"scene": "scenes/evil_name.png"}
    assert [e["scene"] for e in client.get(url).json()] == [SCENE, "scenes/evil_name.png"]


# --- SAM 3.1 box refinement ------------------------------------------------------------

def _det(w, h, x0, y0, x1, y1):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return segment.Detection(mask=m, bbox=segment._bbox(m), area_frac=float(m.mean()),
                             touches_border=False)


def test_refine_picks_copy_overlapping_llm_box(store, monkeypatch):
    # scene is 64x36. LLM box for "Market Stall": x 0.1-0.4, y 0.2-0.7 -> px 6-25, 7-25
    s1_plan.analyze(store, SCENE, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    near, big = _det(64, 36, 8, 8, 24, 24), _det(64, 36, 34, 2, 62, 34)
    asked = []

    def detect(client, image, noun, **kw):
        asked.append(noun)
        return {"market stall": [big, near], "barrel": [], "wall": [big]}.get(noun, [near])
    monkeypatch.setattr(segment, "detect", detect)
    name = "batch_001__scene_000"
    plan = s1_plan.refine_boxes(store, name, client=object())
    stall, stall2, barrel, wall = plan.assets
    assert asked == ["market stall", "stall", "barrel", "wall"]  # nouns, not names
    assert stall.bbox_source == "sam" and stall.bbox == [0.125, 0.2222, 0.375, 0.6667]
    assert stall.bbox_llm == [0.1, 0.2, 0.4, 0.7] and stall.sam_found == 2
    assert np.load(store.root / stall.mask)["mask"].sum() == near.mask.sum()
    assert barrel.bbox_source == "llm" and barrel.bbox == [0.0, 0.9, 0.05, 1.0]
    assert barrel.sam_found == 0 and barrel.mask is None
    assert wall.bbox_source == "sam" and wall.bbox_llm is None   # no LLM box: largest copy
    # re-running matches against the kept LLM box, not the SAM box from the first run
    monkeypatch.setattr(segment, "detect", lambda c, i, noun, **kw: [big, near])
    assert s1_plan.refine_boxes(store, name, client=object()).assets[0].bbox == stall.bbox


def test_refine_keeps_edited_flag_and_drops_stale_masks(store, monkeypatch):
    name = "batch_001__scene_000"
    plan = s1_plan.analyze(store, SCENE, llm=ScriptedLLM(json.dumps(ANALYSIS)))
    monkeypatch.setattr(segment, "detect", lambda c, i, noun, **kw: [_det(64, 36, 8, 8, 24, 24)])
    s1_plan.refine_boxes(store, name, client=object())
    plan = s1_plan.load(store, name)
    plan.assets = plan.assets[:1]
    s1_plan.save(store, name, plan, edited=True)
    out = s1_plan.refine_boxes(store, name, client=object())
    assert out.edited is not None
    assert sorted(p.stem for p in (store.root / "plan/masks" / name).glob("*.npz")) == ["market-stall"]
