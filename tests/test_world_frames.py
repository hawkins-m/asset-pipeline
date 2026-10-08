"""World mode phase 2: control images in the image adapter, depth/canny workflows."""
from types import SimpleNamespace

import pytest
from PIL import Image

from asset_pipeline import config
from asset_pipeline.comfy import workflow
from asset_pipeline.imagegen.base import BackendCapabilityError, ControlImage, GenRequest, RegionalRef
from asset_pipeline.imagegen.comfyui import ComfyUIBackend
from asset_pipeline.schema import StyleAnchor


class Client:
    """Records the graph a backend submits; returns one fake image per batch item."""

    def __init__(self):
        self.graphs, self.uploads = [], []

    def upload_image(self, p):
        self.uploads.append(p.name)
        return p.name

    def run(self, graph, outputs):
        self.graphs.append(graph)
        return [SimpleNamespace(data=b"png")]


def backend(client):
    return ComfyUIBackend(client, config.backends()["comfyui"]["workflows"])


@pytest.fixture
def images(tmp_path):
    out = {}
    for name in ("depth", "canny", "anchor"):
        out[name] = tmp_path / f"{name}.png"
        Image.new("RGB", (64, 32)).save(out[name])
    return out


def by_type(graph, cls):
    return {k: v for k, v in graph.items() if v["class_type"] == cls}


def test_union_with_depth_and_canny_chains_both_after_redux(tmp_path, images):
    c = Client()
    req = GenRequest(prompt="a forum", width=64, height=32, seed=3,
                     anchor=StyleAnchor(images=[images["anchor"]], strength=0.06, style_text="pale stone"),
                     control=[ControlImage(kind="depth", image=images["depth"], strength=0.7, end=0.5),
                              ControlImage(kind="canny", image=images["canny"], strength=0.3, start=0.1, end=0.4)])
    res = backend(c).generate(req, tmp_path / "out")
    g = c.graphs[0]
    cns = by_type(g, "ControlNetApplyAdvanced")
    assert len(cns) == 2 and g["32"]["inputs"]["strength"] == 0.7 and g["32"]["inputs"]["end_percent"] == 0.5
    assert g["34"]["inputs"]["start_percent"] == 0.1
    assert g["31"]["inputs"]["image"] == "depth.png" and g["33"]["inputs"]["image"] == "canny.png"
    # Redux clone feeds the depth control; canny follows depth; the sampler reads canny's outputs
    assert g["32"]["inputs"]["positive"] == ["24_0", 0] and g["34"]["inputs"]["positive"] == ["32", 0]
    assert g["3"]["inputs"]["positive"] == ["34", 0] and g["3"]["inputs"]["negative"] == ["34", 1]
    assert g["5"]["inputs"]["width"] == 64 and "pale stone" in g["6"]["inputs"]["text"]
    assert res[0].meta["control"][0]["kind"] == "depth"


def test_union_with_depth_only_removes_the_canny_block(tmp_path, images):
    c = Client()
    req = GenRequest(prompt="x", control=[ControlImage(kind="depth", image=images["depth"])])
    backend(c).generate(req, tmp_path / "out")
    g = c.graphs[0]
    assert "33" not in g and "34" not in g
    assert g["3"]["inputs"]["positive"] == ["32", 0] and g["3"]["inputs"]["negative"] == ["32", 1]
    assert g["32"]["inputs"]["positive"] == ["11", 0]          # no anchor: straight from guidance
    workflow.validate(g, {"outputs": ["9"], "bindings": {}})


