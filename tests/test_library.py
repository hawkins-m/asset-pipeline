"""World mode phase 3 step 1: the asset library plan from the locked shot frames."""
import json

import numpy as np
import pytest
from PIL import Image

from asset_pipeline import review
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.schema import CitySpec, CivicNode, Material, ShotSpec, SiteLayout, Typology
from asset_pipeline.stages import s0_frames, sw_library, sw_shots, sw_site

EYE = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
W, H = 64, 32


def _slot(id, type, district, typology=None, material=None, pieces=(), category="building"):
    return {"id": id, "type": type, "category": category, "kit": None, "district": district, "material": material,
            "typology": typology, "matrix": EYE, "bbox": [[0, 0, 0], [1, 1, 1]],
            "pieces": [{"name": f"{id}.{p}", "piece": p, "mesh": p, "matrix_local": EYE} for p in pieces]}


def _shot_passes(store, shot, tier, regions):
    """ids.png / ids.json for a shot: regions = {slot id: (x0, x1)} pixel columns."""
    gb = json.loads((store.root / "site/greybox.json").read_text())
    colors = sw_shots.colors_for([s["id"] for s in gb["slots"]])
    rgb = np.zeros((H, W, 3), np.uint8)
    out = {}
    for sid, (x0, x1) in regions.items():
        rgb[:, x0:x1] = colors[sid]
        out[sid] = {"color": colors[sid], "pixels": (x1 - x0) * H, "frac": (x1 - x0) / W,
                    "bbox": [x0 / W, 0.0, x1 / W, 1.0]}
    d = sw_shots.shot_dir(store, shot)
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(d / "ids.png")
    write_json(d / "ids.json", {"shot": shot, "size": [W, H], "slots": out})
    write_json(d / "meta.json", {"shot": {"id": shot, "tier": tier}, "greybox_sha256": "g", "geometry_sha256": "g"})


def _frames(store, shot, n=2, geom="g"):
    b = store.root / "frames" / shot / "batch_001"
    b.mkdir(parents=True)
    for i in range(n):
        Image.new("RGB", (W, H), (200, 180, 150)).save(b / f"frame_{i:03d}.png")
    write_json(b / "meta.json", {"shot": shot, "geometry_sha256": geom, "greybox_sha256": geom,
                                 "frames": [{"file": f"frame_{i:03d}.png", "seed": i, "edge_match": 0.5}
                                            for i in range(n)]})
    return [f"frames/{shot}/batch_001/frame_{i:03d}.png" for i in range(n)]


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    # every frame matches its greybox as well now as when it was generated (0.5): on the current layout
    monkeypatch.setattr(s0_frames, "edge_match", lambda frame, canny: 0.5)
    store = ProjectStore.create("l")
    cat = [Typology(id="rotunda", family="civic", form="drum", max_count=1, ref="moodboard/r.jpg", desc="A rotunda."),
           Typology(id="insula", family="housing", form="perimeter", width=(20, 40), depth=(20, 40), desc="Blocks."),
           Typology(id="hall", family="mixed", form="hall", width=(20, 30), depth=(20, 30))]
    layout = SiteLayout(city=CitySpec(nodes=[CivicNode(id="core", center=(0, 0), radius=100)], typologies=cat),
                        materials=[Material(id="marble", words="white marble"), Material(id="brick", words="red brick"),
                                   Material(id="stone", words="pale stone", types=["stoa"])],
                        shots=[ShotSpec(id=s, pos=(0, 0, 0), look_at=(0, 1, 0), tier=t)
                               for s, t in (("m", "medium"), ("t", "tight"), ("hi", "medium"))])
    sw_site.save_layout(store, layout)
    slots = [_slot("rot", "rotunda", "core", "rotunda", "marble"),
             _slot("col", "colonnade", "core", "rotunda", "marble", category="structure"),
             _slot("ins1", "insula", "core", "insula", "marble"), _slot("ins2", "insula", "edge", "insula", "brick"),
             _slot("ins3", "insula", "edge", "insula", "marble"),
             _slot("stoa1", "stoa", "core", category="structure"),
             _slot("hall1", "hall", "edge", "hall", "brick"),
             _slot("mk", "market", "core", pieces=["market:hut", "market:hut-roof", "market:hut", "market:hut-roof",
                                                   "market:tent", "paving"], category="structure"),
             _slot("park1", "park", "edge", pieces=["tree:pine-trunk", "tree:pine-crown", "tree:cypress"],
                   category="terrain"),
             _slot("ter", "terrain", None, category="terrain")]
    write_json(store.root / "site/greybox.json", {"slots": slots, "shots": [
        {"id": "m", "tier": "medium"}, {"id": "t", "tier": "tight"}, {"id": "hi", "tier": "medium"}],
        "untagged": [], "fixed": [], "blend_sha256": "g", "geometry_sha256": "g"})
    _shot_passes(store, "m", "medium", {"rot": (0, 20), "ins1": (20, 30), "ins2": (30, 34), "mk": (34, 50),
                                         "park1": (50, 56), "ter": (56, 64)})
    _shot_passes(store, "t", "tight", {"rot": (0, 60), "stoa1": (60, 64)})
    _shot_passes(store, "hi", "medium", {"hall1": (0, 64)})
    m = _frames(store, "m", 3)
    t = _frames(store, "t", 1)
    _frames(store, "hi", 1)
    s0_frames.lock(store, "m", m[0])
    review.set_role(store, m[2], "source")          # a second shot frame of the same batch
    s0_frames.lock(store, "t", t[0])
    s0_frames.lock(store, "hi", s0_frames.LOCK_GREYBOX)
    return store, m


