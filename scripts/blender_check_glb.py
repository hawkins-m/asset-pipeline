"""Import a GLB in headless Blender, report what came through, render a preview.

    blender -b --factory-startup --python scripts/blender_check_glb.py -- IN.glb OUT.png
    (writes OUT_v0..v3.png: four views around the asset)
"""
import json
import math
import sys

import bpy
from mathutils import Vector

glb, png = sys.argv[sys.argv.index("--") + 1:][:2]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
images = [i for i in bpy.data.images if i.size[0] > 0]
report = {
    "blender": bpy.app.version_string,
    "meshes": len(meshes),
    "vertices": sum(len(o.data.vertices) for o in meshes),
    "faces": sum(len(o.data.polygons) for o in meshes),
    "materials": sorted({s.material.name for o in meshes for s in o.material_slots if s.material}),
    "images": {i.name: list(i.size) for i in images},
    "uv_layers": sorted({uv.name for o in meshes for uv in o.data.uv_layers}),
}

# Frame the asset: bounding box over all mesh corners in world space.
corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector(min(c[i] for c in corners) for i in range(3))
hi = Vector(max(c[i] for c in corners) for i in range(3))
center, size = (lo + hi) / 2, max(hi - lo)
report["bbox_size"] = [round(v, 4) for v in (hi - lo)]

scene = bpy.context.scene
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
scene.collection.objects.link(cam)
scene.camera = cam
cam.data.lens = 50

sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 3.0
sun.rotation_euler = (math.radians(45), 0, math.radians(30))
scene.collection.objects.link(sun)
world = bpy.data.worlds.new("w")
world.color = (0.6, 0.6, 0.6)
scene.world = world

scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "TEXTURE"
scene.render.resolution_x = scene.render.resolution_y = 512

# Four views around the asset (45° steps from front-right), one PNG each.
stem = png[:-4] if png.endswith(".png") else png
for i, az in enumerate((-45, 45, 135, 225)):
    a = math.radians(az)
    direction = Vector((math.sin(a), -math.cos(a), 0.6)).normalized()
    cam.location = center + direction * size * 2.0
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = f"{stem}_v{i}.png"
    bpy.ops.render.render(write_still=True)

print("GLB_REPORT " + json.dumps(report))