def test_regional_refs_chain_masked_redux_after_the_anchor(tmp_path, images):
    c = Client()
    mask = tmp_path / "mask.png"
    Image.new("L", (64, 32)).save(mask)
    req = GenRequest(prompt="x", anchor=StyleAnchor(images=[images["anchor"]], strength=0.06),
                     control=[ControlImage(kind="depth", image=images["depth"])],
                     regional=[RegionalRef(image=images["anchor"], mask=mask, strength=0.2),
                               RegionalRef(image=images["canny"], mask=mask, strength=0.3)])
    backend(c).generate(req, tmp_path / "out")
    g = c.graphs[0]
    workflow.validate(g, {"outputs": ["9"], "bindings": {}})
    # each material: base (text + anchor) + its image, masked, combined onto the chain
    assert g["42_0"]["inputs"]["conditioning"] == ["24_0", 0] and g["42_1"]["inputs"]["conditioning"] == ["24_0", 0]
    assert g["42_1"]["inputs"]["strength"] == 0.3 and g["43_0"]["inputs"]["image"] == "mask.png"
    assert g["45_0"]["inputs"]["conditioning_1"] == ["24_0", 0] and g["45_1"]["inputs"]["conditioning_1"] == ["45_0", 0]
    assert g["32"]["inputs"]["positive"] == ["45_1", 0]
    # none: the block is bypassed
    backend(c).generate(GenRequest(prompt="x", control=[ControlImage(kind="depth", image=images["depth"])]),
                        tmp_path / "out")
    g = c.graphs[1]
    assert not by_type(g, "ConditioningSetMask") and g["32"]["inputs"]["positive"] == ["11", 0]
    with pytest.raises(BackendCapabilityError):
        backend(c).generate(req.model_copy(update={"control_model": "depth_lora"}), tmp_path / "out")


def test_depth_lora_takes_its_size_from_the_control_image(tmp_path, images):
    c = Client()
    req = GenRequest(prompt="x", n=3, width=999, height=999, control_model="depth_lora",
                     control=[ControlImage(kind="depth", image=images["depth"], strength=0.8)])
    backend(c).generate(req, tmp_path / "out")
    g = c.graphs[0]
    assert not by_type(g, "EmptySD3LatentImage") and g["42"]["inputs"]["amount"] == 3
    assert g["40"]["inputs"]["strength_model"] == 0.8 and g["3"]["inputs"]["latent_image"] == ["42", 0]
    with pytest.raises(BackendCapabilityError):
        backend(Client()).generate(GenRequest(prompt="x", control_model="depth_lora", control=[
            ControlImage(kind="depth", image=images["depth"]), ControlImage(kind="canny", image=images["canny"])]),
            tmp_path / "out")


def test_two_controls_of_one_kind_and_missing_images_are_refused(tmp_path, images):
    with pytest.raises(BackendCapabilityError):
        backend(Client()).generate(GenRequest(prompt="x", control=[
            ControlImage(kind="depth", image=images["depth"]), ControlImage(kind="depth", image=images["depth"])]),
            tmp_path / "out")
    with pytest.raises(FileNotFoundError):
        backend(Client()).generate(GenRequest(prompt="x", control=[
            ControlImage(kind="depth", image=tmp_path / "nope.png")]), tmp_path / "out")


# --- s0_frames: prompts, generation, edge match (no Blender, fake backend) -----------------

import json  # noqa: E402

import numpy as np  # noqa: E402

from asset_pipeline import review  # noqa: E402
from asset_pipeline.imagegen.base import GenResult  # noqa: E402
from asset_pipeline.project import ProjectStore, write_json  # noqa: E402
from asset_pipeline.schema import District, FrameSettings, Material, ShotSpec, SiteLayout  # noqa: E402
from asset_pipeline.stages import s0_frames, s0_style, sw_shots, sw_site  # noqa: E402

EYE = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


