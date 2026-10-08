"""World mode greybox in headless Blender (asset_pipeline/stages/sw_site.py runs it).

    blender -b --factory-startup --python scripts/blender_greybox.py -- --build SPEC.json --out GREYBOX.blend
    blender -b GREYBOX.blend --factory-startup --python scripts/blender_greybox.py -- --extract OUT.json
    blender -b GREYBOX.blend --factory-startup --python scripts/blender_greybox.py -- --add-shot SHOT.json

--build: SPEC (asset_pipeline/greybox.py build_spec + "terrain.npy") -> a new .blend:
  - one Empty per slot (building, plaza, ring...), named and tagged by its id;
  - its pieces as child meshes; pieces with the same primitive share one mesh datablock;
  - terrain grid and sea plane;
  - one camera per shot.
  Tags (custom properties) are the contract with extract and the hand edits:
    slot:   ap_id, ap_type, ap_category, ap_kit, ap_district, ap_material, ap_typology
    piece:  ap_piece
    camera: ap_shot, ap_tier, ap_district, ap_notes, ap_res_x, ap_res_y
--extract: tagged objects -> JSON (slots with world matrix, bbox, pieces; shots).
  - Untagged meshes are listed.
  - Untagged cameras become shots named after the object.
  - Duplicate ids (a duplicated building) get a suffix.
  - The .blend is saved only when ids or camera tags had to be fixed.
--add-shot: create or replace the camera for one shot (ShotSpec JSON), save.
"""
import hashlib
import json
import math
import re
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


# Preview colours (linear RGB) per slot type / piece role.
COLORS = {"terrain": (0.32, 0.30, 0.24), "sea": (0.06, 0.16, 0.24), "avenue": (0.55, 0.53, 0.50),
          "radial": (0.58, 0.56, 0.52), "plaza": (0.62, 0.60, 0.56), "garden": (0.16, 0.30, 0.10),
          "canal": (0.08, 0.22, 0.32), "pool": (0.10, 0.30, 0.40), "terrace": (0.50, 0.48, 0.42),
          "temple": (0.80, 0.78, 0.72), "rotunda": (0.85, 0.84, 0.80), "stoa": (0.76, 0.74, 0.68),
          "block": (0.62, 0.60, 0.56), "villa": (0.82, 0.80, 0.74),
          "housing": (0.66, 0.58, 0.50), "houses": (0.74, 0.66, 0.56), "street": (0.50, 0.49, 0.46),
          "park": (0.20, 0.34, 0.12), "market": (0.70, 0.55, 0.30), "quay": (0.60, 0.58, 0.54),
          "tree": (0.10, 0.24, 0.08)}
WATER = {"pool-water", "court-pool", "canal"}
MERGED = {"ribbon", "poly"}       # unique per piece: merged into one mesh per slot


def log(msg):
    print("GREYBOX", msg, flush=True)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "shot"


# --- primitives --------------------------------------------------------------------------

def _arc(bm, p):
    r_in, r_out, a0, a1, h, seg = p["r_in"], p["r_out"], p["a0"], p["a1"], p["h"], p["seg"]
    span = (a1 - a0) % 360 or 360
    closed = span == 360
    n = seg if closed else seg + 1
    rings = []
    for i in range(n):
        a = math.radians(a0 + span * i / seg)
        c, s = math.cos(a), math.sin(a)
        rings.append([bm.verts.new((r * c, r * s, z)) for r, z in ((r_in, 0), (r_out, 0), (r_out, h), (r_in, h))])
    for i in range(seg):
        j = (i + 1) % n if closed else i + 1
        for k in range(4):
            kk = (k + 1) % 4
            bm.faces.new((rings[i][k], rings[j][k], rings[j][kk], rings[i][kk]))
    if not closed:
        bm.faces.new(rings[0])
        bm.faces.new(rings[-1])


