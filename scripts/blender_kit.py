"""Build one procedural kit piece (arches, colonnades, walls, cornices) from a part list and
export it as a GLB with PBR materials. The part list comes from asset_pipeline/kit_proc.py.

    blender -b --factory-startup --python scripts/blender_kit.py -- SPEC.json

SPEC: {"out": "piece.glb", "repeat": 1, "parts": [...], "materials": {name: {...}}}
  parts, metres, local frame X width, Y depth (front face at -Y), Z up:
    box      x0 x1 y0 y1 z0 z1
    archwall x0 x1 y0 y1 z0 z1 cx span spring seg    wall with a round-arched opening, from
                                                      convex pieces (piers plus a comb of
                                                      trapezoids over the arch: see CLAUDE.md)
    archband cx span spring width y0 y1 seg          an archivolt: a band around the arch (also a
                                                      barrel vault: width = its thickness)
    lathe    x y z profile [[r, dz], ...] seg        a round turned piece (column shafts, bases)
    prism    xz [[x, z], ...] y0 y1                   a convex XZ polygon extruded along Y
    shell    x y z rows [[ro, zo, ri, zi], ...] a0 a1 seg   a thick surface of revolution (apse, dome)
  any part: "rz" (degrees about Z) and "at" [x, y, z] place it after building
  each part has "mat"; a material is
    {"base": png, "normal": png, "rough": png, "uv": "facade", "rect": [x0, x1, z0, z1]}
      front (-Y) faces take the image planar over rect, back (+Y) faces mirrored, other faces
      fall back to the "tile" material named in "fallback"
    {"base": ..., "uv": "tile", "tile_m": 3.0}     box mapping at tile_m metres per repeat
    {"uv": "plain", "color": [r, g, b], "rough": 0.1, "metal": 0.0}   no texture (glass, trim)
  repeat: n copies along X at the piece's width (a preview of a tiled run)
"""
import json
import math
import sys

import bmesh
import bpy
from mathutils import Vector

spec = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.read_factory_settings(use_empty=True)


def _quad_prism(bm, xz, y0, y1):
    """A convex polygon in XZ extruded from y0 to y1 (front face at y0)."""
    f = [bm.verts.new((x, y0, z)) for x, z in xz]
    b = [bm.verts.new((x, y1, z)) for x, z in xz]
    # xz counter-clockwise (x right, z up): the front face as given faces -Y, the back reversed
    # faces +Y, the sides go b-then-f so they face out (checked by signed volume in the tests)
    out = [bm.faces.new(f), bm.faces.new(b[::-1])]
    n = len(xz)
    for i in range(n):
        j = (i + 1) % n
        out.append(bm.faces.new((f[i], b[i], b[j], f[j])))
    return out


def box(bm, p):
    return _quad_prism(bm, [(p["x0"], p["z0"]), (p["x1"], p["z0"]), (p["x1"], p["z1"]), (p["x0"], p["z1"])],
                       p["y0"], p["y1"])


def archwall(bm, p):
    r, cx, sp, seg = p["span"] / 2, p["cx"], p["spring"], p.get("seg", 24)
    z0, z1 = p["z0"], p["z1"]
    faces = []
    for a, b in ((p["x0"], cx - r), (cx + r, p["x1"])):     # piers, full height
        faces += box(bm, dict(p, x0=a, x1=b, z0=z0, z1=z1))
    pts = [(cx - r * math.cos(math.pi * i / seg), sp + r * math.sin(math.pi * i / seg)) for i in range(seg + 1)]
    for (xa, za), (xb, zb) in zip(pts, pts[1:]):
        faces += _quad_prism(bm, [(xa, za), (xb, zb), (xb, z1), (xa, z1)], p["y0"], p["y1"])
    return faces


