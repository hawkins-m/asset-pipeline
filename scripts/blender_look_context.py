"""Export the greybox around a camera for an Unreal look test: the objects within `radius`,
joined into one mesh with a material per kind (building, green, water, paving) in world
coordinates (Interchange then lands them where the shot cameras expect), the terrain as its own
mesh, and optionally a far ridge. The greybox objects that library builds replace are left out.

    blender -b greybox.blend --python scripts/blender_look_context.py -- JOB.json

JOB: {"camera": 4x4 Blender matrix, "lens_mm", "sensor_mm", "resolution": [w, h], "radius": 600,
      "out_context": glb, "out_terrain": glb,
  one of
      "slot": id, "piece": "insula:mass", "bbox": [x0, y0, x1, y1]   one building: the slot's
              block whose piece projects nearest the frame region's centre (its pieces dropped;
              REPORT placement says where the library build goes)
      "drop": [{"x", "y", "w", "d", "yaw"}], "drop_slots": [ids]      library builds placed at
              these footprints: the objects standing on them (and whole slots) are dropped
  "backdrop": {"bearing", "spread", "dist": [near, far], "height": [lo, hi], "out": glb}
              (optional) a far ridge across the view: look-test dressing where the terrain has none;
              or {"crest": [[bearing deg, z m], ...], "dist": [near, far], "pad_deg", "out"}: a ridge
              whose crest stands at `near` along that line (read off the frame), pad_deg wider each side}
Kinds (one material each): building, green (trees, hedges), lawn, water, paving, gravel, ground.
Prints REPORT {placement, removed, objects, faces}.
"""
import json
import math
import random
import sys

import bmesh
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
STREETS = ("plaza-", "way-", "radial-", "ring-", "street-")


def world_box(o):
    cs = [o.matrix_world @ Vector(c) for c in o.bound_box]
    return Vector([min(c[i] for c in cs) for i in range(3)]), Vector([max(c[i] for c in cs) for i in range(3)])


def meshes_of(slot):
    return [o for o in bpy.data.objects if o.type == "MESH" and o.parent and o.parent.name == slot]


def one_building():
    """The slot's block the frame region shows: (drop set, centre, placement)."""
    x0, y0, x1, y1 = job["bbox"]
    mid = ((x0 + x1) / 2, 1 - (y0 + y1) / 2)
    masses = []
    for o in meshes_of(job["slot"]):
        if o.get("ap_piece") == job["piece"]:
            lo, hi = world_box(o)
            masses.append((o, lo, hi))
    cands = []
    for o, lo, hi in masses:
        p = world_to_camera_view(sc, cam, (lo + hi) / 2)
        if p.z > 0:
            cands.append(((p.x - mid[0]) ** 2 + (p.y - mid[1]) ** 2, o, lo, hi))
    if not cands:
        raise SystemExit(f"no {job['piece']} of {job['slot']} in front of the camera")
    _, target, lo, hi = min(cands, key=lambda c: c[0])
    z0 = lo.z
    block, grew = {target.name}, True       # the masses touching it, on the same level (a block's wings)
    while grew:
        grew = False
        for o, a, b in masses:
            if o.name in block:
                continue
            for q, c, d in masses:
                if q.name in block and all(a[i] <= d[i] + 0.5 and c[i] <= b[i] + 0.5 for i in (0, 1)) and abs(a[2] - z0) < 6:
                    block.add(o.name)
                    grew = True
                    break
    lo = Vector([min(a[i] for o, a, b in masses if o.name in block) for i in range(3)])
    hi = Vector([max(b[i] for o, a, b in masses if o.name in block) for i in range(3)])
    lo.z = z0
    base = math.degrees(target.matrix_world.to_euler().z) % 90
    to_cam = (cam.matrix_world.translation - (lo + hi) / 2).xy.normalized()
    yaw = max((base + 90 * k for k in range(4)),
              key=lambda d: Vector((math.sin(math.radians(d)), -math.cos(math.radians(d)))).dot(to_cam))
    drop = set()
    for o in meshes_of(job["slot"]):
        c = sum(world_box(o), Vector()) / 2
        if all(lo[i] - 3 <= c[i] <= hi[i] + 3 for i in (0, 1)):
            drop.add(o.name)
    centre = (lo + hi) / 2
    return drop, centre, {"loc": [round(v, 3) for v in (centre.x, centre.y, lo.z)], "yaw_deg": round(yaw, 2),
                          "dims": [round(hi[i] - lo[i], 2) for i in range(3)], "pieces": sorted(block)}


