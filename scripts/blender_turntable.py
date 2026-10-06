"""Render a GLB from known angles around it, on white (for matching orbit frames).

    blender -b --factory-startup --python scripts/blender_turntable.py -- IN.glb OUT_DIR \
        [--step 5] [--elevations 0,15,30] [--size 256]

Writes OUT_DIR/e<elev>_a<azimuth>.png and OUT_DIR/renders.json.

Camera convention (Blender, Z up): azimuth 0 puts the camera at -Y looking at the asset,
which is glTF +Z, the side TRELLIS treats as the front. Azimuth +90 puts it at +X: the
asset's own LEFT side (for an asset facing the camera, its left is the viewer's right,
+X). Workbench with the texture colours and studio light, perspective 50 mm.
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]
glb, out = args[0], Path(args[1])


def opt(name, default):
    return args[args.index(name) + 1] if name in args else default


step = int(opt("--step", 5))
elevations = [float(e) for e in opt("--elevations", "0,15,30").split(",")]
size = int(opt("--size", 256))
out.mkdir(parents=True, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector(min(c[i] for c in corners) for i in range(3))
hi = Vector(max(c[i] for c in corners) for i in range(3))
center, radius = (lo + hi) / 2, (hi - lo).length / 2

scene = bpy.context.scene
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
scene.collection.objects.link(cam)
scene.camera = cam
cam.data.lens = 50
dist = radius / math.sin(math.atan(18 / 50)) * 1.1  # fit the bounding sphere (36 mm sensor)

scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "TEXTURE"
scene.render.film_transparent = True
scene.render.image_settings.color_mode = "RGBA"
scene.render.resolution_x = scene.render.resolution_y = size

files = []
for elev in elevations:
    for az in range(0, 360, step):
        a, e = math.radians(az), math.radians(elev)
        direction = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
        cam.location = center + direction * dist
        cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
        name = f"e{int(elev):02d}_a{az:03d}.png"
        scene.render.filepath = str(out / name)
        bpy.ops.render.render(write_still=True)
        files.append({"file": name, "azimuth": az, "elevation": elev})
(out / "renders.json").write_text(json.dumps({"glb": glb, "step": step, "renders": files}, indent=1))
print(f"TURNTABLE {len(files)} renders -> {out}")
