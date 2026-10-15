"""Shot previews of the greybox with proposed far terrain added (terrain_plan.preview): a heightfield
(.npz: x0, y0, cell, h with NaN where it adds nothing) becomes one mesh, the cameras see 40 km,
then scripts/blender_shots.py renders the preview pass as usual (to a scratch --out-dir: the shots'
own passes are not touched).

    blender -b greybox.blend --factory-startup --python scripts/blender_terrain_preview.py -- \
        --terrain PROPOSAL.npz --out-dir DIR --colors COLORS.json --shots a,b --passes preview
"""
import runpy
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np

args = sys.argv[sys.argv.index("--") + 1:]
z = np.load(args[args.index("--terrain") + 1])
x0, y0, cell, h = float(z["x0"]), float(z["y0"]), float(z["cell"]), z["h"]
bm = bmesh.new()
H, W = h.shape
vs = {}
for i in range(H):
    for j in range(W):
        if not np.isnan(h[i, j]):
            vs[i, j] = bm.verts.new((x0 + j * cell, y0 + i * cell, float(h[i, j])))
for i in range(H - 1):
    for j in range(W - 1):
        q = [vs.get(k) for k in ((i, j), (i, j + 1), (i + 1, j + 1), (i + 1, j))]
        if all(q):
            bm.faces.new(q)
me = bpy.data.meshes.new("proposed_terrain")
bm.to_mesh(me)
ob = bpy.data.objects.new("proposed_terrain", me)
ob.color = (0.36, 0.43, 0.32, 1.0)          # a muted green-grey: proposed ranges read apart from the tile
t = bpy.data.objects.get("terrain")
if t and t.active_material:               # the greybox's object-colour material
    me.materials.append(t.active_material)
bpy.context.scene.collection.objects.link(ob)
for o in bpy.data.objects:
    if o.type == "CAMERA":
        o.data.clip_end = 40000.0
runpy.run_path(str(Path(__file__).with_name("blender_shots.py")), run_name="__main__")
