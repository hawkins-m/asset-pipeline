"""A top-down plan of the greybox over a rectangle, in frame_plan's class colours, plus the objects
there with their kind and world bounds (to compare with a frame read back onto the plan).

    blender -b greybox.blend --python scripts/blender_plan_topdown.py -- JOB.json

JOB: {"bounds": [x0, y0, x1, y1], "cell": 0.5, "out_png": ..., "out_json": ...}
Kinds: water, grass, paving, gravel, building, tree (by piece and object name); the terrain is
drawn as "other" ground.
"""
import json
import sys

import bpy
from mathutils import Vector

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
x0, y0, x1, y1 = job["bounds"]
cell = job.get("cell", 0.5)
COL = {"water": (40, 120, 200), "grass": (90, 170, 60), "paving": (205, 200, 185), "gravel": (150, 130, 105),
       "building": (200, 90, 70), "tree": (30, 90, 40), "other": (235, 235, 235)}
KINDS = (("water", ("pool", "basin", "water", "lagoon", "sea", "fountain", "canal")),
         ("tree", ("tree", "pine", "cypress", "olive", "palm")),
         ("grass", ("lawn", "grass", "park", "garden", "planting", "hedge", "shrub", "bed")),
         ("gravel", ("gravel", "sand")),
         ("paving", ("paving", "plaza", "street", "road", "ribbon", "path", "walk", "square", "pad", "kerb", "step")))


STREETS = ("way-", "radial-", "ring-", "street-", "plaza-", "square-", "avenue-")


def kind(o):
    n = (o.name + " " + str(o.get("ap_piece", ""))).lower()
    for k, words in KINDS:
        if any(w in n for w in words):
            return k
    if o.name == "terrain":
        return "other"
    slot = (o.parent.name if o.parent else o.name).lower()
    return "paving" if slot.startswith(STREETS) else "building"


sc = bpy.context.scene
mats = {}
for k, c in COL.items():
    m = bpy.data.materials.new(k)
    m.diffuse_color = (*[v / 255 for v in c], 1)
    mats[k] = m
objs = []
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    cs = [o.matrix_world @ Vector(c) for c in o.bound_box]
    lo = [min(c[i] for c in cs) for i in range(3)]
    hi = [max(c[i] for c in cs) for i in range(3)]
    inside = hi[0] > x0 and lo[0] < x1 and hi[1] > y0 and lo[1] < y1
    if not inside and o.name != "terrain":
        o.hide_render = True
        continue
    k = kind(o)
    o.color = (*[v / 255 for v in COL[k]], 1)
    if o.name != "terrain":
        objs.append({"name": o.name, "slot": o.parent.name if o.parent else None, "piece": o.get("ap_piece"),
                     "kind": k, "bbox": [round(v, 2) for v in lo[:2] + hi[:2]], "z": [round(lo[2], 2), round(hi[2], 2)]})
cam = bpy.data.objects.new("plan_cam", bpy.data.cameras.new("plan_cam"))
sc.collection.objects.link(cam)
sc.camera = cam
cam.data.type = "ORTHO"
cam.data.ortho_scale = max(x1 - x0, y1 - y0)
cam.data.sensor_fit = "HORIZONTAL" if x1 - x0 >= y1 - y0 else "VERTICAL"
cam.location = ((x0 + x1) / 2, (y0 + y1) / 2, 2000)
cam.rotation_euler = (0, 0, 0)
cam.data.clip_end = 5000
sc.render.engine = "BLENDER_WORKBENCH"
sc.display.shading.light = "FLAT"
sc.display.shading.color_type = "OBJECT"
sc.view_settings.view_transform = "Standard"
sc.render.resolution_x, sc.render.resolution_y = round((x1 - x0) / cell), round((y1 - y0) / cell)
sc.render.filepath = job["out_png"]
bpy.ops.render.render(write_still=True)
json.dump({"bounds": job["bounds"], "cell": cell, "objects": objs}, open(job["out_json"], "w"), indent=1)
print("REPORT " + json.dumps({"objects": len(objs)}))