def _gable(bm, p):
    w, d, h = p["w"], p["d"], p["h"]
    v = [bm.verts.new(c) for c in ((-w / 2, -d / 2, 0), (w / 2, -d / 2, 0), (0, -d / 2, h),
                                   (-w / 2, d / 2, 0), (w / 2, d / 2, 0), (0, d / 2, h))]
    for f in ((0, 1, 2), (5, 4, 3), (0, 3, 4, 1), (0, 2, 5, 3), (1, 4, 5, 2)):
        bm.faces.new([v[i] for i in f])


def _extrude_xz(bm, outline, d):
    """A prism from an outline in the XZ plane (counter-clockwise seen from -Y), d thick
    along Y, centred on y = 0. Concave outlines are fine (the caps are triangulated)."""
    front = [bm.verts.new((x, -d / 2, z)) for x, z in outline]
    back = [bm.verts.new((x, d / 2, z)) for x, z in outline]
    caps = [bm.faces.new(front), bm.faces.new(back[::-1])]
    n = len(outline)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((front[j], front[i], back[i], back[j]))
    bmesh.ops.triangulate(bm, faces=caps, quad_method="BEAUTY", ngon_method="BEAUTY")


def _vault(bm, p):
    w, d, h, seg = p["w"], p["d"], p["h"], p.get("seg", 12)
    pts = [(w / 2 * math.cos(math.pi * i / seg), h * math.sin(math.pi * i / seg)) for i in range(seg + 1)]
    _extrude_xz(bm, pts, d)


def _archwall(bm, p):
    """A wall w wide, h tall, d thick with an arched opening (span wide, springing at
    `spring`) in its middle: the outline runs up the opening and over the arch."""
    w, d, h, r, spring = p["w"], p["d"], p["h"], p["span"] / 2, p["spring"]
    seg = 16
    arch = [(r * math.cos(math.pi * i / seg), spring + r * math.sin(math.pi * i / seg)) for i in range(seg + 1)]
    # counter-clockwise: along the bottom, up the left jamb, over the arch, down the right
    # jamb, on to the right edge, back along the top
    outline = [(-w / 2, 0), (-r, 0)] + arch[::-1] + [(r, 0), (w / 2, 0), (w / 2, h), (-w / 2, h)]
    _extrude_xz(bm, outline, d)


def _ribbon(bm, p):
    """A strip of width w along a draped polyline, h thick (top faces up)."""
    pts = [Vector(q) for q in p["pts"]]
    w, h = p["w"] / 2, p["h"]
    rows = []
    for i, q in enumerate(pts):
        a = pts[max(i - 1, 0)]
        b = pts[min(i + 1, len(pts) - 1)]
        t = Vector((b.x - a.x, b.y - a.y, 0))
        if t.length < 1e-6:
            t = Vector((1, 0, 0))
        t.normalize()
        n = Vector((-t.y, t.x, 0))
        rows.append([bm.verts.new(q + n * s + Vector((0, 0, z))) for s, z in ((-w, 0), (w, 0), (w, h), (-w, h))])
    for i in range(len(rows) - 1):
        for k in range(4):
            kk = (k + 1) % 4
            bm.faces.new((rows[i][k], rows[i + 1][k], rows[i + 1][kk], rows[i][kk]))
    bm.faces.new(rows[0][::-1])
    bm.faces.new(rows[-1])


def _poly(bm, p):
    """A slab over a polygon outline: top at z + h per vertex, bottom at z, triangulated."""
    pts = [Vector(q) for q in p["pts"]]
    if len(pts) > 1 and (pts[0] - pts[-1]).length < 1e-6:
        pts = pts[:-1]
    top = [bm.verts.new(q + Vector((0, 0, p["h"]))) for q in pts]
    bot = [bm.verts.new(q) for q in pts]
    caps = [bm.faces.new(top), bm.faces.new(bot[::-1])]
    for i in range(len(pts)):
        j = (i + 1) % len(pts)
        bm.faces.new((bot[i], bot[j], top[j], top[i]))
    bmesh.ops.triangulate(bm, faces=caps, quad_method="BEAUTY", ngon_method="BEAUTY")


