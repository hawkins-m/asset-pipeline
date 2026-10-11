"""Export the greybox around one library building for an Unreal look test: everything within
`radius` of the building, joined into one mesh in world coordinates (Interchange then lands it
where the city's shot cameras expect it), with four plain materials (building, green, water,
ground), and the terrain as its own mesh. The building's own greybox pieces are left out: the
library GLB takes their place.

    blender -b greybox.blend --python scripts/blender_look_context.py -- JOB.json

JOB: {"slot": id, "piece": "insula:mass" (the kind of piece the library building replaces),
      "camera": 4x4 Blender matrix, "lens_mm", "sensor_mm", "resolution": [w, h],
      "bbox": [x0, y0, x1, y1] (the building's region in the frame, 0-1 from the top left),
      "radius": 600, "out_context": ...glb, "out_terrain": ...glb}
Prints REPORT {placement: {loc, yaw_deg, dims}, removed, objects}.
"""
import json
import math
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
sc = bpy.context.scene
cam = bpy.data.objects.new("look_cam", bpy.data.cameras.new("look_cam"))
sc.collection.objects.link(cam)
cam.matrix_world = Matrix(job["camera"])
cam.data.lens, cam.data.sensor_width = job["lens_mm"], job["sensor_mm"]
sc.render.resolution_x, sc.render.resolution_y = job["resolution"]
bpy.context.view_layer.update()


def world_box(o):
    cs = [o.matrix_world @ Vector(c) for c in o.bound_box]
    return Vector([min(c[i] for c in cs) for i in range(3)]), Vector([max(c[i] for c in cs) for i in range(3)])


# the slot's building the frame shows: its piece whose centre projects inside the frame region,
# the nearest to the region's centre
x0, y0, x1, y1 = job["bbox"]
mid = ((x0 + x1) / 2, 1 - (y0 + y1) / 2)
cands = []
for o in bpy.data.objects:
    if o.type == "MESH" and o.parent and o.parent.name == job["slot"] and o.get("ap_piece") == job["piece"]:
        lo, hi = world_box(o)
        p = world_to_camera_view(sc, cam, (lo + hi) / 2)
        if p.z > 0:
            cands.append(((p.x - mid[0]) ** 2 + (p.y - mid[1]) ** 2, o, lo, hi))
if not cands:
    raise SystemExit(f"no {job['piece']} of {job['slot']} in front of the camera")
_, target, lo, hi = min(cands, key=lambda c: c[0])
# the whole block: the masses touching this one, and the ones touching those (a perimeter block's
# wings are separate pieces)
masses = [(o, *world_box(o)) for _, o, _, _ in [(0, o, 0, 0) for o in bpy.data.objects if o.type == "MESH" and o.parent
          and o.parent.name == job["slot"] and o.get("ap_piece") == job["piece"]]]
z0 = lo.z
block, grew = {target.name}, True
while grew:
    grew = False
    for o, a, b in masses:
        if o.name in block:
            continue
        for q, c, d in masses:
            if (q.name in block and all(a[i] <= d[i] + 0.5 and c[i] <= b[i] + 0.5 for i in (0, 1))
                    and abs(a[2] - z0) < 6):           # the same block: touching, on the same level
                block.add(o.name)
                grew = True
                break
lo = Vector([min(a[i] for o, a, b in masses if o.name in block) for i in range(3)])
hi = Vector([max(b[i] for o, a, b in masses if o.name in block) for i in range(3)])
lo.z = z0                                              # stand on the chosen wing's base
# turn the building (its street front at local -Y) by quarter turns of the block's own angle so
# that front faces the camera
base = math.degrees(target.matrix_world.to_euler().z) % 90
to_cam = (cam.matrix_world.translation - (lo + hi) / 2).xy.normalized()
yaw = max((base + 90 * k for k in range(4)),
          key=lambda d: Vector((math.sin(math.radians(d)), -math.cos(math.radians(d)))).dot(to_cam))
# leave out the building's own pieces: everything of the slot standing on its footprint
pad = Vector((3, 3, 0))
drop = set()
for o in bpy.data.objects:
    if o.type == "MESH" and o.parent and o.parent.name == job["slot"]:
        c = sum(world_box(o), Vector()) / 2
        if all(lo[i] - pad[i] <= c[i] <= hi[i] + pad[i] for i in (0, 1)):
            drop.add(o.name)

GREEN = ("tree", "pine", "cypress", "olive", "lawn", "park", "garden", "planting", "hedge", "grass", "shrub")
WATER = ("sea", "pool", "lagoon", "water", "basin", "fountain")


def kind(name):
    n = name.lower()
    return "green" if any(k in n for k in GREEN) else "water" if any(k in n for k in WATER) else "building"


mats = {}
for k, rgb in (("building", (0.62, 0.6, 0.57)), ("green", (0.12, 0.2, 0.08)), ("water", (0.05, 0.12, 0.14)),
               ("ground", (0.32, 0.29, 0.24))):
    m = bpy.data.materials.new(k)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1)
    mats[k] = m
centre = (lo + hi) / 2
keep = []
for o in list(bpy.data.objects):
    if o.type != "MESH" or o.name in drop or o.name == "terrain":
        continue
    a, b = world_box(o)
    if ((a + b) / 2 - centre).xy.length > job["radius"]:
        continue
    keep.append(o)
# join copies (instanced meshes made single), each with its kind's material
bpy.ops.object.select_all(action="DESELECT")
copies = []
for o in keep:
    c = o.copy()
    c.data = o.data.copy()
    c.parent = None
    c.matrix_world = o.matrix_world.copy()
    sc.collection.objects.link(c)
    c.data.materials.clear()
    c.data.materials.append(mats[kind(o.name)])
    copies.append(c)
for c in copies:
    c.select_set(True)
bpy.context.view_layer.objects.active = copies[0]
bpy.ops.object.join()
ctx = bpy.context.view_layer.objects.active
ctx.name = "look_context"
bpy.ops.object.select_all(action="DESELECT")
ctx.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["out_context"], export_format="GLB", use_selection=True, export_apply=True)
ter = bpy.data.objects["terrain"]
t2 = ter.copy()
t2.data = ter.data.copy()
t2.parent = None
t2.matrix_world = ter.matrix_world.copy()
sc.collection.objects.link(t2)
t2.data.materials.clear()
t2.data.materials.append(mats["ground"])
bpy.ops.object.select_all(action="DESELECT")
t2.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["out_terrain"], export_format="GLB", use_selection=True, export_apply=True)
print("REPORT " + json.dumps({"placement": {"loc": [round(v, 3) for v in (centre.x, centre.y, lo.z)], "yaw_deg": round(yaw, 2),
                                            "dims": [round(hi[i] - lo[i], 2) for i in range(3)], "pieces": sorted(block)},
                              "removed": len(drop), "objects": len(keep), "faces": len(ctx.data.polygons)}))
