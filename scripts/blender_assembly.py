"""A library entry with its attached pieces as one GLB (asset_pipeline/assembly.py).

    blender -b --factory-startup --python scripts/blender_assembly.py -- JOB.json

JOB: {"out": ".../assembly.glb",
      "pieces": [{"name": "parent-id", "glb": "...", "matrix": 4x4},
                 {"name": "child-id", "glb": "..." | null, "matrix": 4x4,
                  "box": [w, d, h] | null, "at": [x, y, z], "yaw": deg}]}
matrix: the piece's model -> the parent's frame (Blender: metres, Z up, front -Y). A piece with no
model is a grey placeholder box of size `box`, standing at `at`, turned by `yaw`. Each piece goes
under an empty named after it. Prints a REPORT json line.
"""
import json
import math
import sys

import bpy
from mathutils import Matrix

job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
report = {"pieces": []}

grey = bpy.data.materials.new("placeholder")
grey.use_nodes = True
grey.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.55, 0.55, 0.55, 1)

for i, p in enumerate(job["pieces"]):
    root = bpy.data.objects.new(p["name"] if i == 0 else f"{p['name']}.{i}", None)
    sc.collection.objects.link(root)
    if p.get("glb"):
        before = set(sc.objects)
        bpy.ops.import_scene.gltf(filepath=p["glb"])
        new = [o for o in sc.objects if o not in before]
        top = [o for o in new if o.parent is None]
        M = Matrix(p["matrix"])
        for o in top:
            o.matrix_world = M @ o.matrix_world
            o.parent = root
            o.matrix_parent_inverse = root.matrix_world.inverted()
        report["pieces"].append({"name": p["name"], "objects": len(new)})
    else:
        w, d, h = p["box"]
        x, y, z = p["at"]
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, z + h / 2))
        o = bpy.context.object
        o.name = f"placeholder_{p['name']}"
        o.scale = (w, d, h)
        o.rotation_euler = (0, 0, math.radians(p.get("yaw") or 0))
        o.data.materials.append(grey)
        o.parent = root
        report["pieces"].append({"name": p["name"], "placeholder": [w, d, h]})

bpy.ops.export_scene.gltf(filepath=job["out"], export_format="GLB", export_apply=True)
print("REPORT " + json.dumps(report))