def test_library_entries_tiers_sources_and_grouping(lib):
    store, m = lib
    data = sw_library.plan(store)
    by = {e["id"]: e for e in data["entries"]}
    # sources: the locked frame and the other shot frame; mood (tight) and greybox locks never seed
    assert data["sources"] == {"m": [m[0], m[2]]} and data["mood"] == {"t": "frames/t/batch_001/frame_000.png"}
    assert by["rotunda"]["tier"] == "A" and by["rotunda"]["best"] == m[0] and abs(by["rotunda"]["share"] - 20 / 64) < 1e-4
    assert by["rotunda"]["mood"][0]["shot"] == "t"
    # fill typologies split by material; the edge one is grouped under its own district
    assert by["insula-marble"]["tier"] == "C" and by["insula-marble"]["instances"] == 2
    assert by["insula-brick"]["district"] == "edge" and by["insula-marble"]["district"] == "core"
    # props counted once per prop (not per piece), trees by species; terrain is a surface
    assert by["market-hut"]["instances"] == 2 and by["market-tent"]["instances"] == 1 and by["market-hut"]["tier"] == "D"
    assert by["pine-tree"]["instances"] == 1 and by["cypress-tree"]["instances"] == 1
    assert "terrain" in data["surfaces"] and not [e for e in data["entries"] if e["typology"] == "terrain"]
    # only seen in the mood shot, or in a greybox-locked shot: no source, listed as unseen
    unseen = {u["id"]: u for u in data["unseen"]}
    assert unseen["stoa"]["mood"] and "hall" in unseen and unseen["hall"]["tier"] == "B"
    assert "rotunda-colonnade" in unseen                         # in the greybox, on no frame
    assert "Material: white marble" in by["insula-marble"]["description_auto"]
    # outputs: json, a crop per entry, the grouped page
    assert sw_library.load(store)["entries"][0]["id"] == data["entries"][0]["id"]
    crops = store.root / "site/library/crops"
    assert {p.stem for p in crops.glob("*.jpg")} == set(by)
    page = (store.root / "site/library.html").read_text()
    assert page.index("<h2>core") < page.index("<h2>edge") and "insula-brick" in page
    assert "core:" in sw_library.summary(data)


