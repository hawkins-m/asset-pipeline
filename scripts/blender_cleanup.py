"""Stage 6: clean up a generated GLB in headless Blender.

    blender -b --factory-startup --python scripts/blender_cleanup.py -- \
        --in IN.glb --out OUT.glb --report OUT.json --dims W,D,H \
        [--fit height|geomean] [--retopo FACES] [--texture-size 2048] [--threads N]

1. Scale uniformly to the plan's real-world size in metres (W = left-right, D = front-back,
   H = height; the asset faces -Y, which is glTF +Z, TRELLIS's front). "height" fits H
   exactly (default: the LLM anchors heights on doors and people; depth from one image is
   the weakest guess); "geomean" spreads the error over all three. The other axes'
   mismatch is reported, never forced (non-uniform scale would distort the asset).
2. Pivot at the base: origin at the bottom centre of the bounding box, at world origin,
   transforms applied.
3. --retopo TRIS (game assets): decimate the ~1M-face mesh to a triangle budget, new UVs,
   then bake base colour, roughness and a tangent-space normal map from the original onto
   it (Cycles on the CPU, so it never competes with GPU jobs). Metallic becomes the
   original's average. --quad tries quad retopology first (voxel remesh + QuadriFlow);
   experimental: QuadriFlow rejected TRELLIS meshes as non-manifold even after a remesh.
4. Warnings in the report: a flat result (a dimension < 5% of the plan's) and
   proportions far from the plan (an axis < 0.67x or > 1.5x after the fit).
"""
import json
import math
import sys
import time

import bpy
from mathutils import Matrix, Vector

args = sys.argv[sys.argv.index("--") + 1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


src, dst, report_path = opt("--in"), opt("--out"), opt("--report")
plan_w, plan_d, plan_h = (float(v) for v in opt("--dims").split(","))
fit = opt("--fit", "height")
retopo = int(opt("--retopo", "0"))
tex_size = int(opt("--texture-size", "2048"))
threads = int(opt("--threads", "0"))
t0 = time.time()
report = {"input": src, "plan_dims_m": [plan_w, plan_d, plan_h], "fit": fit, "steps": []}


def log(msg):
    report["steps"].append(f"{time.time() - t0:6.1f}s {msg}")
    print("CLEANUP", msg, flush=True)


def bbox(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector(min(p[i] for p in pts) for i in range(3))
    hi = Vector(max(p[i] for p in pts) for i in range(3))
    return lo, hi


def select_only(*objs, active=None):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active or objs[-1]


# --- import ------------------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
for o in bpy.context.scene.objects:
    if o.type != "MESH":
        bpy.data.objects.remove(o, do_unlink=True)
select_only(*meshes)
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
obj.name = "asset"
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
report["input_faces"] = len(obj.data.polygons)

# --- scale to plan dimensions ------------------------------------------------------------
lo, hi = bbox(obj)
size = hi - lo
report["input_dims"] = [round(v, 4) for v in size]
if fit == "geomean":
    s = math.exp(sum(math.log(p / max(m, 1e-9)) for p, m in zip((plan_w, plan_d, plan_h), size)) / 3)
else:
    s = plan_h / max(size.z, 1e-9)
obj.scale = (s, s, s)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

# --- pivot at the base -------------------------------------------------------------------
lo, hi = bbox(obj)
base = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
obj.data.transform(Matrix.Translation(-base))
obj.location = (0, 0, 0)
obj.data.update()
bpy.context.view_layer.update()  # bound_box is cached until the depsgraph updates
lo, hi = bbox(obj)
dims = hi - lo
report["scale"] = round(s, 6)
report["output_dims_m"] = [round(v, 4) for v in dims]
report["dims_vs_plan"] = {k: round(v / p, 3) for k, v, p in zip("wdh", dims, (plan_w, plan_d, plan_h))}
report["base_z"] = round(lo.z, 6)
log(f"scaled x{s:.4f} ({fit}) to {tuple(round(v, 3) for v in dims)} m; pivot at base centre")

# --- retopology + bake (game) ------------------------------------------------------------
if retopo:
    high = obj
    high.name = "high"
    low = high.copy()
    low.data = high.data.copy()
    low.name = "asset"
    bpy.context.scene.collection.objects.link(low)
    select_only(low)
    # glTF stores a separate vertex per UV seam, so the imported mesh is many disconnected
    # islands; decimating those shrinks each island on its own (a test box lost 2/3 of
    # its height) and opens cracks. Weld them first; UVs are per-corner and survive.
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=max(dims) * 1e-5)
    bpy.ops.object.mode_set(mode="OBJECT")
    log(f"welded seams: {len(low.data.vertices)} vertices")
    if "--quad" in args:
        # Experimental quad retopology: decimate, voxel remesh (watertight, ~1/160 of
        # the asset's size so thin parts survive), repair, QuadriFlow. On TRELLIS meshes
        # QuadriFlow still reported "needs to be manifold" (2026-10-05), so it falls back.
        pre = max(retopo * 8, 60000)
        if len(low.data.polygons) > pre:
            mod = low.modifiers.new("dec", "DECIMATE")
            mod.ratio = pre / len(low.data.polygons)
            bpy.ops.object.modifier_apply(modifier=mod.name)
        low.data.remesh_voxel_size = max(dims) / 160
        bpy.ops.object.voxel_remesh()
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.delete_loose()
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode="OBJECT")
        before = len(low.data.polygons)
        r = bpy.ops.object.quadriflow_remesh(target_faces=retopo, use_mesh_symmetry=False,
                                             use_preserve_sharp=True, use_preserve_boundary=True,
                                             preserve_attributes=False, smooth_normals=False,
                                             mode="FACES", seed=0)
        method = "quadriflow" if "FINISHED" in r and len(low.data.polygons) != before else None
        log(f"quadriflow returned {r}: {before} -> {len(low.data.polygons)} faces")
    else:
        method = None
    if method is None:
        # Default: collapse-decimate straight to the triangle budget; the bake carries the
        # detail. Decimate counts triangles, so triangulate first for an exact budget.
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.quads_convert_to_tris()
        bpy.ops.object.mode_set(mode="OBJECT")
        mod = low.modifiers.new("dec", "DECIMATE")
        mod.ratio = min(1.0, retopo / max(len(low.data.polygons), 1))
        bpy.ops.object.modifier_apply(modifier=mod.name)
        method = "decimate"
    report["retopo_method"] = method
    log(f"{method}: {len(low.data.polygons)} faces")

    # UVs
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.003)
    bpy.ops.object.mode_set(mode="OBJECT")
    for p in low.data.polygons:
        p.use_smooth = True

    # Bake targets on a fresh material
    def image(name, colour):
        img = bpy.data.images.new(name, tex_size, tex_size, alpha=False)
        img.colorspace_settings.name = "sRGB" if colour else "Non-Color"
        return img
    imgs = {"base": image("base_color", True), "rough": image("roughness", False),
            "normal": image("normal", False)}
    mat = bpy.data.materials.new("asset")
    if hasattr(mat, "use_nodes") and not mat.use_nodes:  # always on in Blender 5+
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    tex = {}
    for i, (k, img) in enumerate(imgs.items()):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image, n.location = img, (-700, 300 - 300 * i)
        tex[k] = n
    nt.links.new(tex["base"].outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex["rough"].outputs["Color"], bsdf.inputs["Roughness"])
    nmap = nt.nodes.new("ShaderNodeNormalMap")
    nt.links.new(tex["normal"].outputs["Color"], nmap.inputs["Color"])
    nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    # Metallic: the original's average (TRELLIS stores it in the blue channel).
    metal = 0.0
    src_mat = high.material_slots[0].material if high.material_slots else None
    if src_mat:
        mr = [n for n in src_mat.node_tree.nodes if n.type == "TEX_IMAGE" and "METAL" in (n.label or "").upper()]
        if mr and mr[0].image and mr[0].image.size[0]:
            px = mr[0].image.pixels[:]
            blue = px[2::4]
            metal = sum(blue[::97]) / max(len(blue[::97]), 1)
    bsdf.inputs["Metallic"].default_value = metal
    report["metallic"] = round(metal, 3)
    low.data.materials.clear()
    low.data.materials.append(mat)

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    if threads:
        scene.render.threads_mode, scene.render.threads = "FIXED", threads
    bake = scene.render.bake
    bake.use_selected_to_active = True
    bake.cage_extrusion = max(dims) * 0.02
    bake.max_ray_distance = max(dims) * 0.05
    bake.margin = 8
    select_only(high, low, active=low)
    for key, kind, extra in (("base", "DIFFUSE", {"pass_filter": {"COLOR"}}),
                             ("rough", "ROUGHNESS", {}),
                             ("normal", "NORMAL", {"normal_space": "TANGENT"})):
        for n in nt.nodes:
            n.select = False
        tex[key].select = True
        nt.nodes.active = tex[key]
        bpy.ops.object.bake(type=kind, **extra)
        imgs[key].pack()
        log(f"baked {key}")
    bpy.data.objects.remove(high, do_unlink=True)
    obj = low

