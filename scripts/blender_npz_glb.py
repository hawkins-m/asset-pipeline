"""A heightfield (.npz: x0, y0, cell, h rows south -> north, NaN = none; optional skirt_m) as a GLB mesh in world
metres (terrain_build.outer_glb: the outer ring for Unreal, a Nanite mesh there).

    blender -b --factory-startup --python scripts/blender_npz_glb.py -- IN.npz OUT.glb
"""
import os
import sys

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heightgrid  # noqa: E402

src, dst = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
z = np.load(src)
skirt = float(z["skirt_m"]) if "skirt_m" in z.files else 0.0
verts, quads = heightgrid.grid_mesh(z["h"], float(z["x0"]), float(z["y0"]), float(z["cell"]), skirt=skirt)
me = bpy.data.meshes.new("outer_ring")
me.vertices.add(len(verts))
me.vertices.foreach_set("co", verts.astype(np.float32).ravel())
me.loops.add(quads.size)
me.loops.foreach_set("vertex_index", quads.astype(np.int32).ravel())
me.polygons.add(len(quads))
me.polygons.foreach_set("loop_start", (np.arange(len(quads)) * 4).astype(np.int32))
me.update(calc_edges=True)
me.validate()
mat = bpy.data.materials.new("terrain")
me.materials.append(mat)
ob = bpy.data.objects.new("outer_ring", me)
bpy.context.scene.collection.objects.link(ob)
ob.select_set(True)
bpy.context.view_layer.objects.active = ob
bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", use_selection=True)
print("REPORT", len(verts), len(quads))
