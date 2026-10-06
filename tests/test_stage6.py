"""Stage 6 (Blender cleanup): orchestration with a fake Blender, plus one real-Blender run
on a generated textured box (skipped without Blender)."""
import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import AssetPlan, Dimensions, PlanAsset
from asset_pipeline.stages import s1_plan, s5_3d, s6_cleanup

NAME = "batch_001__scene_000"
REAL_BLENDER = Path("/snap/bin/blender")

FAKE_BLENDER = r"""#!/usr/bin/env python3
import json, sys, time, os
a = sys.argv[sys.argv.index("--") + 1:]
o = lambda k: a[a.index(k) + 1]
json.dump(a, open(os.environ["FAKE_BLENDER_ARGS"], "w"))
if os.environ.get("FAKE_BLENDER_HANG"):
    time.sleep(60)
open(o("--out"), "wb").write(b"glTF")
w, d, h = map(float, o("--dims").split(","))
json.dump({"output_faces": int(o("--retopo")) or 1000000, "output_dims_m": [w, d, h],
           "dims_vs_plan": {"w": 1.0, "d": 1.0, "h": 1.0}, "warnings": [], "seconds": 0.1,
           "retopo_method": "decimate" if int(o("--retopo")) else None}, open(o("--report"), "w"))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("demo")
    s1_plan.save(store, NAME, AssetPlan(scene="scenes/x.png", assets=[
        PlanAsset(id="barrel", name="barrel", category="prop", usage="game",
                  dimensions=Dimensions(width=0.6, depth=0.6, height=0.9)),
        PlanAsset(id="house", name="house", category="building", usage="cine",
                  dimensions=Dimensions(width=6, depth=4, height=7)),
        PlanAsset(id="tower", name="tower", category="building", usage="hero",
                  dimensions=Dimensions(width=3, depth=3, height=12))]))
    for a in ("barrel", "house", "tower"):
        d = s5_3d.out_dir(store, NAME, a)
        d.mkdir(parents=True)
        (d / "v1_512_s42.glb").write_bytes(b"glTF")
        (d / "v1_512_s42.json").write_text("{}")
    fake = tmp_path / "fake_blender"
    fake.write_text(FAKE_BLENDER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(s6_cleanup, "BLENDER", fake)
    monkeypatch.setenv("FAKE_BLENDER_ARGS", str(tmp_path / "args.json"))
    return store, tmp_path / "args.json"


def _arg(args, key):
    return args[args.index(key) + 1]


def test_game_gets_category_budget_others_keep_the_mesh(env):
    store, args_file = env
    rep = s6_cleanup.run(store, NAME, "barrel")
    args = json.loads(args_file.read_text())
    assert _arg(args, "--retopo") == str(s6_cleanup.BUDGETS["prop"]) and _arg(args, "--dims") == "0.6,0.6,0.9"
    assert _arg(args, "--fit") == "height"
    assert rep["glb"] == f"cleanup/{NAME}/barrel/v1_512_s42_game.glb" and rep["usage"] == "game"
    for asset, usage in (("house", "cine"), ("tower", "hero")):
        rep = s6_cleanup.run(store, NAME, asset)
        assert _arg(json.loads(args_file.read_text()), "--retopo") == "0" and rep["usage"] == usage
    s6_cleanup.run(store, NAME, "barrel", budget=500, fit="geomean")
    args = json.loads(args_file.read_text())
    assert _arg(args, "--retopo") == "500" and _arg(args, "--fit") == "geomean"
    [res] = s6_cleanup.results(store, NAME, "barrel")
    assert res["output_faces"] == 500


def test_needs_a_3d_result_first(env):
    store, _ = env
    for f in s5_3d.out_dir(store, NAME, "barrel").glob("*"):
        f.unlink()
    with pytest.raises(ValueError, match="Make 3D first"):
        s6_cleanup.run(store, NAME, "barrel")


def test_stop_kills_blender(env, monkeypatch):
    store, _ = env
    monkeypatch.setenv("FAKE_BLENDER_HANG", "1")
    q = JobQueue()
    job = q.submit("cleanup", "demo", lambda: s6_cleanup.run(store, NAME, "barrel"), lane="cpu")
    while q.get(job.id).status != "running":
        time.sleep(0.02)
    time.sleep(0.5)
    t = time.time()
    q.cancel(job.id)
    while q.get(job.id).status == "running" and time.time() - t < 15:
        time.sleep(0.05)
    assert q.get(job.id).status == "canceled" and time.time() - t < 5
    assert s6_cleanup.results(store, NAME, "barrel") == []


# --- real Blender ------------------------------------------------------------------------

MAKE_BOX = """
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, -2, 5))
o = bpy.context.active_object
o.scale = (0.4, 0.3, 0.5)          # 0.4 x 0.3 x 0.5 units, off-centre
bpy.ops.object.modifier_add(type="SUBSURF"); o.modifiers[0].levels = 5
bpy.ops.object.modifier_apply(modifier=o.modifiers[0].name)
bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.uv.smart_project(); bpy.ops.object.mode_set(mode="OBJECT")
img = bpy.data.images.new("tex", 64, 64); img.pixels = [0.8, 0.2, 0.1, 1.0] * 64 * 64; img.pack()
mat = bpy.data.materials.new("m")
nt = mat.node_tree; t = nt.nodes.new("ShaderNodeTexImage"); t.image = img
nt.links.new(t.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
o.data.materials.append(mat)
bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
"""


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_real_blender_scales_pivots_decimates_and_bakes(tmp_path):
    box, out, rep = tmp_path / "box.glb", tmp_path / "out.glb", tmp_path / "out.json"
    script = tmp_path / "make_box.py"
    script.write_text(MAKE_BOX)
    subprocess.run([str(REAL_BLENDER), "-b", "--factory-startup", "--python", str(script), "--", str(box)],
                   check=True, capture_output=True)
    subprocess.run([str(REAL_BLENDER), "-b", "--factory-startup", "--python", str(s6_cleanup.SCRIPT), "--",
                    "--in", str(box), "--out", str(out), "--report", str(rep),
                    "--dims", "1.6,0.06,2.0", "--retopo", "500", "--texture-size", "128"],
                   check=True, capture_output=True, timeout=300)
    r = json.loads(rep.read_text())
    # height fit: 0.5 -> 2.0 m, so x4: 1.6 x 1.2 x 2.0 m, base at z = 0, centred
    assert r["output_dims_m"] == pytest.approx([1.6, 1.2, 2.0], abs=0.01)
    assert r["base_z"] == pytest.approx(0, abs=1e-4)
    assert r["output_faces"] <= 500 and r["retopo_method"] == "decimate"
    assert len(r["warnings"]) == 1 and r["warnings"][0].startswith("depth is 19.9")  # plan depth 0.06 m: flagged
    check = tmp_path / "check.py"
    check.write_text("""
import bpy, sys, json
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[sys.argv.index("--") + 1])
o = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
xs = [v.co.x for v in o.data.vertices]; ys = [v.co.y for v in o.data.vertices]; zs = [v.co.z for v in o.data.vertices]
print("CHECK", json.dumps({"min_z": min(zs), "cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
      "images": sorted(i.name for i in bpy.data.images if i.size[0])}))