def archband(bm, p):
    r0, cx, sp, seg = p["span"] / 2, p["cx"], p["spring"], p.get("seg", 24)
    r1 = r0 + p["width"]
    faces = []
    for i in range(seg):
        a, b = math.pi * i / seg, math.pi * (i + 1) / seg
        q = [(cx - r0 * math.cos(a), sp + r0 * math.sin(a)), (cx - r0 * math.cos(b), sp + r0 * math.sin(b)),
             (cx - r1 * math.cos(b), sp + r1 * math.sin(b)), (cx - r1 * math.cos(a), sp + r1 * math.sin(a))]
        faces += _quad_prism(bm, q, p["y0"], p["y1"])
    return faces


def lathe(bm, p):
    seg, prof = p.get("seg", 32), p["profile"]
    rings = [[bm.verts.new((p["x"] + r * math.cos(2 * math.pi * k / seg), p["y"] + r * math.sin(2 * math.pi * k / seg),
                            p["z"] + dz)) for k in range(seg)] for r, dz in prof]
    faces = []
    for a, b in zip(rings, rings[1:]):
        for k in range(seg):
            kk = (k + 1) % seg
            faces.append(bm.faces.new((a[k], a[kk], b[kk], b[k])))
    faces += [bm.faces.new(rings[0][::-1]), bm.faces.new(rings[-1])]
    return faces


def prism(bm, p):
    """A convex polygon in XZ (from the pierced walls) extruded from y0 to y1, turned
    counter-clockwise whatever order it came in (so its faces point out)."""
    q = [tuple(v) for v in p["xz"]]
    if len(q) < 3:
        return []
    area = sum(x0 * z1 - x1 * z0 for (x0, z0), (x1, z1) in zip(q, q[1:] + q[:1]))
    return _quad_prism(bm, q if area > 0 else q[::-1], p["y0"], p["y1"])


def shell(bm, p):
    """A thick surface of revolution about the vertical axis at (x, y): rows of
    (r_out, z_out, r_in, z_in) from bottom to top, swept from a0 to a1 degrees (an apse wall,
    a half dome, a drum). Built from quads only: outer, inner, both rims and, for a partial
    sweep, the end caps as strips between the outer and inner rows."""
    rows, seg = p["rows"], p.get("seg", 32)
    a0, a1 = math.radians(p.get("a0", 0)), math.radians(p.get("a1", 360))
    full = abs(a1 - a0 - 2 * math.pi) < 1e-6
    n = seg if full else seg + 1
    ang = [a0 + (a1 - a0) * k / seg for k in range(n)]

    def ring(r, z):
        return [bm.verts.new((p["x"] + r * math.cos(a), p["y"] + r * math.sin(a), p["z"] + z)) for a in ang]
    out = [ring(ro, zo) for ro, zo, _, _ in rows]
    inn = [ring(ri, zi) for _, _, ri, zi in rows]
    faces = []
    span = range(seg) if not full else range(seg)
    for k in range(len(rows) - 1):
        for j in span:
            jj = (j + 1) % n
            faces.append(bm.faces.new((out[k][j], out[k][jj], out[k + 1][jj], out[k + 1][j])))
            faces.append(bm.faces.new((inn[k][j], inn[k + 1][j], inn[k + 1][jj], inn[k][jj])))
    for j in span:
        jj = (j + 1) % n
        faces.append(bm.faces.new((out[0][j], inn[0][j], inn[0][jj], out[0][jj])))
        faces.append(bm.faces.new((out[-1][j], out[-1][jj], inn[-1][jj], inn[-1][j])))
    if not full:
        for k in range(len(rows) - 1):
            faces.append(bm.faces.new((out[k][0], out[k + 1][0], inn[k + 1][0], inn[k][0])))
            faces.append(bm.faces.new((out[k][-1], inn[k][-1], inn[k + 1][-1], out[k + 1][-1])))
    return faces


BUILD = {"box": box, "archwall": archwall, "archband": archband, "lathe": lathe, "prism": prism, "shell": shell}


