"""A heightfield (.npz: x0, y0, cell, h rows south -> north, NaN = none) as a GLB mesh in world
metres (terrain_build.outer_glb: the outer ring for Unreal, a Nanite mesh there).

    blender -b --factory-startup --python scripts/blender_npz_glb.py -- IN.npz OUT.glb
"""
import sys

import bpy
import numpy as np

src, dst = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
z = np.load(src)
x0, y0, cell, h = float(z["x0"]), float(z["y0"]), float(z["cell"]), z["h"].astype(np.float64)
n, m = h.shape
X, Y = np.meshgrid(x0 + np.arange(m) * cell, y0 + np.arange(n) * cell)
ok = np.isfinite(h)
vid = np.full(h.shape, -1, np.int64)
vid[ok] = np.arange(int(ok.sum()))
verts = np.stack([X[ok], Y[ok], h[ok]], 1)
a, b, c, d = vid[:-1, :-1], vid[:-1, 1:], vid[1:, 1:], vid[1:, :-1]
good = (a >= 0) & (b >= 0) & (c >= 0) & (d >= 0)
quads = np.stack([a[good], b[good], c[good], d[good]], 1)
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