def footprints():
    """The objects standing on the library builds' footprints, and the dropped slots."""
    slots = set(job.get("drop_slots", []))
    drop = set()
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name == "terrain" or not o.parent:
            continue
        if o.parent.name in slots:
            drop.add(o.name)
            continue
        if o.parent.name.startswith(STREETS):
            continue
        c = sum(world_box(o), Vector()) / 2
        for f in job["drop"]:
            a = math.radians(f["yaw"])
            dx, dy = c.x - f["x"], c.y - f["y"]
            u, v = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
            if abs(u) <= f["w"] / 2 + 1.0 and abs(v) <= f["d"] / 2 + 1.0:
                drop.add(o.name)
                break
    return drop


GREEN = ("tree", "pine", "cypress", "olive", "planting", "hedge", "shrub")
LAWN = ("lawn", "park", "garden", "grass")
WATER = ("pool-water", "lagoon", "canal", "water")
PAVING = ("paving", "kerb", "plinth", "radial", "avenue", "street", "ring", "stair", "lane")


def kind(o):
    n = (o.name + " " + str(o.get("ap_piece", ""))).lower()
    if any(k in n for k in WATER):
        return "water"
    if any(k in n for k in GREEN):
        return "green"
    if any(k in n for k in LAWN):
        return "lawn"
    if "gravel" in n:
        return "gravel"
    if any(k in n for k in PAVING) or (o.parent and o.parent.name.startswith(STREETS)):
        return "paving"
    return "building"


if job.get("drop") is not None:
    drop, centre, placement = footprints(), cam.matrix_world.translation.copy(), None
else:
    drop, centre, placement = one_building()
mats = {}
for k, rgb in (("building", (0.62, 0.6, 0.57)), ("green", (0.12, 0.2, 0.08)), ("water", (0.05, 0.12, 0.14)),
               ("paving", (0.7, 0.68, 0.63)), ("ground", (0.32, 0.29, 0.24)), ("lawn", (0.15, 0.3, 0.08)),
               ("gravel", (0.45, 0.4, 0.33)), ("backdrop", (0.1, 0.13, 0.08))):
    m = bpy.data.materials.new(k)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1)
    mats[k] = m
keep = []
for o in list(bpy.data.objects):
    if o.type != "MESH" or o.name in drop or o.name in ("terrain", "sea"):
        continue
    a, b = world_box(o)
    if ((a + b) / 2 - centre).xy.length <= job["radius"]:
        keep.append(o)


def export(objs, path, material=None):
    bpy.ops.object.select_all(action="DESELECT")
    copies = []
    for o in objs:
        c = o.copy()
        c.data = o.data.copy()
        c.parent = None
        c.matrix_world = o.matrix_world.copy()
        sc.collection.objects.link(c)
        c.data.materials.clear()
        c.data.materials.append(material or mats[kind(o)])
        copies.append(c)
    for c in copies:
        c.select_set(True)
    bpy.context.view_layer.objects.active = copies[0]
    if len(copies) > 1:
        bpy.ops.object.join()
    j = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action="DESELECT")
    j.select_set(True)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True)
    return j


ctx = export(keep, job["out_context"])
export([bpy.data.objects["terrain"]], job["out_terrain"], mats["ground"])
bd = job.get("backdrop")