def place(faces, part):
    """Rotate a part's geometry about Z by rz degrees, then move it by `at`."""
    rz, at = part.get("rz", 0), part.get("at")
    if not rz and not at:
        return
    c, s_ = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    t = Vector(at or (0, 0, 0))
    for v in {v for f in faces for v in f.verts}:
        x, y = v.co.x, v.co.y
        v.co = Vector((c * x - s_ * y, s_ * x + c * y, v.co.z)) + t


def material(name, m):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]

    def img(path, non_color=False):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(path)
        if non_color:
            n.image.colorspace_settings.name = "Non-Color"
        return n
    if m.get("base"):
        nt.links.new(img(m["base"]).outputs["Color"], bsdf.inputs["Base Color"])
    else:                                    # a plain material: glass, metal trim
        bsdf.inputs["Base Color"].default_value = (*m["color"], 1.0)
    if isinstance(m.get("rough"), str):
        nt.links.new(img(m["rough"], True).outputs["Color"], bsdf.inputs["Roughness"])
    elif m.get("rough") is not None:
        bsdf.inputs["Roughness"].default_value = m["rough"]
    if m.get("normal"):
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.inputs["Strength"].default_value = m.get("normal_strength", 1.0)
        nt.links.new(img(m["normal"], True).outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Metallic"].default_value = m.get("metal", 0.0)
    return mat


mats = {k: material(k, m) for k, m in spec["materials"].items()}
names = list(mats)
bm = bmesh.new()
uv = bm.loops.layers.uv.new("UVMap")
assign = []
for part in spec["parts"]:
    fs = BUILD[part["kind"]](bm, part)
    place(fs, part)
    assign += [(f, part["mat"]) for f in fs]
bm.normal_update()


def tile_uv(f, m):
    t = m.get("tile_m", 3.0)
    n = f.normal
    ax = max(range(3), key=lambda i: abs(n[i]))
    for loop in f.loops:
        co = loop.vert.co
        u, v = {0: (co.y, co.z), 1: (co.x, co.z), 2: (co.x, co.y)}[ax]
        loop[uv].uv = (u / t, v / t)


for f, mname in assign:
    m = spec["materials"][mname]
    if m.get("uv") == "plain":
        f.material_index = names.index(mname)
        continue
    if m["uv"] == "facade" and abs(f.normal.y) > 0.9:
        x0, x1, z0, z1 = m["rect"]
        for loop in f.loops:
            co = loop.vert.co
            u = (co.x - x0) / (x1 - x0)
            loop[uv].uv = (u if f.normal.y < 0 else 1 - u, (co.z - z0) / (z1 - z0))
        f.material_index = names.index(mname)
    else:
        fb = m.get("fallback", mname) if m["uv"] == "facade" else mname
        tile_uv(f, spec["materials"][fb])
        f.material_index = names.index(fb)

bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
me = bpy.data.meshes.new(spec.get("name", "piece"))
bm.to_mesh(me)
for m in mats.values():
    me.materials.append(m)
for p in me.polygons:          # smooth round pieces, keep every edge sharper than 35 deg
    p.use_smooth = True
me.set_sharp_from_angle(angle=math.radians(35))
obj = bpy.data.objects.new(spec.get("name", "piece"), me)
bpy.context.scene.collection.objects.link(obj)

n = spec.get("repeat", 1)
if n > 1:
    xs = [v.co.x for v in me.vertices]
    w = spec.get("width") or (max(xs) - min(xs))
    for i in range(1, n):
        o = obj.copy()
        o.location.x = i * w
        bpy.context.scene.collection.objects.link(o)

bpy.ops.export_scene.gltf(filepath=spec["out"], export_format="GLB", export_apply=True)
print(json.dumps({"out": spec["out"], "vertices": len(me.vertices), "faces": len(me.polygons),
                  "bbox": [round(max(v.co[i] for v in me.vertices) - min(v.co[i] for v in me.vertices), 3)
                           for i in range(3)]}))
