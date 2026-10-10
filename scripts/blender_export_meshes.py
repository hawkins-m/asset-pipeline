"""Export the meshes of a greybox as one GLB each (world mode, phase 4 stand-ins).

    blender -b GREYBOX.blend --factory-startup --python scripts/blender_export_meshes.py -- \
        --out-dir DIR --index OUT.json

Every mesh datablock used by a tagged slot (kit pieces share one) is written once, at the
origin with no transform, as DIR/<asset id>/SM_<asset id>.glb with no materials (the
importer assigns its own). The index maps mesh name -> asset id, file and bounds (m).
Asset ids are the mesh names made file-safe (classical:column-base#3fa2c1 ->
classical-column-base-3fa2c1).
"""
import json
import os
import re
import sys

import bpy
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]
out_dir = args[args.index("--out-dir") + 1]
index_path = args[args.index("--index") + 1]


def asset_id(mesh_name):
    return re.sub(r"[^a-z0-9]+", "-", mesh_name.lower()).strip("-")


def slot_of(o):
    while o:
        if "ap_id" in o:
            return o
        o = o.parent
    return None


meshes = sorted({o.data.name for o in bpy.context.scene.objects if o.type == "MESH" and slot_of(o)})
print("EXPORT_TOTAL", len(meshes), flush=True)   # the UI counts GLBs against it
# Each mesh goes out from an empty scene of its own: the exporter walks the whole scene even
# with use_selection, which took ~3.5 s a mesh on an 80k-object city greybox (0.09 s here;
# the GLBs are byte-identical apart from names).
scene = bpy.data.scenes.new("ap_export")
ids = set()
index = {}
for name in meshes:
    me = bpy.data.meshes[name]
    aid = asset_id(name)
    if aid in ids:
        raise SystemExit(f"two meshes map to asset id {aid}")
    ids.add(aid)
    obj = bpy.data.objects.new(f"SM_{aid}", me)    # a temporary object at the origin
    scene.collection.objects.link(obj)
    d = os.path.join(out_dir, aid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"SM_{aid}.glb")
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_active_scene=True,
                                  export_materials="NONE", export_apply=False)
    lo = [min(v.co[i] for v in me.vertices) for i in range(3)]
    hi = [max(v.co[i] for v in me.vertices) for i in range(3)]
    index[name] = {"id": aid, "glb": os.path.relpath(path, out_dir), "faces": len(me.polygons),
                   "bounds": [[round(v, 4) for v in lo], [round(v, 4) for v in hi]]}
    bpy.data.objects.remove(obj)
json.dump(index, open(index_path, "w"), indent=1)
print("EXPORT", len(index), "meshes", flush=True)