def crest_ridge(bd):
    """A ridge whose crest follows bd["crest"] ([bearing, z] sorted by bearing) at dist[0]: a steep
    front from below the ground up to the crest (so the crest is the silhouette), falling behind."""
    cr = sorted(bd["crest"])
    bs = [c[0] for c in cr]
    zs = [c[1] for c in cr]
    pad = bd.get("pad_deg", 25.0)
    near, far = bd["dist"]
    rows = ((near - 1800, None), (near - 1200, 0.3), (near - 600, 0.7), (near, 1.0),
            (near + 0.35 * (far - near), 0.82), (far, 0.35))
    cx, cy = cam.matrix_world.translation.x, cam.matrix_world.translation.y
    n = 160
    lo, hi = bs[0] - pad, bs[-1] + pad
    grid = []
    for i in range(n + 1):
        b = lo + (hi - lo) * i / n
        k = max(0, min(len(bs) - 1, sum(1 for x in bs if x <= b) - 1))
        if b <= bs[0]:
            z = zs[0]
        elif b >= bs[-1]:
            z = zs[-1]
        else:
            t = (b - bs[k]) / max(bs[k + 1] - bs[k], 1e-6)
            z = zs[k] + (zs[k + 1] - zs[k]) * t
        a = math.radians(b)
        grid.append([bm.verts.new((cx + d * math.cos(a), cy + d * math.sin(a), -40.0 if f is None else z * f))
                     for d, f in rows])
    for i in range(n):
        for r in range(len(rows) - 1):
            bm.faces.new((grid[i][r], grid[i + 1][r], grid[i + 1][r + 1], grid[i][r + 1]))


if bd and bd.get("crest"):
    bm = bmesh.new()
    crest_ridge(bd)
elif bd:                     # a ridge: a band of quads between dist[0] and dist[1], peak heights eased
    rnd = random.Random(7)
    bm = bmesh.new()
    n, rows = 96, 6
    peaks = [rnd.uniform(*bd["height"]) for _ in range(12)]
    cx, cy = cam.matrix_world.translation.x, cam.matrix_world.translation.y
    grid = []
    for i in range(n + 1):
        ang = math.radians(bd["bearing"] - bd["spread"] / 2 + bd["spread"] * i / n)
        t = i / n * (len(peaks) - 1)
        k = int(t)
        hpk = peaks[k] + (peaks[min(k + 1, len(peaks) - 1)] - peaks[k]) * (0.5 - 0.5 * math.cos(math.pi * (t - k)))
        hpk *= 0.85 + 0.15 * rnd.random()
        row = []
        for r in range(rows + 1):
            dist = bd["dist"][0] + (bd["dist"][1] - bd["dist"][0]) * r / rows
            prof = math.sin(math.pi * r / rows) ** 0.8          # rises from the near edge, falls behind
            row.append(bm.verts.new((cx + dist * math.cos(ang), cy + dist * math.sin(ang), -40 + (hpk + 40) * prof)))
        grid.append(row)
    for i in range(n):
        for r in range(rows):
            bm.faces.new((grid[i][r], grid[i + 1][r], grid[i + 1][r + 1], grid[i][r + 1]))
if bd:
    bm.normal_update()
    bm.faces.ensure_lookup_table()
    f0 = bm.faces[len(bm.faces) // 2]                      # face the camera (Unreal culls back faces)
    if f0.normal.dot(cam.matrix_world.translation - f0.calc_center_median()) < 0:
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    me = bpy.data.meshes.new("backdrop")
    bm.to_mesh(me)
    ob = bpy.data.objects.new("backdrop", me)
    sc.collection.objects.link(ob)
    export([ob], bd["out"], mats["backdrop"])
print("REPORT " + json.dumps({"placement": placement, "removed": len(drop), "objects": len(keep), "faces": len(ctx.data.polygons)}))
