import io
import json
import zipfile

import numpy as np
import pytest
from PIL import Image

from asset_pipeline import building_proc as bp
from asset_pipeline import material_lib as ml


def _fake_acg(root, aid, rgb=(120, 110, 100)):
    """A tiny ambientCG set on disk (no download): noisy albedo around rgb."""
    d = root / aid
    d.mkdir(parents=True)
    rng = np.random.default_rng(0)
    a = np.clip(np.array(rgb)[None, None] + rng.normal(0, 25, (64, 64, 3)), 0, 255).astype(np.uint8)
    for s in ml.MAPS.values():
        Image.fromarray(a).save(d / f"{aid}_{ml.RES}_{s}.jpg")
    mean = ml._srgb_to_lin(np.asarray(Image.open(d / f"{aid}_{ml.RES}_Color.jpg")).astype(float)).reshape(-1, 3).mean(0)
    (d / "meta.json").write_text(json.dumps({"acg": aid, "mean_lin": mean.tolist(), "size_px": [64, 64]}))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "root", lambda: tmp_path / "acg")
    for k in ml.LIBRARY:
        _fake_acg(tmp_path / "acg", ml.LIBRARY[k]["acg"], {"brick": (160, 90, 70), "concrete": (184, 184, 184)}.get(k, (120, 110, 100)))
    return tmp_path


def test_every_type_has_a_scan_and_megascans_names():
    assert set(ml.TYPES) < set(ml.LIBRARY)            # travertine: briefs and guidance only
    for v in ml.LIBRARY.values():
        assert v["acg"] and v["megascans"] and len(v["tile_m"]) == 2


def test_tinted_moves_the_mean_to_the_sheet_colour_and_keeps_the_detail(lib, tmp_path):
    src = np.asarray(Image.open(ml.fetch("limestone")["color"]).convert("RGB")).astype(float)
    m = ml.tinted("limestone", [205, 198, 185], tmp_path / "out" / "wall.jpg")
    out = np.asarray(Image.open(m["base"]).convert("RGB")).astype(float)
    mean = ml._lin_to_srgb(ml._srgb_to_lin(out).reshape(-1, 3).mean(0))
    assert mean == pytest.approx([205, 198, 185], abs=4)
    assert out[..., 1].std() > 0.5 * src[..., 1].std()                     # the scan's variation survives
    assert m["type"] == "limestone" and m["uv"] == "tile" and m["tile_v_m"] == ml.LIBRARY["limestone"]["tile_m"][1]
    assert m["normal"].endswith("NormalGL.jpg")                            # Blender: OpenGL normals


