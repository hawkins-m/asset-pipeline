"""Silhouette masks of library builds through a shot camera: each GLB placed at its look pose
(yaw about Z, then moved to x, y, z in Blender world metres) and rendered alone as white on
black (Cycles, 1 sample, box filter, Raw: an exact mask, see CLAUDE.md "data passes").

    blender -b --python scripts/blender_silhouette.py -- JOB.json

JOB: {"camera": 4x4, "lens_mm", "sensor_mm", "resolution": [w, h],
      "items": [{"key", "glb", "x", "y", "z", "yaw"}], "out_dir": dir}
Writes <out_dir>/<key>.png per item and prints REPORT {key: pixels}.
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
cam.matrix_world = Matrix(job["camera"])
cam.data.lens, cam.data.sensor_width = job["lens_mm"], job["sensor_mm"]
cam.data.sensor_fit = "HORIZONTAL"
cam.data.clip_end = 20000
sc.camera = cam
sc.render.resolution_x, sc.render.resolution_y = job["resolution"]
sc.render.resolution_percentage = 100
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = 1
sc.cycles.use_denoising = False
sc.render.filter_size = 0.01
sc.render.dither_intensity = 0
sc.view_settings.view_transform = "Raw"
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "BW"
world = bpy.data.worlds.new("w")
world.color = (0, 0, 0)
sc.world = world
white = bpy.data.materials.new("white")
white.use_nodes = True
nt = white.node_tree
nt.nodes.clear()
em = nt.nodes.new("ShaderNodeEmission")
em.inputs["Strength"].default_value = 1.0
out = nt.nodes.new("ShaderNodeOutputMaterial")
nt.links.new(em.outputs[0], out.inputs[0])
sc.view_layers[0].material_override = white

groups = {}
for it in job["items"]:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=it["glb"])
    new = [o for o in bpy.data.objects if o not in before]
    root = bpy.data.objects.new(f"root_{it['key']}", None)
    sc.collection.objects.link(root)
    for o in new:
        if o.parent is None:
            o.parent = root
    a = math.radians(it["yaw"])
    root.matrix_world = Matrix.Translation((it["x"], it["y"], it["z"])) @ Matrix.Rotation(a, 4, "Z")
    groups[it["key"]] = [o for o in new if o.type == "MESH"]

od = Path(job["out_dir"])
od.mkdir(parents=True, exist_ok=True)
rep = {}
for key, objs in groups.items():
    for k2, o2 in groups.items():
        for o in o2:
            o.hide_render = k2 != key
    sc.render.filepath = str(od / f"{key}.png")
    bpy.ops.render.render(write_still=True)
    rep[key] = str(od / f"{key}.png")
print("REPORT " + json.dumps(rep))