""")
    res = subprocess.run([str(REAL_BLENDER), "-b", "--factory-startup", "--python", str(check), "--", str(out)],
                         capture_output=True, text=True, check=True)
    got = json.loads(next(l for l in res.stdout.splitlines() if l.startswith("CHECK"))[6:])
    assert got["min_z"] == pytest.approx(0, abs=1e-3)
    assert got["cx"] == pytest.approx(0, abs=1e-3) and got["cy"] == pytest.approx(0, abs=1e-3)
    assert got["images"] == ["base_color", "normal", "roughness"]


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_module_launches_real_blender(tmp_path, monkeypatch):
    """Through s6_cleanup.run, as the UI and CLI do: the snap Blender silently does nothing
    when its stdout is a regular file, which only this path would catch."""
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("demo")
    s1_plan.save(store, NAME, AssetPlan(scene="scenes/x.png", assets=[
        PlanAsset(id="crate", name="crate", category="prop", usage="game",
                  dimensions=Dimensions(width=0.8, depth=0.6, height=1.0))]))
    d = s5_3d.out_dir(store, NAME, "crate")
    d.mkdir(parents=True)
    script = tmp_path / "make_box.py"
    script.write_text(MAKE_BOX)
    subprocess.run([str(REAL_BLENDER), "-b", "--factory-startup", "--python", str(script), "--",
                    str(d / "v1_512_s42.glb")], check=True, capture_output=True)
    (d / "v1_512_s42.json").write_text("{}")
    rep = s6_cleanup.run(store, NAME, "crate", budget=300, texture_size=64)
    assert rep["output_faces"] <= 300 and rep["output_dims_m"][2] == pytest.approx(1.0, abs=0.01)
    assert "CLEANUP_REPORT" in (s6_cleanup.out_dir(store, NAME, "crate") / "blender.log").read_text()