class FakeBackend:
    name = "fake"

    def __init__(self, canny=None):
        self.requests, self.canny = [], canny

    def generate(self, req, out_dir, prefix="img"):
        self.requests.append(req)
        p = out_dir / f"{prefix}_000.png"
        # a frame that draws exactly the greybox's edges (dark lines on light) if given
        img = Image.open(self.canny).convert("L").point(lambda v: 255 - v) if self.canny else \
            Image.new("L", (req.width, req.height), 200)
        img.save(p)
        return [GenResult(path=p, seed=req.seed, backend=self.name)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("w")
    layout = SiteLayout(districts=[District(id="core", notes="white marble, bronze"),
                                   District(id="edge", notes="brick and concrete")],
                        shots=[ShotSpec(id="a", pos=(0, 0, 0), look_at=(0, 1, 0))])
    sw_site.save_layout(store, layout)
    slot = lambda id, type, d: {"id": id, "type": type, "category": "building", "kit": None, "district": d,  # noqa: E731
                                "matrix": EYE, "bbox": [[0, 0, 0], [1, 1, 1]], "pieces": []}
    shot = {"id": "a", "tier": "medium", "matrix": EYE, "lens_mm": 35, "sensor_mm": 36, "resolution": [64, 32],
            "district": "edge", "notes": "looking down the avenue"}
    write_json(store.root / "site/greybox.json", {
        "slots": [slot("rot", "rotunda", "core"), slot("v1", "villa", "edge"), slot("t", "terrain", None)],
        "shots": [shot], "untagged": [], "fixed": [], "blend_sha256": "sha1"})
    d = sw_shots.shot_dir(store, "a")
    d.mkdir(parents=True)
    write_json(d / "meta.json", {"shot": shot, "greybox_sha256": "sha1"})
    write_json(d / "ids.json", {"shot": "a", "size": [64, 32], "sky_frac": 0.1, "slots": {
        "rot": {"frac": 0.5}, "v1": {"frac": 0.2}, "t": {"frac": 0.2}}})
    Image.new("L", (64, 32), 128).save(d / "depth.png")
    canny = np.zeros((32, 64), np.uint8)
    canny[16, 8:56] = 255
    canny[4:28, 32] = 255
    Image.fromarray(canny).save(d / "canny.png")
    return store


def test_prompt_names_what_the_camera_sees_and_the_focus_district_first(world):
    p = s0_frames.prompt_for(world, "a")
    assert p.startswith("looking down the avenue, showing a domed rotunda")       # the note replaces the tier
    assert p.index("domed rotunda") < p.index("courtyard villas") and "the landscape" not in p
    assert p.index("brick and concrete") < p.index("white marble")   # shot's district first


def test_generate_uses_the_shot_passes_settings_and_refs(world, monkeypatch):
    fake = FakeBackend(canny=sw_shots.shot_dir(world, "a") / "canny.png")
    monkeypatch.setattr(s0_frames, "backend_for", lambda stage, project: fake)
    s0_style.set_style_text(world, "pale stone, soft light")
    assert s0_frames.settings(world).canny_strength == 0                 # default: depth alone
    out = s0_frames.generate(world, "a", n=2, seed=5, fs=FrameSettings(depth_strength=0.7, canny_strength=0.3))
    meta = json.loads((out / "meta.json").read_text())
    assert [f["file"] for f in meta["frames"]] == ["frame_000.png", "frame_001.png"]
    r = fake.requests[0]
    assert (r.width, r.height, r.seed, fake.requests[1].seed) == (64, 32, 5, 6)
    assert [c.kind for c in r.control] == ["depth", "canny"] and r.control[0].strength == 0.7
    assert r.anchor.style_text == "pale stone, soft light"
    assert meta["frames"][0]["edge_match"] == 1.0              # the fake draws the greybox's edges
    # an approved frame as a reference for another batch: added to the Redux images
    key = review.set_star(world, out / "frame_000.png")
    assert s0_frames.approved(world, "a") == [key]
    s0_frames.generate(world, "a", n=1, refs=[key], ref_strength=0.1, fs=FrameSettings(model="depth_lora"))
    r = fake.requests[-1]
    assert r.anchor.images[-1].name == "frame_000.png" and r.anchor.strength == pytest.approx(0.16)
    assert [c.kind for c in r.control] == ["depth"] and r.control_model == "depth_lora"
    assert len(s0_frames.batches(world, "a")) == 2


def _with_materials(store, ref=None):
    """Two materials, a coloured id pass (rotunda left half, villa a corner) and a ref image."""
    layout = sw_site.load_layout(store)
    layout.materials = [Material(id="marble", words="polished white marble, fine grey veining",
                                 types=["rotunda", "temple"], ref=ref, ref_strength=0.25),
                        Material(id="render", words="smooth white lime render", types=["villa"])]
    sw_site.save_layout(store, layout)
    d = sw_shots.shot_dir(store, "a")
    ids = json.loads((d / "ids.json").read_text())
    colors = {"rot": [10, 20, 30], "v1": [40, 50, 60], "t": [70, 80, 90]}
    for k, c in colors.items():
        ids["slots"][k]["color"] = c
    write_json(d / "ids.json", ids)
    rgb = np.zeros((32, 64, 3), np.uint8)
    rgb[:, :32] = colors["rot"]
    rgb[:8, 48:] = colors["v1"]
    rgb[24:, 32:] = colors["t"]
    Image.fromarray(rgb).save(d / "ids.png")


def test_materials_replace_district_notes_and_their_refs_are_masked(world, monkeypatch):
    (world.root / "moodboard").mkdir()
    Image.new("RGB", (8, 8), "white").save(world.root / "moodboard/marble.png")
    _with_materials(world, ref="moodboard/marble.png")
    p = s0_frames.prompt_for(world, "a")
    assert "polished white marble" in p and "lime render" in p
    assert "brick and concrete" in p and "white marble, bronze" not in p   # one identity line: the shot's district
    assert p.index("marble") < p.index("lime render")            # by coverage
    m = s0_frames.material_mask(world, "a", sw_site.load_layout(world).materials[0])
    assert m[:, :32].all() and not m[:, 32:].any()               # exactly the rotunda's pixels
    fake = FakeBackend()
    monkeypatch.setattr(s0_frames, "backend_for", lambda stage, project: fake)
    out = s0_frames.generate(world, "a", n=1)
    (r,) = fake.requests[0].regional                              # only materials with a ref
    assert r.strength == 0.25 and r.image.name == "marble.png" and r.mask == out / "mask_marble.png"
    assert json.loads((out / "meta.json").read_text())["material_refs"][0]["image"] == "moodboard/marble.png"
    s0_frames.generate(world, "a", n=1, material_refs=False)       # wording only
    assert fake.requests[-1].regional == [] and "polished white marble" in fake.requests[-1].prompt


def test_tight_shots_are_mood_only(world):
    gb = json.loads((world.root / "site/greybox.json").read_text())
    gb["shots"].append(gb["shots"][0] | {"id": "close", "tier": "tight"})
    write_json(world.root / "site/greybox.json", gb)
    for shot in ("a", "close"):
        f = world.root / f"frames/{shot}/batch_001/frame_000.png"
        f.parent.mkdir(parents=True)
        Image.new("L", (4, 4)).save(f)
        review.set_star(world, f)
    old = world.root / "frames/_old_layout/a/batch_001/frame_000.png"   # an archived layout's frame
    old.parent.mkdir(parents=True)
    Image.new("L", (4, 4)).save(old)
    review.set_star(world, old)
    assert len(s0_frames.approved(world)) == 3
    assert s0_frames.asset_sources(world) == ["frames/a/batch_001/frame_000.png"]


def test_stale_passes_are_refused(world):
    gb = json.loads((world.root / "site/greybox.json").read_text())
    gb["blend_sha256"] = "changed"
    write_json(world.root / "site/greybox.json", gb)
    with pytest.raises(ValueError, match="stale"):
        s0_frames.generate(world, "a", n=1)


def test_edge_match_drops_when_the_layout_is_lost(world, tmp_path):
    flat = tmp_path / "flat.png"
    Image.new("L", (64, 32), 200).save(flat)
    canny = sw_shots.shot_dir(world, "a") / "canny.png"
    big = np.zeros((360, 640), np.uint8)                      # frame-sized: a few long edges
    big[180, 40:600] = big[30:330, 320] = big[60:300, 100] = 255
    big_canny = tmp_path / "big_canny.png"
    Image.fromarray(big).save(big_canny)
    noise = tmp_path / "noise.png"
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (360, 640), dtype=np.uint8)).save(noise)
    assert s0_frames.edge_match(noise, big_canny) < 0.2       # busy, but not the layout
    assert s0_frames.edge_match(flat, canny) == 0.0
    shifted = tmp_path / "shifted.png"                        # the layout 12 px off
    Image.open(canny).convert("L").point(lambda v: 255 - v).transform(
        (64, 32), Image.AFFINE, (1, 0, 12, 0, 1, 0), fillcolor=255).save(shifted)
    assert s0_frames.edge_match(shifted, canny) < 0.6