def test_crop_brightens_only_the_entry_pixels(lib):
    store, m = lib
    sw_library.plan(store)
    im = np.asarray(Image.open(store.root / "site/library/crops/rotunda.jpg").convert("L"), float)
    w = im.shape[1]
    # the crop spans the rotunda (cols 0-20) plus a margin of its neighbour, which is dimmed
    assert im[:, : w // 2].mean() > 1.8 * im[:, -2:].mean()


def test_old_layout_frames_are_excluded_and_reported(lib, monkeypatch):
    store, m = lib
    # the second shot frame was drawn on an older layout: it matches the current greybox far worse
    monkeypatch.setattr(s0_frames, "edge_match", lambda frame, canny: 0.3 if str(frame).endswith(m[2]) else 0.5)
    data = sw_library.plan(store)
    assert data["sources"] == {"m": [m[0]]}
    bad = [c for c in data["candidates"] if not c["ok"]]
    assert [c["frame"] for c in bad] == [m[2]] and bad[0]["reason"].startswith("old layout")
    # and one that never followed its greybox at all
    monkeypatch.setattr(s0_frames, "edge_match", lambda frame, canny: 0.1)
    meta_p = store.root / "frames/m/batch_001/meta.json"
    meta = json.loads(meta_p.read_text())
    for f in meta["frames"]:
        f["edge_match"] = 0.1
    write_json(meta_p, meta)
    data = sw_library.plan(store)
    assert data["sources"] == {} and all("doesn't follow" in c["reason"] for c in data["candidates"])


def test_check_flags_trees_sky_and_something_else(lib):
    store, m = lib
    # fake SAM masks: trees over the rotunda's right half, sky over insula ins2, buildings elsewhere
    tree = np.zeros((H, W), bool); tree[:, 10:20] = True
    sky = np.zeros((H, W), bool); sky[:, 30:34] = True
    bld = np.zeros((H, W), bool); bld[:, 0:10] = True; bld[:, 20:30] = True
    masks = {"tree": tree, "sky": sky, "building": bld}

    class Det:
        def __init__(self, mask):
            self.mask = mask

    import asset_pipeline.segment as seg
    orig = seg.detect
    seg.detect = lambda client, image, noun, **kw: [Det(masks[noun])] if noun in masks else []
    try:
        data = sw_library.check(store, client=object())
    finally:
        seg.detect = orig
    by = {e["id"]: e for e in data["entries"]}
    assert data["checked"]
    assert by["rotunda"]["scores"] == {"asset": 0.5, "trees": 0.5, "sky": 0.0}
    assert any(f.startswith("trees cover 50%") for f in by["rotunda"]["flags"])
    assert any(f.startswith("sky covers 100%") for f in by["insula-brick"]["flags"])
    assert by["insula-marble"]["flags"] == []                      # fully drawn as a building, 16% of the frame
    # an entry the frame fills with something else entirely keeps its crop (flagged), it isn't dropped
    assert by["market-hut"]["best"] and by["market-hut"]["scores"]["asset"] == 0.0
    assert any("approximate" in f for f in by["market-hut"]["flags"])
    # the masks are cached: a second check runs no SAM at all
    seg.detect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("SAM ran again"))
    try:
        sw_library.check(store, client=object())
    finally:
        seg.detect = orig


def test_decisions_survive_a_replan_and_reach_the_ui(lib):
    from fastapi.testclient import TestClient
    from asset_pipeline.jobs import JobQueue
    from asset_pipeline.ui import app as ui_app
    store, m = lib
    sw_library.plan(store)
    c = TestClient(ui_app.create_app(JobQueue()))
    r = c.post("/api/projects/l/library/decision", json={"entry": "rotunda", "decision": "source"})
    assert r.status_code == 200 and r.json()["rotunda"]["decision"] == "source"
    c.post("/api/projects/l/library/decision", json={"entry": "stoa", "decision": "description"})
    assert c.post("/api/projects/l/library/decision", json={"entry": "nope", "decision": "source"}).status_code == 404
    assert c.post("/api/projects/l/library/decision", json={"entry": "stoa", "decision": "maybe"}).status_code == 422
    sw_library.plan(store)
    got = c.get("/api/projects/l/library").json()
    assert got["decisions"]["rotunda"]["decision"] == "source" and got["plan"]["entries"]
    c.post("/api/projects/l/library/decision", json={"entry": "rotunda", "decision": None})
    assert "rotunda" not in c.get("/api/projects/l/library").json()["decisions"]