def test_fetch_takes_only_the_expected_maps_from_the_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "root", lambda: tmp_path / "acg")
    aid = ml.LIBRARY["brick"]["acg"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        img = io.BytesIO()
        Image.new("RGB", (8, 8), (150, 60, 40)).save(img, "JPEG")
        for s in ml.MAPS.values():
            z.writestr(f"{aid}_{ml.RES}_{s}.jpg", img.getvalue())
        z.writestr("../evil.py", "print('no')")
        z.writestr(f"{aid}.usdc", "x")

    class R:
        def read(self):
            return buf.getvalue()
    monkeypatch.setattr(ml.urllib.request, "urlopen", lambda *a, **k: R())
    m = ml.fetch("brick", log=lambda *a: None)
    files = sorted(p.name for p in (tmp_path / "acg" / aid).iterdir())
    assert files == sorted([f"{aid}_{ml.RES}_{s}.jpg" for s in ml.MAPS.values()] + ["meta.json"])
    assert not (tmp_path / "evil.py").exists()
    assert m["mean_lin"][0] > m["mean_lin"][2]


def _brief(tmp_path):
    a = np.full((200, 300, 3), 255, np.uint8)
    a[20:190, 30:270] = (200, 192, 180)
    a[20:60, 30:270] = (150, 150, 150)                                   # a darker top band
    (tmp_path / "views").mkdir()
    Image.fromarray(a).save(tmp_path / "views" / "v.png")
    meas = bp.measure(tmp_path / "views" / "v.png", [30.0, 20.0, 12.0])
    return {"kind": "building", "form": "cube", "dims": [30.0, 20.0, 12.0], "view": "views/v.png", "measured": meas}


class _Store:
    def __init__(self, root):
        self.root = root


def test_library_surfaces_use_the_band_types_and_colours(lib, tmp_path, monkeypatch):
    b = _brief(tmp_path)
    calls = []

    def classify(view, meas, out, llm=None):
        calls.append(view)
        return {"base": {"type": "concrete", "why": ""}, "wall": {"type": "limestone", "why": ""},
                "top": {"type": "render", "why": ""}}
    monkeypatch.setattr(ml, "classify", classify)
    mats = bp.materials(_Store(tmp_path), b, tmp_path / "maps", surface="library", log=lambda *a: None)
    assert [mats[n]["type"] for n in ("base", "wall", "top")] == ["concrete", "limestone", "render"]
    assert mats["trim"]["type"] == "render" and mats["roof"]["type"] == "limestone"   # copings: smooth, not stone
    assert mats["glass"]["uv"] == "plain"
    assert mats["top"]["colour"][0] < mats["wall"]["colour"][0]           # the band's own colour
    assert b["measured"]["materials"]["wall"]["type"] == "limestone"      # read once, kept in the brief
    b["material_types"] = {"wall": "brick"}                               # a brief override wins
    mats = bp.materials(_Store(tmp_path), b, tmp_path / "maps", surface="library", log=lambda *a: None)
    assert mats["wall"]["type"] == "brick" and len(calls) == 1


def test_library_is_the_default_and_sheet_patches_stay_available(tmp_path):
    b = _brief(tmp_path)
    assert bp.DEFAULT_SURFACE == "library"
    mats = bp.materials(_Store(tmp_path), b, tmp_path / "maps", surface="patch")
    assert "type" not in mats["wall"] and mats["wall"]["base"].endswith("tile_base.png")
    with pytest.raises(ValueError):
        bp.materials(_Store(tmp_path), b, tmp_path / "maps", surface="megascans")


def test_ue_preview_maps_slots_and_refuses_a_failed_material(lib, tmp_path, monkeypatch):
    from asset_pipeline import ue
    from asset_pipeline.project import ProjectStore
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    monkeypatch.setenv("AP_UE_PROJECTS", str(tmp_path / "ue"))
    store = ProjectStore.create("demo-world")
    wall = ml.tinted("limestone", [205, 198, 185], tmp_path / "m" / "wall.jpg")
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"materials": {"wall": wall, "glass": {"uv": "plain", "color": [0.1, 0.1, 0.1], "rough": 0.06}}}))
    seen = {}

    def run_script(store_, script, args, what):
        seen.update(args)
        log = ue.work_dir(store_) / "ap_material_preview.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(seen.pop("_log", ""))
        return {"ok": True, "slots": {}}
    monkeypatch.setattr(ue, "run_script", run_script)
    monkeypatch.setattr(ue, "_render", lambda *a, **k: {"ok": True, "shots": []})
    monkeypatch.setattr(ue, "editor_running", lambda up: False)
    from asset_pipeline import vlm
    monkeypatch.setattr(vlm, "down", lambda **k: "")
    ue.material_preview(store, "a1", tmp_path / "a.glb", job, tmp_path / "out")
    s = seen["slots"]["wall"]["surface"]
    assert s["type"] == "limestone" and s["acg_id"] == ml.LIBRARY["limestone"]["acg"]
    assert s["textures"]["normal_dx"].endswith("NormalDX.jpg")             # Unreal: DirectX normals
    assert s["rgb_lin"] == pytest.approx(ml._srgb_to_lin(np.array([205, 198, 185], float)).tolist(), abs=1e-4)
    assert s["megascans"] == ml.LIBRARY["limestone"]["megascans"]
    assert s["uv_scale_megascans"] == pytest.approx(ml.LIBRARY["limestone"]["tile_m"][0] / 2.0)
    assert seen["slots"]["glass"] == {"plain": {"rgb_lin": [0.1, 0.1, 0.1], "rough": 0.06, "metal": 0.0}}
    seen["_log"] = "LogMaterial: Warning: [AssetLog] /x/Content/AP/Materials/M_AP_Surface.uasset: Failed to compile Material"
    with pytest.raises(RuntimeError, match="failed to compile"):
        ue.material_preview(store, "a1", tmp_path / "a.glb", job, tmp_path / "out")


def test_contrast_scales_the_spread_towards_the_band_and_keeps_the_mean(lib, tmp_path):
    full = ml.tinted("limestone", [205, 198, 185], tmp_path / "a.jpg")
    calm = ml.tinted("limestone", [205, 198, 185], tmp_path / "b.jpg", contrast_std=0.01)
    a, b = (np.asarray(Image.open(m["base"]).convert("RGB")).astype(float) for m in (full, calm))
    assert full["contrast"] == 1.0 and 0.2 <= calm["contrast"] < 0.5
    assert ml.lum_std(b) < 0.6 * ml.lum_std(a)
    assert b.reshape(-1, 3).mean(0) == pytest.approx(a.reshape(-1, 3).mean(0), abs=4)
    assert Image.open(calm["base"]).width == ml.GLB_PX and "/1024/" in calm["normal"]       # 1K in the GLB


# --- texture scale per band --------------------------------------------------------------

def test_scaled_tile_is_a_multiplier_on_the_real_world_size():
    assert ml.scaled_tile([2.0, 2.0], 0.5) == [1.0, 1.0]
    assert ml.scaled_tile([2.6, 1.3], 2) == [5.2, 2.6]
    assert ml.scaled_tile(2.0) == [2.0, 2.0]