def test_moodboard_import_and_style_draft(world, tmp_path):
    src = tmp_path / "export"
    src.mkdir()
    for i in range(12):
        Image.new("RGB", (40 + i, 30), (i * 20, 100, 50)).save(src / f"ref {i}.jpg")
    (src / "notes.txt").write_text("ignored")
    keys = s0_style.import_moodboard(world, "Real Examples", [src])
    assert len(keys) == 12 and keys[0].startswith("style/moodboard/real-examples/ref_")
    s0_style.import_moodboard(world, "paintings", [src / "ref 0.jpg"])
    with pytest.raises(ValueError, match="PureRef"):
        s0_style.import_moodboard(world, "x", [tmp_path / "board.pur"])

    class LLM:
        name = "scripted"
        calls = []

        def json_text(self, system, prompt, images, schema):
            self.calls.append(images)
            return json.dumps({"style_text": "honed stone, bronze, deep shade", "palette": "cream", "avoid": "kitsch"})
    llm = LLM()
    d = s0_style.draft_style_text(world, llm=llm)
    assert d.style_text == "honed stone, bronze, deep shade"
    assert len(llm.calls[0]) == 1                                  # one contact sheet, not 9 images
    assert Image.open(llm.calls[0][0]).size == (1536, 1536)        # 9 images in a 3x3 grid
    assert world.load().anchor is None                             # a draft isn't saved
    s0_style.set_style_text(world, d.style_text)
    assert world.load().anchor.style_text == d.style_text and world.load().anchor.images == []


