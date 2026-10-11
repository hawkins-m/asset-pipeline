"""Review renders of a library asset's GLB: named views around it, quick (Workbench with its
textures: seconds) or Cycles (its PBR materials under a sun and sky: the review images).

    blender -b --factory-startup --python scripts/blender_render_asset.py -- JOB.json

JOB: {"glb": ..., "out_dir": ..., "mode": "quick" | "cycles", "size": [w, h], "samples": 64,
      "views": [{"name": "sheet", "az": -4, "el": 0.12}, {"name": "threeq", "az": -35, "el": 0.35},
                {"name": "close", "az": -8, "el": 0.05, "target": [x, y, z], "dist": 18, "lens": 35}],
      "figure": [x, y, z] | null}      a 1.8 m figure standing there, for scale
az: degrees round from the front (-Y), el: the camera's rise per unit of distance. Writes
<out_dir>/<name>.png per view and prints a REPORT json line.
"""
import json
import math
import sys

import bpy
from mathutils import Vector

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["glb"])
sc = bpy.context.scene
ms = [o for o in sc.objects if o.type == "MESH"]
cs = [o.matrix_world @ Vector(c) for o in ms for c in o.bound_box]
lo = Vector(min(c[i] for c in cs) for i in range(3))
hi = Vector(max(c[i] for c in cs) for i in range(3))
ctr, size = (lo + hi) / 2, (hi - lo).length


def material(rgb, rough=0.8):
    m = bpy.data.materials.new("m")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = rough
    return m


if job.get("figure"):                       # a 1.8 m figure: legs, body, head
    fx, fy, fz = job["figure"]
    fm = material((0.12, 0.16, 0.24), 0.7)
    for loc, scale, kind in (((fx, fy, fz + 0.45), (0.13, 0.1, 0.45), "cube"),
                             ((fx, fy, fz + 1.2), (0.2, 0.12, 0.33), "cube"),
                             ((fx, fy, fz + 1.665), (0.11, 0.11, 0.135), "sphere")):
        if kind == "cube":
            bpy.ops.mesh.primitive_cube_add(size=2, location=loc)
        else:
            bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=loc, segments=16, ring_count=8)
        o = bpy.context.object
        o.scale = scale
        o.data.materials.append(fm)

cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
sc.camera = cam
mode = job.get("mode", "cycles")
if mode == "quick":
    sc.render.engine = "BLENDER_WORKBENCH"
    sc.display.shading.light = "STUDIO"
    sc.display.shading.color_type = "TEXTURE"
    sc.display.shading.show_shadows = True
    sc.display.shading.show_cavity = True
    sc.display.shading.cavity_type = "WORLD"
else:
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 3.2
    sun.data.angle = math.radians(3)
    sun.rotation_euler = (math.radians(50), 0, math.radians(-40))
    sc.collection.objects.link(sun)
    w = bpy.data.worlds.new("w")
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.55, 0.62, 0.72, 1)
    w.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.55
    sc.world = w
    bpy.ops.mesh.primitive_plane_add(size=size * 6, location=(ctr.x, ctr.y, lo.z - 0.01))
    bpy.context.object.data.materials.append(material((0.42, 0.40, 0.37)))
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = job.get("samples", 64)
    sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.exposure = -0.15
sc.render.resolution_x, sc.render.resolution_y = job.get("size", [900, 640])
out = {}
for v in job["views"]:
    az = math.radians(v.get("az", -35))
    d = Vector((math.sin(az), -math.cos(az), v.get("el", 0.35))).normalized()
    tgt = Vector(v["target"]) if v.get("target") else ctr
    cam.data.lens = v.get("lens", 50)
    cam.location = tgt + d * (v.get("dist") or size * 1.55)
    cam.rotation_euler = (tgt - cam.location).to_track_quat("-Z", "Y").to_euler()
    sc.render.filepath = f"{job['out_dir']}/{v['name']}.png"
    bpy.ops.render.render(write_still=True)
    out[v["name"]] = sc.render.filepath
print("REPORT " + json.dumps({"images": out, "mode": mode, "bbox": [round(hi[i] - lo[i], 2) for i in range(3)]}))
