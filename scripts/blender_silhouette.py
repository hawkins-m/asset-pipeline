"""Silhouette masks of library builds through a shot camera: each GLB placed at its look pose
(yaw about Z, then moved to x, y, z in Blender world metres) and rendered alone as white on
black (Cycles, 1 sample, box filter, Raw: an exact mask, see CLAUDE.md "data passes").

    blender -b --python scripts/blender_silhouette.py -- JOB.json

JOB: {"camera": 4x4, "lens_mm", "sensor_mm", "resolution": [w, h],
      "items": [{"key", "glb", "x", "y", "z", "yaw", "w", "d"}], "out_dir": dir, "openings": bool}
Writes <out_dir>/<key>.png per item and prints REPORT {key: pixels}. With openings, also
<key>_openings.png, red: what reads as an opening or recess from the camera: glazing and frames
(materials glass / rail_glass / frame) and every surface set back 0.3 m or more behind the
building's front face (local y > -d/2 + 0.3), outer side walls excepted; green: those side walls.
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


def openings_material(item, glazing):
    """Emission white where the surface is an opening or a recess (object space: the build's own frame)."""
    m = bpy.data.materials.new(f"open_{item['key']}_{glazing}")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(em.outputs[0], out.inputs[0])
    if glazing:                     # red: opening
        em.inputs["Color"].default_value = (1, 0, 0, 1)
        return m
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs["Object"], sep.inputs[0])
    deep = nt.nodes.new("ShaderNodeMath")
    deep.operation = "GREATER_THAN"
    nt.links.new(sep.outputs["Y"], deep.inputs[0])
    deep.inputs[1].default_value = -item["d"] / 2 + 0.3
    ax = nt.nodes.new("ShaderNodeMath")
    ax.operation = "ABSOLUTE"
    nt.links.new(sep.outputs["X"], ax.inputs[0])
    inner = nt.nodes.new("ShaderNodeMath")
    inner.operation = "LESS_THAN"
    nt.links.new(ax.outputs[0], inner.inputs[0])
    inner.inputs[1].default_value = item["w"] / 2 - 0.05
    both = nt.nodes.new("ShaderNodeMath")
    both.operation = "MULTIPLY"
    nt.links.new(deep.outputs[0], both.inputs[0])
    nt.links.new(inner.outputs[0], both.inputs[1])
    side = nt.nodes.new("ShaderNodeMath")          # green: the outer side walls (left out of the comparison)
    side.operation = "SUBTRACT"
    side.inputs[0].default_value = 1.0
    nt.links.new(inner.outputs[0], side.inputs[1])
    rgb = nt.nodes.new("ShaderNodeCombineXYZ")
    nt.links.new(both.outputs[0], rgb.inputs[0])
    nt.links.new(side.outputs[0], rgb.inputs[1])
    nt.links.new(rgb.outputs[0], em.inputs["Color"])
    em.inputs["Strength"].default_value = 1.0
    return m


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
    if job.get("openings"):
        it = next(i for i in job["items"] if i["key"] == key)
        sc.view_layers[0].material_override = None
        mats = {True: openings_material(it, True), False: openings_material(it, False)}
        saved = []
        for o in objs:
            for slot in o.material_slots:
                saved.append((slot, slot.material))
                nm = (slot.material.name if slot.material else "").lower()
                slot.material = mats[any(k in nm for k in ("glass", "frame"))]
        sc.render.filepath = str(od / f"{key}_openings.png")
        sc.render.image_settings.color_mode = "RGB"
        bpy.ops.render.render(write_still=True)
        sc.render.image_settings.color_mode = "BW"
        for slot, m in saved:
            slot.material = m
        sc.view_layers[0].material_override = white
print("REPORT " + json.dumps(rep))