report["output_faces"] = len(obj.data.polygons)
# Re-apply the base pivot: decimation moves the surface slightly (the bake is done, so
# shifting the low-poly by a fraction of a millimetre changes nothing there).
obj.data.update()
bpy.context.view_layer.update()
lo2, hi2 = bbox(obj)
obj.data.transform(Matrix.Translation(-Vector(((lo2.x + hi2.x) / 2, (lo2.y + hi2.y) / 2, lo2.z))))
obj.data.update()
bpy.context.view_layer.update()
lo2, hi2 = bbox(obj)  # what is actually exported
dims2 = hi2 - lo2
report["output_dims_m"] = [round(v, 4) for v in dims2]
report["dims_vs_plan"] = {k: round(v / p, 3) for k, v, p in zip("wdh", dims2, (plan_w, plan_d, plan_h))}
report["base_z"] = round(lo2.z, 6)
warnings = []
shrink = min(b / max(a, 1e-9) for a, b in zip(dims, dims2))
if retopo and shrink < 0.97:
    warnings.append(f"retopo shrank the mesh to {shrink:.0%} of its size")
for k, ratio in report["dims_vs_plan"].items():
    if ratio < 0.05:
        warnings.append(f"flat: {'width depth height'.split()['wdh'.index(k)]} is {ratio:.0%} of the plan")
    elif not 0.67 <= ratio <= 1.5:
        warnings.append(f"{'width depth height'.split()['wdh'.index(k)]} is {ratio:.2f}x the plan")
report["warnings"] = warnings
select_only(obj)
bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", use_selection=True, export_apply=True)
log(f"exported {dst}")
report["seconds"] = round(time.time() - t0, 1)
with open(report_path, "w") as f:
    json.dump(report, f, indent=2)
print("CLEANUP_REPORT " + json.dumps({k: report[k] for k in ("output_faces", "output_dims_m", "dims_vs_plan", "warnings", "seconds")}))