def test_prompt_append_and_override_are_the_users_and_reset_to_auto(world):
    auto = s0_frames.prompt_for(world, "a")
    layout = sw_site.load_layout(world)
    layout.shots[0].prompt_append = "golden hour, long shadows"
    sw_site.save_layout(world, layout)
    pp = s0_frames.prompt_parts(world, "a")
    assert pp["auto"] == auto and pp["prompt"] == f"{auto}, golden hour, long shadows" and pp["source"] == "append"
    layout.shots[0].prompt_override = "a quiet street at dawn"
    sw_site.save_layout(world, layout)
    assert s0_frames.prompt_parts(world, "a")["prompt"] == "a quiet street at dawn"
    layout.shots[0].prompt_override, layout.shots[0].prompt_append = None, ""
    sw_site.save_layout(world, layout)
    assert s0_frames.prompt_parts(world, "a") | {} == {"auto": auto, "append": "", "override": None,
                                                       "prompt": auto, "source": "auto"}


def test_landmark_reference_is_masked_to_its_ensemble_and_named_first(world, monkeypatch):
    from asset_pipeline.schema import CitySpec, Typology
    (world.root / "moodboard").mkdir()
    Image.new("RGB", (8, 8), "orange").save(world.root / "moodboard/landmark.jpg")
    _with_materials(world)
    layout = sw_site.load_layout(world)
    layout.materials.append(Material(id="warm", words="rose granite"))
    layout.city = CitySpec(typologies=[Typology(id="rotunda", prompt="an open arched rotunda", material="warm",
                                                ref="moodboard/landmark.jpg", ref_strength=0.12)])
    sw_site.save_layout(world, layout)
    gb = json.loads((world.root / "site/greybox.json").read_text())
    gb["slots"][0] |= {"typology": "rotunda", "material": "warm"}
    write_json(world.root / "site/greybox.json", gb)
    p = s0_frames.prompt_for(world, "a")
    assert "showing an open arched rotunda" in p and p.index("rose granite") < p.index("lime render")
    fake = FakeBackend()
    monkeypatch.setattr(s0_frames, "backend_for", lambda stage, project: fake)
    out = s0_frames.generate(world, "a", n=1)
    (r,) = fake.requests[0].regional
    assert r.image.name == "landmark.jpg" and r.strength == 0.12
    m = np.array(Image.open(r.mask)) > 0
    assert m[:, :32].all() and not m[:, 32:].any()               # the ensemble's pixels only
    assert json.loads((out / "meta.json").read_text())["prompt_source"] == "auto"


def test_a_camera_edit_stales_only_that_shot():
    shot = lambda i, m: {"id": i, "matrix": m, "lens_mm": 35, "sensor_mm": 36, "resolution": [64, 32]}  # noqa: E731
    gb = {"blend_sha256": "new", "geometry_sha256": "g1", "shots": [shot("a", EYE), shot("b", EYE)]}
    meta = lambda i: {"shot": shot(i, EYE), "greybox_sha256": "old", "geometry_sha256": "g1"}  # noqa: E731
    moved = [r[:] for r in EYE]
    moved[0][3] = 5.0
    gb["shots"][1] = shot("b", moved)
    assert not sw_shots.is_stale(meta("a"), gb, "a") and sw_shots.is_stale(meta("b"), gb, "b")
    assert sw_shots.is_stale(meta("a"), gb | {"geometry_sha256": "g2"}, "a")       # geometry changed
    assert sw_shots.is_stale({"shot": shot("a", EYE), "greybox_sha256": "old"}, gb, "a")   # legacy meta