def test_reference_decisions_and_the_library_asset_plan(lib):
    from asset_pipeline.stages import s1_plan, s2_refs
    store, m = lib
    sw_library.plan(store)
    old = "frames/t/batch_001/frame_000.png"          # any frame: a mood shot's, an old layout's
    with pytest.raises(ValueError, match="needs a frame"):
        sw_library.decide(store, "stoa", "reference")
    with pytest.raises(ValueError, match="no such image"):
        sw_library.decide(store, "stoa", "reference", ref_frame="frames/nope.png")
    sw_library.decide(store, "stoa", "reference", ref_frame=old)              # unseen entry, old frame
    sw_library.decide(store, "rotunda", "source")
    sw_library.decide(store, "insula-brick", "description")
    sw_library.decide(store, "insula-marble", "exclude")
    sw_library.decide(store, "pine-tree", "source")                          # foliage: never generated
    with pytest.raises(ValueError, match="not decided"):
        sw_library.asset_plan(store, only=["rotunda", "hall"])
    plan = sw_library.asset_plan(store, only=["rotunda", "stoa"])
    by = {a.id: a for a in plan.assets}
    assert set(by) == {"rotunda", "stoa", "insula-brick"}                    # undecided stay massing
    assert by["rotunda"].include and by["stoa"].include and not by["insula-brick"].include
    assert by["rotunda"].refs == ["site/library/refs/rotunda.jpg"] and by["rotunda"].ref_strength == 0.06
    assert by["stoa"].refs == [old] and by["stoa"].ref_strength == 0.04 and by["insula-brick"].refs == []
    assert "colonnaded hall" in by["stoa"].description                       # generic kit description
    assert (store.root / "site/library/refs/rotunda.jpg").is_file()
    # stages 2-6 see it as one more plan; its sheets carry the look refs in the Redux anchor
    assert "library" in s1_plan.plan_names(store)
    u = next(u for u in s2_refs.units(store, "library") if u.key == "stoa")
    prompt, w, h, anchor = s2_refs.request(u, None)
    assert "stoa" in prompt and [str(p) for p in anchor.images] == [old] and anchor.strength == 0.04


def test_refined_crops_flag_no_usable_crop_and_default_to_description(lib, monkeypatch):
    from asset_pipeline import crop_refine
    store, m = lib
    seg = store.root / "site/library/seg"
    seg.mkdir(parents=True)
    (seg / "ade20k.json").write_text(json.dumps({0: "building", 1: "sky", 2: "tree"}))
    # the frame paints the rotunda (cols 0-20) as building, insula ins1 (20-30) mostly as tree
    lab = np.ones((H, W), np.uint8)
    lab[:, 0:20] = 0
    lab[:, 20:22] = 0
    lab[:, 22:30] = 2
    for f in m:
        Image.fromarray(lab).save(seg / f"{crop_refine._slug(f)}.oneformer.png")
        Image.fromarray(np.full((H, W), 30000, np.uint16)).save(seg / f"{crop_refine._slug(f)}.depth.png")
    Image.fromarray(np.full((H, W), 300, np.uint16)).save(sw_shots.shot_dir(store, "m") / "depth_raw.png")
    monkeypatch.setattr(crop_refine, "available", lambda: True)
    monkeypatch.setattr(crop_refine, "dump", lambda *a, **k: None)
    import asset_pipeline.segment as seg_mod
    monkeypatch.setattr(seg_mod, "detect", lambda client, image, noun, **kw: [])
    monkeypatch.setattr(seg_mod, "detect_box", lambda client, image, noun, box, **kw: [])
    sw_library.plan(store)
    sw_library.describe(store, "insula-marble", "my words")          # a user description, no decision
    data = sw_library.check(store, client=object())
    by = {e["id"]: e for e in data["entries"]}
    assert by["rotunda"]["kept"] == 1.0 and not any("no usable" in f for f in by["rotunda"]["flags"])
    assert by["insula-marble"]["kept"] == 0.2
    assert any(f.startswith("no usable crop") for f in by["insula-marble"]["flags"])
    dec = sw_library.decisions(store)
    assert dec["insula-marble"] == {"description": "my words", "decision": "description", "auto": True,
                                    "note": "no usable crop"}
    assert "rotunda" not in dec
    # a user decision is never overridden; an auto one goes when the crop becomes usable
    sw_library.decide(store, "insula-marble", "exclude")
    sw_library.plan(store)
    assert sw_library.decisions(store)["insula-marble"]["decision"] == "exclude"
