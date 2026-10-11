"""Procedural kit pieces (kit_proc + scripts/blender_kit.py) and three-quarter sheets."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from asset_pipeline import kit_proc
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.schema import Dimensions, PlanAsset
from asset_pipeline.stages import s2_refs

REAL_BLENDER = Path("/snap/bin/blender")

GATE = {"w": 20.0, "d": 13.0, "plinth_h": 0.6, "body_h": 11.6, "span": 6.5, "spring": 8.0,
        "pilasters": [[-9.0, 1.2], [9.0, 1.2]], "pilaster_proj": 0.25, "band_h": 0.65,
        "cornice": [[0.6, 0.25], [0.7, 0.45]], "attic_h": 3.2, "attic_inset": 0.4, "archivolt_w": 0.6,
        "keystone": [0.8, 1.1]}
BAY = {"w": 4.0, "d": 8.0, "stylobate_h": 0.3,
       "column": {"d": 0.65, "h": 5.6, "plinth_h": 0.7, "base_h": 0.3, "cap_h": 0.35, "abacus_h": 0.2},
       "col_y": -3.4, "ent_h": 0.65, "ent_d": 0.9, "cornice_h": 0.4, "cornice_proj": 0.25, "roof_h": 0.35,
       "wall_t": 0.6, "wall_base_h": 0.45}


def _top(parts):
    tops = [p["z1"] for p in parts if p["kind"] == "box"]
    tops += [p["z"] + p["profile"][-1][1] for p in parts if p["kind"] == "lathe"]
    return max(tops)


def test_gate_parts_stack_to_the_sheet_height_and_keep_the_passage_open():
    parts = kit_proc.arch_gate(GATE)
    total = GATE["plinth_h"] + GATE["body_h"] + GATE["band_h"] + 1.3 + GATE["attic_h"]
    assert _top(parts) == pytest.approx(total)
    wall = next(p for p in parts if p["kind"] == "archwall")
    assert (wall["x0"], wall["x1"], wall["y0"], wall["y1"]) == (-10, 10, -6.5, 6.5)
    # nothing but the arch wall itself crosses the passage below the crown
    r = GATE["span"] / 2
    for p in parts:
        if p["kind"] == "box" and p["z0"] < GATE["spring"] + r - 0.2:
            assert p["x1"] <= -r + 1e-6 or p["x0"] >= r - 1e-6 or p["z1"] <= 0, p


def test_colonnade_bay_fits_its_module_and_has_a_back_wall():
    parts = kit_proc.colonnade_bay(BAY)
    boxes = [p for p in parts if p["kind"] == "box"]
    assert min(p["x0"] for p in boxes) == -2 and max(p["x1"] for p in boxes) == 2     # tiles every 4 m
    assert _top(parts) == pytest.approx(7.3)
    assert any(p["y1"] == 4.0 and p["z0"] == 0 and p["z1"] == pytest.approx(7.3 - 0.35) for p in boxes)
    assert sum(p["kind"] == "lathe" for p in parts) == 1


def test_maps_are_tileable_and_well_formed():
    rng = np.random.default_rng(0)
    patch = rng.uniform(150, 230, (40, 30, 3))
    t = kit_proc.seamless(patch, 64)
    assert t.shape == (64, 64, 3)
    assert np.array_equal(t[:, 0], t[:, -1]) and np.array_equal(t[0], t[-1])        # mirrored edges match
    n = kit_proc.normal_map(t.astype(float))
    assert n.dtype == np.uint8 and n[..., 2].min() > 127                            # all facing out
    r = kit_proc.rough_map(t.astype(float))
    assert 0.4 * 255 - 1 <= r.min() and r.max() <= 0.95 * 255 + 1


def test_three_quarter_request_asks_for_depth():
    a = PlanAsset(id="gate", name="gate", category="structure", description="A gate.",
                  dimensions=Dimensions(width=20, depth=13, height=17))
    u = s2_refs.Unit("library", "gate", "object", [a])
    p, w, h, _ = s2_refs.request(u, None, "three-quarter")
    assert "three-quarter" in p and "full depth" in p and w > h
    p2, *_ = s2_refs.request(u, None)
    assert "LEFT: front view" in p2


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_build_writes_a_textured_glb_at_real_size(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("demo")
    sheet = store.root / "refs/sheet.png"
    sheet.parent.mkdir(parents=True)
    Image.fromarray(np.random.default_rng(1).integers(170, 230, (200, 300, 3), dtype=np.uint8)).save(sheet)
    write_json(kit_proc.spec_path(store, "gate"), {
        "kind": "arch_gate", "params": GATE,
        "facade": {"sheet": "refs/sheet.png", "box_px": [0, 0, 300, 200], "origin_px": [150, 200], "m_per_px": 0.07},
        "tile": {"sheet": "refs/sheet.png", "box_px": [10, 10, 60, 60]}})
    res = kit_proc.build(store, "gate", log=lambda *_: None)
    assert res["bbox"] == pytest.approx([20.9, 13.9, 17.35], abs=0.01)
    check = tmp_path / "check.py"
    check.write_text("""
import bpy, sys, json
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[sys.argv.index("--") + 1])
o = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
print("CHECK", json.dumps({"mats": sorted(s.material.name for s in o.material_slots),
      "images": sorted(i.name for i in bpy.data.images if i.size[0])}))
""")
    out = subprocess.run([str(REAL_BLENDER), "-b", "--factory-startup", "--python", str(check), "--", res["out"]],
                         capture_output=True, text=True, check=True)
    got = json.loads(next(ln for ln in out.stdout.splitlines() if ln.startswith("CHECK"))[6:])
    assert got["mats"] == ["facade", "stone"]
    assert {"facade_base", "facade_normal", "facade_rough", "tile_base", "tile_normal", "tile_rough"} <= set(got["images"])