def test_tinted_scale_changes_the_uv_tile_and_defaults_to_one(lib, tmp_path):
    one = ml.tinted("limestone", [200, 190, 180], tmp_path / "a.jpg")
    half = ml.tinted("limestone", [200, 190, 180], tmp_path / "b.jpg", scale=0.5)
    assert (one["tile_m"], one["tile_v_m"], one["scale"]) == (2.0, 2.0, 1.0)
    assert (half["tile_m"], half["tile_v_m"]) == (1.0, 1.0)              # 2 m joints -> 1 m


def test_band_scales_default_validate_and_reach_the_materials(lib, tmp_path):
    b = _brief(tmp_path)
    assert bp.band_scales(b) == {"base": 1.0, "wall": 1.0, "top": 1.0}
    for bad in ({"wall": 0}, {"wall": -1}, {"wall": "2"}, {"roof": 2}, {"wall": True}):
        with pytest.raises(ValueError):
            bp.band_scales(b | {"material_scale": bad})
    b["measured"]["materials"] = {k: {"type": "limestone", "why": ""} for k in ("base", "wall", "top")}
    plain = bp.materials(_Store(tmp_path), b, tmp_path / "m1", surface="library", log=lambda *a: None)
    b["material_scale"] = {"wall": 0.5, "top": 2}
    sc = bp.materials(_Store(tmp_path), b, tmp_path / "m2", surface="library", log=lambda *a: None)
    assert plain["wall"]["tile_m"] == 2.0 and sc["wall"]["tile_m"] == 1.0
    assert sc["top"]["tile_m"] == 4.0 and sc["base"]["tile_m"] == 2.0     # unlisted bands stay at 1
    assert sc["roof"]["tile_m"] == 1.0 and sc["trim"]["tile_m"] == plain["trim"]["tile_m"]


def test_patch_surface_tiles_scale_too(tmp_path):
    b = _brief(tmp_path)
    plain = bp.materials(_Store(tmp_path), b, tmp_path / "m1", surface="patch")
    sc = bp.materials(_Store(tmp_path), b | {"material_scale": {"wall": 2.0}}, tmp_path / "m2", surface="patch")
    assert abs(sc["wall"]["tile_m"] - 2 * plain["wall"]["tile_m"]) < 1e-3
    assert abs(sc["roof"]["tile_m"] - 2 * plain["roof"]["tile_m"]) < 1e-3
    assert sc["base"]["tile_m"] == plain["base"]["tile_m"]


def test_a_band_named_unlike_the_sheet_takes_the_scans_own_colour_and_detail(lib, tmp_path, monkeypatch):
    """The bug: "base material brick → concrete" was tinted to the sheet's pale band colour at the
    sheet's low contrast, so the new material looked like the old one (in the GLB and in Unreal)."""
    b = _brief(tmp_path)
    monkeypatch.setattr(ml, "classify", lambda *a, **k: {n: {"type": "render", "why": ""} for n in ("base", "wall", "top")})
    sheet = bp.materials(_Store(tmp_path), b, tmp_path / "m0", surface="library", log=lambda *a: None)
    assert bp.named_bands(b) == set()
    b["material_types"] = {"base": "concrete", "wall": "render"}           # wall: what the sheet shows
    assert bp.named_bands(b) == {"base"}
    mats = bp.materials(_Store(tmp_path), b, tmp_path / "m1", surface="library", log=lambda *a: None)
    assert mats["base"]["type"] == "concrete" and mats["base"]["colour"] == ml.natural_rgb("concrete")
    assert mats["base"]["contrast"] == 1.0 and sheet["base"]["contrast"] < 1.0
    assert mats["wall"] == sheet["wall"] | {"base": mats["wall"]["base"]}   # the sheet's band as it was
    b["colors"] = {"base": [90, 80, 70]}                                    # the brief's colour for the band wins
    assert bp.materials(_Store(tmp_path), b, tmp_path / "m2", surface="library", log=lambda *a: None)["base"]["colour"] == [90, 80, 70]


def test_sheet_patch_surfaces_switch_a_named_band_to_its_library_scan(lib, tmp_path):
    b = _brief(tmp_path) | {"material_types": {"wall": "brick"}}
    mats = bp.materials(_Store(tmp_path), b, tmp_path / "maps", surface="patch")
    assert mats["wall"]["type"] == "brick" and mats["roof"]["type"] == "brick"
    assert "type" not in mats["base"] and mats["base"]["base"].endswith("tile_base.png")


def test_travertine_is_a_library_material_and_unknown_ones_are_named(lib):
    assert ml.LIBRARY["travertine"]["acg"] == "Travertine009" and ml.fetch("travertine")["acg"] == "Travertine009"
    assert ml.missing_materials("granite base, concrete walls, travertine top, terracotta tiles") == ["granite", "terracotta", "tile"]
    assert ml.missing_materials("marble and brick, timber or stucco") == []
