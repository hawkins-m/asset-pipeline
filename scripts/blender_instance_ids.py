"""An object-level id pass for some slots through a shot camera: each mesh object of those slots
gets its own colour (index + 1 as 24-bit RGB), every other object one occluder colour (white),
sky black. Cycles, 1 sample, box filter, Raw: exact (docs/notes/world-and-city.md, data passes).

    blender -b greybox.blend --factory-startup --python scripts/blender_instance_ids.py -- JOB.json

JOB: {"shot": id, "slots": [slot ids], "out_png": path, "out_json": path}
JSON: {"objects": {index: {"name", "slot", "piece", "center": [x, y, z]}}}
"""
import json
import sys

import bpy
from mathutils import Vector

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
sc = bpy.context.scene
cam = next(o for o in bpy.data.objects if o.type == "CAMERA" and o.get("ap_shot") == job["shot"])
sc.camera = cam
cam.data.clip_end = 40000.0
sc.render.resolution_x = int(cam.get("ap_res_x", 1344))
sc.render.resolution_y = int(cam.get("ap_res_y", 768))
sc.render.resolution_percentage = 100
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = 1
sc.cycles.use_denoising = False
sc.cycles.pixel_filter_type = "BOX"
sc.cycles.filter_width = 0.01
sc.cycles.max_bounces = 0
sc.render.dither_intensity = 0.0
sc.view_settings.view_transform = "Raw"
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "RGB"
w = bpy.data.worlds.new("ids_world")
w.use_nodes = True
w.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
sc.world = w

m = bpy.data.materials.new("ids_obj")
m.use_nodes = True
nt = m.node_tree
nt.nodes.clear()
info = nt.nodes.new("ShaderNodeObjectInfo")
em = nt.nodes.new("ShaderNodeEmission")
out = nt.nodes.new("ShaderNodeOutputMaterial")
nt.links.new(info.outputs["Color"], em.inputs["Color"])
nt.links.new(em.outputs[0], out.inputs[0])
sc.view_layers[0].material_override = m


def slot_of(o):
    while o:
        if "ap_id" in o:
            return str(o["ap_id"])
        o = o.parent
    return None


want = set(job["slots"])
objs = {}
i = 0
for o in sc.objects:
    if o.type != "MESH":
        continue
    s = slot_of(o)
    if s in want:
        i += 1
        o.color = ((i >> 16) / 255.0, ((i >> 8) & 255) / 255.0, (i & 255) / 255.0, 1.0)
        c = sum((o.matrix_world @ Vector(b) for b in o.bound_box), Vector()) / 8
        objs[i] = {"name": o.name, "slot": s, "piece": str(o.get("ap_piece", "")), "center": [round(v, 2) for v in c]}
    else:
        o.color = (1.0, 1.0, 1.0, 1.0)
sc.render.filepath = job["out_png"]
bpy.ops.render.render(write_still=True)
json.dump({"objects": objs}, open(job["out_json"], "w"))
print("REPORT " + json.dumps({"objects": len(objs)}))