def make_mesh(prim, name):
    bm = bmesh.new()
    k = prim["kind"]
    if k == "box":
        bmesh.ops.create_cube(bm, size=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * prim["w"], v.co.y * prim["d"], (v.co.z + 0.5) * prim["h"]))
    elif k == "cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=prim["seg"], radius1=prim["r"],
                              radius2=prim["r"], depth=prim["h"])
        for v in bm.verts:
            v.co.z += prim["h"] / 2
    elif k == "dome":
        seg = prim["seg"]
        bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=seg // 2, radius=prim["r"])
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z < -1e-4], context="VERTS")
    elif k == "arc":
        _arc(bm, prim)
    elif k == "gable":
        _gable(bm, prim)
    elif k == "vault":
        _vault(bm, prim)
    elif k == "archwall":
        _archwall(bm, prim)
    elif k == "ribbon":
        _ribbon(bm, prim)
    elif k == "poly":
        _poly(bm, prim)
    else:
        raise ValueError(f"unknown primitive {k}")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def preview_material():
    mat = bpy.data.materials.get("AP_preview")
    if mat:
        return mat
    mat = bpy.data.materials.new("AP_preview")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    info = nt.nodes.new("ShaderNodeObjectInfo")
    nt.links.new(info.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.8
    return mat


def collection(name):
    c = bpy.data.collections.get(name)
    if not c:
        c = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(c)
    return c


def set_props(o, **props):
    for k, v in props.items():
        o[k] = "" if v is None else v


# --- build --------------------------------------------------------------------------------

def terrain_mesh(heights, extent):
    n = heights.shape[0]
    s = np.linspace(-extent / 2, extent / 2, n)
    xs, ys = np.meshgrid(s, s[::-1])
    verts = np.stack([xs.ravel(), ys.ravel(), heights.ravel()], axis=1)
    idx = np.arange(n * n).reshape(n, n)
    # rows run north -> south: wind faces so normals point up
    quads = np.stack([idx[1:, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel(), idx[:-1, :-1].ravel()], axis=1)
    me = bpy.data.meshes.new("terrain")
    me.vertices.add(len(verts))
    me.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    me.loops.add(quads.size)
    me.loops.foreach_set("vertex_index", quads.astype(np.int32).ravel())
    me.polygons.add(len(quads))
    me.polygons.foreach_set("loop_start", (np.arange(len(quads)) * 4).astype(np.int32))
    me.update(calc_edges=True)
    me.validate()
    return me


def shot_camera(s, cam_obj=None):
    pos, look = Vector(s["pos"]), Vector(s["look_at"])
    if cam_obj is None:
        cam = bpy.data.cameras.new(s["id"])
        cam_obj = bpy.data.objects.new(s["id"], cam)
        collection("AP_shots").objects.link(cam_obj)
    cam = cam_obj.data
    cam.lens = s.get("lens_mm", 35.0)
    cam.sensor_width = s.get("sensor_mm", 36.0)
    cam.sensor_fit = "HORIZONTAL"
    cam.clip_start, cam.clip_end = 0.1, 100000.0
    cam_obj.location = pos
    cam_obj.rotation_euler = (look - pos).to_track_quat("-Z", "Y").to_euler()
    res = s.get("resolution", [1344, 768])
    set_props(cam_obj, ap_shot=s["id"], ap_tier=s.get("tier", "medium"), ap_district=s.get("district"),
              ap_notes=s.get("notes", ""), ap_res_x=int(res[0]), ap_res_y=int(res[1]))
    return cam_obj


def build(spec_path, out):
    spec = json.load(open(spec_path))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"
    mat = preview_material()
    site = collection("AP_site")

    heights = np.load(spec["terrain"]["npy"])
    tm = terrain_mesh(heights, spec["terrain"]["extent_m"])
    tm.materials.append(mat)
    terrain = bpy.data.objects.new("terrain", tm)
    site.objects.link(terrain)
    set_props(terrain, ap_id="terrain", ap_type="terrain", ap_category="terrain", ap_kit=None,
              ap_district=None, ap_piece="terrain")
    terrain.color = (*COLORS["terrain"], 1)
    sea_size = max(spec["terrain"]["extent_m"] * 4, 100000.0)   # to the horizon of a high wide
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=sea_size / 2)
    sm = bpy.data.meshes.new("sea")
    bm.to_mesh(sm)
    bm.free()
    sm.materials.append(mat)
    sea = bpy.data.objects.new("sea", sm)
    sea.location.z = spec.get("sea_level", 0.0)
    site.objects.link(sea)
    set_props(sea, ap_id="sea", ap_type="sea", ap_category="terrain", ap_kit=None, ap_district=None, ap_piece="sea")
    sea.color = (*COLORS["sea"], 1)

    meshes = {}
    n_pieces = 0
    for s in spec["slots"]:
        coll = collection(f"AP_{s['district']}" if s.get("district") else "AP_site")
        e = bpy.data.objects.new(s["id"], None)
        e.empty_display_type = "PLAIN_AXES"
        e.empty_display_size = 2.0
        e.location = s["loc"]
        e.rotation_euler = (0, 0, math.radians(s["rot_z"]))
        coll.objects.link(e)
        set_props(e, ap_id=s["id"], ap_type=s["type"], ap_category=s["category"], ap_kit=s.get("kit"),
                  ap_district=s.get("district"), ap_material=s.get("material"), ap_typology=s.get("typology"))
        col = COLORS.get(s["type"], (0.7, 0.7, 0.7))
        # draped pieces (streets, lawns) are unique anyway: one merged mesh per slot and piece
        merged = {}
        for p in s["pieces"]:
            if p["prim"]["kind"] in MERGED:
                merged.setdefault(p["piece"], []).append(p)
        for piece, ps in merged.items():
            bm = bmesh.new()
            for p in ps:
                n0 = len(bm.verts)
                {"ribbon": _ribbon, "poly": _poly}[p["prim"]["kind"]](bm, p["prim"])
                bm.verts.ensure_lookup_table()
                off = Vector(p["loc"])
                for v in bm.verts[n0:]:
                    v.co += off
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            me = bpy.data.meshes.new(f"{s['id']}.{piece}")
            bm.to_mesh(me)
            bm.free()
            me.materials.append(mat)
            o = bpy.data.objects.new(f"{s['id']}.{piece}", me)
            o.parent = e
            o["ap_piece"] = piece
            o.color = (*COLORS.get(piece, col), 1)
            coll.objects.link(o)
            n_pieces += 1
        for p in s["pieces"]:
            if p["prim"]["kind"] in MERGED:
                continue
            key = json.dumps(p["prim"], sort_keys=True)
            me = meshes.get(key)
            if me is None:
                name = f"{p['piece']}#{hashlib.sha1(key.encode()).hexdigest()[:6]}"
                me = meshes[key] = make_mesh(p["prim"], name)
                me.materials.append(mat)
            o = bpy.data.objects.new(p["name"], me)
            o.parent = e
            o.location = p["loc"]
            o.rotation_euler = (0, 0, math.radians(p["rot_z"]))
            if "scale" in p:
                o.scale = p["scale"]
            o["ap_piece"] = p["piece"]
            o.color = (*(COLORS["pool"] if p["piece"] in WATER else COLORS.get(p["piece"], COLORS.get(p["piece"].split(":")[0], col))), 1)
            coll.objects.link(o)
            n_pieces += 1
    for s in spec.get("shots", []):
        shot_camera(s)
    cams = [o for o in sc.objects if o.type == "CAMERA"]
    if cams:
        sc.camera = cams[0]
    bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
    log(f"built {out}: {len(spec['slots']) + 2} slots, {n_pieces} pieces, {len(meshes)} meshes, {len(cams)} cameras")


# --- extract ------------------------------------------------------------------------------

def rows(m):
    return [[round(v, 6) for v in r] for r in m]


def extract(out):
    sc = bpy.context.scene
    seen, slots, fixed = set(), [], []
    tagged = [o for o in sc.objects if "ap_id" in o]
    tagged.sort(key=lambda o: o.name)
    for o in tagged:
        sid = str(o["ap_id"]) or slug(o.name)
        if sid in seen:  # a duplicated building keeps its tags: give the copy a new id
            base, i = sid, 2
            while f"{base}-{i}" in seen:
                i += 1
            sid = f"{base}-{i}"
            fixed.append(f"{o.name}: duplicate id {o['ap_id']} -> {sid}")
            o["ap_id"] = sid
        seen.add(sid)
        meshes = ([o] if o.type == "MESH" else []) + \
                 [c for c in o.children_recursive if c.type == "MESH" and "ap_id" not in c]
        inv = o.matrix_world.inverted()
        pieces, lo, hi = [], [math.inf] * 3, [-math.inf] * 3
        for m in meshes:
            pieces.append({"name": m.name, "piece": str(m.get("ap_piece", "mass")), "mesh": m.data.name,
                           "matrix_local": rows(inv @ m.matrix_world)})
            for c in m.bound_box:
                w = m.matrix_world @ Vector(c)
                lo = [min(a, b) for a, b in zip(lo, w)]
                hi = [max(a, b) for a, b in zip(hi, w)]
        if not meshes:
            lo = hi = list(o.matrix_world.translation)
        slots.append({"id": sid, "type": str(o.get("ap_type", "mass")) or "mass",
                      "category": str(o.get("ap_category", "other")) or "other",
                      "kit": str(o.get("ap_kit", "")) or None, "district": str(o.get("ap_district", "")) or None,
                      "material": str(o.get("ap_material", "")) or None,
                      "typology": str(o.get("ap_typology", "")) or None,
                      "matrix": rows(o.matrix_world), "bbox": [[round(v, 4) for v in lo], [round(v, 4) for v in hi]],
                      "pieces": pieces})
    def has_slot(o):
        while o:
            if "ap_id" in o:
                return True
            o = o.parent
        return False
    untagged = sorted(o.name for o in sc.objects if o.type == "MESH" and not has_slot(o))
    shots, shot_ids = [], set()
    for o in sorted((o for o in sc.objects if o.type == "CAMERA"), key=lambda o: o.name):
        sid = str(o.get("ap_shot", "")) or slug(o.name)
        if sid in shot_ids:
            base, i = sid, 2
            while f"{base}-{i}" in shot_ids:
                i += 1
            sid = f"{base}-{i}"
        if o.get("ap_shot") != sid:
            fixed.append(f"camera {o.name}: shot id {sid}")
            o["ap_shot"] = sid
        shot_ids.add(sid)
        cam = o.data
        shots.append({"id": sid, "tier": str(o.get("ap_tier", "medium")) or "medium",
                      "matrix": rows(o.matrix_world), "lens_mm": round(cam.lens, 4),
                      "sensor_mm": round(cam.sensor_width, 4),
                      "resolution": [int(o.get("ap_res_x", sc.render.resolution_x)),
                                     int(o.get("ap_res_y", sc.render.resolution_y))],
                      "district": str(o.get("ap_district", "")) or None, "notes": str(o.get("ap_notes", ""))})
    if fixed:
        bpy.ops.wm.save_mainfile()
    json.dump({"slots": slots, "shots": shots, "untagged": untagged, "fixed": fixed}, open(out, "w"), indent=1)
    log(f"extracted {len(slots)} slots, {len(shots)} shots, {len(untagged)} untagged meshes, {len(fixed)} fixes")


def add_shot(path):
    s = json.load(open(path))
    cam = next((o for o in bpy.context.scene.objects if o.type == "CAMERA" and o.get("ap_shot") == s["id"]), None)
    shot_camera(s, cam)
    bpy.ops.wm.save_mainfile()
    log(f"{'replaced' if cam else 'added'} shot {s['id']}")


if "--build" in args:
    build(opt("--build"), opt("--out"))
elif "--extract" in args:
    extract(opt("--extract"))
elif "--add-shot" in args:
    add_shot(opt("--add-shot"))
else:
    raise SystemExit("blender_greybox.py: pass --build, --extract or --add-shot")
