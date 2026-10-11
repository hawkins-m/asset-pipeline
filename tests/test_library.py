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
